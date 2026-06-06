"""
Shared state für interaktive OTP-Eingabe über das Web-Interface.

Der Amazon-Provider ruft request_otp() auf – das blockiert bis der Nutzer
im Browser den SMS-Code eingibt. Das Web-Interface pollt /api/otp/status
und zeigt bei Bedarf ein Eingabefeld an.
"""

from __future__ import annotations

import threading

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
