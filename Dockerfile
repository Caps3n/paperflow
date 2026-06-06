FROM python:3.12-slim

# System-Abhängigkeiten für Playwright
RUN apt-get update && apt-get install -y \
    wget curl gnupg \
    libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 \
    libgbm1 libasound2 libxshmfence1 libgtk-3-0 \
    fonts-liberation libappindicator3-1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright-Browser installieren (Chromium)
RUN playwright install chromium
RUN playwright install-deps chromium

COPY app/ ./app/

CMD ["python", "-m", "app.main"]
