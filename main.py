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

# Não derruba o processo se faltar token (para o Gunicorn não falhar em import)
bot = Bot(BOT_TOKEN) if BOT_TOKEN else None
last_signal = {"text": None}
bankroll_state = {"bankroll": BANKROLL}

# ------------------ FLASK APP ------------------
app = Flask(__name__)  # <<<<<<<<<<<<<<  ESSA VARIÁVEL PRECISA EXISTIR

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
    (1.70,1.78,3.0,1.73),(1.79,1.92,2.8,1.88),
    (1.93,2.10,2.6,2.07),(2.11,2.35,2.4,2.33),(2.36,2.70,2.2,2.65)
]
def lam_from_o25(o25):
    best=(None,999)
    for _,_,lam,fair in LAMBDA_TABLE:
        d=abs(o25-fair)
        if d<best[1]: best=((lam,fair),d)
    return best[0][0]

def fmt_money(x): return f"R${x:,.2f}".replace(",", "X").replace(".", ",").replace("X",".")

# ------------------ EXTRAÇÃO DE ODDS ------------------
def parse_float(txt):
    try: return float(txt.replace(",", "."))
    except: return None

def extract_by_regex(html):
    o15=o25=btts=None
    num=r"([1-9]\d*(?:[.,]\d{1,2})?)"
    m15=re.search(r"[\s>](?:1[.,]5)[\s\S]{0,120}?"+num, html)
    if m15: o15=parse_float(m15.group(1))
    m25=re.search(r"[\s>](?:2[.,]5)[\s\S]{0,120}?"+num, html)
    if m25: o25=parse_float(m25.group(1))
    mb=re.search(r"Ambos\s+os\s+Times[\s\S]{0,200}?Sim[\s\S]{0,20}?"+num, html)
    if mb: btts=parse_float(mb.group(1))
    return {"O15":o15,"O25":o25,"BTTS":btts}

def extract_by_locators(page):
    o15=o25=btts=None
    try:
        html = page.content()
        for needle,key in [("1.5","O15"),("2.5","O25")]:
            m = re.search(needle+r"[\s\S]{0,150}?([1-9]\d*(?:[.,]\d{1,2})?)", html)
            if m:
                val=parse_float(m.group(1))
                if key=="O15": o15=val
                else: o25=val
        node = page.locator("text=Ambos os Times").first
        if node:
            parent = node.locator("xpath=..")
            inner = parent.inner_html()
            m = re.search(r"Sim[\s\S]{0,40}?([1-9]\d*(?:[.,]\d{1,2})?)", inner)
            if m: btts=parse_float(m.group(1))
    except: pass
    return {"O15":o15,"O25":o25,"BTTS":btts}

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
            # Bloqueia imagens/mídias para economizar memória
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
            page.wait_for_timeout(6000)
            res = fn(page)
            context.close(); browser.close()
            return res
    return _wrap

def decide_and_text(odds):
    o25=odds.get("O25"); o15=odds.get("O15"); b=odds.get("BTTS")
    if not o25: return None, "Sem O2.5 na tela — pulando."
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

def merge_odds(a,b):
    out={}
    for k in ["O15","O25","BTTS"]:
        out[k]=a.get(k) or b.get(k)
    return out

@with_browser
def scan_once(page):
    html = page.content()
    o_regex = extract_by_regex(html)
    o_loc   = extract_by_locators(page)
    odds = merge_odds(o_regex, o_loc)
    print("ODDS EXTRAIDAS:", odds)
    signal, err = decide_and_text(odds)
    if err or not signal: 
        return {"odds":odds,"sent":False,"reason":err}
    _, text = signal
    if bot and text != last_signal["text"]:
        bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        last_signal["text"] = text
        return {"odds":odds,"sent":True}
    return {"odds":odds,"sent":False,"reason":"duplicated or no bot"}

def scheduler_loop():
    schedule.every(INTERVAL_MIN).minutes.do(scan_once)
    time.sleep(40)  # atraso para não pesar no boot
    scan_once()
    while True:
        schedule.run_pending()
        time.sleep(1)

# thread do agendador ao importar o módulo
t = threading.Thread(target=scheduler_loop, daemon=True)
t.start()

# Endpoint de diagnóstico
@app.get("/scan")
def scan_endpoint():
    try:
        result = scan_once()
        return jsonify(result)
    except Exception as e:
        return jsonify(error=str(e)), 500
