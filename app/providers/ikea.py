"""
IKEA Provider – lädt Kassenbons von IKEA herunter.

CDP-Modus:
  Verbindet sich mit dem paperflow-chrome Container via CDP.
  Nutzer loggt sich einmalig manuell ein (inkl. 2FA) → Session bleibt erhalten.
  Setzt CHROME_CDP_URL=http://paperflow-chrome:9222 voraus.

Gelernter Flow:
  1. Login → https://www.ikea.com/de/de/profile/login/
     → Redirect zu de.accounts.ikea.com/login?state=... (SSO/OAuth)
     → Bei Erfolg: Redirect zu /de/de/loyalty-hub/
  2. Bestellliste: https://www.ikea.com/de/de/purchases/
     → <a href="/de/de/purchases/{ORDER_ID}/"> mit Datum, Betrag, Typ im Text
  3. Bestelldetail: /de/de/purchases/{ORDER_ID}/
     → "Kassenbon & Rechnung" Button → Side-Panel → "Kassenbon herunterladen"
"""

from __future__ import annotations

import logging
import os
import random
import re
import socket
import time
import urllib.parse
import urllib.request
from pathlib import Path

import base64 as _base64

import requests as _requests
from playwright.sync_api import Page, sync_playwright

from app import database
from app.providers import BaseProvider, Invoice

logger = logging.getLogger("provider.ikea")

LOGIN_URL = "https://www.ikea.com/de/de/profile/login/"
PURCHASES_URL = "https://www.ikea.com/de/de/purchases/"

_CDP_URL = os.environ.get("CHROME_CDP_URL", "").strip()


def _sleep(min_s: float = 1.0, max_s: float = 2.5) -> None:
    time.sleep(random.uniform(min_s, max_s))


def _is_logged_in_url(url: str) -> bool:
    """Prüft ob URL auf eingeloggten Zustand hindeutet."""
    return "accounts.ikea.com" not in url and "/profile/login" not in url


class IkeaProvider(BaseProvider):
    provider_name = "ikea"

    def __init__(self, config: dict):
        super().__init__(config)
        self.months_back = int(os.environ.get("IKEA_MONTHS_BACK") or "12")

    # ── Haupt-Dispatch ─────────────────────────────────────────────

    def fetch_invoices(self) -> list[Invoice]:
        if not _CDP_URL:
            logger.error("CHROME_CDP_URL nicht gesetzt – IKEA Provider deaktiviert")
            return []
        return self._fetch_via_cdp()

    # ── CDP-Modus ──────────────────────────────────────────────────

    def _fetch_via_cdp(self) -> list[Invoice]:
        """
        CDP-Modus: Nutzt den persistenten paperflow-chrome Browser.
        Der Nutzer loggt sich einmalig manuell ein (inkl. 2FA).
        Session wird automatisch wiederverwendet.
        """
        invoices: list[Invoice] = []
        logger.info("CDP-Modus: Verbinde mit Chrome auf %s", _CDP_URL)

        # Resolve hostname → IP (Chrome rejects Host headers that are hostnames,
        # not IPs/localhost, as DNS-rebinding protection).
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
                # Login-Check
                page.goto(LOGIN_URL, timeout=30_000)
                try:
                    page.wait_for_load_state("load", timeout=15_000)
                except Exception:
                    pass  # Seite lädt evtl. noch Tracker – egal, Login-Check trotzdem
                _sleep(1, 2)

                if not _is_logged_in_url(page.url):
                    logger.error(
                        "IKEA: Nicht eingeloggt.\n"
                        "→ Öffne http://<server>:6080/vnc.html\n"
                        "→ Navigiere zu ikea.com/de/de und logge dich manuell ein (inkl. 2FA)\n"
                        "→ Danach erneut starten"
                    )
                    return []

                logger.info("IKEA: Eingeloggt – starte Bestellscan")
                invoices = self._collect_invoices(page)

            except Exception:
                logger.exception("IKEA CDP-Fehler")
            finally:
                page.close()
                # Browser NICHT schließen – Session bleibt erhalten

        return invoices

    # ── Scan-Logik ─────────────────────────────────────────────────

    def _collect_invoices(self, page: Page) -> list[Invoice]:
        """Bestellscan + Download – wird von CDP- und Fallback-Modus verwendet."""
        years_filter: set[int] | None = None
        yf = os.environ.get("PAPERFLOW_YEARS_FILTER", "").strip()
        if yf:
            years_filter = {int(y) for y in yf.split(",") if y.strip().isdigit()}
        elif self.scan_from_year:
            current_year = __import__("datetime").date.today().year
            years_filter = set(range(self.scan_from_year, current_year + 1))

        orders = self._parse_orders(page)
        invoices: list[Invoice] = []

        for order in orders:
            if years_filter and order["year"] not in years_filter:
                logger.info(
                    "Überspringe %s (Jahr %d nicht im Filter)",
                    order["id"],
                    order["year"],
                )
                continue

            invoice_id = f"ikea_{order['id']}"
            if database.is_processed(self.provider_name, invoice_id):
                logger.info("Bereits verarbeitet: %s", invoice_id)
                continue

            pdf_path = self._download_receipt(page, order)
            if pdf_path and pdf_path.exists():
                date_str = f"{order['year']}-{order['month']:02d}-{order['day']:02d}"
                invoices.append(
                    Invoice(
                        invoice_id=invoice_id,
                        file_path=pdf_path,
                        title=f"IKEA Kassenbon {date_str}",
                        date=date_str,
                        extra_tags=[str(order["year"])],
                    )
                )
            else:
                logger.warning("Kein PDF für %s", order["id"])

        logger.info("IKEA: %d Rechnungen gefunden", len(invoices))
        return invoices

    # ── Bestellliste ───────────────────────────────────────────────

    # Mögliche Selektoren für den "Mehr anzeigen"-Button auf IKEA
    _LOAD_MORE_SELECTORS = [
        "button:has-text('Weitere Informationen')",  # IKEA DE: "10 von 47 Ergebnissen"
        "button:has-text('Mehr anzeigen')",
        "button:has-text('Weitere Bestellungen')",
        "button:has-text('Mehr laden')",
        "button:has-text('Load more')",
        "button:has-text('Show more')",
        "[data-testid='load-more']",
        "[data-testid='show-more']",
    ]

    def _parse_orders(self, page: Page) -> list[dict]:
        """Liest alle Bestellungen aus der Übersichtsseite (inkl. Pagination)."""
        page.goto(PURCHASES_URL, timeout=30_000)
        try:
            page.wait_for_load_state("load", timeout=15_000)
        except Exception:
            pass
        _sleep(2, 3)

        # "Mehr anzeigen"-Button solange klicken bis er verschwindet
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
                break  # Kein Button mehr → alle Bestellungen geladen

        orders = []
        seen: set[str] = set()

        links = page.query_selector_all("a[href*='/purchases/']")
        for link in links:
            href = link.get_attribute("href") or ""
            m = re.search(r"/purchases/(\d+)/", href)
            if not m:
                continue
            order_id = m.group(1)
            if order_id in seen:
                continue
            seen.add(order_id)

            text = link.inner_text().strip()
            date_m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
            if not date_m:
                logger.debug("Kein Datum in Bestellkarte: %s", text[:50])
                continue

            day, month, year = (
                int(date_m.group(1)),
                int(date_m.group(2)),
                int(date_m.group(3)),
            )
            full_href = f"https://www.ikea.com{href}" if href.startswith("/") else href

            orders.append(
                {
                    "id": order_id,
                    "url": full_href,
                    "year": year,
                    "month": month,
                    "day": day,
                }
            )
            logger.info("Bestellung: %s  %02d.%02d.%d", order_id, day, month, year)

        logger.info("Gesamt %d Bestellungen gefunden", len(orders))
        return orders

    # ── Download ───────────────────────────────────────────────────

    def _download_receipt(self, page: Page, order: dict) -> Path | None:
        """Öffnet Bestelldetail und lädt den Kassenbon herunter."""
        logger.info("Lade Kassenbon für %s", order["id"])
        page.goto(order["url"], timeout=30_000)
        try:
            page.wait_for_load_state("load", timeout=15_000)
        except Exception:
            pass
        _sleep(2, 3)

        receipt_btn = None
        for sel in [
            "button:has-text('Kassenbon & Rechnung')",
            "button:has-text('Kassenbon')",
        ]:
            try:
                receipt_btn = page.wait_for_selector(sel, timeout=8_000)
                if receipt_btn and receipt_btn.is_visible():
                    break
            except Exception:
                receipt_btn = None
                continue

        if not receipt_btn:
            logger.warning("Kein 'Kassenbon & Rechnung' Button für %s", order["id"])
            return None

        receipt_btn.click()
        _sleep(1, 2)

        for sel in [
            "button:has-text('Kassenbon herunterladen')",
            "button:has-text('herunterladen')",
        ]:
            try:
                dl_btn = page.wait_for_selector(sel, timeout=5_000)
                if dl_btn and dl_btn.is_visible():
                    out_path = self.download_dir / f"ikea_{order['id']}.pdf"
                    with page.expect_download(timeout=30_000) as dl_info:
                        dl_btn.click()
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
                            logger.warning(
                                "Data-URL Dekodierung fehlgeschlagen: %s", de
                            )
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
                                    c["name"]: c["value"]
                                    for c in page.context.cookies()
                                }
                                resp = _requests.get(
                                    download.url,
                                    cookies=cookies,
                                    timeout=30,
                                    headers={"User-Agent": "Mozilla/5.0"},
                                )
                                content = resp.content
                                if (
                                    resp.ok
                                    and len(content) > 500
                                    and content[:4] == b"%PDF"
                                ):
                                    pdf_bytes = content
                                    logger.info(
                                        "HTTP-Fallback OK: %d bytes", len(content)
                                    )
                                else:
                                    logger.warning(
                                        "HTTP-Fallback kein PDF: status=%s size=%d",
                                        resp.status_code,
                                        len(content),
                                    )
                            except Exception as de:
                                logger.warning("HTTP-Fallback Fehler: %s", de)

                    if pdf_bytes is None or pdf_bytes[:4] != b"%PDF":
                        logger.warning("Kein gültiges PDF für %s", order["id"])
                        return None

                    out_path.write_bytes(pdf_bytes)
                    logger.info(
                        "Kassenbon gespeichert: %s (%d bytes)",
                        out_path.name,
                        len(pdf_bytes),
                    )
                    return out_path
            except Exception as e:
                logger.warning("Download fehlgeschlagen (%s): %s", sel, e)
                continue

        logger.warning(
            "'Kassenbon herunterladen' Button nicht gefunden für %s", order["id"]
        )
        return None
