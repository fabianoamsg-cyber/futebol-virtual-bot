# Bot Bet365 – Futebol Virtual (Sniper Contínuo, 3 min)
# - Varredura automática a cada 3 minutos (leve para Render Free)
# - Mercados: Over 1.5, Over 2.5, BTTS, 1X2 (Vitória/Empate)
# - Gira ligas e clica nos horários para fugir de "Evento iniciado"
# - Filtros assertivos por faixas de odds (config abaixo)
# - Envio automático para Telegram, com anti-spam
# - Endpoints: / (status), /health, /scan (forçar uma varredura)

import os, re, json, time, threading
from datetime import datetime
from flask import Flask, jsonify
import requests
from playwright.sync_api import sync_playwright

# ================== CONFIG GERAL ==================
BOT_TOKEN    = os.getenv("BOT_TOKEN", "7599991522:AAHSR8pkqQ_Btinnxi_YvgTfaifF1pvrxps")
CHANNEL_ID   = int(os.getenv("CHANNEL_ID", "-1002814723832"))
BET365_URL   = os.getenv("BET365_URL", "https://www.bet365.bet.br/#/AVR/B146/R^1/")
COOKIES_JSON = os.getenv("COOKIES_JSON", "")   # cole o JSON do Cookie-Editor
INTERVAL_MIN = int(os.getenv("INTERVAL_MIN", "3"))  # 🔁 frequência (3 min)
USER_AGENT   = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# Gestão banca (opcional)
BANKROLL  = float(os.getenv("BANKROLL", "200"))
STAKE_PCT = float(os.getenv("STAKE_PCT", "0.03"))  # 3%

# Ligas para girar (adapte se desejar)
LEAGUES_PT = [
    "Express Cup", "Copa do Mundo", "Euro Cup",
    "Super Liga Sul-Americana", "Premier League"
]

# ================== FAIXAS ASSERTIVAS ==================
# Mantém qualidade, mas dá volume. Ajuste se quiser mais/menos sinais.
RANGE_O15   = (1.15, 1.70)
RANGE_O25   = (1.55, 2.35)
RANGE_BTTS  = (1.55, 2.35)
RANGE_HOME  = (1.40, 2.70)  # Vitória time da casa
RANGE_DRAW  = (2.80, 4.20)  # Empate (opcionalmente mais restrito)
RANGE_AWAY  = (1.60, 2.90)  # Vitória visitante

PRIORITY = [
    "Over 2.5", "BTTS (Ambos Marcam)", "Over 1.5",
    "Vitória Casa", "Empate", "Vitória Visitante"
]
# =======================================================

app = Flask(__name__)
_last_text = {"txt": None}

ODD_PAT = r"([1-9]\d*(?:[.,]\d{1,2})?)"

def _fnum(x):  # float brasil
    return float(str(x).replace(",", "."))

def _fmt_money(x: float) -> str:
    s = f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R${s}"

def _send_telegram(text: str):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHANNEL_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=20
        )
        return r.json()
    except Exception as e:
        return {"ok": False, "reason": str(e)}

def with_browser(fn):
    """Abre Chromium leve, executa fn(page) e fecha tudo corretamente."""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage",
                "--disable-gpu","--no-zygote","--single-process",
                "--disable-extensions","--disable-background-networking",
                "--mute-audio","--hide-scrollbars",
                "--disable-features=TranslateUI,BlinkGenPropertyTrees"
            ]
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 720},
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            extra_http_headers={"Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"}
        )
        # não bloquear 'font' — várias casas usam webfont pros dígitos
        if COOKIES_JSON.strip():
            try:
                context.add_cookies(json.loads(COOKIES_JSON))
            except Exception as e:
                print("cookies load error:", e)

        page = context.new_page()
        page.set_default_timeout(25000)

        try:
            page.goto(BET365_URL, timeout=60000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except:
                pass
            try:
                page.wait_for_selector("text=/Gols\\s+Mais\\/Menos|Total\\s+de\\s+Gols/i", timeout=8000)
            except:
                pass
            page.wait_for_timeout(3500)
            res = fn(page)
        finally:
            context.close()
            browser.close()
        return res

# ------------------ Navegação & Extração ------------------
def click_timeslot(page):
    """Clica em um horário (9:52 / 21:52) para sair de Evento Iniciado."""
    try:
        slots = page.locator("text=/\\b\\d{1,2}:\\d{2}\\b/")
        n = slots.count()
        for i in range(min(n, 6)):
            try:
                slots.nth(i).click(timeout=900)
                page.wait_for_timeout(900)
                return True
            except:
                continue
    except:
        pass
    return False

def goto_league(page, name):
    try:
        tab = page.locator(f"text={name}").first
        if tab:
            tab.click(timeout=2000)
            page.wait_for_timeout(1200)
            # tenta expandir bloco de gols
            try:
                page.locator("text=/Gols\\s+Mais\\/Menos|Total\\s+de\\s+Gols/i").first.click(timeout=500)
                page.wait_for_timeout(400)
            except:
                pass
            return True
    except:
        pass
    return False

def extract_markets(text: str):
    """
    Parse de mercados do texto renderizado (innerText).
    Retorna dict com O15, O25, BTTS, 1X2 (home/draw/away).
    """
    t = " ".join(text.split())
    markets = {"O15": None, "O25": None, "BTTS": None, "HOME": None, "DRAW": None, "AWAY": None}

    # Over 1.5 e 2.5
    m = re.search(r"Mais\s*de\s*1[.,]5\s+" + ODD_PAT, t, re.I)
    if m: markets["O15"] = _fnum(m.group(1))
    m = re.search(r"Mais\s*de\s*2[.,]5\s+" + ODD_PAT, t, re.I)
    if m: markets["O25"] = _fnum(m.group(1))

    # BTTS (Ambos Marcam)
    m = re.search(r"(Ambos\s+os\s+Times|Ambas\s+as\s+Equipes|Ambos\s+Marcam).*?Sim\s+" + ODD_PAT, t, re.I)
    if m:
        # último grupo é o número
        num = m.groups()[-1]
        try: markets["BTTS"] = _fnum(num)
        except: pass

    # 1X2 — heurísticas simples (Bet365 muda labels; cobrimos variações)
    # Buscamos padrões de "Casa", "Empate", "Visitante" seguidos de odd
    mhome = re.search(r"(Casa|Mandante)[^\d]{0,20}" + ODD_PAT, t, re.I)
    if mhome: markets["HOME"] = _fnum(mhome.group(2) if mhome.lastindex and mhome.lastindex >= 2 else mhome.group(1))

    mdraw = re.search(r"(Empate)[^\d]{0,20}" + ODD_PAT, t, re.I)
    if mdraw: markets["DRAW"] = _fnum(mdraw.group(2) if mdraw.lastindex and mdraw.lastindex >= 2 else mdraw.group(1))

    maway = re.search(r"(Visitante|Fora)[^\d]{0,20}" + ODD_PAT, t, re.I)
    if maway: markets["AWAY"] = _fnum(maway.group(2) if maway.lastindex and maway.lastindex >= 2 else maway.group(1))

    # fallbacks sem label explícito (menos confiável, mas ajuda)
    if markets["O25"] is None:
        m = re.search(r"\b2[.,]5\b\s+" + ODD_PAT, t)
        if m: markets["O25"] = _fnum(m.group(1))
    if markets["O15"] is None:
        m = re.search(r"\b1[.,]5\b\s+" + ODD_PAT, t)
        if m: markets["O15"] = _fnum(m.group(1))

    return markets

def grab_any_open_market(page):
    """Tenta na liga atual; se vazio, gira ligas e clica horários, então extrai."""
    # liga atual
    txt = page.evaluate("document.body.innerText")
    mk = extract_markets(txt)
    if any(mk.values()):
        return None, mk

    # clica horário
    if click_timeslot(page):
        txt = page.evaluate("document.body.innerText")
        mk = extract_markets(txt)
        if any(mk.values()):
            return None, mk

    # gira ligas
    for lg in LEAGUES_PT:
        if goto_league(page, lg):
            click_timeslot(page)
            txt = page.evaluate("document.body.innerText")
            mk = extract_markets(txt)
            if any(mk.values()):
                return lg, mk

    return None, mk  # possivelmente vazio

# ------------------ Seleção & Formatação ------------------
def pick_signal(markets):
    """Escolhe melhor mercado dentro das faixas assertivas, por prioridade."""
    cands = []

    o15, o25, b = markets.get("O15"), markets.get("O25"), markets.get("BTTS")
    h, d, a = markets.get("HOME"), markets.get("DRAW"), markets.get("AWAY")

    if o25 and RANGE_O25[0] <= o25 <= RANGE_O25[1]:
        cands.append(("Over 2.5", o25))
    if b and RANGE_BTTS[0] <= b <= RANGE_BTTS[1]:
        cands.append(("BTTS (Ambos Marcam)", b))
    if o15 and RANGE_O15[0] <= o15 <= RANGE_O15[1]:
        cands.append(("Over 1.5", o15))
    if h and RANGE_HOME[0] <= h <= RANGE_HOME[1]:
        cands.append(("Vitória Casa", h))
    if d and RANGE_DRAW[0] <= d <= RANGE_DRAW[1]:
        cands.append(("Empate", d))
    if a and RANGE_AWAY[0] <= a <= RANGE_AWAY[1]:
        cands.append(("Vitória Visitante", a))

    if not cands:
        return None

    # ordena por PRIORITY (lista fixa) e depois pela menor odd (maior prob. implícita)
    cands.sort(key=lambda x: (PRIORITY.index(x[0]) if x[0] in PRIORITY else 99, x[1]))
    return cands[0]  # (market, price)

def fmt_message(markets, pick, league_used):
    o15, o25, b = markets.get("O15"), markets.get("O25"), markets.get("BTTS")
    h, d, a = markets.get("HOME"), markets.get("DRAW"), markets.get("AWAY")
    market, price = pick
    stake = max(1.0, BANKROLL * STAKE_PCT)
    gale  = min(BANKROLL - stake, stake * 2.0)
    liga  = f" — <i>{league_used}</i>" if league_used else ""

    lines = [
        f"⚽ <b>Futebol Virtual — Bet365</b>{liga}  ({datetime.now().strftime('%H:%M')})",
        "Mercados (casa): " +
        (f"O1.5 {o15:.2f} | " if o15 else "") +
        (f"O2.5 {o25:.2f} | " if o25 else "") +
        (f"BTTS {b:.2f} | " if b else "") +
        (f"1 {h:.2f} | " if h else "") +
        (f"X {d:.2f} | " if d else "") +
        (f"2 {a:.2f}" if a else ""),
        "",
        f"✅ <b>Entrada</b>: <u>{market}</u> @ <b>{price:.2f}</b>",
        f"💵 Stake: <b>{_fmt_money(stake)}</b>",
        f"🪙 Gale 1x (se Red): <b>{_fmt_money(gale)}</b>",
        "⛔ Sem 2º gale. Volte à stake base após a sequência."
    ]
    return "\n".join(lines)

# ------------------ Core Scan ------------------
def scan_once():
    def _run(page):
        league_used, mk = grab_any_open_market(page)
        print("LEAGUE:", league_used or "atual", "MARKETS:", mk)
        pick = pick_signal(mk)
        if not pick:
            return {"sent": False, "reason": "Sem odds dentro das faixas", "markets": mk, "league": league_used}
        text = fmt_message(mk, pick, league_used)
        if text == _last_text["txt"]:
            return {"sent": False, "reason": "duplicate", "markets": mk, "league": league_used}
        res = _send_telegram(text)
        if res.get("ok") or "result" in res:
            _last_text["txt"] = text
            return {"sent": True, "markets": mk, "league": league_used}
        return {"sent": False, "reason": str(res), "markets": mk, "league": league_used}

    try:
        return with_browser(_run)
    except Exception as e:
        return {"sent": False, "error": str(e)}

# ------------------ Scheduler (3 min) ------------------
def scheduler_loop():
    time.sleep(25)  # atraso inicial pro serviço estabilizar
    while True:
        try:
            r = scan_once()
            print("SCAN:", r)
        except Exception as e:
            print("SCHED ERROR:", e)
        time.sleep(max(60, INTERVAL_MIN * 60))  # mínimo 60s de folga

t = threading.Thread(target=scheduler_loop, daemon=True)
t.start()

# ------------------ Endpoints ------------------
@app.get("/")
def root():
    return jsonify({"status": "online", "message": "Servidor ativo no Render"})

@app.get("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})

@app.get("/scan")
def scan_endpoint():
    return jsonify(scan_once())

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
