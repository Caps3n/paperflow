"""
Invoice Fetcher – Hauptprogramm.

Läuft als Docker-Container, führt alle aktiven Provider aus
und lädt neue Rechnungen zu Paperless-NGX hoch.
Startet außerdem ein Web-Interface auf Port 8080.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys
import threading
import time
from pathlib import Path

import schedule
import uvicorn
import yaml
from dotenv import load_dotenv

from app import database
from app.paperless_client import PaperlessClient
from app.providers import BaseProvider, Invoice
from app.version import __version__

# .env laden – zuerst aus /app/config/.env (via Web-UI gespeichert), dann Fallback
load_dotenv(Path("/app/config/.env"))
load_dotenv()  # lokale .env für Entwicklung

# ── Logging – in Datei UND stdout ─────────────────────────────────────────────
LOG_PATH = Path("/app/data/fetcher.log")
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

_fmt = logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)
_file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
_file_handler.setFormatter(_fmt)
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(_fmt)

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stream_handler])
logger = logging.getLogger("main")

CONFIG_PATH = Path("/app/config/providers.yml")
CUSTOM_PROVIDERS_DIR = Path("/app/providers_custom")


# ── Provider-Loader ────────────────────────────────────────────────────────────


def load_providers(config: dict) -> list[BaseProvider]:
    """Lädt alle in providers.yml aktivierten Provider dynamisch.
    Sucht zuerst in app/providers/, dann in providers_custom/."""
    # Custom-Provider-Verzeichnis zum Python-Pfad hinzufügen
    custom_dir = str(CUSTOM_PROVIDERS_DIR)
    if custom_dir not in sys.path:
        sys.path.insert(0, custom_dir)

    providers: list[BaseProvider] = []
    for name, cfg in config.get("providers", {}).items():
        if not cfg.get("enabled", False):
            logger.info("Provider '%s' ist deaktiviert – überspringe", name)
            continue
        try:
            # Zuerst built-in, dann custom
            try:
                module = importlib.import_module(f"app.providers.{name}")
            except ModuleNotFoundError:
                # Custom Provider: direkt als Modul laden
                spec = importlib.util.spec_from_file_location(
                    name, CUSTOM_PROVIDERS_DIR / f"{name}.py"
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

            class_name = name.capitalize() + "Provider"
            cls = getattr(module, class_name)
            providers.append(cls(cfg))
            logger.info("Provider geladen: %s", name)
        except (ModuleNotFoundError, AttributeError, FileNotFoundError) as e:
            logger.error("Provider '%s' konnte nicht geladen werden: %s", name, e)
    return providers


# ── Haupt-Workflow ─────────────────────────────────────────────────────────────


def run_once() -> None:
    """Ein kompletter Durchlauf: alle Provider → Download → Upload."""
    logger.info("=" * 60)
    logger.info("Starte Rechnungs-Fetch...")
    logger.info("=" * 60)

    # Config laden
    if not CONFIG_PATH.exists():
        logger.error("Keine Konfiguration gefunden: %s", CONFIG_PATH)
        return

    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)

    # Paperless-NGX Verbindung prüfen
    paperless = PaperlessClient()
    if not paperless.test_connection():
        logger.error("Paperless-NGX nicht erreichbar – Abbruch")
        return

    # Provider ausführen
    providers = load_providers(config)
    if not providers:
        logger.warning("Keine aktiven Provider konfiguriert!")
        return

    total_new = 0

    for provider in providers:
        logger.info(
            "── Provider: %s ──────────────────────────", provider.provider_name
        )
        try:
            invoices: list[Invoice] = provider.fetch_invoices()
        except Exception as e:
            logger.exception("Provider '%s' abgestürzt: %s", provider.provider_name, e)
            continue

        for invoice in invoices:
            # Schon verarbeitet?
            if database.is_processed(provider.provider_name, invoice.invoice_id):
                logger.debug("Bereits hochgeladen: %s", invoice.invoice_id)
                continue

            database.mark_pending(
                provider.provider_name,
                invoice.invoice_id,
                invoice.file_path.name,
            )

            # Upload zu Paperless
            logger.info("Uploade: %s → %s", invoice.invoice_id, invoice.title)
            all_tags = provider.tags + invoice.extra_tags
            result = paperless.upload_document(
                file_path=invoice.file_path,
                title=invoice.title,
                tags=all_tags,
                correspondent=provider.correspondent,
                created_date=invoice.date,
            )

            if result is not None:
                database.mark_uploaded(
                    provider.provider_name, invoice.invoice_id, result
                )
                total_new += 1
                logger.info("✓ Hochgeladen: %s (Task: %s)", invoice.title, result)
            else:
                database.mark_failed(
                    provider.provider_name, invoice.invoice_id, "Upload fehlgeschlagen"
                )
                logger.error("✗ Upload fehlgeschlagen: %s", invoice.invoice_id)

    # Statistik
    logger.info("=" * 60)
    logger.info("Fertig! %d neue Rechnungen hochgeladen.", total_new)
    stats = database.get_stats()
    for prov, s in stats.items():
        logger.info("  %s: %s", prov, s)
    logger.info("=" * 60)


# ── Scheduler ─────────────────────────────────────────────────────────────────


def _scheduler_loop(interval_hours: int) -> None:
    """Läuft in einem eigenen Thread – prüft jede Minute ob ein Job fällig ist."""
    schedule.every(interval_hours).hours.do(run_once)
    logger.info("Scheduler aktiv – nächster Lauf in %dh", interval_hours)
    while True:
        schedule.run_pending()
        time.sleep(60)


def main() -> None:
    database.init_db()

    interval_hours = int(os.environ.get("RUN_INTERVAL_HOURS", "24"))
    run_on_startup = os.environ.get("RUN_ON_STARTUP", "true").lower() == "true"

    logger.info("=" * 60)
    logger.info("paperflow v%s", __version__)
    logger.info("Web-Interface: http://localhost:8080")
    logger.info("Intervall: alle %d Stunden", interval_hours)
    logger.info("=" * 60)

    # Scheduler in Hintergrund-Thread
    t = threading.Thread(target=_scheduler_loop, args=(interval_hours,), daemon=True)
    t.start()

    if run_on_startup:
        threading.Thread(target=run_once, daemon=True).start()

    # Web-Server (blockiert Hauptthread)
    from app.web import app as web_app

    uvicorn.run(web_app, host="0.0.0.0", port=8080, log_level="warning")


if __name__ == "__main__":
    main()
