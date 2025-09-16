import os, math, re, time, threading
from datetime import datetime
import schedule

from playwright.sync_api import sync_playwright
from telegram import Bot
from telegram.constants import ParseMode

# ========= Config =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")  # ex: -1002814723832 ou seu chat
STAKE_PCT = float(os.getenv("STAKE_PCT", "0.03"))          # 3% da banca
BANKROLL = float(os.getenv("BANKROLL", "200"))             # R$200 default
MARGIN_VALUE = float(os.getenv("MARGIN_VALUE", "0.03"))    # 3% folga para "value"
INTERVAL_MIN = int(os.getenv("INTERVAL_MIN", "3"))         # varredura a cada 3 min
BET365_URL = os.getenv("BET365_URL", "https://www.bet365.com/#/AX/K_/_/")  # Virtuais Futebol (ajuste se necessário)
COOKIES_JSON = os.getenv("COOKIES_JSON", "")               # opcional: cookies exportados (JSON) da sua sessão

bot = Bot(BOT_TOKEN)
last_signal = {"text": None}  # para não repetir sinal idêntico
bankroll_state = {"bankroll": BANKROLL}

# ========= Modelo de gols =========
def p_over15(lmbd):
    return 1.0 - math.exp(-lmbd)*(1 + lmbd)

def p_over25(lmbd):
    p0 = math.exp(-lmbd)
    p1 = p0*lmbd
    p2 = p1*lmbd/2.0
    return 1.0 - (p0 + p1 + p2)

def p_btts(lmbd):
    lam = lmbd/2.0
    return 1.0 - 2*math.exp(-lam) + math.exp(-lmbd)

def odd(p): 
    return float('inf') if p <= 0 else 1.0/max(min(p, 0.999999), 1e-9)

LAMBDA_TABLE = [
    (1.70, 1.78, 3.0, 1.73),
    (1.79, 1.92, 2.8, 1.88),
    (1.93, 2.10, 2.6, 2.07),
    (2.11, 2.35, 2.4, 2.33),
    (2.36, 2.70, 2.2, 2.65),
]

def lambda_from_o25(o25):
    best = (None, 999)
    for lo, hi, lam, fair in LAMBDA_TABLE:
        d = abs(o25 - fair)
        if d < best[1]:
            best = ((lam, fair), d)
    return best[0][0]

def fmt_money(x):
    return f"R${x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def percent(x):
    return f"{x*100:.1f}%"

# ========= Leitura de odds (Playwright) =========
def parse_float(txt):
    try:
        return float(txt.replace(",", "."))
    except:
        return None

def extract_odds(page):
    """
    Lê odds relevantes na tela:
      - Over 1.5, Over 2.5 (tabela 'Gols Mais/Menos')
      - BTTS 'Ambos os Times – Sim'
    Faz matching por texto em português exibido pela Bet365.
    """
    html = page.content()
    # procura blocos por regex simples
    # O 1.5 / 2.5 pegando a coluna "Mais de"
    o15 = None
    o25 = None
    btts = None

    # Padrões tolerantes (captura número decimal típico de odds)
    odd_pat = r"([1-9]\d*(?:[.,]\d{1,2})?)"

    # Over 1.5: linha "1.5" -> pega a odd da coluna "Mais de", que costuma estar próxima
    m15 = re.search(r"[\s>](?:1[.,]5)[\s\S]{0,120}?"+odd_pat, html)
    if m15:
        o15 = parse_float(m15.group(1))

    # Over 2.5
    m25 = re.search(r"[\s>](?:2[.,]5)[\s\S]{0,120}?"+odd_pat, html)
    if m25:
        o25 = parse_float(m25.group(1))

    # BTTS (Ambos os Times) -> coluna "Sim"
    mb = re.search(r"Ambos\s+os\s+Times[\s\S]{0,200}?Sim[\s\S]{0,20}?"+odd_pat, html)
    if mb:
        btts = parse_float(mb.group(1))

    return {"O15": o15, "O25": o25, "BTTS": btts}

def with_browser(fn):
    def _wrap():
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            if COOKIES_JSON:
                context = browser.new_context()
                try:
                    import json
                    cookies = json.loads(COOKIES_JSON)
                    context.add_cookies(cookies)
                except Exception:
                    pass
            else:
                context = browser.new_context()

            page = context.new_page()
            page.goto(BET365_URL, timeout=60000)
            page.wait_for_timeout(5000)  # dar tempo do layout carregar
            res = fn(page)
            context.close()
            browser.close()
            return res
    return _wrap

# ========= Lógica de decisão =========
def decide_and_format(odds):
    o25 = odds.get("O25")
    o15 = odds.get("O15")
    btts = odds.get("BTTS")

    if not o25:
        return None, "Sem O2.5 na tela — pulando."

    lam = lambda_from_o25(o25)
    p15 = p_over15(lam); fair15 = odd(p15)
    p25 = p_over25(lam); fair25 = odd(p25)
    pbt = p_btts(lam);   fairbt = odd(pbt)

    # prioridade: O2.5 > BTTS > O1.5
    candidates = []
    def diff(h,f): return (h/f)-1.0 if f and f != float('inf') else -1

    candidates.append(("Over 2.5", o25, fair25, p25, diff(o25,fair25)))
    if btts: candidates.append(("BTTS (Ambos Marcam)", btts, fairbt, pbt, diff(btts,fairbt)))
    if o15:  candidates.append(("Over 1.5", o15, fair15, p15, diff(o15,fair15)))

    prio = {"Over 2.5":0, "BTTS (Ambos Marcam)":1, "Over 1.5":2}
    candidates.sort(key=lambda x: (prio[x[0]], -x[4]))
    top = candidates[0]
    market, house, fair, p, dv = top

    value = dv >= MARGIN_VALUE
    stake = max(1.0, bankroll_state["bankroll"] * STAKE_PCT)
    gale  = min(bankroll_state["bankroll"] - stake, stake*2.0)

    header = f"⚽ <b>Futebol Virtual — Sinal</b>  ({datetime.now().strftime('%H:%M')})"
    body = [
        f"λ estimado: <b>{lam:.1f}</b>",
        f"Justas → O1.5 <b>{fair15:.2f}</b> | O2.5 <b>{fair25:.2f}</b> | BTTS <b>{fairbt:.2f}</b>",
        "Casa  → " + (f"O1.5 {o15:.2f} | " if o15 else "") + f"O2.5 <b>{o25:.2f}</b> | " + (f"BTTS {btts:.2f}" if btts else "BTTS —"),
    ]
    if value:
        body += [
            f"✅ <b>Entrada</b>: <u>{market}</u> @ <b>{house:.2f}</b>  (value ~ {dv*100:.1f}%)",
            f"💵 Stake: <b>{fmt_money(stake)}</b>",
            f"🪙 Gale 1x: <b>{fmt_money(gale)}</b> (apenas se Red)",
            "⛔ Sem 2º gale. Volta ao stake base depois."
        ]
    else:
        body += [f"⚠️ Sem value claro no topo (diferença ~ {dv*100:.1f}%). Pular."]

    text = header + "\n" + "\n".join(body)
    return (value, text), None

# ========= Loop automático =========
@with_browser
def scan_once(page):
    odds = extract_odds(page)
    signal, err = decide_and_format(odds)
    if err:
        return

    is_value, text = signal
    # evitar spam: só manda se mudou
    if text != last_signal["text"]:
        bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        last_signal["text"] = text

def scheduler_loop():
    schedule.every(INTERVAL_MIN).minutes.do(scan_once)
    scan_once()  # primeira execução imediata
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    # loop em thread para facilitar start simples
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    # mantém processo vivo
    while True:
        time.sleep(3600)
