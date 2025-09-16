# main.py — Web Service (Flask) + Bot automático com Playwright
# Observação: Virtuais são RNG; o bot apenas filtra "value". Use gestão e 1 gale.

import os, math, re, time, threading
from datetime import datetime
import schedule

from flask import Flask, jsonify
from playwright.sync_api import sync_playwright
from telegram import Bot
from telegram.constants import ParseMode

# ===== CONFIG =====
BOT_TOKEN   = os.getenv("BOT_TOKEN") or "7599991522:AAHSR8pkqQ_Btinnxi_YvgTfaifF1pvrxps"
CHANNEL_ID  = int(os.getenv("CHANNEL_ID") or "-1002814723832")
BANKROLL    = float(os.getenv("BANKROLL")    or "200")
STAKE_PCT   = float(os.getenv("STAKE_PCT")   or "0.03")   # 3%
MARGIN_VAL  = float(os.getenv("MARGIN_VALUE") or "0.03")  # 3% acima da justa
INTERVAL_MIN= int(os.getenv("INTERVAL_MIN")  or "3")      # a cada 3 min
BET365_URL  = os.getenv("BET365_URL") or "https://www.bet365.bet.br/#/AVR/B146/R^1/"
COOKIES_JSON= os.getenv("COOKIES_JSON") or ""

if not BOT_TOKEN:
    raise SystemExit("Defina BOT_TOKEN")

bot = Bot(BOT_TOKEN)
last_signal = {"text": None}
bankroll_state = {"bankroll": BANKROLL}

# ===== Flask (para rodar como Web Service Free) =====
app = Flask(__name__)

@app.get("/")
def root():
    return "OK - futebol-virtual-bot"

@app.get("/health")
def health():
    return jsonify(status="ok", time=datetime.utcnow().isoformat())

# ===== Modelo de gols =====
def p_over15(lmbd): return 1.0 - math.exp(-lmbd)*(1 + lmbd)
def p_over25(lmbd):
    p0 = math.exp(-lmbd); p1 = p0*lmbd; p2 = p1*lmbd/2.0
    return 1.0 - (p0 + p1 + p2)
def p_btts(lmbd):
    lam = lmbd/2.0
    return 1.0 - 2*math.exp(-lam) + math.exp(-lmbd)
def odd(p): return float('inf') if p <= 0 else 1.0/max(min(p, 0.999999), 1e-9)

LAMBDA_TABLE = [
    (1.70, 1.78, 3.0, 1.73),
    (1.79, 1.92, 2.8, 1.88),
    (1.93, 2.10, 2.6, 2.07),
    (2.11, 2.35, 2.4, 2.33),
    (2.36, 2.70, 2.2, 2.65),
]
def lambda_from_o25(o25):
    best = (None, 999)
    for _, _, lam, fair in LAMBDA_TABLE:
        d = abs(o25 - fair)
        if d < best[1]: best = ((lam, fair), d)
    return best[0][0]

def fmt_money(x): return f"R${x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

# ===== Extração de odds =====
def parse_float(txt):
    try: return float(txt.replace(",", "."))
    except: return None

def extract_odds(page):
    html = page.content()
    o15 = o25 = btts = None
    odd_pat = r"([1-9]\d*(?:[.,]\d{1,2})?)"
    m15 = re.search(r"[\s>](?:1[.,]5)[\s\S]{0,120}?"+odd_pat, html)
    if m15: o15 = parse_float(m15.group(1))
    m25 = re.search(r"[\s>](?:2[.,]5)[\s\S]{0,120}?"+odd_pat, html)
    if m25: o25 = parse_float(m25.group(1))
    mb  = re.search(r"Ambos\s+os\s+Times[\s\S]{0,200}?Sim[\s\S]{0,20}?"+odd_pat, html)
    if mb: btts = parse_float(mb.group(1))
    return {"O15": o15, "O25": o25, "BTTS": btts}

def with_browser(fn):
    def _wrap():
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = browser.new_context()
            if COOKIES_JSON:
                try:
                    import json; context.add_cookies(json.loads(COOKIES_JSON))
                except: pass
            page = context.new_page()
            page.goto(BET365_URL, timeout=60000)
            page.wait_for_timeout(5000)
            res = fn(page)
            context.close(); browser.close()
            return res
    return _wrap

# ===== Decisão =====
def decide_and_format(odds):
    o25 = odds.get("O25"); o15 = odds.get("O15"); b = odds.get("BTTS")
    if not o25: return None, "Sem O2.5 na tela — pulando."

    lam = lambda_from_o25(o25)
    p15 = p_over15(lam); fair15 = odd(p15)
    p25 = p_over25(lam); fair25 = odd(p25)
    pbt = p_btts(lam);   fairbt = odd(pbt)

    def diff(h,f): 
        if not f or f == float('inf'): return -1
        return (h/f) - 1.0

    cands = [("Over 2.5", o25, fair25, p25, diff(o25,fair25))]
    if b:   cands.append(("BTTS (Ambos Marcam)", b, fairbt, pbt, diff(b,fairbt)))
    if o15: cands.append(("Over 1.5", o15, fair15, p15, diff(o15,fair15)))

    prio = {"Over 2.5":0, "BTTS (Ambos Marcam)":1, "Over 1.5":2}
    cands.sort(key=lambda x: (prio[x[0]], -x[4]))
    market, house, fair, _, dv = cands[0]

    value = dv >= MARGIN_VAL
    stake = max(1.0, bankroll_state["bankroll"] * STAKE_PCT)
    gale  = min(bankroll_state["bankroll"] - stake, stake*2.0)

    header = f"⚽ <b>Futebol Virtual — Sinal</b>  ({datetime.now().strftime('%H:%M')})"
    body = [
        f"λ estimado: <b>{lam:.1f}</b>",
        f"Justas → O1.5 <b>{fair15:.2f}</b> | O2.5 <b>{fair25:.2f}</b> | BTTS <b>{fairbt:.2f}</b>",
        "Casa  → " + (f"O1.5 {o15:.2f} | " if o15 else "") + f"O2.5 <b>{o25:.2f}</b> | " + (f"BTTS {b:.2f}" if b else "BTTS —"),
    ]
    if value:
        body += [
            f"✅ <b>Entrada</b>: <u>{market}</u> @ <b>{house:.2f}</b>  (value ~ {dv*100:.1f}%)",
            f"💵 Stake: <b>{fmt_money(stake)}</b>",
            f"🪙 Gale 1x: <b>{fmt_money(gale)}</b> (apenas se Red)",
            "⛔ Sem 2º gale. Volte ao stake base depois."
        ]
    else:
        body += [f"⚠️ Sem value claro (diferença ~ {dv*100:.1f}%). Pular."]

    return (True, header + "\n" + "\n".join(body)), None

# ===== Loop automático (thread) =====
@with_browser
def scan_once(page):
    odds = extract_odds(page)
    signal, err = decide_and_format(odds)
    if err or not signal: return
    _, text = signal
    if text != last_signal["text"]:
        bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        last_signal["text"] = text

def scheduler_loop():
    schedule.every(INTERVAL_MIN).minutes.do(scan_once)
    scan_once()
    while True:
        schedule.run_pending()
        time.sleep(1)

# Inicia a thread ao subir o web service
t = threading.Thread(target=scheduler_loop, daemon=True)
t.start()

# app = Flask(...) já definido acima; o Render/Gunicorn usará `main:app`
