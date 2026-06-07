"""
Gemeinsamer Laufzeit-Zustand zwischen Web-Interface und Scan-Worker.
Wird von main.py (Schreiben) und web.py (Lesen) genutzt.
"""

from __future__ import annotations

# Fortschritt des aktuellen Scan-Laufs
scan_progress: dict = {
    "phase": "",       # "discover" | "download" | "upload" | ""
    "provider": "",    # aktueller Provider
    "total": 0,        # Gesamtzahl Rechnungen in dieser Phase
    "done": 0,         # Bereits verarbeitete
    "current": "",     # Aktuelle Rechnung (z.B. order_id)
}


def reset_progress() -> None:
    scan_progress.update(phase="", provider="", total=0, done=0, current="")


def set_phase(phase: str, provider: str = "", total: int = 0) -> None:
    scan_progress.update(phase=phase, provider=provider, total=total, done=0, current="")


def tick(current: str = "") -> None:
    """Erhöht done um 1 und setzt current."""
    scan_progress["done"] += 1
    scan_progress["current"] = current
