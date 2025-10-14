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
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-software-rasterizer",
                    "--disable-setuid-sandbox",
                    "--single-process"
                ]
            )
            page = await browser.new_page()
            await page.goto("https://www.bet365.bet.br/#/AVR/B146/R^1/", timeout=60000)
            await asyncio.sleep(5)
            await browser.close()
        return jsonify({"status": "ok", "reason": "Navegação concluída com sucesso!"})
    except Exception as e:
        return jsonify({"status": "erro", "motivo": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
