"""
Klarna Provider – lädt Kaufbelege von Klarna herunter.

CDP-Modus:
  Verbindet sich mit dem paperflow-chrome Container via CDP.
  Nutzer loggt sich einmalig manuell ein → Session bleibt erhalten.
  Setzt CHROME_CDP_URL=http://paperflow-chrome:9222 voraus.

Gelernter Flow:
  1. Login → https://app.klarna.com/
     → Redirect zu Klarna SSO/OAuth → Bei Erfolg: Redirect zu /purchases/
  2. Kaufliste: https://app.klarna.com/purchases/
     → Liste aller Käufe mit Datum, Händler, Betrag
     → Pagination via "Mehr laden" oder Scroll
  3. Kaufdetail → "Kaufbeleg herunterladen" / "Beleg" Button → PDF
     → PDF kann als data:application/pdf;base64,... URL kommen (wie IKEA)
"""

from __future__ import annotations

import base64 as _base64
import logging
import os
import random
import re
import socket
import time
import urllib.parse
import urllib.request
from pathlib import Path

import requests as _requests
from playwright.sync_api import Page, sync_playwright

from app import database
from app.providers import BaseProvider, Invoice

logger = logging.getLogger("provider.klarna")

LOGIN_URL = "https://app.klarna.com/"
PURCHASES_URL = "https://app.klarna.com/purchases/"

_CDP_URL = os.environ.get("CHROME_CDP_URL", "").strip()


def _sleep(min_s: float = 1.0, max_s: float = 2.5) -> None:
    time.sleep(random.uniform(min_s, max_s))


def _is_logged_in_url(url: str) -> bool:
    """Prüft ob URL auf eingeloggten Zustand hindeutet."""
    logged_out_patterns = [
        "login.klarna.com",
        "auth.klarna.com",
        "/login",
        "/signin",
        "/authorize",
        "oauth",
    ]
    return not any(p in url for p in logged_out_patterns)


class KlarnaProvider(BaseProvider):
    provider_name = "klarna"

    def __init__(self, config: dict):
        super().__init__(config)

    # ── Haupt-Dispatch ─────────────────────────────────────────────

    def fetch_invoices(self) -> list[Invoice]:
        if not _CDP_URL:
            logger.error("CHROME_CDP_URL nicht gesetzt – Klarna Provider deaktiviert")
            return []
        return self._fetch_via_cdp()

    # ── CDP-Modus ──────────────────────────────────────────────────

    def _fetch_via_cdp(self) -> list[Invoice]:
        """
        CDP-Modus: Nutzt den persistenten paperflow-chrome Browser.
        Der Nutzer loggt sich einmalig manuell ein.
        Session wird automatisch wiederverwendet.
        """
        invoices: list[Invoice] = []
        logger.info("CDP-Modus: Verbinde mit Chrome auf %s", _CDP_URL)

        # Resolve hostname → IP (Chrome DNS-rebinding protection)
        cdp_url = _CDP_URL
        try:
            parsed = urllib.parse.urlparse(_CDP_URL)
            hostname = parsed.hostname or ""
            if hostname and not hostname.replace(".", "").isdigit():
                ip = socket.gethostbyname(hostname)
                port = parsed.port
                new_netloc = f"{ip}:{port}" if port else ip
                cdp_url = urllib.parse.urlunparse(parsed._replace(netloc=new_netloc))
                logger.info("CDP: %s → %s (Host-Header-Fix)", _CDP_URL, cdp_url)
        except Exception as e:
            logger.warning("CDP hostname resolution failed, using original URL: %s", e)

        # Warten bis Chrome bereit
        for attempt in range(30):
            try:
                urllib.request.urlopen(f"{cdp_url}/json/version", timeout=2)
                break
            except Exception:
                if attempt == 0:
                    logger.info("Warte auf Chrome CDP (%s)...", cdp_url)
                time.sleep(2)
        else:
            logger.error("Chrome CDP nicht erreichbar nach 60s: %s", cdp_url)
            return []

        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp(cdp_url)
                logger.info(
                    "Chrome CDP verbunden: %d Context(s)", len(browser.contexts)
                )
            except Exception as e:
                logger.error("CDP-Verbindung fehlgeschlagen: %s", e)
                return []

            context = (
                browser.contexts[0]
                if browser.contexts
                else browser.new_context(
                    locale="de-DE", viewport={"width": 1280, "height": 900}
                )
            )
            page = context.new_page()

            try:
                # Login-Check
                page.goto(LOGIN_URL, timeout=30_000)
                try:
                    page.wait_for_load_state("load", timeout=15_000)
                except Exception:
                    pass
                _sleep(2, 3)

                if not _is_logged_in_url(page.url):
                    logger.error(
                        "Klarna: Nicht eingeloggt.\n"
                        "→ Öffne http://<server>:6080/vnc.html\n"
                        "→ Navigiere zu app.klarna.com und logge dich manuell ein\n"
                        "→ Danach erneut starten"
                    )
                    return []

                logger.info("Klarna: Eingeloggt – starte Kaufscan (URL: %s)", page.url)
                invoices = self._collect_invoices(page)

            except Exception:
                logger.exception("Klarna CDP-Fehler")
            finally:
                page.close()
                # Browser NICHT schließen – Session bleibt erhalten

        return invoices

    # ── Scan-Logik ─────────────────────────────────────────────────

    def _collect_invoices(self, page: Page) -> list[Invoice]:
        """Kaufscan + Download."""
        years_filter: set[int] | None = None
        yf = os.environ.get("PAPERFLOW_YEARS_FILTER", "").strip()
        if yf:
            years_filter = {int(y) for y in yf.split(",") if y.strip().isdigit()}
        elif self.scan_from_year:
            current_year = __import__("datetime").date.today().year
            years_filter = set(range(self.scan_from_year, current_year + 1))

        purchases = self._parse_purchases(page)
        invoices: list[Invoice] = []

        for purchase in purchases:
            if years_filter and purchase["year"] not in years_filter:
                logger.info(
                    "Überspringe %s (Jahr %d nicht im Filter)",
                    purchase["id"],
                    purchase["year"],
                )
                continue

            invoice_id = f"klarna_{purchase['id']}"
            if database.is_processed(self.provider_name, invoice_id):
                logger.info("Bereits verarbeitet: %s", invoice_id)
                continue

            pdf_path = self._download_receipt(page, purchase)
            if pdf_path and pdf_path.exists():
                date_str = (
                    f"{purchase['year']}-{purchase['month']:02d}-{purchase['day']:02d}"
                )
                merchant = purchase.get("merchant", "Klarna")
                invoices.append(
                    Invoice(
                        invoice_id=invoice_id,
                        file_path=pdf_path,
                        title=f"Klarna Kaufbeleg {merchant} {date_str}",
                        date=date_str,
                        extra_tags=[str(purchase["year"])],
                    )
                )
            else:
                logger.warning("Kein PDF für %s", purchase["id"])

        logger.info("Klarna: %d Belege gefunden", len(invoices))
        return invoices

    # ── Kaufliste ──────────────────────────────────────────────────

    # Selektoren für den "Mehr laden"-Button auf Klarna
    _LOAD_MORE_SELECTORS = [
        "button:has-text('Mehr laden')",
        "button:has-text('Mehr anzeigen')",
        "button:has-text('Weitere Käufe')",
        "button:has-text('Load more')",
        "button:has-text('Show more')",
        "button:has-text('See more')",
        "[data-testid='load-more-button']",
        "[data-testid='show-more']",
        "[data-testid='load-more']",
    ]

    # Selektoren für Kaufkarten / Links in der Übersicht
    _PURCHASE_CARD_SELECTORS = [
        "a[href*='/purchases/']",
        "a[href*='/order/']",
        "[data-testid='purchase-card'] a",
        "[data-testid='order-card'] a",
        "article a[href]",
    ]

    def _parse_purchases(self, page: Page) -> list[dict]:
        """Liest alle Käufe aus der Übersichtsseite (inkl. Pagination)."""
        page.goto(PURCHASES_URL, timeout=30_000)
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass
        _sleep(2, 3)

        # "Mehr laden"-Button solange klicken bis er verschwindet
        while True:
            clicked = False
            for sel in self._LOAD_MORE_SELECTORS:
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        logger.info("Klicke 'Mehr laden'-Button (%s)", sel)
                        btn.scroll_into_view_if_needed()
                        btn.click()
                        _sleep(2, 3)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                break  # Kein Button mehr → alle Käufe geladen

        purchases = []
        seen: set[str] = set()

        # Versuche verschiedene Selektoren für Kauflinks
        links = []
        for sel in self._PURCHASE_CARD_SELECTORS:
            links = page.query_selector_all(sel)
            if links:
                logger.info("Kauflinks gefunden via '%s': %d", sel, len(links))
                break

        if not links:
            # Fallback: alle Links auf der Seite nach /purchase/ oder /order/ durchsuchen
            links = page.query_selector_all("a[href]")
            links = [
                lnk
                for lnk in links
                if re.search(r"/(purchase|order)/", lnk.get_attribute("href") or "")
            ]
            logger.info("Fallback: %d Kauflinks gefunden", len(links))

        for link in links:
            href = link.get_attribute("href") or ""
            # Extrahiere Purchase-ID aus URL
            m = re.search(r"/(purchases?|orders?)/([a-zA-Z0-9_-]+)", href)
            if not m:
                continue
            purchase_id = m.group(2)
            if purchase_id in seen:
                continue
            seen.add(purchase_id)

            text = link.inner_text().strip()

            # Datum parsen – Klarna DE: "15. Jun. 2024" oder "15.06.2024"
            year, month, day = None, None, None

            # Format: DD.MM.YYYY
            date_m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
            if date_m:
                day, month, year = (
                    int(date_m.group(1)),
                    int(date_m.group(2)),
                    int(date_m.group(3)),
                )

            # Format: D. Monatsname YYYY (z.B. "5. Jun. 2024" oder "5. Juni 2024")
            if not year:
                MONTHS_DE = {
                    "jan": 1,
                    "feb": 2,
                    "mär": 3,
                    "mar": 3,
                    "apr": 4,
                    "mai": 5,
                    "may": 5,
                    "jun": 6,
                    "jul": 7,
                    "aug": 8,
                    "sep": 9,
                    "okt": 10,
                    "oct": 10,
                    "nov": 11,
                    "dez": 12,
                    "dec": 12,
                }
                date_m2 = re.search(r"(\d{1,2})\.\s*([A-Za-zä]{3,}\.?)\s*(\d{4})", text)
                if date_m2:
                    day = int(date_m2.group(1))
                    mon_str = date_m2.group(2).lower().rstrip(".").strip()[:3]
                    month = MONTHS_DE.get(mon_str)
                    year = int(date_m2.group(3))

            if not year or not month or not day:
                logger.debug("Kein Datum in Kaufkarte: %s", text[:80])
                continue

            # Händlername aus Text extrahieren (erste Zeile / erstes Element)
            merchant = text.split("\n")[0].strip()[:60] if text else "Klarna"

            full_href = (
                f"https://app.klarna.com{href}" if href.startswith("/") else href
            )

            purchases.append(
                {
                    "id": purchase_id,
                    "url": full_href,
                    "year": year,
                    "month": month,
                    "day": day,
                    "merchant": merchant,
                }
            )
            logger.info(
                "Kauf: %s  %02d.%02d.%d  %s",
                purchase_id,
                day,
                month,
                year,
                merchant[:30],
            )

        logger.info("Gesamt %d Käufe gefunden", len(purchases))
        return purchases

    # ── Download ───────────────────────────────────────────────────

    # Selektoren für den "Kaufbeleg herunterladen"-Button
    _RECEIPT_OPEN_SELECTORS = [
        "button:has-text('Kaufbeleg')",
        "button:has-text('Beleg')",
        "button:has-text('Rechnung')",
        "a:has-text('Kaufbeleg')",
        "a:has-text('Beleg herunterladen')",
        "[data-testid='receipt-button']",
        "[data-testid='download-receipt']",
        "[aria-label*='Beleg']",
        "[aria-label*='Kaufbeleg']",
        "[aria-label*='receipt']",
    ]

    _RECEIPT_DOWNLOAD_SELECTORS = [
        "button:has-text('Herunterladen')",
        "button:has-text('Download')",
        "button:has-text('PDF')",
        "a[download]",
        "a[href*='.pdf']",
        "[data-testid='download-button']",
        "[aria-label*='Download']",
        "[aria-label*='Herunterladen']",
    ]

    def _download_receipt(self, page: Page, purchase: dict) -> Path | None:
        """Öffnet Kaufdetail und lädt den Kaufbeleg herunter."""
        logger.info("Lade Kaufbeleg für %s", purchase["id"])
        page.goto(purchase["url"], timeout=30_000)
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass
        _sleep(2, 3)

        out_path = self.download_dir / f"klarna_{purchase['id']}.pdf"

        # Zunächst direkt nach Download-Link suchen (manchmal direkt auf Detailseite)
        for dl_sel in self._RECEIPT_DOWNLOAD_SELECTORS:
            try:
                dl_el = page.query_selector(dl_sel)
                if dl_el and dl_el.is_visible():
                    logger.info("Direkter Download-Button gefunden (%s)", dl_sel)
                    pdf_path = self._do_download(page, dl_el, out_path, purchase["id"])
                    if pdf_path:
                        return pdf_path
                    break
            except Exception:
                continue

        # Dann: Panel/Modal öffnen via "Kaufbeleg"-Button
        receipt_btn = None
        for sel in self._RECEIPT_OPEN_SELECTORS:
            try:
                receipt_btn = page.wait_for_selector(sel, timeout=5_000)
                if receipt_btn and receipt_btn.is_visible():
                    logger.info("Kaufbeleg-Button gefunden (%s)", sel)
                    break
                receipt_btn = None
            except Exception:
                receipt_btn = None
                continue

        if not receipt_btn:
            logger.warning("Kein Kaufbeleg-Button gefunden für %s", purchase["id"])
            return None

        receipt_btn.click()
        _sleep(1, 2)

        # Nach dem Klick: Download-Button im Panel suchen
        for dl_sel in self._RECEIPT_DOWNLOAD_SELECTORS:
            try:
                dl_el = page.wait_for_selector(dl_sel, timeout=8_000)
                if dl_el and dl_el.is_visible():
                    pdf_path = self._do_download(page, dl_el, out_path, purchase["id"])
                    if pdf_path:
                        return pdf_path
            except Exception:
                continue

        logger.warning("Download-Button nicht gefunden für %s", purchase["id"])
        return None

    def _do_download(
        self, page: Page, btn, out_path: Path, purchase_id: str
    ) -> Path | None:
        """Klickt den Download-Button und speichert das PDF."""
        try:
            with page.expect_download(timeout=30_000) as dl_info:
                btn.click()
            download = dl_info.value

            # CDP-Modus: Browser läuft in separatem Container.
            # download.url kann ein data:-URL sein (base64-kodiertes PDF) →
            # direkt dekodieren statt save_as() zu nutzen.
            pdf_bytes: bytes | None = None

            if download.url.startswith("data:"):
                # data:application/pdf;base64,JVBERi...
                try:
                    _, b64 = download.url.split(",", 1)
                    pdf_bytes = _base64.b64decode(b64)
                    logger.info("Data-URL dekodiert: %d bytes", len(pdf_bytes))
                except Exception as de:
                    logger.warning("Data-URL Dekodierung fehlgeschlagen: %s", de)
            else:
                # Normaler HTTP-Download: save_as() versuchen
                download.save_as(str(out_path))
                if out_path.exists() and out_path.stat().st_size > 500:
                    candidate = out_path.read_bytes()
                    if candidate[:4] == b"%PDF":
                        pdf_bytes = candidate
                    else:
                        logger.info(
                            "save_as() kein PDF (%d bytes) – versuche requests",
                            len(candidate),
                        )

                if pdf_bytes is None:
                    # Fallback: HTTP-Download mit Browser-Cookies
                    try:
                        cookies = {
                            c["name"]: c["value"] for c in page.context.cookies()
                        }
                        resp = _requests.get(
                            download.url,
                            cookies=cookies,
                            timeout=30,
                            headers={"User-Agent": "Mozilla/5.0"},
                        )
                        content = resp.content
                        if resp.ok and len(content) > 500 and content[:4] == b"%PDF":
                            pdf_bytes = content
                            logger.info("HTTP-Fallback OK: %d bytes", len(content))
                        else:
                            logger.warning(
                                "HTTP-Fallback kein PDF: status=%s size=%d",
                                resp.status_code,
                                len(content),
                            )
                    except Exception as de:
                        logger.warning("HTTP-Fallback Fehler: %s", de)

            if pdf_bytes is None or pdf_bytes[:4] != b"%PDF":
                logger.warning("Kein gültiges PDF für %s", purchase_id)
                return None

            out_path.write_bytes(pdf_bytes)
            logger.info(
                "Kaufbeleg gespeichert: %s (%d bytes)",
                out_path.name,
                len(pdf_bytes),
            )
            return out_path

        except Exception as e:
            logger.warning("Download-Fehler für %s: %s", purchase_id, e)
            return None
