"""
IKEA Provider – lädt Kassenbons von IKEA herunter.

Gelernter Flow (direkt auf ikea.com/de/de beobachtet):
  1. Login → https://www.ikea.com/de/de/profile/login/
     → Bei Erfolg: Redirect zu /de/de/loyalty-hub/
  2. Bestellliste: https://www.ikea.com/de/de/purchases/
     → <a href="/de/de/purchases/{ORDER_ID}/"> mit Datum, Betrag, Typ im Text
  3. Bestelldetail: /de/de/purchases/{ORDER_ID}/
     → Rechts: "Aktionen" Panel mit "Kassenbon & Rechnung" Button
  4. Klick "Kassenbon & Rechnung" → Side-Panel öffnet sich
  5. Klick "Kassenbon herunterladen" → PDF-Download via page.expect_download()
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright

from app import database
from app.providers import BaseProvider, Invoice

logger = logging.getLogger("provider.ikea")

COOKIES_FILE = Path("/app/data/ikea_cookies.json")
LOGIN_URL = "https://www.ikea.com/de/de/profile/login/"
PURCHASES_URL = "https://www.ikea.com/de/de/purchases/"

# Xvfb – virtuelles Display
try:
    from pyvirtualdisplay import Display as _XvfbDisplay

    _HAS_XVFB = True
except ImportError:
    _HAS_XVFB = False


def _sleep(min_s: float = 1.0, max_s: float = 2.5) -> None:
    time.sleep(random.uniform(min_s, max_s))


class IkeaProvider(BaseProvider):
    provider_name = "ikea"

    def __init__(self, config: dict):
        super().__init__(config)
        self.email = os.environ.get("IKEA_EMAIL", "")
        self.password = os.environ.get("IKEA_PASSWORD", "")
        self.months_back = int(os.environ.get("IKEA_MONTHS_BACK") or "12")

    # ── Browser ────────────────────────────────────────────────────

    def _launch(self) -> tuple:
        display = None
        if _HAS_XVFB:
            display = _XvfbDisplay(visible=False, size=(1280, 900))
            display.start()
            logger.info("Xvfb gestartet")

        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="de-DE",
            timezone_id="Europe/Berlin",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            accept_downloads=True,
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        return display, pw, browser, context

    def _save_cookies(self, context: BrowserContext) -> None:
        cookies = context.cookies()
        COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        COOKIES_FILE.write_text(json.dumps(cookies, ensure_ascii=False, indent=2))
        logger.info("Cookies gespeichert (%d)", len(cookies))

    def _load_cookies(self, context: BrowserContext) -> bool:
        if not COOKIES_FILE.exists():
            return False
        try:
            cookies = json.loads(COOKIES_FILE.read_text())
            context.add_cookies(cookies)
            logger.info("Cookies geladen (%d)", len(cookies))
            return True
        except Exception as e:
            logger.warning("Cookies nicht ladbar: %s", e)
            return False

    # ── Login ──────────────────────────────────────────────────────

    def _is_logged_in(self, page: Page) -> bool:
        try:
            page.goto(LOGIN_URL, timeout=30_000)
            page.wait_for_load_state("networkidle", timeout=20_000)
            url = page.url
            # Wenn IKEA eingeloggt ist, leitet es zur loyalty-hub oder purchases um
            logged_in = "/profile/login" not in url
            logger.info("Login-Check: %s → %s", url[:70], "✓" if logged_in else "✗")
            return logged_in
        except Exception as e:
            logger.warning("Login-Check Fehler: %s", e)
            return False

    def _dismiss_overlays(self, page: Page) -> None:
        for sel in [
            "button:has-text('Alle akzeptieren')",
            "button:has-text('Accept All')",
            "#onetrust-accept-btn-handler",
            "[data-testid='accept-all-button']",
        ]:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click()
                    _sleep(0.5, 1)
                    return
            except Exception:
                continue

    def _login(self, page: Page) -> bool:
        if not self.email or not self.password:
            logger.error("IKEA_EMAIL / IKEA_PASSWORD nicht gesetzt")
            return False

        logger.info("IKEA Login für %s", self.email)
        page.goto(LOGIN_URL, timeout=30_000)
        page.wait_for_load_state("networkidle", timeout=20_000)
        _sleep(2, 3)
        self._dismiss_overlays(page)

        # Bereits eingeloggt?
        if "/profile/login" not in page.url:
            logger.info("Bereits eingeloggt (Redirect erkannt)")
            return True

        # E-Mail Feld
        email_field = None
        for sel in [
            "#username",
            "input[type='email']",
            "input[name='username']",
            "input[autocomplete='email']",
            "input[autocomplete='username']",
        ]:
            try:
                f = page.wait_for_selector(sel, timeout=5_000)
                if f and f.is_visible():
                    email_field = f
                    logger.info("E-Mail Feld: %s", sel)
                    break
            except Exception:
                continue

        if not email_field:
            page.screenshot(path="/app/data/ikea_debug_login.png", full_page=True)
            logger.error("Kein E-Mail-Feld gefunden – Screenshot gespeichert")
            return False

        email_field.fill(self.email)
        _sleep(0.5, 1)

        # Weiter-Button
        for sel in [
            "button[type='submit']",
            "button:has-text('Weiter')",
            "button:has-text('Continue')",
            "button:has-text('Fortfahren')",
        ]:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click()
                    break
            except Exception:
                continue
        else:
            email_field.press("Enter")

        _sleep(2, 3)

        # Passwort Feld
        pwd_field = None
        for sel in [
            "#password",
            "input[type='password']",
            "input[name='password']",
        ]:
            try:
                f = page.wait_for_selector(sel, timeout=5_000)
                if f and f.is_visible():
                    pwd_field = f
                    logger.info("Passwort Feld: %s", sel)
                    break
            except Exception:
                continue

        if not pwd_field:
            logger.error("Kein Passwort-Feld gefunden")
            return False

        pwd_field.fill(self.password)
        _sleep(0.5, 1)

        for sel in [
            "button[type='submit']",
            "button:has-text('Anmelden')",
            "button:has-text('Einloggen')",
            "button:has-text('Sign in')",
        ]:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click()
                    break
            except Exception:
                continue
        else:
            pwd_field.press("Enter")

        page.wait_for_load_state("networkidle", timeout=30_000)
        _sleep(2, 3)

        logged_in = "/profile/login" not in page.url
        if logged_in:
            self._save_cookies(context=page.context)
        logger.info(
            "Login %s: %s",
            "erfolgreich" if logged_in else "fehlgeschlagen",
            page.url[:70],
        )
        return logged_in

    # ── Bestellliste ───────────────────────────────────────────────

    def _parse_orders(self, page: Page) -> list[dict]:
        """Liest alle Bestellungen aus der Übersichtsseite."""
        page.goto(PURCHASES_URL, timeout=30_000)
        page.wait_for_load_state("networkidle", timeout=20_000)
        _sleep(2, 3)

        orders = []
        seen: set[str] = set()

        # Links zu Bestelldetails: <a href="/de/de/purchases/{ID}/">
        # Text-Inhalt: "30.05.2026\n\n168,34 €\n\nCash & Carry"
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
        page.wait_for_load_state("networkidle", timeout=20_000)
        _sleep(2, 3)

        # "Kassenbon & Rechnung" Button im Aktionen-Panel klicken
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

        # "Kassenbon herunterladen" im Side-Panel
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
                    download.save_as(str(out_path))
                    logger.info("Kassenbon gespeichert: %s", out_path.name)
                    return out_path
            except Exception as e:
                logger.warning("Download fehlgeschlagen (%s): %s", sel, e)
                continue

        logger.warning(
            "'Kassenbon herunterladen' Button nicht gefunden für %s", order["id"]
        )
        return None

    # ── Hauptmethode ───────────────────────────────────────────────

    def fetch_invoices(self) -> list[Invoice]:
        display, pw, browser, context = self._launch()
        invoices: list[Invoice] = []

        try:
            page = context.new_page()
            self._load_cookies(context)

            if not self._is_logged_in(page):
                if not self._login(page):
                    logger.error("IKEA Login fehlgeschlagen – abbruch")
                    return []

            # Jahr-Filter
            years_filter: set[int] | None = None
            yf = os.environ.get("PAPERFLOW_YEARS_FILTER", "").strip()
            if yf:
                years_filter = {int(y) for y in yf.split(",") if y.strip().isdigit()}

            orders = self._parse_orders(page)

            for order in orders:
                if years_filter and order["year"] not in years_filter:
                    logger.info(
                        "Überspringe %s (Jahr %d nicht im Filter)",
                        order["id"],
                        order["year"],
                    )
                    continue

                invoice_id = f"ikea_{order['id']}"
                if database.invoice_exists(invoice_id):
                    logger.info("Bereits verarbeitet: %s", invoice_id)
                    continue

                pdf_path = self._download_receipt(page, order)
                if pdf_path and pdf_path.exists():
                    date_str = (
                        f"{order['year']}-{order['month']:02d}-{order['day']:02d}"
                    )
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

            self._save_cookies(context)

        except Exception:
            logger.exception("IKEA Provider Fehler")
        finally:
            try:
                browser.close()
            except Exception:
                pass
            try:
                pw.stop()
            except Exception:
                pass
            if display:
                try:
                    display.stop()
                except Exception:
                    pass

        logger.info("IKEA: %d Rechnungen gefunden", len(invoices))
        return invoices
