"""
Amazon Provider – lädt Rechnungen von Amazon.de / Amazon.com herunter.

Nutzt Playwright (headless Chromium) für Browser-Automation.
Beim ersten Login: Playwright öffnet den Browser und wartet auf 2FA-Eingabe.
Danach werden Cookies gespeichert und beim nächsten Lauf wiederverwendet.

PDF-Download-Strategie (gelernt durch direktes Inspizieren von amazon.de):
  Auf der Bestellübersicht gibt es pro Bestellung ein "Rechnung ▼" Dropdown.
  Nach dem Klick erscheint ein direkter Link:
    https://www.amazon.de/documents/download/{UUID}/invoice.pdf
  Dieser wird direkt heruntergeladen – kein Print-Dialog, kein Seitenrendering.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

from pathlib import Path

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

from app import otp_state
from app.providers import BaseProvider, Invoice

logger = logging.getLogger("provider.amazon")

COOKIES_FILE = Path("/app/data/amazon_cookies.json")


class AmazonProvider(BaseProvider):
    provider_name = "amazon"

    DOMAINS = {
        "amazon.de": {
            "base": "https://www.amazon.de",
            "orders": "https://www.amazon.de/gp/your-account/order-history",
            "login": "https://www.amazon.de/ap/signin",
        },
        "amazon.com": {
            "base": "https://www.amazon.com",
            "orders": "https://www.amazon.com/gp/your-account/order-history",
            "login": "https://www.amazon.com/ap/signin",
        },
    }

    # Amazon Bestellnummer-Muster: 3 Gruppen à 3-7-7 Ziffern
    ORDER_ID_RE = re.compile(r"\b(\d{3}-\d{7}-\d{7})\b")

    def __init__(self, config: dict):
        super().__init__(config)
        self.email = os.environ["AMAZON_EMAIL"]
        self.password = os.environ["AMAZON_PASSWORD"]
        self.domain = os.environ.get("AMAZON_DOMAIN", "amazon.de")
        self.start_year = int(os.environ.get("AMAZON_START_YEAR", "2009"))
        self.urls = self.DOMAINS.get(self.domain, self.DOMAINS["amazon.de"])

    # ──────────────────────────────────────────────────────────────
    # Öffentliche Hauptmethode
    # ──────────────────────────────────────────────────────────────

    def fetch_invoices(self) -> list[Invoice]:
        invoices: list[Invoice] = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = self._create_context(browser)
            page = context.new_page()

            try:
                if not self._ensure_logged_in(page):
                    logger.error("Amazon Login fehlgeschlagen – überspringe Provider")
                    return []

                self._save_cookies(context)

                # Bestellungen + direkte PDF-URLs aus dem "Rechnung ▼" Dropdown holen
                invoice_map = self._get_invoice_map(page)
                logger.info("Rechnungen gefunden: %d", len(invoice_map))

                for order_id, pdf_url in invoice_map.items():
                    invoice = self._download_pdf(page, order_id, pdf_url)
                    if invoice:
                        invoices.append(invoice)

            except Exception as e:
                logger.exception("Unerwarteter Fehler beim Amazon-Fetch: %s", e)
            finally:
                browser.close()

        return invoices

    # ──────────────────────────────────────────────────────────────
    # Login & Session
    # ──────────────────────────────────────────────────────────────

    def _create_context(self, browser: Browser) -> BrowserContext:
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            accept_downloads=True,
            locale="de-DE",
        )
        if COOKIES_FILE.exists():
            try:
                cookies = json.loads(COOKIES_FILE.read_text())
                context.add_cookies(cookies)
                logger.info("Gespeicherte Cookies geladen")
            except Exception:
                logger.warning("Cookies konnten nicht geladen werden")
        return context

    def _save_cookies(self, context: BrowserContext) -> None:
        try:
            cookies = context.cookies()
            COOKIES_FILE.write_text(json.dumps(cookies))
            logger.debug("Cookies gespeichert")
        except Exception as e:
            logger.warning("Cookies konnten nicht gespeichert werden: %s", e)

    def _ensure_logged_in(self, page: Page) -> bool:
        """Prüft Login-Status und führt ggf. Login durch."""
        page.goto(self.urls["orders"], wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)

        if "order-history" in page.url or "your-orders" in page.url:
            logger.info("Amazon: bereits eingeloggt (Cookies)")
            return True

        logger.info("Amazon: Login notwendig...")
        return self._do_login(page)

    def _do_login(self, page: Page) -> bool:
        try:
            page.goto(
                f"{self.urls['login']}?returnTo={self.urls['orders']}",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            time.sleep(1)

            page.locator("#ap_email").fill(self.email)
            page.locator("#continue").click()
            time.sleep(1)

            page.locator("#ap_password").fill(self.password)
            page.locator("#signInSubmit").click()
            time.sleep(3)

            # 2FA / OTP (SMS oder Authenticator)
            if page.locator("#auth-mfa-otpcode").is_visible():
                otp = os.environ.get("AMAZON_OTP_CODE", "")
                if not otp:
                    logger.warning(
                        "⚠️  Amazon verlangt 2FA – warte auf SMS-Code über Web-UI (5 Min)..."
                    )
                    otp = otp_state.request_otp(timeout=300)
                if otp:
                    page.locator("#auth-mfa-otpcode").fill(otp)
                    remember = page.locator("#auth-rememberme-checkbox")
                    if remember.is_visible():
                        remember.check()
                    page.locator("#auth-signin-button").click()
                    time.sleep(3)
                else:
                    logger.error("Kein OTP eingegeben – Login abgebrochen")
                    return False

            page.goto(self.urls["orders"], wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
            if "order-history" in page.url or "your-orders" in page.url:
                logger.info("Amazon Login erfolgreich!")
                return True

            logger.error("Login fehlgeschlagen. URL: %s", page.url)
            return False

        except Exception as e:
            logger.exception("Login-Fehler: %s", e)
            return False

    # ──────────────────────────────────────────────────────────────
    # Rechnungs-URLs aus "Rechnung ▼" Dropdown holen
    # ──────────────────────────────────────────────────────────────

    def _get_invoice_map(self, page: Page) -> dict[str, str]:
        """
        Iteriert durch alle Jahre seit AMAZON_START_YEAR und sammelt pro Bestellung
        den direkten PDF-Link.

        Verifiziert auf amazon.de:
          - URL-Parameter: ?timeFilter=year-2024  (NICHT orderFilter!)
          - "Rechnung" Link führt zu /your-orders/invoice/popover?orderId=...
          - Der Popover-Endpunkt liefert HTML mit dem echten documents/download Link
          - Wird direkt per fetch() geholt – kein Popup-Klick nötig
        """
        import datetime

        result: dict[str, str] = {}
        current_year = datetime.date.today().year

        years = list(range(current_year, self.start_year - 1, -1))
        logger.info(
            "Scanne %d Jahre (%d–%d)...", len(years), self.start_year, current_year
        )

        for year in years:
            time_filter = f"year-{year}"
            found = self._scan_order_filter(page, time_filter, result)
            logger.info(
                "Jahr %d: %d neue Rechnungen (gesamt: %d)", year, found, len(result)
            )

        return result

    def _scan_order_filter(
        self, page: Page, time_filter: str, result: dict[str, str]
    ) -> int:
        """
        Lädt eine gefilterte Bestellliste, liest alle Popover-URLs aus dem DOM
        und holt per fetch() direkt die PDF-Links – ohne Popup-Klick.
        Paginiert automatisch. Gibt die Anzahl neu gefundener Rechnungen zurück.
        """
        url = f"{self.urls['orders']}?timeFilter={time_filter}"
        page.goto(url, wait_until="networkidle", timeout=45000)
        time.sleep(3)

        found_new = 0
        page_num = 0

        while True:
            page_num += 1

            # Diagnose: was ist auf der Seite?
            diag = page.evaluate(
                """() => ({
                    url: location.href,
                    popoverLinks: document.querySelectorAll('a[href*="invoice/popover"]').length,
                    orderCards: document.querySelectorAll('.order-card, [class*="order-card"]').length,
                    bodySnippet: document.body.innerText.substring(0, 300)
                })"""
            )
            logger.info(
                "Seite %d (%s): URL=%s | popoverLinks=%d | orderCards=%d",
                page_num,
                time_filter,
                diag.get("url", "?")[-60:],
                diag.get("popoverLinks", 0),
                diag.get("orderCards", 0),
            )
            if diag.get("popoverLinks", 0) == 0:
                logger.info(
                    "Seiteninhalt (Anfang): %s", diag.get("bodySnippet", "")[:200]
                )

            # Sammle alle Popover-URLs + Bestellnummern in einem JS-Call
            items = page.evaluate(
                """() => {
                    const results = [];
                    const links = document.querySelectorAll('a[href*="invoice/popover"]');
                    for (const a of links) {
                        const card = a.closest(
                            '.order-card, [class*="order-card"], .a-box-group, ' +
                            '.order-header, [class*="order"]'
                        );
                        const html = card ? card.innerHTML : document.body.innerHTML;
                        const m = html.match(/\\d{3}-\\d{7}-\\d{7}/);
                        if (m) {
                            results.push({ orderId: m[0], popoverUrl: a.href });
                        }
                    }
                    return results;
                }"""
            )

            if not items:
                logger.info(
                    "Keine Bestellungen auf Seite %d (%s) – weiter",
                    page_num,
                    time_filter,
                )
                break

            logger.debug(
                "%d Bestellungen auf Seite %d (%s)", len(items), page_num, time_filter
            )

            for item in items:
                order_id = (
                    item["order_id"] if "order_id" in item else item.get("orderId")
                )
                popover_url = item.get("popoverUrl") or item.get("popover_url")

                if not order_id or order_id in result:
                    continue

                # PDF-Link via fetch() aus dem Popover-HTML holen
                pdf_url = page.evaluate(
                    """async (url) => {
                        try {
                            const resp = await fetch(url, { credentials: 'include' });
                            const html = await resp.text();
                            const m = html.match(
                                /href="([^"]*documents\\/download[^"]*invoice\\.pdf[^"]*)"/
                            );
                            return m ? m[1] : null;
                        } catch(e) {
                            return null;
                        }
                    }""",
                    popover_url,
                )

                if pdf_url:
                    result[order_id] = pdf_url
                    found_new += 1
                    logger.debug("PDF: %s → %s", order_id, pdf_url[:60])
                else:
                    logger.debug(
                        "Kein PDF für %s (kein Rechnungs-Link im Popover)", order_id
                    )

            # Nächste Seite?
            next_btn = page.locator("ul.a-pagination li.a-last a")
            if next_btn.count() == 0 or not next_btn.is_visible():
                break
            next_btn.click()
            time.sleep(2)

            if page_num > 50:
                logger.warning("Seitenlimit (50) erreicht für %s", time_filter)
                break

        return found_new

    # ──────────────────────────────────────────────────────────────
    # PDF direkt herunterladen
    # ──────────────────────────────────────────────────────────────

    def _download_pdf(self, page: Page, order_id: str, pdf_url: str) -> Invoice | None:
        """Lädt das PDF direkt über die authentifizierte Session herunter."""
        output_path = self.download_dir / f"amazon_{order_id}.pdf"

        if output_path.exists() and output_path.stat().st_size > 1000:
            logger.debug("Bereits vorhanden: %s", order_id)
            return Invoice(
                invoice_id=order_id,
                file_path=output_path,
                title=f"Amazon Rechnung {order_id}",
            )

        try:
            # PDF-URL direkt aufrufen – Playwright hat die Session-Cookies
            response = page.goto(pdf_url, wait_until="load", timeout=30000)
            if response and response.ok:
                pdf_bytes = response.body()
                if len(pdf_bytes) > 1000:
                    output_path.write_bytes(pdf_bytes)
                    logger.info(
                        "✓ PDF heruntergeladen: %s (%d KB)",
                        order_id,
                        len(pdf_bytes) // 1024,
                    )
                    return Invoice(
                        invoice_id=order_id,
                        file_path=output_path,
                        title=f"Amazon Rechnung {order_id}",
                    )
                else:
                    logger.warning(
                        "PDF für %s zu klein (%d bytes)", order_id, len(pdf_bytes)
                    )
            else:
                logger.warning(
                    "HTTP %s für %s", response.status if response else "?", order_id
                )
        except Exception as e:
            logger.error("Download fehlgeschlagen %s: %s", order_id, e)

        return None
