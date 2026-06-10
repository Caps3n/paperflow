FROM python:3.12-slim

WORKDIR /app

# Minimale System-Libs für Playwright CDP-Modus (kein lokaler Browser nötig)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

CMD ["python", "-m", "app.main"]
