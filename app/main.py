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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import schedule
import uvicorn
import yaml
from dotenv import load_dotenv

from app import database, state
from app.paperless_client import PaperlessClient
from app.providers import BaseProvider, Invoice
from app.version import __version__

# .env laden – zuerst aus /app/data/settings.env (override=True damit docker-compose-Leerwerte überschrieben werden)
load_dotenv(Path("/app/data/settings.env"), override=True)
load_dotenv(Path("/app/config/.env"), override=False)
load_dotenv(override=False)  # lokale .env für Entwicklung

# Anzahl paralleler Upload-Threads (Standard: 3, nach load_dotenv lesen)
UPLOAD_WORKERS = int(os.environ.get("UPLOAD_WORKERS") or "3")

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

CONFIG_PATH = Path("/app/data/providers.yml")
_CONFIG_LEGACY = Path("/app/config/providers.yml")
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


def _upload_worker(
    provider_name: str,
    invoice: Invoice,
    provider_tags: list[str],
    correspondent: str | None,
) -> tuple[str, object, str | None]:
    """Upload einer Rechnung in einem eigenen Thread.
    Gibt (invoice_id, paperless_task_id_or_None, error_message_or_None) zurück."""
    from app.paperless_client import PaperlessClient  # je Thread eigene Session

    paperless = PaperlessClient()
    all_tags = provider_tags + invoice.extra_tags
    try:
        # Duplikatsprüfung: Dateiname bereits in Paperless?
        if paperless.document_exists(invoice.file_path.name):
            logger.info("⏭ Duplikat übersprungen: %s", invoice.file_path.name)
            return (invoice.invoice_id, "duplicate", None)
        result = paperless.upload_document(
            file_path=invoice.file_path,
            title=invoice.title,
            tags=all_tags,
            correspondent=correspondent,
            created_date=invoice.date,
        )
        return (invoice.invoice_id, result, None)
    except Exception as e:
        return (invoice.invoice_id, None, str(e))


def run_once() -> None:
    """Ein kompletter Durchlauf: alle Provider → Download → Upload."""
    state.reset_progress()
    logger.info("=" * 60)
    logger.info("Starte Rechnungs-Fetch...")
    logger.info("=" * 60)

    # Config laden – neue Stelle bevorzugt, Fallback auf alte
    cfg_path = CONFIG_PATH if CONFIG_PATH.exists() else _CONFIG_LEGACY
    if not cfg_path.exists():
        logger.error("Keine Konfiguration gefunden: %s", CONFIG_PATH)
        return

    with open(cfg_path) as f:
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
        pname = provider.provider_name
        logger.info("── Provider: %s ──────────────────────────", pname)

        # ── Phase 1: Discovery + Download ─────────────────────────────────────
        state.set_phase("discover", pname)
        try:
            invoices: list[Invoice] = provider.fetch_invoices()
        except Exception as e:
            logger.exception("Provider '%s' abgestürzt: %s", pname, e)
            continue

        # ── Phase 2: Filtern + DB-Vorbereitung ────────────────────────────────
        to_upload: list[Invoice] = []
        for invoice in invoices:
            if database.is_processed(pname, invoice.invoice_id):
                logger.debug("Bereits hochgeladen: %s", invoice.invoice_id)
                continue
            database.mark_pending(pname, invoice.invoice_id, invoice.file_path.name)
            to_upload.append(invoice)

        if not to_upload:
            logger.info("Keine neuen Rechnungen für %s", pname)
            continue

        # ── Phase 3: Parallel-Upload ───────────────────────────────────────────
        state.set_phase("upload", pname, total=len(to_upload))
        logger.info(
            "Uploade %d Rechnungen (max. %d parallel)…", len(to_upload), UPLOAD_WORKERS
        )

        with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as executor:
            future_map = {
                executor.submit(
                    _upload_worker, pname, inv, provider.tags, provider.correspondent
                ): inv
                for inv in to_upload
            }
            for future in as_completed(future_map):
                inv = future_map[future]
                state.tick(inv.invoice_id)
                try:
                    inv_id, task_id, error = future.result()
                    if task_id == "duplicate":
                        database.mark_uploaded(pname, inv_id, "duplicate")
                        logger.info("⏭ Duplikat: %s", inv.title[:60])
                    elif task_id is not None:
                        database.mark_uploaded(pname, inv_id, task_id)
                        total_new += 1
                        logger.info("✓ %s (Task: %s)", inv.title[:60], task_id)
                    else:
                        database.mark_failed(
                            pname,
                            inv_id,
                            error or "Upload fehlgeschlagen",
                            error_type="upload_failed",
                        )
                        logger.error("✗ Upload fehlgeschlagen: %s – %s", inv_id, error)
                except Exception as exc:
                    database.mark_failed(
                        pname, inv.invoice_id, str(exc), error_type="upload_failed"
                    )
                    logger.error("✗ Fehler bei %s: %s", inv.invoice_id, exc)

    # ── Fertig ────────────────────────────────────────────────────────────────
    state.reset_progress()
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
