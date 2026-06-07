FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Im CDP-Modus (CHROME_CDP_URL gesetzt) wird kein lokaler Browser benötigt.
# Xvfb nur als Fallback falls kein chrome-desktop Container läuft.
RUN apt-get update && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

CMD ["python", "-m", "app.main"]
