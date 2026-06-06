"""
Amazon Provider – lädt Rechnungen von Amazon.de / Amazon.com herunter.

Nutzt Playwright (headless Chromium) für Browser-Automation.
Beim ersten Login: Playwright öffnet den Browser und wartet auf 2FA-Eingabe.
Danach werden Cookies gespeichert und beim nächsten Lauf wiederverwendet.
"""

from __future__ import annotations

import json
import logging
import os
import time

from pathlib import Path

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

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

    def __init__(self, config: dict):
        super().__init__(config)
        self.email = os.environ["AMAZON_EMAIL"]
        self.password = os.environ["AMAZON_PASSWORD"]
        self.domain = os.environ.get("AMAZON_DOMAIN", "amazon.de")
        self.months_back = int(os.environ.get("AMAZON_MONTHS_BACK", "12"))
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
                order_ids = self._get_order_ids(page)
                logger.info("Gefundene Bestellungen: %d", len(order_ids))

                for order_id in order_ids:
                    invoice = self._download_invoice(page, order_id)
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

        # Schon eingeloggt?
        if "order-history" in page.url or "your-orders" in page.url:
            logger.info("Amazon: bereits eingeloggt (Cookies)")
            return True

        # Login durchführen
        logger.info("Amazon: Login notwendig...")
        return self._do_login(page)

    def _do_login(self, page: Page) -> bool:
        try:
            # E-Mail eingeben
            page.goto(
                f"{self.urls['login']}?returnTo={self.urls['orders']}",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            time.sleep(1)

            email_field = page.locator("#ap_email")
            email_field.fill(self.email)
            page.locator("#continue").click()
            time.sleep(1)

            # Passwort eingeben
            page.locator("#ap_password").fill(self.password)
            page.locator("#signInSubmit").click()
            time.sleep(3)

            # 2FA / OTP Check
            if page.locator("#auth-mfa-otpcode").is_visible():
                logger.warning(
                    "⚠️  Amazon verlangt 2FA (OTP). "
                    "Bitte setze AMAZON_OTP_CODE in der .env oder nutze "
                    "ein App-Passwort. Für den ersten Login: headless=False setzen."
                )
                # Warte auf OTP aus Umgebungsvariable
                otp = os.environ.get("AMAZON_OTP_CODE", "")
                if otp:
                    page.locator("#auth-mfa-otpcode").fill(otp)
                    page.locator("#auth-signin-button").click()
                    time.sleep(3)
                else:
                    return False

            # Prüfe ob Login erfolgreich
            page.goto(self.urls["orders"], wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
            if "order-history" in page.url or "your-orders" in page.url:
                logger.info("Amazon Login erfolgreich!")
                return True

            logger.error("Amazon Login fehlgeschlagen. Aktuelle URL: %s", page.url)
            return False

        except Exception as e:
            logger.exception("Login-Fehler: %s", e)
            return False

    # ──────────────────────────────────────────────────────────────
    # Bestellungen finden
    # ──────────────────────────────────────────────────────────────

    def _get_order_ids(self, page: Page) -> list[str]:
        """Liest alle Bestellnummern der letzten N Monate."""
        order_ids: list[str] = []
        # Zeitraum-Filter setzen
        page.goto(
            f"{self.urls['orders']}?orderFilter=months-{min(self.months_back, 6)}",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        time.sleep(2)

        page_num = 0
        while True:
            page_num += 1
            logger.debug("Scanne Bestellseite %d...", page_num)

            # Bestellnummern aus der Seite extrahieren
            ids = page.locator(".yohtmlc-order-id span:last-child").all_inner_texts()
            if not ids:
                # Fallback-Selektor
                ids = page.locator("[class*='order-id'] span").all_inner_texts()

            for oid in ids:
                oid = oid.strip()
                if oid and oid not in order_ids:
                    order_ids.append(oid)

            # Nächste Seite?
            next_btn = page.locator("ul.a-pagination li.a-last a")
            if next_btn.count() == 0 or not next_btn.is_visible():
                break

            next_btn.click()
            time.sleep(2)

            # Abbruch wenn zu alt
            if page_num > 20:
                break

        return order_ids

    # ──────────────────────────────────────────────────────────────
    # Rechnungs-Download
    # ──────────────────────────────────────────────────────────────

    def _download_invoice(self, page: Page, order_id: str) -> Invoice | None:
        """Öffnet die Rechnungsseite und lädt das PDF herunter."""
        output_path = self.download_dir / f"amazon_{order_id}.pdf"

        if output_path.exists():
            logger.debug("Bereits heruntergeladen: %s", order_id)
            return Invoice(
                invoice_id=order_id,
                file_path=output_path,
                title=f"Amazon Rechnung {order_id}",
            )

        try:
            # Rechnungs-URL
            invoice_url = (
                f"{self.urls['base']}/gp/css/summary/print.html"
                f"?orderID={order_id}"
            )
            page.goto(invoice_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(1)

            # PDF via Browser-Druck generieren
            page.pdf(
                path=str(output_path),
                format="A4",
                margin={"top": "10mm", "bottom": "10mm", "left": "10mm", "right": "10mm"},
                print_background=True,
            )

            if output_path.exists() and output_path.stat().st_size > 1000:
                logger.info("✓ Rechnung heruntergeladen: %s", order_id)
                return Invoice(
                    invoice_id=order_id,
                    file_path=output_path,
                    title=f"Amazon Rechnung {order_id}",
                )
            else:
                logger.warning("PDF für %s scheint leer", order_id)
                return None

        except Exception as e:
            logger.error("Fehler beim Download von %s: %s", order_id, e)
            return None
