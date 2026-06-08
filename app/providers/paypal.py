"""
PayPal Provider – lädt monatliche Kontoauszüge (PDF) von PayPal herunter.

Ablauf:
  1. Login mit E-Mail + Passwort (+ SMS/Authenticator OTP falls nötig)
  2. Session in Cookies speichern → nächster Lauf braucht keinen Login mehr
  3. Für jeden Monat rückwirkend bis PAYPAL_MONTHS_BACK:
     – falls noch nicht heruntergeladen → Kontoauszug als PDF speichern
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import random
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

from app import database, otp_state
from app.providers import BaseProvider, Invoice

logger = logging.getLogger("provider.paypal")

COOKIES_FILE = Path("/app/data/paypal_cookies.json")

# Xvfb – virtuelles Display für headless-freies Rendering
try:
    from pyvirtualdisplay import Display as _XvfbDisplay

    _HAS_XVFB = True
except ImportError:
    _HAS_XVFB = False


def _sleep(min_s: float = 1.5, max_s: float = 3.5) -> None:
    time.sleep(random.uniform(min_s, max_s))


class PaypalProvider(BaseProvider):
    provider_name = "paypal"

    LOGIN_URL = "https://www.paypal.com/signin"
    STATEMENTS_URL = "https://www.paypal.com/myaccount/statement/"
    ACTIVITY_URL = "https://www.paypal.com/myaccount/activity/"

    def __init__(self, config: dict):
        super().__init__(config)
        self.email = os.environ.get("PAYPAL_EMAIL", "")
        self.password = os.environ.get("PAYPAL_PASSWORD", "")
        self.months_back = int(os.environ.get("PAYPAL_MONTHS_BACK") or "12")

    # ──────────────────────────────────────────────────────────────
    # Browser-Lifecycle
    # ──────────────────────────────────────────────────────────────

    def _launch(self) -> tuple[object | None, Browser, BrowserContext]:
        """Startet Xvfb + Playwright. Gibt (display, browser, context) zurück."""
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
        )
        # Automation-Merkmale verstecken
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = {runtime: {}};
        """)
        return display, browser, context

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
            logger.warning("Cookies konnten nicht geladen werden: %s", e)
            return False

    # ──────────────────────────────────────────────────────────────
    # Login
    # ──────────────────────────────────────────────────────────────

    def _is_logged_in(self, page: Page) -> bool:
        """Prüft ob PayPal-Session aktiv ist."""
        try:
            page.goto("https://www.paypal.com/myaccount/summary/", timeout=20_000)
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
            url = page.url
            logged_in = "/myaccount/" in url and "/signin" not in url
            logger.info("Login-Check: %s → %s", url[:60], "✓" if logged_in else "✗")
            return logged_in
        except Exception as e:
            logger.warning("Login-Check fehlgeschlagen: %s", e)
            return False

    def _dismiss_cookie_banner(self, page: Page) -> None:
        """Klickt Cookie-Banner / DSGVO-Overlays weg falls vorhanden."""
        selectors = [
            "button[id*='accept'], button[id*='cookie'], button[id*='consent']",
            "button:has-text('Alle akzeptieren')",
            "button:has-text('Accept All')",
            "button:has-text('Akzeptieren')",
            "button:has-text('Zustimmen')",
            "#acceptAllButton",
            "[data-testid='accept-all-button']",
        ]
        for sel in selectors:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click()
                    logger.info("Cookie-Banner weggeklickt: %s", sel)
                    _sleep(0.5, 1)
                    return
            except Exception:
                continue

    def _login(self, page: Page) -> bool:
        """Vollständiger Login-Flow mit E-Mail, Passwort und optionalem OTP."""
        if not self.email or not self.password:
            logger.error("PAYPAL_EMAIL oder PAYPAL_PASSWORD nicht gesetzt")
            return False

        logger.info("Starte PayPal-Login für %s", self.email)
        page.goto(self.LOGIN_URL, timeout=30_000)
        page.wait_for_load_state("domcontentloaded")
        _sleep(2, 4)

        # Cookie-Banner wegklicken
        self._dismiss_cookie_banner(page)

        # E-Mail-Selektoren – PayPal ändert diese regelmäßig
        EMAIL_SELECTORS = (
            "#email",
            "#splitEmail",
            "input[name='login_email']",
            "input[type='email']",
            "input[placeholder*='E-Mail']",
            "input[placeholder*='email']",
            "input[placeholder*='Email']",
            "input[autocomplete='email']",
            "input[autocomplete='username']",
        )

        # E-Mail eingeben
        email_field = None
        for sel in EMAIL_SELECTORS:
            try:
                email_field = page.wait_for_selector(sel, timeout=5_000)
                if email_field and email_field.is_visible():
                    logger.info("E-Mail-Feld gefunden: %s", sel)
                    break
                email_field = None
            except Exception:
                continue

        if not email_field:
            # Debug: Screenshot + alle sichtbaren Inputs loggen
            try:
                screenshot_path = Path("/app/data/paypal_debug_email.png")
                page.screenshot(path=str(screenshot_path))
                logger.error("Debug-Screenshot gespeichert: %s", screenshot_path)
            except Exception:
                pass
            inputs = page.query_selector_all("input")
            logger.error(
                "E-Mail-Feld nicht gefunden. Alle Inputs: %s | URL: %s",
                [
                    f"id={i.get_attribute('id') or '-'} "
                    f"name={i.get_attribute('name') or '-'} "
                    f"type={i.get_attribute('type') or '-'} "
                    f"visible={i.is_visible()}"
                    for i in inputs[:10]
                ],
                page.url[:80],
            )
            return False

        try:
            email_field.fill(self.email)
            _sleep(0.5, 1.5)

            # "Weiter"-Button
            next_btn = page.query_selector(
                "#btnNext, button[type='submit'], "
                "button:has-text('Weiter'), button:has-text('Next')"
            )
            if next_btn and next_btn.is_visible():
                next_btn.click()
            else:
                email_field.press("Enter")
            _sleep(2, 4)
        except Exception as e:
            logger.error("E-Mail-Eingabe fehlgeschlagen: %s", e)
            return False

        # Cookie-Banner ggf. nochmal (nach Seitenübergang)
        self._dismiss_cookie_banner(page)

        # Passwort-Selektoren
        PWD_SELECTORS = (
            "#password",
            "input[name='login_password']",
            "input[type='password']",
            "input[autocomplete='current-password']",
        )

        pwd_field = None
        for sel in PWD_SELECTORS:
            try:
                pwd_field = page.wait_for_selector(sel, timeout=8_000)
                if pwd_field and pwd_field.is_visible():
                    logger.info("Passwort-Feld gefunden: %s", sel)
                    break
                pwd_field = None
            except Exception:
                continue

        if not pwd_field:
            logger.error("Passwort-Feld nicht gefunden. URL: %s", page.url[:80])
            return False

        try:
            pwd_field.fill(self.password)
            _sleep(0.5, 1.5)

            login_btn = page.query_selector(
                "#btnLogin, button[type='submit'], "
                "button:has-text('Einloggen'), button:has-text('Log In'), "
                "button:has-text('Anmelden')"
            )
            if login_btn and login_btn.is_visible():
                login_btn.click()
            else:
                pwd_field.press("Enter")
            _sleep(3, 5)
        except Exception as e:
            logger.error("Passwort-Eingabe fehlgeschlagen: %s", e)
            return False

        # OTP / 2FA prüfen
        for attempt in range(60):
            url = page.url
            if "/myaccount/" in url and "/signin" not in url and "/auth" not in url:
                logger.info("PayPal Login erfolgreich")
                return True

            # SMS / Authenticator Code?
            otp_input = page.query_selector(
                "input[id*='otp'], input[id*='code'], input[placeholder*='Code'], "
                "input[autocomplete='one-time-code']"
            )
            if otp_input:
                logger.info("OTP-Feld erkannt – warte auf Code…")
                otp_state.request_otp()
                for _ in range(90):  # max 90s warten
                    time.sleep(1)
                    code = otp_state.get_otp()
                    if code:
                        otp_state.clear_otp()
                        otp_input.fill(code)
                        _sleep(0.5, 1)
                        submit = page.query_selector(
                            "button[type='submit'], button:has-text('Bestätigen'), button:has-text('Confirm')"
                        )
                        if submit:
                            submit.click()
                        else:
                            otp_input.press("Enter")
                        _sleep(3, 5)
                        break
                continue

            # "Später" / "Überspringen" bei optionaler 2FA
            skip_btn = page.query_selector(
                "button:has-text('Später'), a:has-text('Später'), "
                "button:has-text('Skip'), a:has-text('Skip'), "
                "button:has-text('Nicht jetzt'), button:has-text('Not now')"
            )
            if skip_btn:
                logger.info("Optionale 2FA übersprungen")
                skip_btn.click()
                _sleep(2, 3)
                continue

            time.sleep(2)

        logger.error("Login nach 120s fehlgeschlagen – aktuelle URL: %s", page.url[:80])
        return False

    # ──────────────────────────────────────────────────────────────
    # Kontoauszüge herunterladen
    # ──────────────────────────────────────────────────────────────

    def _months_to_scan(self) -> list[tuple[int, int]]:
        """Gibt eine Liste von (Jahr, Monat) zurück – neueste zuerst."""
        today = datetime.date.today()
        months = []
        for i in range(self.months_back):
            # Monats-Offset rückwärts
            month = today.month - i
            year = today.year
            while month <= 0:
                month += 12
                year -= 1
            months.append((year, month))
        return months

    def _statement_id(self, year: int, month: int) -> str:
        return f"statement_{year:04d}_{month:02d}"

    def _download_statement(self, page: Page, year: int, month: int) -> Path | None:
        """
        Versucht den Kontoauszug für year/month von PayPal zu laden.
        Gibt den Dateipfad zurück oder None bei Fehler.
        """
        # PayPal Statement-Download-URL
        # Letzter Tag des Monats
        import calendar

        last_day = calendar.monthrange(year, month)[1]
        start = f"{year:04d}-{month:02d}-01"
        end = f"{year:04d}-{month:02d}-{last_day:02d}"

        dest = self.download_dir / f"paypal_statement_{year:04d}_{month:02d}.pdf"

        # Versuche direkte Download-URL
        download_url = (
            f"https://www.paypal.com/myaccount/statement/download"
            f"?startDate={start}&endDate={end}&format=PDF"
        )

        logger.info("Lade Kontoauszug %04d/%02d …", year, month)

        try:
            # Download via Playwright-Download-Event
            with page.expect_download(timeout=30_000) as dl_info:
                page.goto(download_url, timeout=30_000)

            download = dl_info.value
            download.save_as(str(dest))

            # Prüfen ob echte PDF (nicht leere Fehlerseite)
            if dest.stat().st_size < 500:
                logger.warning(
                    "Kontoauszug %04d/%02d zu klein (%d Bytes) – vermutlich leer",
                    year,
                    month,
                    dest.stat().st_size,
                )
                dest.unlink(missing_ok=True)
                return None

            logger.info(
                "Kontoauszug %04d/%02d gespeichert (%d KB)",
                year,
                month,
                dest.stat().st_size // 1024,
            )
            return dest

        except Exception:
            # Fallback: Statements-Seite aufrufen und Link suchen
            return self._download_via_statements_page(page, year, month, dest)

    def _download_via_statements_page(
        self, page: Page, year: int, month: int, dest: Path
    ) -> Path | None:
        """Fallback: Statements-Übersichtsseite → PDF-Link suchen."""
        try:
            page.goto(self.STATEMENTS_URL, timeout=20_000)
            page.wait_for_load_state("domcontentloaded")
            _sleep(2, 3)

            # Monatsnamen auf Deutsch
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
            month_name = _DE_MONTHS[month - 1]
            year_str = str(year)

            # Zeile mit Jahr+Monat finden → PDF-Link daneben klicken
            selectors = [
                f"a:has-text('{month_name} {year_str}')",
                f"a:has-text('{month_name[:3]}. {year_str}')",
                f"*:has-text('{month_name} {year_str}') >> a[href*='pdf']",
                f"*:has-text('{month_name} {year_str}') >> a[href*='download']",
                f"*:has-text('{month_name} {year_str}') >> button",
            ]

            for sel in selectors:
                try:
                    link = page.query_selector(sel)
                    if not link:
                        continue
                    with page.expect_download(timeout=20_000) as dl_info:
                        link.click()
                    download = dl_info.value
                    download.save_as(str(dest))
                    if dest.stat().st_size >= 500:
                        logger.info(
                            "Kontoauszug %04d/%02d via Statements-Seite (%d KB)",
                            year,
                            month,
                            dest.stat().st_size // 1024,
                        )
                        return dest
                    dest.unlink(missing_ok=True)
                except Exception:
                    continue

            logger.info("Kein Kontoauszug für %04d/%02d gefunden", year, month)
            return None

        except Exception as e:
            logger.warning(
                "Statements-Seite für %04d/%02d fehlgeschlagen: %s", year, month, e
            )
            return None

    # ──────────────────────────────────────────────────────────────
    # fetch_invoices – Hauptmethode
    # ──────────────────────────────────────────────────────────────

    def fetch_invoices(self) -> list[Invoice]:
        invoices: list[Invoice] = []
        display = None

        try:
            display, browser, context = self._launch()
            page = context.new_page()

            # Cookies laden → ggf. direkt eingeloggt
            self._load_cookies(context)

            if not self._is_logged_in(page):
                if not self._login(page):
                    logger.error("PayPal Login fehlgeschlagen – Abbruch")
                    return []
                self._save_cookies(context)
            else:
                logger.info("PayPal: bereits eingeloggt (Cookies)")

            months = self._months_to_scan()
            logger.info(
                "Prüfe %d Monate (bis %d/%02d zurück)",
                len(months),
                months[-1][0],
                months[-1][1],
            )

            for year, month in months:
                stmt_id = self._statement_id(year, month)

                # Bereits in DB → überspringen
                if database.is_processed("paypal", stmt_id):
                    logger.debug("Bereits vorhanden: %s", stmt_id)
                    continue

                # Laufender Monat noch nicht vollständig → Vormonat ab 5. überspringen
                today = datetime.date.today()
                if year == today.year and month == today.month:
                    if today.day < 5:
                        logger.info(
                            "Laufender Monat (%04d/%02d) noch nicht vollständig – überspringe",
                            year,
                            month,
                        )
                        continue

                pdf_path = self._download_statement(page, year, month)
                if pdf_path is None:
                    continue

                import calendar

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
                last_day = calendar.monthrange(year, month)[1]
                invoices.append(
                    Invoice(
                        invoice_id=stmt_id,
                        file_path=pdf_path,
                        title=f"PayPal Kontoauszug {_DE_MONTHS[month - 1]} {year}",
                        date=f"{year:04d}-{month:02d}-{last_day:02d}",
                        amount=None,
                    )
                )
                _sleep(1, 2)

            # Cookies nach erfolgreichem Lauf aktualisieren
            self._save_cookies(context)

            logger.info("PayPal: %d neue Kontoauszüge gefunden", len(invoices))
            browser.close()

        except Exception as e:
            logger.exception("PayPal Provider fehlgeschlagen: %s", e)
        finally:
            if display:
                try:
                    display.stop()
                except Exception:
                    pass

        return invoices
