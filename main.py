from flask import Flask, jsonify
import asyncio
from playwright.async_api import async_playwright

app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({"status": "online", "message": "Servidor ativo no Render"})

@app.route('/scan')
async def scan():
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-software-rasterizer",
                    "--single-process",
                    "--no-zygote",
                    "--disable-extensions",
                    "--disable-infobars",
                    "--disable-notifications",
                    "--mute-audio",
                    "--disable-background-networking",
                    "--disable-default-apps",
                    "--disable-sync",
                    "--disable-translate",
                    "--hide-scrollbars",
                    "--disable-features=site-per-process,TranslateUI,BlinkGenPropertyTrees"
                ]
            )

            page = await browser.new_page()
            await page.goto("https://www.bet365.bet.br/#/AVR/B146/R^1/", timeout=45000)

            # Espera alguns segundos para garantir que o site carregou
            await asyncio.sleep(5)

            # Retorna um texto simples para confirmar que rodou
            await browser.close()
            return jsonify({"status": "ok", "reason": "Scan executado com sucesso!"})

    except Exception as e:
        return jsonify({"status": "erro", "motivo": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
