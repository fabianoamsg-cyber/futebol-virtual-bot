import os
import json
import asyncio
from datetime import datetime
from flask import Flask, jsonify
from playwright.async_api import async_playwright

app = Flask(__name__)

COOKIES_JSON = os.getenv("COOKIES_JSON")
GROUP_ID = os.getenv("GRUPO_ID")
TOKEN = os.getenv("BOT_TOKEN")

# URL principal da Bet365 (Futebol Virtual)
BET_URL = "https://www.bet365.bet.br/#/AVR/B146/R^1/"

# Função principal de leitura das odds
async def get_odds():
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context()

            # Carrega cookies da variável de ambiente
            if COOKIES_JSON:
                cookies = json.loads(COOKIES_JSON)
                await context.add_cookies(cookies)

            page = await context.new_page()
            await page.goto(BET_URL, timeout=60000)

            # Espera até os elementos das odds aparecerem
            await page.wait_for_timeout(7000)

            # Se a página não tiver odds, tenta atualizar
            if "login" in page.url or "error" in page.url:
                await page.goto(BET_URL, timeout=60000)
                await page.wait_for_timeout(7000)

            # Captura os textos
            content = await page.content()

            # Odds padrão
            odds = {"O15": None, "O25": None, "BTTS": None}

            # Busca pelas odds de gols
            try:
                elementos = await page.query_selector_all("div.gl-MarketGroupContainer")
                for e in elementos:
                    texto = (await e.inner_text()).lower()
                    if "mais de 1.5" in texto:
                        odds["O15"] = float(texto.split("mais de 1.5")[-1].split("\n")[0].strip().replace(",", "."))
                    if "mais de 2.5" in texto:
                        odds["O25"] = float(texto.split("mais de 2.5")[-1].split("\n")[0].strip().replace(",", "."))
                    if "ambas as equipes marcam" in texto:
                        odds["BTTS"] = float(texto.split("sim")[-1].split("\n")[0].strip().replace(",", "."))
            except Exception as e:
                print("Erro ao extrair odds:", e)

            await browser.close()

            # Retorna odds capturadas ou motivo
            if odds["O25"]:
                return {"league": "Express Cup", "odds": odds, "sent": True}
            else:
                return {"league": None, "odds": odds, "reason": "Sem O2.5 na tela — pulando.", "sent": False}

    except Exception as e:
        print(f"Erro geral: {e}")
        return {"error": str(e), "sent": False}


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})


@app.route("/scan")
async def scan():
    data = await get_odds()
    return jsonify(data)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
