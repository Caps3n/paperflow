"""
Provider-Plugin-System.

Jeder Provider erbt von BaseProvider und implementiert `fetch_invoices()`.
Neue Provider einfach als neue Datei in diesem Ordner anlegen.
"""

from __future__ import annotations

import logging
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Invoice:
    """Repräsentiert eine heruntergeladene Rechnung."""

    # Eindeutige ID des Providers (z.B. Bestellnummer)
    invoice_id: str
    # Lokaler Dateipfad der heruntergeladenen PDF
    file_path: Path
    # Titel für Paperless
    title: str
    # Datum der Rechnung (ISO-Format: YYYY-MM-DD)
    date: str | None = None
    # Betrag als String (nur für Logging)
    amount: str | None = None
    # Zusätzliche Tags
    extra_tags: list[str] = field(default_factory=list)


class BaseProvider:
    """
    Basisklasse für alle Invoice-Provider.

    Um einen neuen Provider hinzuzufügen:
    1. Neue Datei in app/providers/ anlegen (z.B. ebay.py)
    2. Klasse erbt von BaseProvider
    3. `provider_name` und `fetch_invoices()` implementieren
    4. In config/providers.yml aktivieren
    """

    # Eindeutiger Name des Providers (z.B. "amazon", "ebay")
    provider_name: str = "base"

    def __init__(self, config: dict):
        self.config = config
        self.tags: list[str] = config.get("tags", [])
        self.correspondent: str | None = config.get("correspondent")
        self.scan_from_year: int | None = config.get("scan_from_year")
        self.download_dir = Path("/app/downloads") / self.provider_name
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger(f"provider.{self.provider_name}")

    def fetch_invoices(self) -> list[Invoice]:
        """
        Hauptmethode: Lädt alle neuen Rechnungen herunter.
        Muss von Unterklassen implementiert werden.
        Gibt eine Liste von Invoice-Objekten zurück.
        """
        raise NotImplementedError


def wait_for_cdp(
    cdp_url: str,
    logger: logging.Logger,
    max_attempts: int = 30,
    poll_interval_s: float = 2.0,
) -> bool:
    """Wartet bis der Chrome-CDP-Endpunkt erreichbar ist (Container-Start kann
    etwas dauern). Gibt True zurück sobald erreichbar, sonst False nach Timeout."""
    for attempt in range(max_attempts):
        try:
            urllib.request.urlopen(f"{cdp_url}/json/version", timeout=2)
            return True
        except Exception:
            if attempt == 0:
                logger.info("Warte auf Chrome CDP (%s)...", cdp_url)
            time.sleep(poll_interval_s)
    logger.error(
        "Chrome CDP nicht erreichbar nach %ds: %s",
        int(max_attempts * poll_interval_s),
        cdp_url,
    )
    return False
