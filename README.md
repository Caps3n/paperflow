# 📄 paperflow

**Automatically fetch invoices from online providers and import them into [Paperless-NGX](https://github.com/paperless-ngx/paperless-ngx).**

![Version](https://img.shields.io/badge/version-1.0.0-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Docker](https://img.shields.io/badge/docker-ready-blue?logo=docker)
![Python](https://img.shields.io/badge/python-3.12-blue?logo=python)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-ffdd00?style=flat&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/caps3n)

paperflow runs as a Docker container, periodically logs into your provider accounts, downloads invoices as PDFs, and uploads them to your Paperless-NGX instance — fully automatically. A SQLite database tracks which invoices have already been processed to avoid duplicates.

A built-in **web interface** (port `8085`) lets you configure everything, manage providers, view the invoice history, and watch live logs — no terminal needed.

---

## ✨ Features

- **Automatic invoice download** from Amazon.de / Amazon.com, IKEA, and Klarna
- **Paperless-NGX upload** via REST API — sets tags, correspondent, date, and title automatically
- **Product title extraction** — Paperless title shows the actual product name, not just the order number
- **Duplicate prevention** — SQLite database tracks every processed invoice
- **Year-skip optimization** — past years that were fully scanned are skipped on subsequent runs
- **Incremental scan mode** — optionally scan only the last 30 days for fast daily runs
- **Parallel uploads** — multiple PDFs uploaded simultaneously (configurable workers)
- **Correspondent dropdown** — select the correct Paperless-NGX correspondent from a live list
- **Year tags** — each invoice is automatically tagged with its year (e.g. `2024`)
- **Progress bar** — real-time upload progress shown in the web UI
- **Error categories** — history shows whether failure was `no PDF`, `Download ✗`, or `Upload ✗`
- **Plugin architecture** — add new providers by dropping a single `.py` file
- **CDP browser mode** — connects to a persistent Chrome instance via Remote Debugging (no repeated logins, supports 2FA)
- **Cookie import** — log in via Cookie Editor extension instead of VNC (useful for IKEA, Amazon)
- **Docker-first** — two containers: `paperflow` (FastAPI + Python) + `paperflow-chrome` (Chrome + noVNC)

---

## 🚀 Quick Start (Docker Compose)

### 1. Clone the repo

```bash
git clone https://github.com/Caps3n/paperflow.git
cd paperflow
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env`:

| Variable | Description | Default |
|---|---|---|
| `PAPERLESS_URL` | Your Paperless-NGX URL | `http://paperless:8000` |
| `PAPERLESS_TOKEN` | API token from Paperless-NGX admin | — |
| `AMAZON_EMAIL` | Amazon account email | — |
| `AMAZON_PASSWORD` | Amazon account password | — |
| `AMAZON_DOMAIN` | `amazon.de` or `amazon.com` | `amazon.de` |
| `AMAZON_MONTHS_BACK` | How many months back to scan | `12` |
| `IKEA_EMAIL` | IKEA account email | — |
| `IKEA_PASSWORD` | IKEA account password | — |
| `UPLOAD_WORKERS` | Parallel upload threads | `3` |
| `RUN_INTERVAL_HOURS` | How often to run (hours) | `24` |

### 3. Start

```bash
docker compose up -d
```

Open the web interface at **http://localhost:8085**

On first run, open the browser at **http://localhost:6080** (noVNC), log into Amazon or IKEA manually once — the session is then reused automatically.

---

## 🚀 Portainer Deployment

1. In Portainer → **Stacks** → **Add Stack** → **Repository**
2. Set:
   - **Repository URL:** `https://github.com/Caps3n/paperflow`
   - **Compose path:** `docker-compose.portainer.yml`
3. Add environment variables in the **Environment variables** tab (see table above)
4. Click **Deploy**

Portainer builds the `paperflow-chrome` browser container from source and pulls `paperflow` from `ghcr.io` automatically.

---

## 🖥️ Web Interface

| Page | Description |
|---|---|
| **Dashboard** | Stats, progress bar, last run status, manual trigger |
| **Settings** | Edit all credentials and intervals in-browser |
| **Providers** | Enable/disable providers, edit tags & correspondent, upload custom `.py` scripts |
| **History** | Invoice history with status, error category, and link to Paperless document |
| **Logs** | Live log output with auto-refresh |

---

## 🔒 Security

By default the web UI is accessible without authentication. To enable login protection:

```env
UI_USER=admin
UI_PASSWORD=yourpassword
```

Or set it in **Settings → Security** in the web UI.

> **Note:** paperflow runs HTTP only. For external access, place it behind a reverse proxy with TLS (e.g. [Caddy](https://caddyserver.com/) for automatic HTTPS).

---

## 🏗️ Architecture

```
┌─────────────────────────┐    CDP     ┌──────────────────────┐
│   paperflow             │ ─────────► │   paperflow-chrome   │
│   FastAPI + Python      │            │   Chrome + noVNC     │
│   port 8085 (Web UI)    │            │   port 6080 (VNC)    │
└──────────┬──────────────┘            └──────────────────────┘
           │ REST API
           ▼
┌─────────────────────────┐
│   Paperless-NGX         │
└─────────────────────────┘
```

paperflow connects to Chrome over CDP (Chrome DevTools Protocol), uses the live browser session to download invoice PDFs, then uploads them to Paperless-NGX via REST API.

---

## 🔐 Browser Login (Amazon, IKEA & Klarna)

paperflow uses a persistent Chrome browser (`paperflow-chrome`) so you only log in once:

1. Open **http://\<server\>:6080** in your browser (noVNC web UI)
2. Log into Amazon, IKEA, or Klarna — including any 2FA prompts
3. Start a scan from the web UI — your session is reused automatically

**Alternative — Cookie import (no VNC needed):**

1. Install the [Cookie Editor](https://cookie-editor.com/) browser extension
2. Log into the provider in your regular browser
3. Export cookies as JSON via Cookie Editor
4. Paste the JSON in **Settings → Amazon / IKEA → Import Cookies**

---

## 🔌 Adding Custom Providers

paperflow has a plugin system. To add a new provider:

1. Create a file `myprovider.py` following this template:

```python
from app.providers import BaseProvider, Invoice
from pathlib import Path

class MyproviderProvider(BaseProvider):
    provider_name = "myprovider"

    def fetch_invoices(self) -> list[Invoice]:
        # Your download logic here
        return [
            Invoice(
                invoice_id="2024-001",
                file_path=Path("/app/downloads/myprovider/invoice.pdf"),
                title="My Provider Invoice 2024-001",
                date="2024-01-15",
                extra_tags=["2024"],
            )
        ]
```

2. Upload via the **Providers** page in the web UI, or place the file in `providers_custom/`
3. Enable the provider in the web UI — done!

**Convention:** class name must be `<Providername>Provider` (capitalized), file name must be `<providername>.py` (lowercase).

---

## 🗂️ Project Structure

```
paperflow/
├── app/
│   ├── main.py              # Entry point — scheduler + parallel uploads
│   ├── web.py               # FastAPI web interface + API endpoints
│   ├── ui.html              # Single-page web UI
│   ├── database.py          # SQLite tracking (invoices + scanned years)
│   ├── paperless_client.py  # Paperless-NGX API client
│   ├── state.py             # Shared scan progress state
│   └── providers/
│       ├── __init__.py      # BaseProvider + Invoice dataclass
│       ├── amazon.py        # Amazon provider (CDP mode + fallback)
│       ├── ikea.py          # IKEA provider (CDP mode + cookie import)
│       └── klarna.py        # Klarna provider (CDP mode, Kaufbelege)
├── chrome-desktop/          # Chrome + noVNC Docker image (paperflow-chrome)
│   ├── Dockerfile
│   └── start.sh
├── providers_custom/        # Drop custom provider .py files here
├── data/                    # SQLite DB + logs + settings (persisted volume)
├── downloads/               # Temporary PDF storage
├── Dockerfile
├── docker-compose.yml       # Local development
├── docker-compose.portainer.yml  # Portainer / production deployment
└── .env.example
```

---

## 🛣️ Roadmap

- [ ] eBay provider
- [ ] Email/IMAP provider (catch invoices sent by email)
- [ ] Notification on completion (Telegram / ntfy)
- [ ] Dark/light mode toggle in web UI

---

## 🤝 Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) first.

The easiest way to contribute is to **write a provider** for a service you use and open a pull request.

---

## 📜 License

MIT — see [LICENSE](LICENSE)
