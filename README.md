# 📄 paperflow

**Automatically fetch invoices from online providers and import them into [Paperless-NGX](https://github.com/paperless-ngx/paperless-ngx).**

![Version](https://img.shields.io/badge/version-0.0.1-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Docker](https://img.shields.io/badge/docker-ready-blue?logo=docker)
![Python](https://img.shields.io/badge/python-3.12-blue?logo=python)

paperflow runs as a Docker container, periodically logs into your provider accounts (e.g. Amazon), downloads invoices as PDFs, and uploads them to your Paperless-NGX instance — fully automatically. A SQLite database tracks which invoices have already been processed to avoid duplicates.

A built-in **web interface** (port `8080`) lets you configure everything, manage providers, upload custom provider scripts, and watch live logs — no terminal needed.

---

## ✨ Features

- **Automatic invoice download** from Amazon (more providers coming)
- **Paperless-NGX upload** via REST API — sets tags, correspondent, and date automatically
- **Duplicate prevention** — SQLite database tracks every processed invoice
- **Plugin architecture** — add new providers by dropping a single `.py` file
- **Web UI** — configure credentials, toggle providers, upload custom scripts, view logs
- **Docker-first** — runs headless, no display required

---

## 🚀 Quick Start

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/paperflow.git
cd paperflow
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env`:

| Variable | Description |
|---|---|
| `PAPERLESS_URL` | Your Paperless-NGX URL, e.g. `http://localhost:8000` |
| `PAPERLESS_TOKEN` | API token from Paperless-NGX admin |
| `AMAZON_EMAIL` | Amazon account email |
| `AMAZON_PASSWORD` | Amazon account password |
| `AMAZON_DOMAIN` | `amazon.de` or `amazon.com` |
| `AMAZON_MONTHS_BACK` | How many months back to scan (default: `12`) |
| `RUN_INTERVAL_HOURS` | How often to run (default: `24`) |

### 3. Enable providers

Edit `config/providers.yml`:

```yaml
providers:
  amazon:
    enabled: true
    tags: ["Amazon", "Invoice"]
    correspondent: "Amazon"
```

### 4. Start

```bash
docker compose up -d
```

Open the web interface at **http://localhost:8080**

---

## 🖥️ Web Interface

| Page | Description |
|---|---|
| **Dashboard** | Stats, last run status, manual trigger button |
| **Settings** | Edit all credentials and intervals in-browser |
| **Providers** | Enable/disable providers, edit tags, upload custom `.py` scripts |
| **Logs** | Live log output with auto-refresh |

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
            )
        ]
```

2. Upload via the **Providers** page in the web UI (drag & drop), or place the file in `providers_custom/`

3. Enable the provider in the web UI — done!

**Convention:** class name must be `<Providername>Provider` (capitalized), file name must be `<providername>.py` (lowercase).

---

## 🔐 Amazon 2FA

If Amazon requires an OTP on first login:

1. Set `AMAZON_OTP_CODE=123456` in Settings (or `.env`)
2. Restart the container — cookies are saved after successful login
3. Remove `AMAZON_OTP_CODE` afterwards

---

## 🗂️ Project Structure

```
paperflow/
├── app/
│   ├── main.py              # Entry point — scheduler + web server
│   ├── web.py               # FastAPI web interface
│   ├── ui.html              # Single-page web UI
│   ├── database.py          # SQLite tracking
│   ├── paperless_client.py  # Paperless-NGX API client
│   └── providers/
│       ├── __init__.py      # BaseProvider base class
│       └── amazon.py        # Amazon provider
├── providers_custom/        # Drop custom provider .py files here
├── config/
│   └── providers.yml        # Provider configuration
├── data/                    # SQLite DB + logs (persisted via volume)
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
- [ ] Web UI: invoice history table
- [ ] Web UI: dark/light mode toggle

---

## 🤝 Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) first.

The easiest way to contribute is to **write a provider** for a service you use and open a pull request.

---

## 📜 License

MIT — see [LICENSE](LICENSE)
