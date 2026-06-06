"""
Shared state für interaktive OTP-Eingabe und Login-Status über das Web-Interface.

Der Amazon-Provider ruft request_otp() auf – das blockiert bis der Nutzer
im Browser den SMS-Code eingibt. Das Web-Interface pollt /api/otp/status
und zeigt bei Bedarf ein Eingabefeld an.

Zusätzlich: login_required-State damit die UI informiert wird wenn
die Amazon-Session abgelaufen ist und ein neuer Login nötig ist.
"""

from __future__ import annotations

import threading

# ── OTP (SMS 2FA) ──────────────────────────────────────────────
needed: bool = False
code: str = ""
_event: threading.Event = threading.Event()


def request_otp(timeout: int = 300) -> str:
    """Blockiert bis der Nutzer einen OTP-Code eingibt (max. timeout Sekunden)."""
    global needed, code
    needed = True
    code = ""
    _event.clear()
    _event.wait(timeout=timeout)
    needed = False
    return code


def submit_otp(otp: str) -> None:
    """Wird vom Web-Interface aufgerufen wenn der Nutzer den Code eingibt."""
    global code
    code = otp.strip()
    _event.set()


# ── Login-Status ────────────────────────────────────────────────
login_required: bool = False  # True wenn Session abgelaufen
login_running: bool = False   # True während Login läuft
_cookies_file_path: str = "/app/data/amazon_cookies.json"


def notify_login_required() -> None:
    """Wird aufgerufen wenn Amazon-Session abgelaufen ist."""
    global login_required
    login_required = True


def notify_login_running() -> None:
    """Wird aufgerufen wenn Login-Prozess startet."""
    global login_running
    login_running = True


def notify_login_done(success: bool) -> None:
    """Wird aufgerufen wenn Login abgeschlossen ist (egal ob erfolgreich oder nicht)."""
    global login_required, login_running
    login_running = False
    if success:
        login_required = False


def clear_cookies() -> None:
    """Löscht gespeicherte Amazon-Cookies damit beim nächsten Lauf neu eingeloggt wird."""
    import os

    try:
        os.remove(_cookies_file_path)
    except FileNotFoundError:
        pass
