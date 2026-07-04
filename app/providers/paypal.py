"""
PayPal Provider – lädt Kontoauszüge (PDF) von PayPal herunter.

CDP-Modus:
  Verbindet sich mit dem paperflow-chrome Container via CDP.
  Nutzer loggt sich einmalig manuell ein (inkl. 2FA/Passkey) → Session bleibt
  erhalten. Setzt CHROME_CDP_URL=http://paperflow-chrome:9222 voraus.

  Der vorherige PayPal-Provider nutzte einen automatisierten E-Mail/Passwort-
  Login, der regelmäßig am Passkey-Dialog scheiterte. Wie bei IKEA und Klarna
  übernimmt der Nutzer den Login einmalig manuell über noVNC – danach wird nur
  noch die bestehende Browser-Session weiterverwendet.

Berichte sind ASYNCHRON (Stand 2026, https://www.paypal.com/reports/accountStatements):
  "Kontoauszüge – monatlich und benutzerdefiniert" → Datumsbereich wählen →
  "Erstellen" → der Bericht erscheint als neue Zeile in der Berichteverlauf-
  Tabelle mit Status "In Bearbeitung" und wird typischerweise erst am
  nächsten Tag fertig (Aktion wechselt zu "Herunterladen").

Ablauf:
  1. Login-Check über https://www.paypal.com/myaccount/summary/
  2. Berichteverlauf-Tabelle auf REPORTS_URL einlesen (Spalten: Erstellt am,
     Datumsbereich, Lieferart, Format, Aktion)
  3. Für jeden Monat rückwirkend bis PAYPAL_MONTHS_BACK:
     – Zeile mit passendem Datumsbereich + "Herunterladen" → PDF laden
     – Zeile mit "In Bearbeitung" → überspringen, nächsten Lauf erneut prüfen
       (wie Klarnas "Zahlung in Bearbeitung" – kein Fehler, kein DB-Eintrag)
     – Keine Zeile vorhanden → neuen Bericht anfordern, Ergebnis kommt beim
       nächsten Lauf
"""

from __future__ import annotations

import calendar
import datetime
import logging
import os
import random
import re
import time
from pathlib import Path

from playwright.sync_api import ElementHandle, Page, sync_playwright

from app import database
from app.providers import BaseProvider, Invoice, wait_for_cdp

logger = logging.getLogger("provider.paypal")

SUMMARY_URL = "https://www.paypal.com/myaccount/summary/"
REPORTS_URL = "https://www.paypal.com/reports/accountStatements"

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

_RANGE_RE = re.compile(r"\d{2}\.\d{2}\.\d{2}\s*-\s*\d{2}\.\d{2}\.\d{2}")


def _sleep(min_s: float = 1.0, max_s: float = 2.5) -> None:
    time.sleep(random.uniform(min_s, max_s))


def _is_logged_in_url(url: str) -> bool:
    """True wenn der Browser eingeloggt ist (kein Login-Redirect)."""
    return "/myaccount/" in url and "/signin" not in url


def _range_text(year: int, month: int) -> str:
    """Baut den Datumsbereich-Text wie ihn PayPal in der Berichtstabelle
    anzeigt, z.B. '01.06.26 - 30.06.26'."""
    last_day = calendar.monthrange(year, month)[1]
    yy = year % 100
    return f"01.{month:02d}.{yy:02d} - {last_day:02d}.{month:02d}.{yy:02d}"


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

        page.goto(REPORTS_URL, timeout=30_000)
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass
        _sleep(2, 3)

        if not _is_logged_in_url(page.url):
            logger.warning(
                "PayPal: Session abgelaufen beim Zugriff auf die Berichte-Seite – "
                "bitte über noVNC neu einloggen. Aktuelle URL: %s",
                page.url,
            )
            return []

        report_rows = self._parse_report_rows(page)
        logger.info(
            "PayPal: %d Berichte in der Berichteverlauf-Tabelle", len(report_rows)
        )

        for year, month in self._months_to_scan():
            if years_filter and year not in years_filter:
                continue

            # Laufender Monat noch nicht abgeschlossen → erst ab dem 5. anfordern
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

            expected = _range_text(year, month)
            row = next((r for r in report_rows if r["range_text"] == expected), None)

            if row is None:
                # Noch nie angefordert → anfordern, Ergebnis kommt i.d.R. erst
                # beim nächsten Lauf. Kein Fehler, kein DB-Eintrag.
                logger.info(
                    "Kein Bericht für %04d/%02d vorhanden – fordere neuen Bericht an",
                    year,
                    month,
                )
                self._request_report(page, year, month)
                _sleep(1, 2)
                continue

            if not row["ready"]:
                # Bereits angefordert, aber noch "In Bearbeitung" – wie Klarnas
                # "Zahlung in Bearbeitung": kein Fehler, einfach nächsten Lauf
                # erneut prüfen.
                logger.info(
                    "Bericht für %04d/%02d noch in Bearbeitung – überspringe",
                    year,
                    month,
                )
                continue

            pdf_path = self._download_ready_report(page, row["row"], year, month)
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
            else:
                # Bericht war laut Tabelle fertig, Download ist aber
                # fehlgeschlagen (Timeout, kein gültiges PDF) → transient,
                # beim nächsten Lauf erneut versuchen.
                logger.warning(
                    "Download fehlgeschlagen für fertigen Bericht %04d/%02d – wird "
                    "beim nächsten Lauf erneut versucht",
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

    # ── Berichteverlauf-Tabelle ──────────────────────────────────────

    def _parse_report_rows(self, page: Page) -> list[dict]:
        """Liest die Berichteverlauf-Tabelle (Spalten: Erstellt am,
        Datumsbereich, Lieferart, Format, Aktion). Gibt eine Liste von
        {"range_text": "01.06.26 - 30.06.26", "ready": bool, "row": ElementHandle}
        zurück. "ready" ist True wenn die Aktion "Herunterladen" zeigt, False
        wenn "In Bearbeitung"."""
        rows: list[dict] = []
        try:
            trs = page.query_selector_all("tr")
        except Exception:
            trs = []

        for tr in trs:
            try:
                text = tr.inner_text()
            except Exception:
                continue
            m = _RANGE_RE.search(text)
            if not m:
                continue
            ready = "herunterladen" in text.lower()
            rows.append({"range_text": m.group(0), "ready": ready, "row": tr})

        if not rows:
            logger.info("Keine Zeilen in der Berichteverlauf-Tabelle gefunden")
            self._debug_dump(page, "Berichteverlauf-Tabelle leer/nicht gefunden")

        return rows

    def _download_ready_report(
        self, page: Page, row: ElementHandle, year: int, month: int
    ) -> Path | None:
        out_path = self.download_dir / f"paypal_statement_{year:04d}_{month:02d}.pdf"
        try:
            dl_el = row.query_selector("text=Herunterladen") or row.query_selector(
                "button, a"
            )
            if not dl_el:
                logger.warning(
                    "'Herunterladen' Element nicht in Zeile für %04d/%02d gefunden",
                    year,
                    month,
                )
                return None
            with page.expect_download(timeout=30_000) as dl_info:
                dl_el.click()
            download = dl_info.value
            download.save_as(str(out_path))
            if out_path.exists() and out_path.stat().st_size > 500:
                candidate = out_path.read_bytes()
                if candidate[:4] == b"%PDF":
                    logger.info(
                        "Kontoauszug gespeichert: %s (%d bytes)",
                        out_path.name,
                        len(candidate),
                    )
                    return out_path
            return None
        except Exception as exc:
            logger.warning(
                "Download für fertigen Bericht %04d/%02d fehlgeschlagen: %s",
                year,
                month,
                exc,
            )
            return None

    # ── Bericht anfordern ────────────────────────────────────────────

    def _request_report(self, page: Page, year: int, month: int) -> None:
        """Fordert über das Datumsbereich-Dropdown + 'Erstellen'-Button einen
        neuen Bericht für year/month an. Best-effort: die genaue Struktur des
        'Benutzerdefiniert'-Datumsfelds ist nicht bekannt (kein Live-Zugriff
        beim Schreiben dieses Codes) – bei Fehlschlag wird ein Debug-Dump der
        Dropdown-/Eingabe-Elemente geloggt, um die Selektoren nachzuschärfen."""
        try:
            dropdown = page.query_selector("text=Datumsbereich")
            if dropdown:
                dropdown.click()
            _sleep(0.5, 1)

            custom_opt = page.query_selector("text=Benutzerdefiniert")
            if custom_opt:
                custom_opt.click()
                _sleep(0.5, 1)
            else:
                logger.warning(
                    "'Benutzerdefiniert'-Option nicht im Datumsbereich-Dropdown "
                    "gefunden für %04d/%02d",
                    year,
                    month,
                )
                self._debug_dump(page, f"Datumsbereich-Dropdown für {year}/{month:02d}")
                return

            last_day = calendar.monthrange(year, month)[1]
            start_str = f"{year:04d}-{month:02d}-01"
            end_str = f"{year:04d}-{month:02d}-{last_day:02d}"

            date_inputs = page.query_selector_all("input[type='date']")
            if len(date_inputs) < 2:
                date_inputs = page.query_selector_all("input")
            if len(date_inputs) >= 2:
                date_inputs[0].fill(start_str)
                date_inputs[1].fill(end_str)
            else:
                logger.warning(
                    "Keine Datumsfelder für Benutzerdefiniert gefunden – Bericht "
                    "%04d/%02d evtl. nicht korrekt angefordert",
                    year,
                    month,
                )
                self._debug_dump(page, f"Datumsfelder für {year}/{month:02d}")
                return
            _sleep(0.5, 1)

            create_btn = page.query_selector("button:has-text('Erstellen')")
            if not create_btn:
                logger.warning(
                    "'Erstellen'-Button nicht gefunden für %04d/%02d", year, month
                )
                self._debug_dump(page, f"Erstellen-Button für {year}/{month:02d}")
                return

            create_btn.click()
            logger.info(
                "Bericht für %04d/%02d angefordert – Ergebnis kommt i.d.R. erst "
                "beim nächsten Lauf",
                year,
                month,
            )
        except Exception as exc:
            logger.warning(
                "Bericht-Anfrage für %04d/%02d fehlgeschlagen: %s", year, month, exc
            )
            self._debug_dump(page, f"Bericht-Anfrage-Exception für {year}/{month:02d}")

    def _debug_dump(self, page: Page, context: str) -> None:
        """Loggt Buttons/Optionen/Eingabefelder auf der Seite zur Diagnose,
        wenn ein Selektor nicht wie erwartet passt."""
        try:
            texts = page.evaluate(
                """
                () => [...document.querySelectorAll('button, [role="option"], li, input, a')]
                    .map(el => el.tagName + ':' + (el.textContent || el.placeholder || el.getAttribute('aria-label') || '').trim())
                    .filter(t => t.length > 3)
                    .slice(0, 50)
                """
            )
            logger.info("DEBUG PayPal (%s): %s", context, texts)
        except Exception as de:
            logger.debug("DEBUG-Dump fehlgeschlagen (%s): %s", context, de)
