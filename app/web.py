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

import ast
import base64
import logging
import os
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path

import yaml
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from app import database, otp_state, state
from app.version import __version__

logger = logging.getLogger("web")

ENV_PATH = Path("/app/data/settings.env")  # In data/ – immer persistent
CONFIG_PATH = Path("/app/data/providers.yml")  # Ebenfalls in data/
_ENV_LEGACY = Path("/app/config/.env")  # Alter Pfad – Migration
_CONFIG_LEGACY = Path("/app/config/providers.yml")
PROVIDERS_DIR = Path("/app/providers_custom")  # Nutzer-Provider
LOG_PATH = Path("/app/data/fetcher.log")

PROVIDERS_DIR.mkdir(parents=True, exist_ok=True)
Path("/app/data").mkdir(parents=True, exist_ok=True)

# Migration: alte Dateien aus /app/config/ nach /app/data/ verschieben
for _src, _dst in [(_ENV_LEGACY, ENV_PATH), (_CONFIG_LEGACY, CONFIG_PATH)]:
    if _src.exists() and not _dst.exists():
        import shutil

        shutil.copy2(_src, _dst)
        logger.info("Migriert: %s → %s", _src, _dst)

app = FastAPI(title="Invoice Fetcher", docs_url=None, redoc_url=None)


# ── CDP URL Helper ────────────────────────────────────────────────────────────

def _get_cdp_url() -> str:
    """Always reads CHROME_CDP_URL fresh (user may update it via web UI)."""
    return os.environ.get("CHROME_CDP_URL", "").strip()


def _resolve_cdp_url(cdp_url: str) -> str:
    """Resolve hostname → IP so Chrome's Host-header DNS-rebinding check passes."""
    import socket
    import urllib.parse
    try:
        parsed = urllib.parse.urlparse(cdp_url)
        hostname = parsed.hostname or ""
        if hostname and not hostname.replace(".", "").isdigit():
            ip = socket.gethostbyname(hostname)
            port = parsed.port
            new_netloc = f"{ip}:{port}" if port else ip
            return urllib.parse.urlunparse(parsed._replace(netloc=new_netloc))
    except Exception:
        pass
    return cdp_url


# ── Basic Auth Middleware ──────────────────────────────────────────────────────


class _BasicAuthMiddleware(BaseHTTPMiddleware):
    """Schützt alle Routen mit HTTP Basic Auth.
    Nur aktiv wenn UI_PASSWORD gesetzt ist.
    Der /api/amazon/cookies-raw Endpunkt ist ausgenommen (wird vom Browser injiziert).
    """

    _EXCLUDED = {"/api/amazon/cookies-raw"}

    async def dispatch(self, request: Request, call_next):
        ui_pass = os.environ.get("UI_PASSWORD", "").strip()
        if not ui_pass:
            # Kein Passwort gesetzt → Auth deaktiviert (Abwärtskompatibilität)
            return await call_next(request)

        if request.url.path in self._EXCLUDED:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Basic "):
            return Response(
                "Authentifizierung erforderlich",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Invoice Fetcher"'},
            )

        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            username, _, password = decoded.partition(":")
        except Exception:
            return Response(
                "Ungültige Authentifizierung",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Invoice Fetcher"'},
            )

        ui_user = os.environ.get("UI_USER", "admin").strip()
        user_ok = secrets.compare_digest(username.encode(), ui_user.encode())
        pass_ok = secrets.compare_digest(password.encode(), ui_pass.encode())
        if not user_ok or not pass_ok:
            return Response(
                "Falsche Zugangsdaten",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Invoice Fetcher"'},
            )

        return await call_next(request)


app.add_middleware(_BasicAuthMiddleware)


# ── Hintergrund-Job-State ──────────────────────────────────────────────────────
_run_lock = threading.Lock()
_last_run = {"time": None, "status": None}


def _do_run_locked(selected_years: list[int] | None = None) -> None:
    """Führt run_once() aus – hält dabei immer den _run_lock."""
    with _run_lock:
        _last_run["time"] = datetime.utcnow().isoformat()
        _last_run["status"] = "running"
        try:
            from app.main import run_once

            if selected_years is not None:
                os.environ["PAPERFLOW_YEARS_FILTER"] = ",".join(
                    str(y) for y in selected_years
                )
            else:
                os.environ.pop("PAPERFLOW_YEARS_FILTER", None)

            run_once()
            _last_run["status"] = "ok"
        except Exception as e:
            _last_run["status"] = f"Fehler: {e}"
            logger.exception("run_once fehlgeschlagen")
        finally:
            os.environ.pop("PAPERFLOW_YEARS_FILTER", None)


def trigger_startup_run() -> None:
    """Startet den Startup-Run im Hintergrund – verwendet denselben Lock wie der Web-UI-Trigger."""
    threading.Thread(target=_do_run_locked, daemon=True).start()


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


@app.get("/api/progress")
async def get_progress():
    """Fortschritt des aktuellen Scan-Laufs."""
    return {
        "running": _run_lock.locked(),
        **state.scan_progress,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  API – Manueller Start
# ══════════════════════════════════════════════════════════════════════════════


class RunRequest(BaseModel):
    years: list[int] | None = None  # None = alle Jahre, [] = nur aktuelles Jahr


@app.post("/api/run")
async def trigger_run(body: RunRequest = RunRequest()):
    if _run_lock.locked():
        raise HTTPException(status_code=409, detail="Läuft bereits")

    selected_years = body.years  # z.B. [2024, 2023] oder None
    threading.Thread(target=_do_run_locked, args=(selected_years,), daemon=True).start()
    return {"started": True}


# ══════════════════════════════════════════════════════════════════════════════
#  API – Einstellungen (.env)
# ══════════════════════════════════════════════════════════════════════════════

# Globale Einstellungen (nur Paperless + Scheduler)
ENV_FIELDS = [
    {
        "key": "UI_USER",
        "label": "Username",
        "type": "text",
        "group": "Security",
        "placeholder": "admin",
    },
    {
        "key": "UI_PASSWORD",
        "label": "Password (empty = no auth required)",
        "type": "password",
        "group": "Security",
    },
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
    {
        "key": "RUN_INTERVAL_HOURS",
        "label": "Interval (hours)",
        "type": "number",
        "group": "General",
    },
    {
        "key": "RUN_ON_STARTUP",
        "label": "Run on startup",
        "type": "select",
        "group": "General",
        "options": ["true", "false"],
    },
    {
        "key": "CHROME_CDP_URL",
        "label": "Chrome CDP URL",
        "type": "text",
        "group": "Browser",
        "placeholder": "http://paperflow-chrome:9222",
    },
    {
        "key": "UPLOAD_WORKERS",
        "label": "Parallel upload threads",
        "type": "number",
        "group": "General",
    },
]

# Provider-spezifische Einstellungen (werden ebenfalls in .env gespeichert)
PROVIDER_ENV_FIELDS: dict[str, list[dict]] = {
    "amazon": [
        {"key": "AMAZON_EMAIL", "label": "Email", "type": "email"},
        {"key": "AMAZON_PASSWORD", "label": "Password", "type": "password"},
        {
            "key": "AMAZON_DOMAIN",
            "label": "Domain",
            "type": "select",
            "options": ["amazon.de", "amazon.com"],
        },
        {
            "key": "AMAZON_START_YEAR",
            "label": "Start year (e.g. 2015)",
            "type": "number",
        },
        {"key": "AMAZON_OTP_CODE", "label": "2FA OTP (one-time)", "type": "text"},
    ],
    "ikea": [
        {
            "key": "IKEA_MONTHS_BACK",
            "label": "Months back (e.g. 12)",
            "type": "number",
        },
    ],
}


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
                "has_env_settings": name in PROVIDER_ENV_FIELDS,
            }
        )
    return {"providers": result}


@app.get("/api/providers/{name}/settings")
async def get_provider_settings(name: str):
    fields = PROVIDER_ENV_FIELDS.get(name, [])
    values = _read_env()
    return {
        "fields": fields,
        "values": {f["key"]: values.get(f["key"], "") for f in fields},
    }


@app.post("/api/providers/{name}/settings")
async def save_provider_settings(name: str, body: SettingsSave):
    current = _read_env()
    current.update(body.values)
    _write_env(current)
    return {"ok": True}


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

    # Syntax-Check: Ungültiges Python wird sofort abgelehnt
    try:
        ast.parse(text)
    except SyntaxError as e:
        raise HTTPException(400, f"Syntax-Fehler in Zeile {e.lineno}: {e.msg}")

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
#  API – Verlauf
# ══════════════════════════════════════════════════════════════════════════════


@app.get("/api/history")
async def get_history(
    status: str | None = None,
    provider: str | None = None,
    limit: int = 500,
):
    rows = database.get_all_invoices(
        limit=limit, status=status or None, provider=provider or None
    )
    total = database.count_invoices(status=status or None, provider=provider or None)
    return {"invoices": rows, "total": total}


class BulkIds(BaseModel):
    ids: list[int]


# Bulk-Endpunkte VOR /{db_id} definieren – sonst matcht FastAPI "bulk" als int-Parameter
@app.delete("/api/history/bulk")
async def bulk_delete(body: BulkIds):
    deleted = sum(1 for i in body.ids if database.delete_invoice(i))
    return {"deleted": deleted}


@app.post("/api/history/bulk/retry")
async def bulk_retry(body: BulkIds):
    retried = sum(1 for i in body.ids if database.reset_invoice(i))
    return {"retried": retried}


@app.delete("/api/history/{db_id}")
async def delete_history_entry(db_id: int):
    if not database.delete_invoice(db_id):
        raise HTTPException(404, "Eintrag nicht gefunden")
    return {"ok": True}


@app.post("/api/history/{db_id}/retry")
async def retry_history_entry(db_id: int):
    if not database.reset_invoice(db_id):
        raise HTTPException(404, "Eintrag nicht gefunden")
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
#  API – OTP (SMS 2FA)
# ══════════════════════════════════════════════════════════════════════════════


@app.get("/api/otp/status")
async def get_otp_status():
    return {
        "needed": otp_state.needed,
        "login_required": otp_state.login_required,
        "login_running": otp_state.login_running,
    }


@app.post("/api/amazon/reset-session")
async def reset_amazon_session():
    """Löscht gespeicherte Cookies – nächster Lauf startet frischen Login."""
    otp_state.clear_cookies()
    otp_state.login_required = False
    return {"ok": True}


# ── IKEA ──────────────────────────────────────────────────────────────────────

_IKEA_LOGIN_URL = "https://www.ikea.com/de/de/profile/login/"


@app.post("/api/ikea/open-login")
async def ikea_open_login():
    """Öffnet die IKEA Login-Seite im CDP-Browser (noVNC).
    Der Nutzer kann sich dort manuell einloggen (inkl. 2FA)."""
    cdp_url = _get_cdp_url()
    if not cdp_url:
        raise HTTPException(400, "CHROME_CDP_URL nicht gesetzt – CDP-Modus inaktiv")

    resolved = _resolve_cdp_url(cdp_url)
    try:
        import urllib.request as _ur
        _ur.urlopen(f"{resolved}/json/version", timeout=3)
    except Exception:
        raise HTTPException(503, "Chrome CDP nicht erreichbar")

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(resolved)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            page.goto(_IKEA_LOGIN_URL, timeout=20_000)
            # Seite offen lassen – Nutzer loggt sich manuell ein
        return {
            "ok": True,
            "message": "IKEA Login-Seite geöffnet – bitte im Browser einloggen",
        }
    except Exception as e:
        raise HTTPException(500, f"Fehler beim Öffnen: {e}")


COOKIES_FILE = Path("/app/data/amazon_cookies.json")


@app.post("/api/amazon/cookies-raw")
async def import_cookies_raw(request: Request):
    """Empfängt document.cookie String direkt vom Browser (kein CORS-Preflight nötig)."""
    import json as _json

    body = await request.body()
    cookie_str = body.decode("utf-8", errors="replace")
    cookies = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            cookies.append(
                {
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".amazon.de",
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                }
            )
    if cookies:
        COOKIES_FILE.write_text(_json.dumps(cookies))
        otp_state.login_required = False
        logger.info("Amazon Cookies via Browser-Inject importiert: %d", len(cookies))
    from fastapi.responses import Response

    return Response(
        content=_json.dumps({"ok": True, "count": len(cookies)}),
        media_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


class CookieImport(BaseModel):
    cookies: str  # JSON-String aus Cookie-Editor Extension


@app.post("/api/amazon/import-cookies")
async def import_amazon_cookies(body: CookieImport):
    """Importiert Cookies aus dem echten Browser (Cookie Editor Extension)."""
    import json as _json

    try:
        raw = _json.loads(body.cookies)
    except Exception:
        raise HTTPException(400, "Ungültiges JSON")

    # Cookie Editor exportiert als Liste von Objekten – Playwright erwartet
    # dieselbe Struktur, aber nur bestimmte Felder
    def _normalize(c: dict) -> dict:
        out: dict = {
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "domain": c.get("domain", ""),
            "path": c.get("path", "/"),
            "secure": bool(c.get("secure", False)),
            "httpOnly": bool(c.get("httpOnly", False)),
        }
        if "sameSite" in c and c["sameSite"] in ("Strict", "Lax", "None"):
            out["sameSite"] = c["sameSite"]
        if "expirationDate" in c:
            out["expires"] = int(c["expirationDate"])
        elif "expires" in c:
            out["expires"] = int(c["expires"])
        return out

    if isinstance(raw, list):
        cookies = [_normalize(c) for c in raw if isinstance(c, dict)]
    else:
        raise HTTPException(400, "Cookies müssen eine Liste sein")

    if not cookies:
        raise HTTPException(400, "Keine Cookies gefunden")

    COOKIES_FILE.write_text(_json.dumps(cookies))
    otp_state.login_required = False
    logger.info("Amazon Cookies importiert: %d Cookies", len(cookies))
    return {"ok": True, "count": len(cookies)}


@app.get("/api/amazon/cookies-status")
async def get_cookies_status():
    """Zeigt ob Cookies vorhanden sind und wann sie ablaufen."""
    import json as _json
    import time as _time

    if not COOKIES_FILE.exists():
        return {"loaded": False}
    try:
        cookies = _json.loads(COOKIES_FILE.read_text())
        now = _time.time()
        # Finde den frühesten Ablauf unter den Session-relevanten Cookies
        expiries = [
            c.get("expires", 0)
            for c in cookies
            if c.get("expires", 0) > now and "amazon" in c.get("domain", "")
        ]
        earliest = min(expiries) if expiries else None
        return {
            "loaded": True,
            "count": len(cookies),
            "expires": earliest,
        }
    except Exception:
        return {"loaded": False}


class OtpSubmit(BaseModel):
    code: str


@app.post("/api/otp")
async def submit_otp(body: OtpSubmit):
    if not otp_state.needed:
        raise HTTPException(400, "Kein OTP angefordert")
    otp_state.submit_otp(body.code)
    return {"ok": True}


# ── UI compatibility aliases ──────────────────────────────────────────────────


@app.post("/api/amazon/otp")
async def submit_amazon_otp(body: OtpSubmit):
    return await submit_otp(body)


@app.get("/api/amazon/otp-status")
async def amazon_otp_status():
    return await get_otp_status()


@app.post("/api/amazon/reset-cookies")
async def reset_amazon_cookies():
    return await reset_amazon_session()


# ══════════════════════════════════════════════════════════════════════════════
#  API – Paperless-NGX Metadaten (Korrespondenten, Tags)
# ══════════════════════════════════════════════════════════════════════════════


@app.get("/api/paperless/ping")
async def paperless_ping():
    """Schneller Verbindungstest zu Paperless-NGX (kein vollständiger Tag-Load)."""
    try:
        from app.paperless_client import PaperlessClient

        client = PaperlessClient()
        ok = client.test_connection()
        if ok:
            return {"ok": True}
        raise HTTPException(status_code=503, detail="Paperless nicht erreichbar")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/api/debug/file/{filename}")
async def debug_file(filename: str):
    """Liefert Debug-Dateien aus /app/data (Screenshots, Logs) – nur PNG/JPG/TXT."""
    allowed = {".png", ".jpg", ".jpeg", ".txt", ".log"}
    path = Path("/app/data") / filename
    if path.suffix.lower() not in allowed or not path.exists():
        raise HTTPException(status_code=404, detail="Datei nicht gefunden")
    return FileResponse(str(path))


@app.get("/api/paperless/correspondents")
async def get_paperless_correspondents():
    """Lädt alle Korrespondenten aus Paperless-NGX für das Dropdown."""
    try:
        from app.paperless_client import PaperlessClient

        client = PaperlessClient()
        return {"correspondents": client.list_correspondents()}
    except Exception as e:
        return {"correspondents": [], "error": str(e)}


@app.get("/api/paperless/tags")
async def get_paperless_tags():
    """Lädt alle Tags aus Paperless-NGX für das Dropdown."""
    try:
        from app.paperless_client import PaperlessClient

        client = PaperlessClient()
        return {"tags": client.list_tags()}
    except Exception as e:
        return {"tags": [], "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
#  API – Logs
# ══════════════════════════════════════════════════════════════════════════════


@app.get("/api/logs")
async def get_logs(lines: int = 200):
    if not LOG_PATH.exists():
        return {"lines": []}
    all_lines = LOG_PATH.read_text(errors="replace").splitlines()
    return {"lines": all_lines[-lines:]}


# ══════════════════════════════════════════════════════════════════════════════
#  API – Browser (chrome-desktop CDP)
# ══════════════════════════════════════════════════════════════════════════════


@app.get("/api/browser/status")
async def browser_status():
    """Prüft ob der paperflow-chrome Container erreichbar ist."""
    import urllib.request as _urllib

    cdp_url = _get_cdp_url()
    if not cdp_url:
        return {"available": False, "reason": "CHROME_CDP_URL nicht gesetzt"}
    resolved = _resolve_cdp_url(cdp_url)
    try:
        _urllib.urlopen(f"{resolved}/json/version", timeout=2)
        return {"available": True, "cdp_url": cdp_url}
    except Exception as e:
        return {"available": False, "reason": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
#  API – Recorder (Provider-Template aus laufender Browser-Session)
# ══════════════════════════════════════════════════════════════════════════════


def _make_provider_template(domain: str, url: str) -> str:
    """Generiert ein Python-Provider-Template basierend auf der aufgenommenen URL."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    raw_name = parsed.netloc.replace("www.", "").split(".")[0]
    # Nur erlaubte Zeichen
    name = re.sub(r"[^a-z0-9_]", "_", raw_name.lower())
    class_name = name.capitalize()
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    return f'''"""
{class_name} Provider – lädt Rechnungen von {base_url} herunter.
Generiert von paperflow Recorder.

Anleitung:
  1. Öffne den Browser-Desktop (http://<server>:6080/vnc.html)
  2. Logge dich bei {base_url} ein
  3. Passe fetch_invoices() an – finde und lade alle Rechnungs-PDFs herunter
  4. Aktiviere den Provider im Web-UI unter "Provider"
"""

from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import sync_playwright

from app.providers import BaseProvider, Invoice

# Startseite aufgenommen von Recorder: {url}
_RECORDED_URL = "{url}"


class {class_name}Provider(BaseProvider):
    provider_name = "{name}"

    BASE_URL = "{base_url}"

    def fetch_invoices(self) -> list[Invoice]:
        invoices: list[Invoice] = []
        cdp_url = os.environ.get("CHROME_CDP_URL", "").strip()

        with sync_playwright() as p:
            if cdp_url:
                # Echten Browser via CDP nutzen – Login bleibt erhalten
                browser = p.chromium.connect_over_cdp(cdp_url)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
            else:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                context = browser.new_context()

            page = context.new_page()

            # TODO: Zur Rechnungsseite navigieren
            page.goto(_RECORDED_URL)

            # TODO: Login prüfen und PDFs finden
            # Beispiel: alle PDF-Links auf der Seite sammeln
            # pdf_links = page.locator("a[href*='.pdf']").all()
            # for link in pdf_links:
            #     pdf_url = link.get_attribute("href")
            #     order_id = "..."  # eindeutige ID ableiten
            #     output = self.download_dir / f"{{self.provider_name}}_{{order_id}}.pdf"
            #     response = page.goto(pdf_url, wait_until="load", timeout=30000)
            #     if response and response.ok:
            #         output.write_bytes(response.body())
            #         invoices.append(Invoice(
            #             invoice_id=order_id,
            #             file_path=output,
            #             title=f"{class_name} Rechnung {{order_id}}",
            #         ))

            page.close()
            if cdp_url:
                pass  # Browser offen lassen (gehört chrome-desktop)
            else:
                browser.close()

        return invoices
'''


@app.get("/api/recorder/capture")
async def recorder_capture():
    """
    Verbindet sich via CDP mit dem chrome-desktop Browser,
    liest die aktuell geöffnete URL und generiert ein Provider-Template.
    """
    import concurrent.futures

    cdp_url = _get_cdp_url()
    if not cdp_url:
        raise HTTPException(
            400, "Kein Browser verbunden – CHROME_CDP_URL nicht gesetzt"
        )

    def _do_capture():
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(_resolve_cdp_url(cdp_url))
            if not browser.contexts:
                raise ValueError("Keine Browser-Contexts gefunden")
            ctx = browser.contexts[0]
            pages = ctx.pages
            if not pages:
                raise ValueError("Keine offene Seite gefunden")
            page = pages[-1]  # letzte aktive Seite
            current_url = page.url
            title = page.title()
            return current_url, title

    loop = __import__("asyncio").get_event_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        try:
            current_url, title = await loop.run_in_executor(pool, _do_capture)
        except Exception as e:
            raise HTTPException(500, f"Capture fehlgeschlagen: {e}")

    from urllib.parse import urlparse

    parsed = urlparse(current_url)
    domain = parsed.netloc
    template = _make_provider_template(domain, current_url)

    return {
        "url": current_url,
        "title": title,
        "domain": domain,
        "template": template,
    }
