"""
Web-Interface für Invoice Fetcher.
Erreichbar unter http://localhost:8080

Features:
  - Dashboard mit Statistiken und manuellem Start
  - Einstellungen (.env) im Browser bearbeiten
  - Provider aktivieren/deaktivieren
  - Eigene Provider als .py hochladen
  - Live-Logs
"""

from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path

import yaml
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app import database
from app.version import __version__

logger = logging.getLogger("web")

ENV_PATH = Path("/app/config/.env")
CONFIG_PATH = Path("/app/config/providers.yml")
PROVIDERS_DIR = Path("/app/providers_custom")  # Nutzer-Provider
LOG_PATH = Path("/app/data/fetcher.log")

PROVIDERS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Invoice Fetcher", docs_url=None, redoc_url=None)

# ── Hintergrund-Job-State ──────────────────────────────────────────────────────
_run_lock = threading.Lock()
_last_run = {"time": None, "status": None}


# ══════════════════════════════════════════════════════════════════════════════
#  UI
# ══════════════════════════════════════════════════════════════════════════════


@app.get("/", response_class=HTMLResponse)
async def ui():
    html_path = Path(__file__).parent / "ui.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


# ══════════════════════════════════════════════════════════════════════════════
#  API – Dashboard / Stats
# ══════════════════════════════════════════════════════════════════════════════


@app.get("/api/stats")
async def get_stats():
    stats = database.get_stats()
    return {
        "stats": stats,
        "last_run": _last_run,
        "running": _run_lock.locked(),
        "version": __version__,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  API – Manueller Start
# ══════════════════════════════════════════════════════════════════════════════


@app.post("/api/run")
async def trigger_run():
    if _run_lock.locked():
        raise HTTPException(status_code=409, detail="Läuft bereits")

    def _do_run():
        with _run_lock:
            _last_run["time"] = datetime.utcnow().isoformat()
            _last_run["status"] = "running"
            try:
                # Import hier um zirkuläre Imports zu vermeiden
                from app.main import run_once

                run_once()
                _last_run["status"] = "ok"
            except Exception as e:
                _last_run["status"] = f"Fehler: {e}"
                logger.exception("run_once fehlgeschlagen")

    threading.Thread(target=_do_run, daemon=True).start()
    return {"started": True}


# ══════════════════════════════════════════════════════════════════════════════
#  API – Einstellungen (.env)
# ══════════════════════════════════════════════════════════════════════════════

# Felder die im UI angezeigt werden (Reihenfolge + Metadaten)
ENV_FIELDS = [
    {
        "key": "PAPERLESS_URL",
        "label": "Paperless URL",
        "type": "text",
        "group": "Paperless-NGX",
    },
    {
        "key": "PAPERLESS_TOKEN",
        "label": "API Token",
        "type": "password",
        "group": "Paperless-NGX",
    },
    {"key": "AMAZON_EMAIL", "label": "E-Mail", "type": "email", "group": "Amazon"},
    {
        "key": "AMAZON_PASSWORD",
        "label": "Passwort",
        "type": "password",
        "group": "Amazon",
    },
    {
        "key": "AMAZON_DOMAIN",
        "label": "Domain",
        "type": "select",
        "group": "Amazon",
        "options": ["amazon.de", "amazon.com"],
    },
    {
        "key": "AMAZON_MONTHS_BACK",
        "label": "Monate zurück",
        "type": "number",
        "group": "Amazon",
    },
    {
        "key": "AMAZON_OTP_CODE",
        "label": "2FA OTP (einmalig)",
        "type": "text",
        "group": "Amazon",
    },
    {
        "key": "RUN_INTERVAL_HOURS",
        "label": "Intervall (Stunden)",
        "type": "number",
        "group": "Allgemein",
    },
    {
        "key": "RUN_ON_STARTUP",
        "label": "Beim Start ausführen",
        "type": "select",
        "group": "Allgemein",
        "options": ["true", "false"],
    },
]


def _read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                values[k.strip()] = v.strip()
    # Fallback: aktuelle Umgebungsvariablen
    for f in ENV_FIELDS:
        if f["key"] not in values and f["key"] in os.environ:
            values[f["key"]] = os.environ[f["key"]]
    return values


def _write_env(values: dict[str, str]) -> None:
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    # Bekannte Felder in definierter Reihenfolge
    written = set()
    current_group = None
    for f in ENV_FIELDS:
        k = f["key"]
        if f["group"] != current_group:
            current_group = f["group"]
            lines.append(f"\n# ── {current_group} {'─' * (40 - len(current_group))}")
        val = values.get(k, "")
        lines.append(f"{k}={val}")
        written.add(k)
    # Unbekannte Felder anhängen
    for k, v in values.items():
        if k not in written:
            lines.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(lines) + "\n")
    # Auch in aktuellen Prozess laden
    for k, v in values.items():
        os.environ[k] = v


@app.get("/api/settings")
async def get_settings():
    return {"fields": ENV_FIELDS, "values": _read_env()}


class SettingsSave(BaseModel):
    values: dict[str, str]


@app.post("/api/settings")
async def save_settings(body: SettingsSave):
    _write_env(body.values)
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
#  API – Provider
# ══════════════════════════════════════════════════════════════════════════════


def _read_config() -> dict:
    if CONFIG_PATH.exists():
        return yaml.safe_load(CONFIG_PATH.read_text()) or {}
    return {"providers": {}}


def _write_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(yaml.dump(cfg, allow_unicode=True, default_flow_style=False))


def _list_provider_files() -> list[str]:
    """Alle .py Dateien in app/providers/ und providers_custom/"""
    built_in = [
        p.stem
        for p in (Path(__file__).parent / "providers").glob("*.py")
        if p.stem not in ("__init__",)
    ]
    custom = [p.stem for p in PROVIDERS_DIR.glob("*.py")]
    return built_in + custom


@app.get("/api/providers")
async def list_providers():
    cfg = _read_config()
    provider_cfgs = cfg.get("providers", {})
    available = _list_provider_files()
    result = []
    for name in available:
        pc = provider_cfgs.get(name, {})
        result.append(
            {
                "name": name,
                "enabled": pc.get("enabled", False),
                "tags": pc.get("tags", []),
                "correspondent": pc.get("correspondent", ""),
                "custom": name in [p.stem for p in PROVIDERS_DIR.glob("*.py")],
            }
        )
    return {"providers": result}


class ProviderUpdate(BaseModel):
    enabled: bool
    tags: list[str] = []
    correspondent: str = ""


@app.put("/api/providers/{name}")
async def update_provider(name: str, body: ProviderUpdate):
    if re.search(r"[^a-z0-9_]", name):
        raise HTTPException(400, "Ungültiger Provider-Name")
    cfg = _read_config()
    cfg.setdefault("providers", {})[name] = {
        "enabled": body.enabled,
        "tags": body.tags,
        "correspondent": body.correspondent,
    }
    _write_config(cfg)
    return {"ok": True}


@app.delete("/api/providers/{name}")
async def delete_provider(name: str):
    """Löscht einen custom Provider."""
    target = PROVIDERS_DIR / f"{name}.py"
    if not target.exists():
        raise HTTPException(404, "Nur custom Provider können gelöscht werden")
    target.unlink()
    cfg = _read_config()
    cfg.get("providers", {}).pop(name, None)
    _write_config(cfg)
    return {"ok": True}


@app.post("/api/providers/upload")
async def upload_provider(file: UploadFile = File(...)):
    """Lädt eine custom Provider .py hoch und validiert sie grob."""
    if not file.filename.endswith(".py"):
        raise HTTPException(400, "Nur .py Dateien erlaubt")

    name = Path(file.filename).stem
    if re.search(r"[^a-z0-9_]", name):
        raise HTTPException(400, "Dateiname darf nur a-z, 0-9, _ enthalten")

    content = await file.read()
    text = content.decode("utf-8", errors="replace")

    # Basis-Validierung: Muss BaseProvider importieren und fetch_invoices haben
    if "BaseProvider" not in text:
        raise HTTPException(
            400, "Provider muss 'from app.providers import BaseProvider' importieren"
        )
    if "fetch_invoices" not in text:
        raise HTTPException(400, "Provider muss 'fetch_invoices()' implementieren")

    target = PROVIDERS_DIR / file.filename
    target.write_bytes(content)

    # Automatisch in providers.yml eintragen (deaktiviert)
    cfg = _read_config()
    if name not in cfg.get("providers", {}):
        cfg.setdefault("providers", {})[name] = {
            "enabled": False,
            "tags": [name.capitalize()],
            "correspondent": name.capitalize(),
        }
        _write_config(cfg)

    logger.info("Custom Provider hochgeladen: %s", name)
    return {"ok": True, "name": name}


# ══════════════════════════════════════════════════════════════════════════════
#  API – Logs
# ══════════════════════════════════════════════════════════════════════════════


@app.get("/api/logs")
async def get_logs(lines: int = 200):
    if not LOG_PATH.exists():
        return {"lines": []}
    all_lines = LOG_PATH.read_text(errors="replace").splitlines()
    return {"lines": all_lines[-lines:]}
