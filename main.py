# Bot Bet365 – Futebol Virtual → sinais automáticos no Telegram
# - Leitura: Over 1.5, Over 2.5, BTTS (Ambos Marcam)
# - Filtros conservadores (mais assertivos) com 1º mercado prioritário
# - Envio automático a cada INTERVAL_MIN (default 10)
# - Endpoints: /health (status) e /scan (forçar leitura + envio)
# Observação: Virtuais são RNG. Use gestão de banca. Sem garantias.

import os, re, time, threading, json
from datetime import datetime

from flask import Flask, jsonify
import requests
from playwright.sync_api import sync_playwright

# ================== CONFIG ==================
BOT_TOKEN    = os.getenv("BOT_TOKEN", "7599991522:AAHSR8pkqQ_Btinnxi_YvgTfaifF1pvrxps")
CHANNEL_ID   = int(os.getenv("CHANNEL_ID", "-1002814723832"))
BET365_URL   = os.getenv("BET365_URL", "https://www.bet365.bet.br/#/AVR/B146/R^1/")
COOKIES_JSON = os.getenv("COOKIES_JSON", "")  # JSON exportado pelo Cookie-Editor
INTERVAL_MIN = int(os.getenv("INTERVAL_MIN", "10"))

# Faixas “mais assertivas”
RANGE_O15  = (1.25, 1.45)
RANGE_O25  = (1.85, 2.15)
RANGE_BTTS = (1.70, 2.00)

# Se quiser stake/banca, use ENV (opcional)
BANKROLL  = float(os.getenv("BANKROLL", "200"))
STAKE_PCT = float(os.getenv("STAKE_PCT", "0.03"))  # 3% default
# ============================================

app = Flask(__name__)
_last_text = {"txt": None}  # anti-spam simples

def _fmt_money(x: float) -> str:
    s = f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R${s}"

def _send_telegram(text: str):
    if not BOT_TOKEN or not CHANNEL_ID:
        return {"ok": False, "reason": "BOT_TOKEN/CHANNEL_ID ausente"}
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        r = requests.post(url, json={
            "chat_id": CHANNEL_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=20)
        return r.json()
    except Exception as e:
        return {"ok": False, "reason": str(e)}

def _parse_float(s: str):
    try:
        return float(s.replace(",", "."))
    except:
        return None

ODD_PAT = r"([1-9]\d*(?:[.,]\d{1,2})?)"

def extract_odds_from_text(text: str):
    """Lê odds a partir do texto renderizado (innerText) da página."""
    # normaliza
    t = " ".join(text.split())
    o15 = o25 = btts = None

    # 'Mais de 1,5' / 'Mais de 2,5'
    m25 = re.search(r"Mais\s*de\s*2[.,]5\s+" + ODD_PAT, t, re.I)
    if m25: o25 = _parse_float(m25.group(1))
    m15 = re.search(r"Mais\s*de\s*1[.,]5\s+" + ODD_PAT, t, re.I)
    if m15: o15 = _parse_float(m15.group(1))

    # BTTS: “Ambos os Times/Equipes marcam – Sim”
    mb  = re.search(r"(Ambos\s+os\s+Times|Ambas\s+as\s+Equipes|Ambos\s+Marcam).*?Sim\s+" + ODD_PAT,
                    t, re.I)
    if mb: btts = _parse_float(mb.group(2) if mb.lastindex and mb.lastindex >= 2 else mb.group(1))

    # Fallback simples se o label “Mais de” não vier junto do número
    if not o25:
        m = re.search(r"\b2[.,]5\b\s+" + ODD_PAT, t)
        if m: o25 = _parse_float(m.group(1))
    if not o15:
        m = re.search(r"\b1[.,]5\b\s+" + ODD_PAT, t)
        if m: o15 = _parse_float(m.group(1))

    return {"O15": o15, "O25": o25, "BTTS": btts}

def with_browser(fn):
    """Abre o Chromium em modo leve e executa a função fn(page) → any."""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-zygote",
                "--single-process",
                "--disable-extensions",
                "--disable-background-networking",
                "--mute-audio",
                "--hide-scrollbars",
                "--disable-features=TranslateUI,BlinkGenPropertyTrees"
            ]
        )
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0 Safari/537.36"),
            viewport={"width": 1280, "height": 720},
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            extra_http_headers={"Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"}
        )

        # Cookies (se houver)
        if COOKIES_JSON.strip():
            try:
                cookies = json.loads(COOKIES_JSON)
                context.add_cookies(cookies)
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
            # tenta garantir que os mercados de gols apareçam
            try:
                page.wait_for_selector("text=/Gols\\s+Mais\\/Menos|Total\\s+de\\s+Gols/i", timeout=10000)
            except:
                pass
            page.wait_for_timeout(5000)
            result = fn(page)
        finally:
            context.close()
            browser.close()
        return result

def click_any_timeslot(page):
    """Clica em um horário (p.ex. 9:52 / 21:52) para sair de 'Evento Iniciado'."""
    try:
        slots = page.locator("text=/\\b\\d{1,2}:\\d{2}\\b/")
        n = slots.count()
        for i in range(min(n, 6)):
            try:
                slots.nth(i).click(timeout=900)
                page.wait_for_timeout(1200)
                return True
            except:
                continue
    except:
        pass
    return False

def grab_odds_now(page):
    """Extrai odds do frame principal (texto renderizado)."""
    # Se cair em 'Evento iniciado', tenta mudar para outro horário
    click_any_timeslot(page)
    txt = page.evaluate("document.body.innerText")
    return extract_odds_from_text(txt)

def choose_signal(odds):
    """Escolhe o melhor mercado dentro das faixas mais assertivas."""
    o15, o25, b = odds.get("O15"), odds.get("O25"), odds.get("BTTS")
    candidates = []
    if o25 and RANGE_O25[0] <= o25 <= RANGE_O25[1]:
        candidates.append(("Over 2.5", o25, 1))
    if b and RANGE_BTTS[0] <= b <= RANGE_BTTS[1]:
        candidates.append(("BTTS (Ambos Marcam)", b, 2))
    if o15 and RANGE_O15[0] <= o15 <= RANGE_O15[1]:
        candidates.append(("Over 1.5", o15, 3))
    # prioridade: 2.5 > BTTS > 1.5 (índice menor é mais prioritário)
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[2], -x[1]))  # por prioridade, depois odd desc
    return candidates[0]  # (market, odd, prio)

def format_message(odds, pick):
    """Monta a mensagem para o Telegram."""
    o15, o25, b = odds.get("O15"), odds.get("O25"), odds.get("BTTS")
    market, price, _ = pick
    stake = max(1.0, BANKROLL * STAKE_PCT)
    gale  = min(BANKROLL - stake, stake * 2.0)

    lines = [
        f"⚽ <b>Futebol Virtual — Bet365</b> ({datetime.now().strftime('%H:%M')})",
        f"Mercados (casa): " +
        (f"O1.5 {o15:.2f} | " if o15 else "") +
        (f"O2.5 {o25:.2f} | " if o25 else "") +
        (f"BTTS {b:.2f}" if b else "BTTS —"),
        "",
        f"✅ <b>Entrada</b>: <u>{market}</u> @ <b>{price:.2f}</b>",
        f"💵 Stake: <b>{_fmt_money(stake)}</b>",
        f"🪙 Gale 1x (se Red): <b>{_fmt_money(gale)}</b>",
        "⛔ Sem 2º gale. Volte à stake base após a sequência."
    ]
    return "\n".join(lines)

def scan_and_maybe_send():
    """Faz a leitura e envia se houver sinal dentro das faixas."""
    def _run(page):
        odds = grab_odds_now(page)
        print("ODDS:", odds)
        pick = choose_signal(odds)
        if not pick:
            return {"sent": False, "reason": "Sem odds dentro da faixa assertiva", "odds": odds}
        text = format_message(odds, pick)
        # anti-duplicação simples
        if text == _last_text["txt"]:
            return {"sent": False, "reason": "duplicate", "odds": odds}
        res = _send_telegram(text)
        if res.get("ok") or "result" in res:
            _last_text["txt"] = text
            return {"sent": True, "odds": odds}
        return {"sent": False, "reason": str(res), "odds": odds}

    try:
        result = with_browser(_run)
    except Exception as e:
        result = {"sent": False, "error": str(e)}
    return result

# =============== SCHEDULER ===============
def _scheduler_loop():
    # atraso inicial pequeno para o serviço estabilizar
    time.sleep(30)
    while True:
        try:
            r = scan_and_maybe_send()
            print("SCAN:", r)
        except Exception as e:
            print("SCHED ERROR:", e)
        # intervalo padrão 10 min (ajustável via ENV)
        time.sleep(max(60, INTERVAL_MIN * 60))

t = threading.Thread(target=_scheduler_loop, daemon=True)
t.start()
# ========================================

# =============== ENDPOINTS ===============
@app.get("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})

@app.get("/")
def root():
    return jsonify({"status": "online", "message": "Servidor ativo no Render"})

@app.get("/scan")
def scan_endpoint():
    r = scan_and_maybe_send()
    return jsonify(r)
# ========================================

if __name__ == "__main__":
    # Para rodar localmente (no Render o gunicorn chama main:app)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
