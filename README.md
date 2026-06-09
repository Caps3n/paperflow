# 📄 paperflow

**Automatically fetch invoices from online providers and import them into [Paperless-NGX](https://github.com/paperless-ngx/paperless-ngx).**

![Version](https://img.shields.io/badge/version-1.0.0-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Docker](https://img.shields.io/badge/docker-ready-blue?logo=docker)
![Python](https://img.shields.io/badge/python-3.12-blue?logo=python)

paperflow runs as a Docker container, periodically logs into your provider accounts (e.g. Amazon), downloads invoices as PDFs, and uploads them to your Paperless-NGX instance — fully automatically. A SQLite database tracks which invoices have already been processed to avoid duplicates.

A built-in **web interface** (port `8085`) lets you configure everything, manage providers, view the invoice history, and watch live logs — no terminal needed.

---

## ✨ Features

- **Automatic invoice download** from Amazon.de / Amazon.com (back to any start year)
- **Paperless-NGX upload** via REST API — sets tags, correspondent, date, and title automatically
- **Product title extraction** — Paperless title shows the actual product name, not just the order number
- **Duplicate prevention** — SQLite database tracks every processed invoice
- **Year-skip optimization** — past years that were fully scanned are skipped on subsequent runs
- **Incremental scan mode** — optionally scan only the last 30 days for fast daily runs
- **Parallel uploads** — multiple PDFs uploaded simultaneously (configurable workers)
- **Correspondent dropdown** — select the correct Paperless-NGX correspondent from a live list
- **Year tags** — each invoice is automatically tagged with its year (e.g. `2024`)
- **Progress bar** — real-time upload progress shown in the web UI
- **Error categories** — Verlauf shows whether failure was `no PDF`, `Download ✗`, or `Upload ✗`
- **Plugin architecture** — add new providers by dropping a single `.py` file
- **Web UI** — configure credentials, toggle providers, upload custom scripts, view logs
- **CDP browser mode** — connects to a persistent Chrome instance via Remote Debugging (no repeated logins)
- **Docker-first** — two containers: `invoice-fetcher` (FastAPI + Python) + `chrome-desktop` (Chrome + noVNC)

---

## 🚀 Quick Start

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
| `AMAZON_START_YEAR` | Earliest year to scan | `2009` |
| `AMAZON_INCREMENTAL` | `true` = only scan last 30 days | `false` |
| `UPLOAD_WORKERS` | Parallel upload threads | `3` |
| `RUN_INTERVAL_HOURS` | How often to run | `24` |
| `CHROME_CDP_URL` | Chrome DevTools Protocol URL | `http://chrome-desktop:9222` |

### 3. Enable providers

Open the web UI at **http://localhost:8085** → **Providers** → enable Amazon and configure:

- **Tags** — comma-separated tags to add in Paperless (e.g. `Amazon, Rechnung`)
- **Correspondent** — select from your Paperless-NGX correspondent list via dropdown
- **Start year** — how far back to scan

Or edit `data/providers.yml` directly:

```yaml
providers:
  amazon:
    enabled: true
    tags: ["Amazon", "Rechnung"]
    correspondent: "Amazon DE"
```

### 4. Start

```bash
docker compose up -d
```

Open the web interface at **http://localhost:8085**

On first run, open the Chrome browser at **http://localhost:6080** (noVNC), log into Amazon manually once — the session is then reused automatically.

---

## 🖥️ Web Interface

| Page | Description |
|---|---|
| **Dashboard** | Stats, progress bar, last run status, manual trigger |
| **Settings** | Edit all credentials and intervals in-browser |
| **Providers** | Enable/disable providers, edit tags & correspondent, upload custom `.py` scripts |
| **Verlauf** | Invoice history with status, error category, and link to Paperless document |
| **Logs** | Live log output with auto-refresh |

---

## 🔒 Security

By default, the web UI is accessible without authentication. To enable login protection:

```env
UI_USER=admin          # optional, defaults to "admin"
UI_PASSWORD=yourpassword
```

Or set it directly in the **Settings → Sicherheit** section of the web UI.

> **Note:** paperflow runs HTTP only. For external access, place it behind a reverse proxy with TLS:
>
> ```nginx
> # nginx example
> location / {
>     proxy_pass http://localhost:8085;
> }
> ```
>
> [Caddy](https://caddyserver.com/) is the easiest option — it handles HTTPS automatically.

---

## 🏗️ Architecture

```
┌─────────────────────────┐    CDP     ┌──────────────────────┐
│   invoice-fetcher       │ ─────────► │   chrome-desktop     │
│   FastAPI + Python      │            │   Chrome + noVNC     │
│   port 8085 (Web UI)    │            │   port 6080 (VNC)    │
└──────────┬──────────────┘            └──────────────────────┘
           │ REST API
           ▼
┌─────────────────────────┐
│   Paperless-NGX         │
│   port 8777             │
└─────────────────────────┘
```

paperflow connects to Chrome over CDP (Chrome DevTools Protocol), uses the live browser session to download invoice PDFs via `fetch()` with full cookie access, then uploads to Paperless-NGX via REST API.

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
                date="2024-01-15",          # ISO format, passed to Paperless
                extra_tags=["2024"],        # Additional tags beyond provider config
            )
        ]
```

2. Upload via the **Providers** page in the web UI, or place the file in `providers_custom/`

3. Enable the provider in the web UI — done!

**Convention:** class name must be `<Providername>Provider` (capitalized), file name must be `<providername>.py` (lowercase).

---

## 🔐 Amazon Login

paperflow uses a persistent Chrome browser (the `chrome-desktop` container) so you only log in once:

1. Open **http://localhost:6080** in your browser (noVNC)
2. Log into Amazon manually
3. Start a scan from the web UI — your session is reused automatically

If Amazon requires a 2FA OTP:
- Enter the code via the web UI when prompted (paperflow waits up to 5 minutes)
- Or set `AMAZON_OTP_CODE=123456` in Settings before starting

---

## ⚡ Incremental Scan

For daily scheduled runs, set `AMAZON_INCREMENTAL=true` in Settings. paperflow will then only scan Amazon's "last 30 days" filter instead of all years — much faster.

For the first full historical import, run once without incremental mode to scan back to `AMAZON_START_YEAR`.

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
│       └── amazon.py        # Amazon provider (CDP mode + fallback)
├── chrome-desktop/          # Chrome + noVNC Docker image
│   ├── Dockerfile
│   └── start.sh
├── providers_custom/        # Drop custom provider .py files here
├── data/                    # SQLite DB + logs + settings (persisted volume)
├── downloads/               # Temporary PDF storage
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

---

## 🛣️ Roadmap

- [ ] eBay provider
- [ ] Email/IMAP provider (catch invoices sent by email)
- [ ] Notification on completion (Telegram / ntfy)
- [ ] Dark/light mode toggle in web UI
- [ ] Titel-Verbesserung: Bestellnummer aus Paperless-Volltext-Suche de-duplizieren

---

## 🤝 Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) first.

The easiest way to contribute is to **write a provider** for a service you use and open a pull request.

---

## 📜 License

MIT — see [LICENSE](LICENSE)
