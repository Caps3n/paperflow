"""
Klarna Provider – lädt Zahlungsauszüge von Klarna herunter.

CDP-Modus:
  Verbindet sich mit dem paperflow-chrome Container via CDP.
  Nutzer loggt sich einmalig manuell ein → Session bleibt erhalten.
  Setzt CHROME_CDP_URL=http://paperflow-chrome:9222 voraus.

Gelernter Flow (aus Browser-Analyse):
  1. Login  → https://app.klarna.com/
  2. Zahlungen-Übersicht: https://app.klarna.com/manage-payments
     → Transaktionsliste gruppiert nach Status (Zahlung in Bearbeitung / Bezahlt im …)
     → Jede Zeile: Händler · Datum · Karte ···· 5206 · Betrag
     → URL der Detailseite: /manage-payments/transactions/internal/
           krn%3Accs%3Atransaction%3A{UUID}/details?captureKrn=krn%3Accs%3A...
  3. Detailseite:
     → ••• Button → "Mehr"-Dialog → "Auszug herunterladen"
     → Klick triggert Browser-Download (expect_download) oder data-URL-PDF
     → Status "Gestern bezahlt" / "Am D. Monat bezahlt" = abgeschlossen
     → Status "Zahlung in Bearbeitung" = ausstehend (Auszug eventuell unvollständig)
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
MANAGE_PAYMENTS_URL = "https://app.klarna.com/manage-payments"

_CDP_URL = os.environ.get("CHROME_CDP_URL", "").strip()

# Deutsche Monatsnamen → Nummer
_MONTHS_DE: dict[str, int] = {
    "jan": 1, "feb": 2, "mär": 3, "mar": 3,
    "apr": 4, "mai": 5, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10,
    "oct": 10, "nov": 11, "dez": 12, "dec": 12,
}


def _sleep(min_s: float = 1.0, max_s: float = 2.5) -> None:
    time.sleep(random.uniform(min_s, max_s))


def _is_logged_in_url(url: str) -> bool:
    """True wenn der Browser eingeloggt ist (kein Login/Auth-Redirect)."""
    return not any(
        p in url for p in [
            "login.klarna.com", "auth.klarna.com",
            "/login", "/signin", "/authorize", "oauth",
        ]
    )


def _parse_klarna_date(text: str) -> tuple[int, int, int] | None:
    """
    Versucht ein Datum aus Klarna-Texten zu extrahieren.
    Formate: "5. Juni, 06:25", "30. Mai, 17:40", "Am 2. Juni bezahlt",
             "15.06.2024", "15. Jun. 2024"
    Gibt (year, month, day) zurück oder None.
    """
    import datetime

    # "D. Monatsname[,] [YYYY]" – z.B. "5. Juni, 06:25" (ohne Jahr → aktuelles Jahr)
    m = re.search(
        r"(\d{1,2})\.\s*([A-Za-zäöüÄÖÜ]{3,})(?:[.,]|\s|$)", text, re.IGNORECASE
    )
    if m:
        day = int(m.group(1))
        mon_key = m.group(2).lower()[:3].replace("ä", "a")
        month = _MONTHS_DE.get(mon_key)
        # Jahr: suche vierstellige Zahl in Text; sonst aktuelles Jahr
        yr_m = re.search(r"\b(20\d{2})\b", text)
        year = int(yr_m.group(1)) if yr_m else datetime.date.today().year
        if month:
            return year, month, day

    # "DD.MM.YYYY"
    m2 = re.search(r"(\d{2})\.(\d{2})\.(20\d{2})", text)
    if m2:
        return int(m2.group(3)), int(m2.group(2)), int(m2.group(1))

    return None


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
                logger.info("CDP: %s → %s", _CDP_URL, cdp_url)
        except Exception as exc:
            logger.warning("CDP hostname resolution failed: %s", exc)

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
                logger.info("Chrome CDP verbunden: %d Context(s)", len(browser.contexts))
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
                        "→ Öffne den Browser (noVNC)\n"
                        "→ Navigiere zu app.klarna.com und logge dich ein\n"
                        "→ Danach erneut starten"
                    )
                    return []

                logger.info("Klarna: Eingeloggt (URL: %s)", page.url)
                invoices = self._collect_invoices(page)

            except Exception:
                logger.exception("Klarna CDP-Fehler")
            finally:
                page.close()
                # Browser NICHT schließen – Session bleibt erhalten

        return invoices

    # ── Scan-Logik ─────────────────────────────────────────────────

    def _collect_invoices(self, page: Page) -> list[Invoice]:
        """Transaktionsscan + Download."""
        years_filter: set[int] | None = None
        yf = os.environ.get("PAPERFLOW_YEARS_FILTER", "").strip()
        if yf:
            years_filter = {int(y) for y in yf.split(",") if y.strip().isdigit()}
        elif self.scan_from_year:
            import datetime
            current_year = datetime.date.today().year
            years_filter = set(range(self.scan_from_year, current_year + 1))

        transactions = self._parse_transactions(page)
        invoices: list[Invoice] = []

        for txn in transactions:
            if years_filter and txn.get("year") and txn["year"] not in years_filter:
                logger.info(
                    "Überspringe %s (Jahr %s nicht im Filter)",
                    txn["id"], txn.get("year")
                )
                continue

            invoice_id = f"klarna_{txn['id']}"
            if database.is_processed(self.provider_name, invoice_id):
                logger.info("Bereits verarbeitet: %s", invoice_id)
                continue

            pdf_path = self._download_auszug(page, txn)
            if pdf_path and pdf_path.exists():
                year = txn.get("year", 2000)
                month = txn.get("month", 1)
                day = txn.get("day", 1)
                date_str = f"{year}-{month:02d}-{day:02d}"
                merchant = txn.get("merchant", "Klarna")
                invoices.append(
                    Invoice(
                        invoice_id=invoice_id,
                        file_path=pdf_path,
                        title=f"Klarna Auszug {merchant} {date_str}",
                        date=date_str,
                        extra_tags=[str(year)],
                    )
                )
            else:
                logger.warning("Kein PDF für %s", txn["id"])

        logger.info("Klarna: %d Auszüge gefunden", len(invoices))
        return invoices

    # ── Transaktionsliste ──────────────────────────────────────────

    _LOAD_MORE_SELECTORS = [
        "button:has-text('Mehr laden')",
        "button:has-text('Mehr anzeigen')",
        "button:has-text('Alle anzeigen')",
        "button:has-text('Load more')",
        "button:has-text('Show more')",
        "[data-testid='load-more']",
    ]

    def _parse_transactions(self, page: Page) -> list[dict]:
        """
        Liest alle Transaktionen von /manage-payments.
        Sucht nach <a href*='transactions/internal'> Links (React Router rendert diese als <a>).
        """
        page.goto(MANAGE_PAYMENTS_URL, timeout=30_000)
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass
        _sleep(2, 4)

        # "Mehr laden" solange klicken bis Button weg
        for _ in range(20):
            clicked = False
            for sel in self._LOAD_MORE_SELECTORS:
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        logger.info("Klicke 'Mehr laden' (%s)", sel)
                        btn.scroll_into_view_if_needed()
                        btn.click()
                        _sleep(2, 3)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                break

        transactions: list[dict] = []
        seen: set[str] = set()

        # Strategie 1: direkte <a href> Links (React Router)
        links = page.query_selector_all(
            "a[href*='/manage-payments/transactions/internal/']"
        )
        logger.info("Strategie 1 (direkte Links): %d gefunden", len(links))

        for link in links:
            href = link.get_attribute("href") or ""
            txn = self._parse_txn_from_href(href)
            if txn and txn["id"] not in seen:
                seen.add(txn["id"])
                # Text aus der Link-Karte für Händlername
                try:
                    text = link.inner_text().strip()
                    txn["merchant"] = text.split("\n")[0].strip()[:60]
                except Exception:
                    txn["merchant"] = "Klarna"
                transactions.append(txn)
                logger.info(
                    "Transaktion: %s  %s", txn["id"][:8], txn.get("merchant", "?")[:30]
                )

        # Strategie 2: JavaScript – falls React kein href setzt (onClick stattdessen)
        if not transactions:
            logger.info("Strategie 2: JS-basierte Suche nach Transaktions-URLs")
            try:
                hrefs = page.evaluate("""
                    () => {
                        const result = [];
                        // Alle Elemente mit passenden hrefs suchen
                        document.querySelectorAll('[href*="transactions/internal"]').forEach(el => {
                            result.push(el.getAttribute('href'));
                        });
                        // Auch onClick-Handler prüfen (nicht 100% zuverlässig)
                        if (result.length === 0) {
                            // React-Router Links können auch als data-* Attribute vorkommen
                            document.querySelectorAll('[data-href*="transactions"]').forEach(el => {
                                result.push(el.getAttribute('data-href'));
                            });
                        }
                        return [...new Set(result)];
                    }
                """)
                for href in hrefs:
                    txn = self._parse_txn_from_href(href or "")
                    if txn and txn["id"] not in seen:
                        seen.add(txn["id"])
                        txn["merchant"] = "Klarna"
                        transactions.append(txn)
            except Exception as exc:
                logger.warning("JS-Strategie fehlgeschlagen: %s", exc)

        # Strategie 3: HTML-Source nach Transaction-UUIDs durchsuchen
        if not transactions:
            logger.info("Strategie 3: HTML-Quelltext nach Transaction-IDs durchsuchen")
            try:
                html = page.content()
                # Suche nach krn:ccs:transaction:{UUID} Mustern (encoded oder plain)
                uuids = set(re.findall(
                    r"krn(?:%3A|:)ccs(?:%3A|:)transaction(?:%3A|:)"
                    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                    html, re.IGNORECASE
                ))
                logger.info("Strategie 3: %d UUIDs in HTML gefunden", len(uuids))
                for uuid in uuids:
                    if uuid not in seen:
                        seen.add(uuid)
                        txn_url = (
                            f"{MANAGE_PAYMENTS_URL}/transactions/internal/"
                            f"krn%3Accs%3Atransaction%3A{uuid}/details"
                        )
                        transactions.append({
                            "id": uuid,
                            "url": txn_url,
                            "merchant": "Klarna",
                            "year": None,
                            "month": None,
                            "day": None,
                        })
            except Exception as exc:
                logger.warning("HTML-Suche fehlgeschlagen: %s", exc)

        if not transactions:
            logger.warning(
                "Keine Transaktionen gefunden auf %s\n"
                "Prüfe ob der Browser eingeloggt ist und die Seite Transaktionen enthält.\n"
                "Aktueller URL: %s",
                MANAGE_PAYMENTS_URL, page.url
            )
            # DEBUG: HTML-Ausschnitt loggen damit wir die Seitenstruktur sehen
            try:
                html_debug = page.content()
                # Ersten 3000 Zeichen des Body-Inhalts loggen
                body_start = html_debug.find("<body")
                snippet = html_debug[body_start:body_start + 3000] if body_start >= 0 else html_debug[:3000]
                logger.info("DEBUG Klarna HTML-Ausschnitt:\n%s", snippet)
                # Alle Links auf der Seite loggen
                all_links = page.evaluate("""
                    () => [...document.querySelectorAll('a[href]')]
                        .map(a => a.getAttribute('href'))
                        .filter(h => h && h.length > 1)
                        .slice(0, 30)
                """)
                logger.info("DEBUG alle Links auf Seite: %s", all_links)
                # Alle Buttons
                all_btns = page.evaluate("""
                    () => [...document.querySelectorAll('button')]
                        .map(b => b.textContent.trim().slice(0, 50))
                        .filter(t => t.length > 0)
                        .slice(0, 20)
                """)
                logger.info("DEBUG alle Buttons: %s", all_btns)
            except Exception as de:
                logger.debug("DEBUG-Dump fehlgeschlagen: %s", de)
        else:
            logger.info("Gesamt %d Transaktionen gefunden", len(transactions))

        return transactions

    def _parse_txn_from_href(self, href: str) -> dict | None:
        """Extrahiert Transaktions-UUID und baut die Detail-URL."""
        if not href:
            return None
        # URL-Muster: .../krn%3Accs%3Atransaction%3A{UUID}/details?captureKrn=...
        # Oder decoded: .../krn:ccs:transaction:{UUID}/details
        m = re.search(
            r"krn(?:%3A|:)ccs(?:%3A|:)transaction(?:%3A|:)"
            r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            href, re.IGNORECASE
        )
        if not m:
            return None
        uuid = m.group(1).lower()
        # Vollständige Detail-URL
        if href.startswith("http"):
            full_url = href
        else:
            full_url = f"https://app.klarna.com{href}"
        # captureKrn muss im URL enthalten sein für korrektes Rendering
        if "captureKrn" not in full_url:
            full_url = (
                f"{MANAGE_PAYMENTS_URL}/transactions/internal/"
                f"krn%3Accs%3Atransaction%3A{uuid}/details"
            )
        return {
            "id": uuid,
            "url": full_url,
            "merchant": "Klarna",
            "year": None,
            "month": None,
            "day": None,
        }

    # ── Detail-Seite & Download ────────────────────────────────────

    # Selektoren für den ••• Button auf der Detailseite
    _MORE_BTN_SELECTORS = [
        # Aria-Label
        "button[aria-label*='Mehr']",
        "button[aria-label*='mehr']",
        "button[aria-label*='More']",
        "button[aria-label*='more']",
        "button[aria-label*='options']",
        "button[aria-label*='Options']",
        # Text-basiert (drei Punkte)
        "button:has-text('...')",
        "button:has-text('⋯')",
        "button:has-text('…')",
        # Klarna-spezifische Test-IDs
        "[data-testid*='more']",
        "[data-testid*='options']",
        "[data-testid*='menu']",
    ]

    def _download_auszug(self, page: Page, txn: dict) -> Path | None:
        """
        Navigiert zur Transaktionsdetailseite, klickt ••• → "Auszug herunterladen",
        und speichert das PDF.
        """
        logger.info("Lade Auszug für %s", txn["id"])
        page.goto(txn["url"], timeout=30_000)
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass
        _sleep(2, 3)

        # Datum + Händler aus Detailseite extrahieren (falls nicht aus Liste bekannt)
        if txn.get("year") is None:
            self._enrich_txn_from_detail(page, txn)

        out_path = self.download_dir / f"klarna_{txn['id']}.pdf"

        # ••• Button suchen
        more_btn = None
        for sel in self._MORE_BTN_SELECTORS:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    more_btn = btn
                    logger.info("••• Button gefunden via '%s'", sel)
                    break
            except Exception:
                continue

        # Fallback: JavaScript – alle Buttons auf der Seite finden und
        # den kleinsten / letzten im Header-Bereich wählen
        if not more_btn:
            logger.info("••• Button nicht via CSS gefunden – versuche JS-Fallback")
            try:
                # Klarna rendert den ••• Button oft als Button ohne sichtbaren Text
                # aber mit einem SVG Icon oder sehr kurzem Text
                btn_handle = page.evaluate_handle("""
                    () => {
                        const buttons = [...document.querySelectorAll('button')];
                        // Suche nach Button mit sehr kurzem Text (1-3 Zeichen = "..." o.ä.)
                        const dotBtn = buttons.find(b => {
                            const t = b.textContent.trim();
                            return t.length <= 3 && t.length >= 1 &&
                                   /^[.·•⋯…]+$/.test(t);
                        });
                        if (dotBtn) return dotBtn;
                        // Letzter Button im sichtbaren Bereich (oft oben rechts)
                        const visible = buttons.filter(b => {
                            const r = b.getBoundingClientRect();
                            return r.width > 0 && r.height > 0 && r.top < 400;
                        });
                        return visible.length > 1 ? visible[visible.length - 1] : null;
                    }
                """)
                if btn_handle:
                    more_btn = btn_handle.as_element()
                    if more_btn:
                        logger.info("••• Button via JS-Fallback gefunden")
            except Exception as exc:
                logger.warning("JS-Fallback für ••• fehlgeschlagen: %s", exc)

        if not more_btn:
            logger.warning("Kein ••• Button für %s – überspringe", txn["id"])
            return None

        # ••• klicken → Mehr-Dialog öffnen
        try:
            more_btn.click()
        except Exception as exc:
            logger.warning("••• Klick fehlgeschlagen: %s", exc)
            return None
        _sleep(0.5, 1.5)

        # "Auszug herunterladen" klicken
        auszug_btn = None
        for sel in [
            "button:has-text('Auszug herunterladen')",
            "*:has-text('Auszug herunterladen')",
            "[data-testid*='auszug']",
            "[data-testid*='download-statement']",
            "button:has-text('Herunterladen')",
            "button:has-text('Download')",
        ]:
            try:
                btn = page.wait_for_selector(sel, timeout=5_000)
                if btn and btn.is_visible():
                    auszug_btn = btn
                    logger.info("'Auszug herunterladen' via '%s'", sel)
                    break
            except Exception:
                continue

        if not auszug_btn:
            logger.warning(
                "'Auszug herunterladen' nicht im Dialog für %s", txn["id"]
            )
            # Dialog schließen
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return None

        # Download auslösen
        return self._do_download(page, auszug_btn, out_path, txn["id"])

    def _enrich_txn_from_detail(self, page: Page, txn: dict) -> None:
        """Liest Datum und Händlername von der geöffneten Detailseite."""
        try:
            text = page.inner_text("body")
            # Händlername: Tab-Titel enthält oft "HÄNDLER - Klarna"
            title = page.title()
            merchant_m = re.match(r"^(.+?)\s*[-–]\s*Klarna", title)
            if merchant_m:
                txn["merchant"] = merchant_m.group(1).strip()

            # Datum parsen: "5. Juni, 06:25" oder "30. Mai, 17:40"
            date_result = _parse_klarna_date(text)
            if date_result:
                txn["year"], txn["month"], txn["day"] = date_result
                logger.info(
                    "Datum für %s: %02d.%02d.%d",
                    txn["id"][:8], txn["day"], txn["month"], txn["year"]
                )

            # Ausstehend-Check: "Zahlung in Bearbeitung" → skip
            if "zahlung in bearbeitung" in text.lower():
                logger.info(
                    "Transaktion %s noch in Bearbeitung – Auszug eventuell unvollständig",
                    txn["id"][:8]
                )
        except Exception as exc:
            logger.debug("Anreicherung fehlgeschlagen: %s", exc)

    def _do_download(
        self, page: Page, btn, out_path: Path, txn_id: str
    ) -> Path | None:
        """Klickt den Download-Button und speichert das PDF."""
        try:
            with page.expect_download(timeout=30_000) as dl_info:
                btn.click()
            download = dl_info.value

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
                # HTTP-Download: save_as() versuchen
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
                                resp.status_code, len(content),
                            )
                    except Exception as de:
                        logger.warning("HTTP-Fallback Fehler: %s", de)

            if pdf_bytes is None or pdf_bytes[:4] != b"%PDF":
                logger.warning("Kein gültiges PDF für %s", txn_id)
                return None

            out_path.write_bytes(pdf_bytes)
            logger.info(
                "Auszug gespeichert: %s (%d bytes)", out_path.name, len(pdf_bytes)
            )
            return out_path

        except Exception as exc:
            logger.warning("Download-Fehler für %s: %s", txn_id, exc)
            return None
