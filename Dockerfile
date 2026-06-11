FROM python:3.12-slim

WORKDIR /app

# Minimale System-Libs für Playwright CDP-Modus (kein lokaler Browser nötig)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# pkg_resources stub: setuptools 70+ does not install pkg_resources as a top-level
# module on python:3.12-slim, but playwright-stealth 1.x requires it.
RUN python3 -c "\
import sys, os; \
d = os.path.join(sys.prefix, 'lib', f'python{sys.version_info.major}.{sys.version_info.minor}', 'site-packages', 'pkg_resources'); \
os.makedirs(d, exist_ok=True); \
open(os.path.join(d, '__init__.py'), 'w').write( \
'import os, importlib.util\n' \
'def resource_string(pkg, res):\n' \
'    spec = importlib.util.find_spec(pkg)\n' \
'    path = os.path.join(os.path.dirname(spec.origin), res)\n' \
'    with open(path, \"rb\") as f: return f.read()\n' \
)"

COPY app/ ./app/

# Custom-Provider-Verzeichnis sicherstellen (auch ohne Volume-Mount vorhanden)
RUN mkdir -p /app/providers_custom

CMD ["python", "-m", "app.main"]
