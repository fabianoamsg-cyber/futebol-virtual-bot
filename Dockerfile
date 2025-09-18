FROM python:3.11-slim

# Dependências do Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates libnss3 libx11-6 libx11-xcb1 libxcomposite1 libxcursor1 \
    libxdamage1 libxi6 libxtst6 libdrm2 libgbm1 libxrandr2 libasound2 libpangocairo-1.0-0 \
    libatk1.0-0 libcups2 libgtk-3-0 libxshmfence1 libxfixes3 libglib2.0-0 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir gunicorn && \
    python -m playwright install chromium

COPY main.py .

# Tempo maior pro worker inicializar com folga
CMD ["sh", "-c", "gunicorn -w 1 -t 180 -b 0.0.0.0:${PORT} main:app"]
