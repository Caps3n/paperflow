FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Xvfb – virtuelles Display für headless=False Chromium (umgeht Amazon Bot-Erkennung)
RUN apt-get update && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

CMD ["python", "-m", "app.main"]
