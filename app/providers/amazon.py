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
            "orders": "https://www.amazon.de/your-orders/orders",
            "login": "https://www.amazon.de/ap/signin",
        },
        "amazon.com": {
            "base": "https://www.amazon.com",
            "orders": "https://www.amazon.com/your-orders/orders",
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
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
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
            viewport={"width": 1280, "height": 900},
        )
        # Webdriver-Flag entfernen damit Amazon uns nicht als Bot erkennt
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
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

    def _is_login_page(self, page: Page) -> bool:
        """Erkennt ob Amazon zur Login-Seite weitergeleitet hat."""
        url = page.url
        return (
            "ap/signin" in url
            or "openid.ns" in url
            or "ap/cvf" in url
            or "signIn" in url
        )

    def _ensure_logged_in(self, page: Page) -> bool:
        """Prüft Login-Status und führt ggf. Login durch."""
        page.goto(self.urls["orders"], wait_until="networkidle", timeout=45000)
        time.sleep(2)

        if not self._is_login_page(page) and (
            "order-history" in page.url
            or "your-orders" in page.url
            or "/orders" in page.url
        ):
            logger.info("Amazon: bereits eingeloggt (Cookies)")
            return True

        logger.info("Amazon: Login notwendig...")
        otp_state.notify_login_required()
        result = self._do_login(page)
        otp_state.notify_login_done(result)
        return result

    def _do_login(self, page: Page) -> bool:
        try:
            # Zur Bestellseite – Amazon leitet selbst zur Login-Seite weiter
            # (mit allen nötigen OpenID-Parametern)
            page.goto(self.urls["orders"], wait_until="networkidle", timeout=45000)
            time.sleep(3)

            # Falls schon eingeloggt
            if not self._is_login_page(page):
                logger.info("Bereits eingeloggt nach _do_login – kein Login nötig")
                return True

            logger.info("Login-Seite erkannt: %s", page.url[-120:])

            # Diagnose: was zeigt Amazon wirklich?
            diag = page.evaluate(
                """() => ({
                    title: document.title,
                    emailVisible: !!document.querySelector('#ap_email'),
                    captcha: !!document.querySelector('#captchacharacters, .a-box-inner img[src*="captcha"]'),
                    bodySnippet: document.body.innerText.substring(0, 400)
                })"""
            )
            logger.info(
                "Login-Diagnose: title=%r | #ap_email=%s | captcha=%s",
                diag.get("title"),
                diag.get("emailVisible"),
                diag.get("captcha"),
            )
            if not diag.get("emailVisible"):
                logger.warning("Seiteninhalt:\n%s", diag.get("bodySnippet", "")[:300])
                # Screenshot für Debugging
                try:
                    screenshot_path = Path("/app/data/login_debug.png")
                    page.screenshot(path=str(screenshot_path))
                    logger.info("Debug-Screenshot: %s", screenshot_path)
                except Exception:
                    pass

            # E-Mail per JS eintippen (löst React/native Events aus)
            import json as _json_mod

            page.evaluate(f"""() => {{
                const sel = '#ap_email, input[name="email"], input[type="email"], input[type="text"], input[type="tel"]';
                const el = document.querySelector(sel);
                if (!el) return;
                const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                setter.call(el, {_json_mod.dumps(self.email)});
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
                el.dispatchEvent(new Event('blur', {{bubbles: true}}));
            }}""")
            time.sleep(0.5)

            # "Weiter" klicken oder Enter drücken
            cont_els = page.query_selector_all(
                "#continue, [name='continue'], input[type='submit'], button[type='submit']"
            )
            if cont_els:
                try:
                    cont_els[0].click()
                except Exception:
                    page.keyboard.press("Return")
            else:
                page.keyboard.press("Return")

            time.sleep(3)
            page.wait_for_load_state("networkidle")

            # Passwort per JS eintippen – umgeht visibility-Anforderung komplett
            page.evaluate(f"""() => {{
                const el = document.querySelector('#ap_password, input[name="password"], input[type="password"]');
                if (!el) return;
                // Hidden-Attribute entfernen damit Form-Submit funktioniert
                el.removeAttribute('hidden');
                el.style.cssText = '';
                const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                setter.call(el, {_json_mod.dumps(self.password)});
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
            }}""")
            time.sleep(0.5)

            # Submit per JS – kein Playwright-Click nötig
            page.evaluate("""() => {
                const btn = document.querySelector('#signInSubmit, input[id="signInSubmit"], [name="signIn"], input[type="submit"], button[type="submit"]');
                if (btn) { btn.click(); return; }
                const form = document.querySelector('form[name="signIn"], form');
                if (form) form.submit();
            }""")
            time.sleep(4)

            # 2FA / OTP (SMS oder Authenticator) – optional, falls aktiv
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

            # "Angemeldet bleiben?" / "Keep me signed in" Dialog
            for keep_sel in ["#remember_me", 'input[name="rememberMe"]']:
                try:
                    if page.locator(keep_sel).is_visible():
                        page.locator(keep_sel).check()
                        time.sleep(1)
                        break
                except Exception:
                    pass

            # Nach Login zur Bestellseite
            page.goto(self.urls["orders"], wait_until="networkidle", timeout=45000)
            time.sleep(2)
            if "order-history" in page.url or "your-orders" in page.url:
                logger.info("Amazon Login erfolgreich!")
                return True

            # Nochmal Diagnose falls Login fehlschlug
            logger.error("Login fehlgeschlagen. URL: %s", page.url)
            try:
                snippet = page.evaluate(
                    "() => document.body.innerText.substring(0, 300)"
                )
                logger.error("Seiteninhalt nach Login: %s", snippet)
            except Exception:
                pass
            return False

        except Exception as e:
            logger.exception("Login-Fehler: %s", e)
            return False

    # ──────────────────────────────────────────────────────────────
    # Rechnungs-URLs aus "Rechnung ▼" Dropdown holen
    # ──────────────────────────────────────────────────────────────

    def _get_invoice_map(self, page: Page) -> dict[str, str]:
        """
        Iteriert durch alle Jahre seit AMAZON_START_YEAR.

        Neue URL: /your-orders/orders?timeFilter=year-X
        (die alte /gp/your-account/order-history?timeFilter=year-X wurde von Amazon
        für headless Sessions blockiert – die neue URL funktioniert)
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

    def _navigate_to_filter(self, page: Page, time_filter: str) -> bool:
        """
        Navigiert zur gefilterten Bestellliste.
        Verwendet die neue Amazon-URL /your-orders/orders?timeFilter=year-X
        (die alte /gp/your-account/order-history URL wird von Amazon blockiert).
        """
        url = f"{self.urls['orders']}?timeFilter={time_filter}"
        page.goto(url, wait_until="networkidle", timeout=45000)
        time.sleep(2)
        return not self._is_login_page(page)

    def _scan_order_filter(
        self, page: Page, time_filter: str, result: dict[str, str]
    ) -> int:
        """
        Lädt eine gefilterte Bestellliste, liest alle Popover-URLs aus dem DOM
        und holt per fetch() direkt die PDF-Links – ohne Popup-Klick.
        Paginiert automatisch. Gibt die Anzahl neu gefundener Rechnungen zurück.
        """
        # Navigiere zur gefilterten Seite (Select-Dropdown bevorzugt)
        if not self._navigate_to_filter(page, time_filter):
            # Session abgelaufen → re-login
            logger.warning(
                "Session abgelaufen bei %s – versuche Re-Login...", time_filter
            )
            otp_state.notify_login_required()
            success = self._do_login(page)
            otp_state.notify_login_done(success)
            if not success:
                logger.error("Re-Login fehlgeschlagen – überspringe %s", time_filter)
                return 0
            # Nach Login nochmal zur Zielseite
            if not self._navigate_to_filter(page, time_filter):
                logger.error(
                    "Nach Re-Login immer noch kein Zugriff auf %s", time_filter
                )
                return 0

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

            if page_num > 500:
                logger.warning("Seitenlimit (500) erreicht für %s", time_filter)
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
