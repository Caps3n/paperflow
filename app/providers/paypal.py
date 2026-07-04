"""
PayPal Provider – lädt monatliche Kontoauszüge (PDF) von PayPal herunter.

CDP-Modus:
  Verbindet sich mit dem paperflow-chrome Container via CDP.
  Nutzer loggt sich einmalig manuell ein (inkl. 2FA/Passkey) → Session bleibt
  erhalten. Setzt CHROME_CDP_URL=http://paperflow-chrome:9222 voraus.

  Der vorherige PayPal-Provider nutzte einen automatisierten E-Mail/Passwort-
  Login, der regelmäßig am Passkey-Dialog scheiterte. Wie bei IKEA und Klarna
  übernimmt der Nutzer den Login einmalig manuell über noVNC – danach wird nur
  noch die bestehende Browser-Session weiterverwendet.

Ablauf:
  1. Login-Check über https://www.paypal.com/myaccount/summary/
  2. Für jeden Monat rückwirkend bis PAYPAL_MONTHS_BACK:
     – Kontoauszug per Direct-Download-URL laden
     – Fallback: Statements-Übersichtsseite → passenden Monat suchen
"""

from __future__ import annotations

import calendar
import datetime
import logging
import os
import random
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from app import database
from app.providers import BaseProvider, Invoice, wait_for_cdp

logger = logging.getLogger("provider.paypal")

SUMMARY_URL = "https://www.paypal.com/myaccount/summary/"
STATEMENTS_URL = "https://www.paypal.com/myaccount/statement/"

_CDP_URL = os.environ.get("CHROME_CDP_URL", "").strip()

_DE_MONTHS = [
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]


def _sleep(min_s: float = 1.0, max_s: float = 2.5) -> None:
    time.sleep(random.uniform(min_s, max_s))


def _is_logged_in_url(url: str) -> bool:
    """True wenn der Browser eingeloggt ist (kein Login-Redirect)."""
    return "/myaccount/" in url and "/signin" not in url


class PaypalProvider(BaseProvider):
    provider_name = "paypal"

    def __init__(self, config: dict):
        super().__init__(config)
        self.months_back = int(os.environ.get("PAYPAL_MONTHS_BACK") or "12")

    # ── Haupt-Dispatch ─────────────────────────────────────────────

    def fetch_invoices(self) -> list[Invoice]:
        if not _CDP_URL:
            logger.error("CHROME_CDP_URL nicht gesetzt – PayPal Provider deaktiviert")
            return []
        return self._fetch_via_cdp()

    # ── CDP-Modus ──────────────────────────────────────────────────

    def _fetch_via_cdp(self) -> list[Invoice]:
        invoices: list[Invoice] = []
        logger.info("CDP-Modus: Verbinde mit Chrome auf %s", _CDP_URL)

        cdp_url = _CDP_URL

        if not wait_for_cdp(cdp_url, logger):
            return []

        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp(cdp_url)
                logger.info(
                    "Chrome CDP verbunden: %d Context(s)", len(browser.contexts)
                )
            except Exception as exc:
                logger.error("CDP-Verbindung fehlgeschlagen: %s", exc)
                return []

            _MAC_UA = (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )
            context = (
                browser.contexts[0]
                if browser.contexts
                else browser.new_context(
                    locale="de-DE",
                    viewport={"width": 1280, "height": 900},
                    user_agent=_MAC_UA,
                )
            )
            page = context.new_page()

            try:
                page.goto(SUMMARY_URL, timeout=30_000)
                try:
                    page.wait_for_load_state("load", timeout=15_000)
                except Exception:
                    pass
                _sleep(1, 2)

                if not _is_logged_in_url(page.url):
                    logger.error(
                        "PayPal: Nicht eingeloggt.\n"
                        "→ Öffne den Browser (noVNC)\n"
                        "→ Navigiere zu paypal.com und logge dich ein\n"
                        "→ Danach erneut starten"
                    )
                    return []

                logger.info("PayPal: Eingeloggt (URL: %s)", page.url)
                invoices = self._collect_invoices(page)

            except Exception:
                logger.exception("PayPal CDP-Fehler")
            finally:
                page.close()
                # Browser NICHT schließen – Session bleibt erhalten

        return invoices

    # ── Scan-Logik ─────────────────────────────────────────────────

    def _months_to_scan(self) -> list[tuple[int, int]]:
        """Gibt eine Liste von (Jahr, Monat) zurück – neueste zuerst."""
        today = datetime.date.today()
        months = []
        for i in range(self.months_back):
            month = today.month - i
            year = today.year
            while month <= 0:
                month += 12
                year -= 1
            months.append((year, month))
        return months

    def _collect_invoices(self, page: Page) -> list[Invoice]:
        years_filter: set[int] | None = None
        yf = os.environ.get("PAPERFLOW_YEARS_FILTER", "").strip()
        if yf:
            years_filter = {int(y) for y in yf.split(",") if y.strip().isdigit()}
        elif self.scan_from_year:
            current_year = datetime.date.today().year
            years_filter = set(range(self.scan_from_year, current_year + 1))

        invoices: list[Invoice] = []
        today = datetime.date.today()

        for year, month in self._months_to_scan():
            if years_filter and year not in years_filter:
                continue

            # Laufender Monat noch nicht vollständig → erst ab dem 5. versuchen
            if year == today.year and month == today.month and today.day < 5:
                logger.info(
                    "Laufender Monat (%04d/%02d) noch nicht vollständig – überspringe",
                    year,
                    month,
                )
                continue

            invoice_id = f"paypal_statement_{year:04d}_{month:02d}"
            if database.is_processed(self.provider_name, invoice_id):
                logger.info("Bereits verarbeitet: %s", invoice_id)
                continue

            stmt = {"year": year, "month": month}
            pdf_path = self._download_statement(page, stmt)

            if pdf_path and pdf_path.exists():
                last_day = calendar.monthrange(year, month)[1]
                invoices.append(
                    Invoice(
                        invoice_id=invoice_id,
                        file_path=pdf_path,
                        title=f"PayPal Kontoauszug {_DE_MONTHS[month - 1]} {year}",
                        date=f"{year:04d}-{month:02d}-{last_day:02d}",
                        extra_tags=[str(year)],
                    )
                )
            elif stmt.get("_no_statement"):
                # Kein Auszug für diesen Monat vorhanden (z.B. keine Aktivität,
                # oder Account existierte noch nicht) → dauerhaft überspringen.
                logger.warning(
                    "Kein Auszug für %04d/%02d – wird als 'no_pdf' markiert "
                    "(wird nicht erneut versucht)",
                    year,
                    month,
                )
                database.mark_pending(
                    self.provider_name, invoice_id, f"{invoice_id}.pdf"
                )
                database.mark_failed(
                    self.provider_name,
                    invoice_id,
                    "Kein Kontoauszug für diesen Monat gefunden",
                    error_type="no_pdf",
                )
            else:
                # Auszug sollte existieren, Download ist aber fehlgeschlagen
                # (z.B. Timeout, Netzwerkfehler) → beim nächsten Lauf erneut versuchen.
                logger.warning(
                    "Download fehlgeschlagen für %04d/%02d – wird beim nächsten "
                    "Lauf erneut versucht",
                    year,
                    month,
                )
                database.mark_pending(
                    self.provider_name, invoice_id, f"{invoice_id}.pdf"
                )
                database.mark_failed(
                    self.provider_name,
                    invoice_id,
                    "Download fehlgeschlagen",
                    error_type="download_failed",
                )

        logger.info("PayPal: %d Auszüge gefunden", len(invoices))
        return invoices

    # ── Download ───────────────────────────────────────────────────

    def _download_statement(self, page: Page, stmt: dict) -> Path | None:
        """Lädt den Kontoauszug für stmt['year']/stmt['month'].
        Setzt stmt['_no_statement'] = True wenn dauerhaft kein Auszug existiert
        (z.B. keine Aktivität in diesem Monat)."""
        year, month = stmt["year"], stmt["month"]
        last_day = calendar.monthrange(year, month)[1]
        start = f"{year:04d}-{month:02d}-01"
        end = f"{year:04d}-{month:02d}-{last_day:02d}"
        out_path = self.download_dir / f"paypal_statement_{year:04d}_{month:02d}.pdf"

        download_url = (
            f"https://www.paypal.com/myaccount/statement/download"
            f"?startDate={start}&endDate={end}&format=PDF"
        )

        logger.info("Lade Kontoauszug %04d/%02d …", year, month)

        pdf_bytes = self._try_direct_download(page, download_url, out_path)
        if pdf_bytes is None:
            pdf_bytes = self._try_statements_page(page, year, month, out_path, stmt)

        if pdf_bytes is None:
            return None

        out_path.write_bytes(pdf_bytes)
        logger.info(
            "Kontoauszug gespeichert: %s (%d bytes)", out_path.name, len(pdf_bytes)
        )
        return out_path

    def _try_direct_download(
        self, page: Page, download_url: str, out_path: Path
    ) -> bytes | None:
        try:
            with page.expect_download(timeout=30_000) as dl_info:
                page.goto(download_url, timeout=30_000)
            download = dl_info.value
            download.save_as(str(out_path))
            if out_path.exists() and out_path.stat().st_size > 500:
                candidate = out_path.read_bytes()
                if candidate[:4] == b"%PDF":
                    return candidate
                logger.info(
                    "Direct-Download kein PDF (%d bytes) – versuche Statements-Seite",
                    len(candidate),
                )
            return None
        except Exception as exc:
            logger.info(
                "Direct-Download fehlgeschlagen: %s – versuche Statements-Seite", exc
            )
            return None

    def _try_statements_page(
        self, page: Page, year: int, month: int, out_path: Path, stmt: dict
    ) -> bytes | None:
        """Fallback: Statements-Übersichtsseite → passenden Monat suchen."""
        try:
            page.goto(STATEMENTS_URL, timeout=30_000)
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass
            _sleep(2, 3)

            month_name = _DE_MONTHS[month - 1]
            year_str = str(year)

            link = None
            for sel in [
                f"a:has-text('{month_name} {year_str}')",
                f"a:has-text('{month_name[:3]}. {year_str}')",
                f"*:has-text('{month_name} {year_str}') >> a[href*='pdf']",
                f"*:has-text('{month_name} {year_str}') >> a[href*='download']",
                f"*:has-text('{month_name} {year_str}') >> button",
            ]:
                try:
                    candidate_el = page.query_selector(sel)
                    if candidate_el:
                        link = candidate_el
                        break
                except Exception:
                    continue

            if not link:
                logger.info(
                    "Kein Auszug-Link für %04d/%02d auf Statements-Seite", year, month
                )
                stmt["_no_statement"] = True
                return None

            try:
                with page.expect_download(timeout=20_000) as dl_info:
                    link.click()
                download = dl_info.value
                download.save_as(str(out_path))
                if out_path.exists() and out_path.stat().st_size > 500:
                    candidate = out_path.read_bytes()
                    if candidate[:4] == b"%PDF":
                        return candidate
                return None
            except Exception as exc:
                logger.warning(
                    "Download über Statements-Seite fehlgeschlagen für %04d/%02d: %s",
                    year,
                    month,
                    exc,
                )
                return None
        except Exception as exc:
            logger.warning(
                "Statements-Seite für %04d/%02d fehlgeschlagen: %s", year, month, exc
            )
            return None
