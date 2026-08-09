"""
HP Instant Ink Provider – lädt einzelne Rechnungen aus dem "Druck- und
Zahlungsverlauf" im HP Smart Portal herunter.

CDP-Modus:
  Verbindet sich mit dem paperflow-chrome Container via CDP.
  Nutzer loggt sich einmalig manuell ein → Session bleibt erhalten.
  Setzt CHROME_CDP_URL=http://paperflow-chrome:9222 voraus.

Seite (Stand 2026, https://portal.hpsmart.com/de/de/print_plans/account_history):
  Menü "HP Instant Ink" → "Druck- und Zahlungsverlauf" zeigt eine paginierte
  Tabelle (Datum | Beschreibung | Rechnung) mit ca. 10 Einträgen pro Seite und
  einem "Herunterladen"-Link je Zeile. WICHTIG: Der "Alle Rechnungen
  herunterladen"-Button erzeugt eine EINZIGE zusammengefasste PDF über alle
  Rechnungen – der wird bewusst NICHT angeklickt, wir laden ausschließlich
  die individuellen Zeilen-Links.

Ablauf:
  1. Login-Check über REPORTS_URL (kein bekanntes Login-URL-Muster – gilt
     als "nicht eingeloggt" wenn die Verlauf-Tabelle nicht auftaucht)
  2. Seite für Seite durch die Tabelle paginieren (Gesamtzahl aus "X von Y"
     ermittelt), pro Zeile Datum/Beschreibung/Download-Link auslesen
  3. Sobald eine Seite keine neuen (noch nicht verarbeiteten) Einträge mehr
     liefert, abbrechen – die Liste ist neueste-zuerst sortiert, ältere
     Seiten enthalten dann garantiert auch nichts Neues mehr
  4. Die "Seite weiter"-Steuerung ist best-effort (kein Live-Zugriff beim
     Schreiben dieses Codes) – bei Fehlschlag wird ein Diagnose-Dump der
     Pagination-Elemente geloggt
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import re
import time
from pathlib import Path

from playwright.sync_api import ElementHandle, Page, sync_playwright

from app import database
from app.providers import BaseProvider, Invoice, wait_for_cdp

logger = logging.getLogger("provider.hpinstantink")

REPORTS_URL = "https://portal.hpsmart.com/de/de/print_plans/account_history"

_CDP_URL = os.environ.get("CHROME_CDP_URL", "").strip()

_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_TOTAL_RE = re.compile(r"of\s+(\d+)", re.IGNORECASE)


def _sleep(min_s: float = 1.0, max_s: float = 2.5) -> None:
    time.sleep(random.uniform(min_s, max_s))


class HpinstantinkProvider(BaseProvider):
    provider_name = "hpinstantink"

    # ── Haupt-Dispatch ─────────────────────────────────────────────

    def fetch_invoices(self) -> list[Invoice]:
        if not _CDP_URL:
            logger.error(
                "CHROME_CDP_URL nicht gesetzt – HP Instant Ink Provider deaktiviert"
            )
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
                page.goto(REPORTS_URL, timeout=30_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=20_000)
                except Exception:
                    pass
                _sleep(2, 3)

                if not page.query_selector("text=Druck- und Zahlungsverlauf"):
                    logger.error(
                        "HP Instant Ink: Nicht eingeloggt oder Verlauf-Seite nicht "
                        "gefunden.\n"
                        "→ Öffne den Browser (noVNC)\n"
                        "→ Navigiere zu portal.hpsmart.com und logge dich ein\n"
                        "→ Danach erneut starten"
                    )
                    self._debug_dump(page, "Login-Check fehlgeschlagen")
                    return []

                logger.info("HP Instant Ink: Eingeloggt – starte Verlauf-Scan")
                self._expand_history_section(page)
                invoices = self._collect_invoices(page)

            except Exception:
                logger.exception("HP Instant Ink CDP-Fehler")
            finally:
                page.close()
                # Browser NICHT schließen – Session bleibt erhalten

        return invoices

    # ── Verlauf-Bereich aufklappen ──────────────────────────────────

    def _expand_history_section(self, page: Page) -> None:
        """Der 'Druck- und Zahlungsverlauf'-Bereich ist standardmäßig als
        Akkordion eingeklappt – die Tabelle (und die 'X von Y'-Seitenzahl)
        erscheint erst nach einem Klick auf den Abschnitts-Header. Es gibt
        zusätzlich einen gleichnamigen Link im Seitenmenü, deshalb gezielt
        nach dem Button (nicht dem Link) suchen."""
        try:
            section_btn = page.query_selector(
                "button:has-text('Druck- und Zahlungsverlauf')"
            )
            if not section_btn:
                logger.warning(
                    "Akkordion-Button 'Druck- und Zahlungsverlauf' nicht gefunden"
                )
                self._debug_dump(page, "Akkordion-Button nicht gefunden")
                return
            section_btn.click()
            _sleep(1, 2)
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
        except Exception as exc:
            logger.warning("Verlauf-Bereich konnte nicht aufgeklappt werden: %s", exc)

    # ── Scan-Logik ─────────────────────────────────────────────────

    def _collect_invoices(self, page: Page) -> list[Invoice]:
        invoices: list[Invoice] = []

        total_pages = self._detect_total_pages(page)
        logger.info("HP Instant Ink: %d Seite(n) im Verlauf", total_pages)

        page_num = 1
        while page_num <= total_pages:
            rows = self._parse_rows(page)
            logger.info(
                "Seite %d/%d: %d Zeilen gefunden", page_num, total_pages, len(rows)
            )

            new_on_this_page = 0
            for row in rows:
                date_iso = self._parse_date(row["date_text"])
                if not date_iso:
                    continue

                desc_hash = hashlib.sha1(row["row_text"].encode()).hexdigest()[:8]
                invoice_id = f"hpinstantink_{date_iso}_{desc_hash}"

                if database.is_processed(self.provider_name, invoice_id):
                    continue

                new_on_this_page += 1

                # Frisches ElementHandle statt der beim Seitenscan gesammelten
                # Referenz: frühere Downloads auf derselben Seite können das
                # DOM neu rendern und alte Handles ungültig machen (bestätigt
                # durch Produktionslog: "Element is not attached to the DOM"
                # bei späteren Zeilen nach mehreren erfolgreichen Downloads).
                fresh_row = self._find_row(page, row["date_text"], row["row_text"])
                if fresh_row is None:
                    logger.warning(
                        "Zeile für %s nach DOM-Änderung nicht mehr gefunden",
                        invoice_id,
                    )
                    pdf_path = None
                else:
                    pdf_path = self._download_row(page, fresh_row, invoice_id)
                if pdf_path and pdf_path.exists():
                    invoices.append(
                        Invoice(
                            invoice_id=invoice_id,
                            file_path=pdf_path,
                            title=f"HP Instant Ink {date_iso}",
                            date=date_iso,
                            extra_tags=[date_iso[:4]],
                        )
                    )
                else:
                    logger.warning(
                        "Download fehlgeschlagen für %s – wird beim nächsten Lauf "
                        "erneut versucht",
                        invoice_id,
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

            # Liste ist neueste-zuerst sortiert: keine neuen Einträge auf dieser
            # Seite → ältere Seiten enthalten garantiert auch nichts Neues mehr.
            if new_on_this_page == 0 and page_num > 1:
                logger.info("Seite %d ohne neue Einträge – beende Pagination", page_num)
                break

            if page_num >= total_pages:
                break

            if not self._goto_next_page(page, page_num):
                logger.warning(
                    "Konnte nicht zu Seite %d weiterblättern – beende Pagination",
                    page_num + 1,
                )
                break
            page_num += 1

        logger.info("HP Instant Ink: %d Rechnungen gefunden", len(invoices))
        return invoices

    # ── Tabelle lesen ─────────────────────────────────────────────

    def _detect_total_pages(self, page: Page) -> int:
        """Ermittelt die Gesamtseitenzahl aus dem 'X von Y'-Text (10 pro Seite)."""
        try:
            body_text = page.inner_text("body")
            m = _TOTAL_RE.search(body_text)
            if m:
                total = int(m.group(1))
                return max(1, (total + 9) // 10)
        except Exception:
            pass
        return 1

    def _parse_rows(self, page: Page) -> list[dict]:
        """Liest die aktuell sichtbare Tabellenseite. Gibt eine Liste von
        {"date_text": "28/07/2026", "row_text": "...", "download": ElementHandle}
        zurück – robust gegenüber der genauen DOM-Struktur (Tabelle oder
        div-basiertes Grid), indem von jedem Datums-Element aus nach oben
        gelaufen wird bis eine Zeile mit einem 'Herunterladen'-Link gefunden ist."""
        rows: list[dict] = []
        try:
            date_els = page.query_selector_all("text=/^\\d{2}\\/\\d{2}\\/\\d{4}$/")
        except Exception:
            date_els = []

        for date_el in date_els:
            try:
                date_text = date_el.inner_text().strip()
                if not _DATE_RE.match(date_text):
                    continue
                row_handle = date_el.evaluate_handle(
                    """el => {
                        let node = el;
                        for (let i = 0; i < 6 && node; i++) {
                            if (node.innerText && node.innerText.includes('Herunterladen')) {
                                return node;
                            }
                            node = node.parentElement;
                        }
                        return null;
                    }"""
                )
                row = row_handle.as_element()
                if not row:
                    continue
                dl_link = row.query_selector("text=Herunterladen")
                if not dl_link:
                    continue
                rows.append(
                    {
                        "date_text": date_text,
                        "row_text": row.inner_text(),
                        "download": dl_link,
                    }
                )
            except Exception:
                continue

        if not rows:
            self._debug_dump(page, "Keine Verlauf-Zeilen gefunden")

        return rows

    def _find_row(self, page: Page, date_text: str, row_text: str) -> dict | None:
        """Sucht eine Zeile anhand von Datum + Zeilentext erneut, um ein
        frisches (nicht-veraltetes) ElementHandle zu bekommen – siehe
        _collect_invoices."""
        for row in self._parse_rows(page):
            if row["date_text"] == date_text and row["row_text"] == row_text:
                return row
        return None

    def _parse_date(self, date_text: str) -> str | None:
        """'28/07/2026' (DD/MM/YYYY) → '2026-07-28'."""
        try:
            day, month, year = date_text.split("/")
            return f"{year}-{month}-{day}"
        except Exception:
            return None

    def _download_row(self, page: Page, row: dict, invoice_id: str) -> Path | None:
        """Klickt den 'Herunterladen'-Link. Bestätigt per Screenshot: das
        öffnet einen neuen Tab, der direkt zu einer PDF-URL navigiert
        (instantink.hpconnected.com/api/dashboard/.../pdf?key=...). Chrome
        zeigt PDFs mit seinem eingebauten Viewer an statt sie herunterzuladen
        – dabei feuert KEIN Playwright-'download'-Event (bestätigt durch
        Produktions-Logs: 30s-Timeout trotz Download-Event-Listener auf
        beiden Tabs). Deshalb: neuen Tab abfangen, seine finale URL auslesen,
        und die PDF-Bytes direkt per HTTP-Request holen (geteilter
        Browser-Context, also inkl. Cookies/Session) statt auf einen Download
        zu warten."""
        out_path = self.download_dir / f"{invoice_id}.pdf"
        dl_el: ElementHandle = row["download"]
        context = page.context

        try:
            with context.expect_page(timeout=15_000) as new_page_info:
                dl_el.click()
            new_page = new_page_info.value
        except Exception as exc:
            logger.warning("Kein neuer Tab beim Download von %s: %s", invoice_id, exc)
            return None

        try:
            try:
                new_page.wait_for_load_state("load", timeout=15_000)
            except Exception:
                pass
            pdf_url = new_page.url
        finally:
            try:
                new_page.close()
            except Exception:
                pass

        if not pdf_url or pdf_url == "about:blank":
            logger.warning("Kein PDF-URL im neuen Tab für %s gefunden", invoice_id)
            return None

        try:
            response = context.request.get(pdf_url)
        except Exception as exc:
            logger.warning("PDF-Request fehlgeschlagen für %s: %s", invoice_id, exc)
            return None

        if not response.ok:
            logger.warning(
                "PDF-Request fehlgeschlagen für %s: HTTP %d",
                invoice_id,
                response.status,
            )
            return None

        candidate = response.body()
        if len(candidate) > 500 and candidate[:4] == b"%PDF":
            out_path.write_bytes(candidate)
            logger.info(
                "Rechnung gespeichert: %s (%d bytes)", out_path.name, len(candidate)
            )
            return out_path

        logger.warning("Download für %s: Antwort ist kein gültiges PDF", invoice_id)
        return None

    # ── Pagination ───────────────────────────────────────────────

    def _goto_next_page(self, page: Page, current_page: int) -> bool:
        """Klickt zur nächsten Seite der Verlauf-Tabelle. Best-effort: die
        genaue Selektor-Struktur der Pagination war beim Schreiben dieses
        Codes nicht bekannt."""
        next_page_str = str(current_page + 1)
        for sel in [
            "button[aria-label*='ext']",
            "button[aria-label*='Weiter']",
            "a[aria-label*='ext']",
            f"button:has-text('{next_page_str}')",
            f"a:has-text('{next_page_str}')",
        ]:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    _sleep(1.5, 2.5)
                    try:
                        page.wait_for_load_state("networkidle", timeout=10_000)
                    except Exception:
                        pass
                    return True
            except Exception:
                continue

        self._debug_dump(page, f"Pagination zu Seite {next_page_str}")
        return False

    def _debug_dump(self, page: Page, context: str) -> None:
        """Loggt Buttons/Links auf der Seite zur Diagnose, wenn ein Selektor
        nicht wie erwartet passt."""
        try:
            texts = page.evaluate(
                """
                () => [...document.querySelectorAll('button, a, [role="button"]')]
                    .map(el => el.tagName + ':' + (el.textContent || el.getAttribute('aria-label') || '').trim())
                    .filter(t => t.length > 3)
                    .slice(0, 50)
                """
            )
            logger.info("DEBUG HP Instant Ink (%s): %s", context, texts)
        except Exception as de:
            logger.debug("DEBUG-Dump fehlgeschlagen (%s): %s", context, de)
