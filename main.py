# main.py — Futebol Virtual -> Sinais no Telegram (Web Service + Playwright leve)
# Notas:
# - Virtuais são RNG. O bot só filtra "value". Use gestão de banca e apenas 1 gale.
# - Este arquivo inclui: /health, /scan (diagnóstico), giro de ligas e horários.

import os, math, re, time, threading
from datetime import datetime
import schedule

from flask import Flask, jsonify
from playwright.sync_api import sync_playwright
from telegram import Bot
from telegram.constants import ParseMode

# ------------------ CONFIG ------------------
BOT_TOKEN   = os.getenv("BOT_TOKEN") or "7599991522:AAHSR8pkqQ_Btinnxi_YvgTfaifF1pvrxps"
CHANNEL_ID  = int(os.getenv("CHANNEL_ID") or "-1002814723832")
BANKROLL    = float(os.getenv("BANKROLL")    or "200")
STAKE_PCT   = float(os.getenv("STAKE_PCT")   or "0.03")
MARGIN_VAL  = float(os.getenv("MARGIN_VALUE") or "0.03")
INTERVAL_MIN= int(os.getenv("INTERVAL_MIN")  or "3")
BET365_URL  = os.getenv("BET365_URL") or "https://www.bet365.bet.br/#/AVR/B146/R^1/"
COOKIES_JSON= os.getenv("COOKIES_JSON") or ""

bot = Bot(BOT_TOKEN) if BOT_TOKEN else None
last_signal = {"text": None}
bankroll_state = {"bankroll": BANKROLL}

# ------------------ FLASK ------------------
app = Flask(__name__)  # <- o gunicorn usa main:app

@app.get("/")
def root():
    return "OK - futebol-virtual-bot"

@app.get("/health")
def health():
    return jsonify(status="ok", time=datetime.utcnow().isoformat())

# ------------------ MODELO DE GOLS ------------------
def p_over15(l): return 1 - math.exp(-l)*(1+l)
def p_over25(l):
    p0 = math.exp(-l); p1 = p0*l; p2 = p1*l/2
    return 1 - (p0+p1+p2)
def p_btts(l):
    lam=l/2
    return 1 - 2*math.exp(-lam) + math.exp(-l)
def odd(p): return float('inf') if p<=0 else 1.0/max(min(p, 0.999999), 1e-9)

LAMBDA_TABLE = [
    (1.70,1.78,3.0,1.73),
    (1.79,1.92,2.8,1.88),
    (1.93,2.10,2.6,2.07),
    (2.11,2.35,2.4,2.33),
    (2.36,2.70,2.2,2.65)
]
def lam_from_o25(o25):
    best=(None,999)
    for _,_,lam,fair in LAMBDA_TABLE:
        d=abs(o25-fair)
        if d<best[1]: best=((lam,fair),d)
    return best[0][0]

def fmt_money(x): return f"R${x:,.2f}".replace(",", "X").replace(".", ",").replace("X",".")

# ------------------ PARSERS ------------------
ODD_PAT = r"([1-9]\d*(?:[.,]\d{1,2})?)"

def parse_float(txt):
    try: return float(txt.replace(",", "."))
    except: return None

def extract_from_html(html: str):
    """Regex no HTML (main frame)."""
    o15=o25=btts=None
    m25=re.search(r"(?:Mais\s+de\s*)?2[.,]5[\s\S]{0,120}?"+ODD_PAT, html, re.I)
    if m25: o25=parse_float(m25.group(1))
    m15=re.search(r"(?:Mais\s+de\s*)?1[.,]5[\s\S]{0,120}?"+ODD_PAT, html, re.I)
    if m15: o15=parse_float(m15.group(1))
    mb =re.search(r"(Ambos\s+os\s+Times|Ambas\s+as\s+Equipes|Ambos\s+Marcam)[\s\S]{0,160}?(?:Sim)[\s\S]{0,32}?"+ODD_PAT, html, re.I)
    if mb:  btts=parse_float(mb.group(1))
    return {"O15":o15,"O25":o25,"BTTS":btts}

def extract_from_text(text: str):
    """Regex no texto bruto renderizado (innerText)."""
    o15=o25=btts=None
    m25=re.search(r"Mais\s*de\s*2[.,]5[\s\n\r]{0,10}"+ODD_PAT, text, re.I)
    if m25: o25=parse_float(m25.group(1))
    m15=re.search(r"Mais\s*de\s*1[.,]5[\s\n\r]{0,10}"+ODD_PAT, text, re.I)
    if m15: o15=parse_float(m15.group(1))
    mb =re.search(r"(Ambos\s+os\s+Times|Ambas\s+as\s+Equipes|Ambos\s+Marcam)[\s\S]{0,160}?(?:Sim)[\s\S]{0,32}?"+ODD_PAT, text, re.I)
    if mb:  btts=parse_float(mb.group(1))
    # Fallback sem “Mais de”: 2.5 em linha com odd ao lado
    if not o25:
        m=re.search(r"\b2[.,]5\b[\s\n\r]{0,20}"+ODD_PAT, text)
        if m: o25=parse_float(m.group(1))
    if not o15:
        m=re.search(r"\b1[.,]5\b[\s\n\r]{0,20}"+ODD_PAT, text)
        if m: o15=parse_float(m.group(1))
    return {"O15":o15,"O25":o25,"BTTS":btts}

def extract_by_locators(page):
    """Vasculha o DOM por vizinhança de '1.5'/'2.5' e por 'Ambos os Times'."""
    o15=o25=btts=None
    try:
        # odds próximas de “2.5” e “1.5”
        for needle,key in [("2.5","O25"),("1.5","O15")]:
            node = page.locator(f"text=/\\b{needle}\\b/").first
            if node:
                # pega HTML do contêiner mais próximo
                html = node.locator("xpath=ancestor-or-self::*[1]").inner_html()
                m = re.search(ODD_PAT, html)
                if m:
                    val = parse_float(m.group(1))
                    if key=="O25": o25=val
                    else: o15=val
        # BTTS
        node = page.locator("text=/Amb(os|as).*(Times|Equipes)|Ambos\\s+Marcam/i").first
        if node:
            inner = node.locator("xpath=..").inner_html()
            m = re.search(r"Sim[\s\\S]{0,40}?"+ODD_PAT, inner, re.I)
            if m: btts=parse_float(m.group(1))
    except:
        pass
    return {"O15":o15,"O25":o25,"BTTS":btts}

def merge_odds(a,b,c):
    out={}
    for k in ["O15","O25","BTTS"]:
        out[k]=a.get(k) or b.get(k) or c.get(k)
    return out

# ------------------ NAVEGAÇÃO ------------------
LEAGUES_PT = ["Express Cup", "Copa do Mundo", "Euro Cup", "Super Liga Sul-Americana", "Premier League"]

def goto_league(page, name):
    try:
        tab = page.locator(f"text={name}").first
        if tab:
            tab.click(timeout=2500)
            page.wait_for_timeout(2000)
            # tenta expandir Gols Mais/Menos
            try:
                page.locator("text=/Gols\\s+Mais\\/Menos|Total\\s+de\\s+Gols/i").first.click(timeout=800)
                page.wait_for_timeout(400)
            except: pass
            return True
    except: pass
    return False

def click_time_slot(page):
    """Clica no primeiro horário visível (ex.: 21:52) para sair de 'Evento iniciado'."""
    try:
        slots = page.locator("text=/^\\d{2}:\\d{2}$/")
        n = slots.count()
        for i in range(min(n,5)):
            try:
                slots.nth(i).click(timeout=800)
                page.wait_for_timeout(1200)
                return True
            except:
                continue
    except:
        pass
    return False

def grab_odds_now(page):
    """Combina 3 estratégias: HTML, innerText e locators."""
    html = page.content()
    text = page.evaluate("document.body.innerText")  # captura texto renderizado
    a = extract_from_html(html)
    b = extract_from_text(text)
    c = extract_by_locators(page)
    return merge_odds(a,b,c)

# ------------------ PLAYWRIGHT (modo leve) ------------------
def with_browser(fn):
    def _wrap():
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox","--disable-dev-shm-usage","--disable-gpu",
                    "--disable-extensions","--disable-background-networking",
                    "--disable-background-timer-throttling","--disable-renderer-backgrounding",
                    "--no-zygote","--single-process","--js-flags=--max-old-space-size=128"
                ]
            )
            context = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"),
                viewport={"width":1280,"height":720}
            )
            # bloqueia imagens/mídias p/ economizar RAM
            def _route(route):
                if route.request.resource_type in ("image","media","font"):
                    return route.abort()
                return route.continue_()
            context.route("**/*", _route)

            if COOKIES_JSON:
                try:
                    import json; context.add_cookies(json.loads(COOKIES_JSON))
                except Exception as e:
                    print("cookies load error:", e)

            page = context.new_page()
            page.set_default_timeout(20000)
            page.goto(BET365_URL, timeout=60000, wait_until="domcontentloaded")
            try: page.wait_for_load_state("networkidle", timeout=15000)
            except: pass
            page.wait_for_timeout(5000)
            res = fn(page)
            context.close(); browser.close()
            return res
    return _wrap

# ------------------ DECISÃO ------------------
def decide_and_text(odds):
    o25=odds.get("O25"); o15=odds.get("O15"); b=odds.get("BTTS")
    if not o25: return None, "Sem O2.5 na tela — pulando."
    # λ estimado pela faixa de O2.5
    lam=lam_from_o25(o25)
    p15=p_over15(lam); fair15=odd(p15)
    p25=p_over25(lam); fair25=odd(p25)
    pbt=p_btts(lam);   fairbt=odd(pbt)
    def diff(h,f): 
        if not f or f==float('inf'): return -1
        return (h/f)-1.0
    c=[("Over 2.5",o25,fair25,p25,diff(o25,fair25))]
    if b:   c.append(("BTTS (Ambos Marcam)",b,fairbt,pbt,diff(b,fairbt)))
    if o15: c.append(("Over 1.5",o15,fair15,p15,diff(o15,fair15)))
    prio={"Over 2.5":0,"BTTS (Ambos Marcam)":1,"Over 1.5":2}
    c.sort(key=lambda x:(prio[x[0]],-x[4]))
    market,house,fair,_,dv=c[0]
    value = dv>=MARGIN_VAL
    stake=max(1.0,bankroll_state["bankroll"]*STAKE_PCT)
    gale=min(bankroll_state["bankroll"]-stake, stake*2.0)
    header=f"⚽ <b>Futebol Virtual — Sinal</b>  ({datetime.now().strftime('%H:%M')})"
    body=[
        f"λ estimado: <b>{lam:.1f}</b>",
        f"Justas → O1.5 <b>{fair15:.2f}</b> | O2.5 <b>{fair25:.2f}</b> | BTTS <b>{fairbt:.2f}</b>",
        "Casa  → "+(f"O1.5 {o15:.2f} | " if o15 else "")+f"O2.5 <b>{o25:.2f}</b> | "+(f"BTTS {b:.2f}" if b else "BTTS —"),
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
    return (True, header+"\n"+"\n".join(body)), None

# ------------------ SCAN ------------------
@with_browser
def scan_once(page):
    odds = {"O15": None, "O25": None, "BTTS": None}
    league_used = None

    # 1) tenta liga atual
    try:
        odds = grab_odds_now(page)
    except:
        pass

    # 2) se ainda sem O2.5, clica em horários (foge do 'Evento iniciado')
    if not odds.get("O25"):
        if click_time_slot(page):
            odds = grab_odds_now(page)

    # 3) se ainda não achou, gira ligas
    if not odds.get("O25"):
        for lg in LEAGUES_PT:
            if goto_league(page, lg):
                # após trocar de liga, também tenta clicar num horário
                click_time_slot(page)
                tmp = grab_odds_now(page)
                if tmp.get("O25"):
                    odds = tmp
                    league_used = lg
                    break

    print("LIGA:", league_used or "atual", " — ODDS:", odds)

    signal, err = decide_and_text(odds)
    if err or not signal:
        return {"odds": odds, "sent": False, "reason": err, "league": league_used}

    _, text = signal
    if bot and text != last_signal["text"]:
        bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        last_signal["text"] = text
        return {"odds": odds, "sent": True, "league": league_used}

    return {"odds": odds, "sent": False, "reason": "duplicated or no bot", "league": league_used}

# ------------------ AGENDADOR ------------------
def scheduler_loop():
    schedule.every(INTERVAL_MIN).minutes.do(scan_once)
    time.sleep(40)  # atraso no boot p/ não sobrecarregar
    scan_once()
    while True:
        schedule.run_pending()
        time.sleep(1)

# inicializa thread do agendador ao importar
t = threading.Thread(target=scheduler_loop, daemon=True)
t.start()

# diagnóstico manual
@app.get("/scan")
def scan_endpoint():
    try:
        result = scan_once()
        return jsonify(result)
    except Exception as e:
        return jsonify(error=str(e)), 500
