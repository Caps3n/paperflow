"""
Amazon Provider – lädt Rechnungen von Amazon.de / Amazon.com herunter.

Zwei Modi:
  CDP-Modus (bevorzugt):
    Verbindet sich mit einem echten Chrome-Browser via Remote-Debugging (CDP).
    Der Browser läuft im paperflow-chrome Container und ist über noVNC zugänglich.
    Der Nutzer loggt sich einmalig manuell ein → Session bleibt dauerhaft erhalten.
    Setzt CHROME_CDP_URL=http://paperflow-chrome:9222 in der Umgebung voraus.

  Fallback-Modus:
    Startet einen eigenen Chromium-Browser (headless oder mit Xvfb).
    Nutzt playwright-stealth und Virtual Authenticator.
"""

from __future__ import annotations

import base64
import datetime
import json
import logging
import os
import random
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page
from playwright_stealth import stealth as stealth_sync

from app import database, otp_state
from app.providers import BaseProvider, Invoice

logger = logging.getLogger("provider.amazon")

COOKIES_FILE = Path("/app/data/amazon_cookies.json")

# Deutsche Monatsnamen → Monatsnummer
_GERMAN_MONTHS = {
    "januar": 1,
    "februar": 2,
    "märz": 3,
    "april": 4,
    "mai": 5,
    "juni": 6,
    "juli": 7,
    "august": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "dezember": 12,
}

# CDP-Modus: Verbindung zu externem Chrome (chrome-desktop Container)
# Gesetzt via CHROME_CDP_URL=http://chrome-desktop:9222
_CDP_URL = os.environ.get("CHROME_CDP_URL", "").strip()

# Xvfb (pyvirtualdisplay) – Fallback falls kein CDP verfügbar
try:
    from pyvirtualdisplay import Display as _XvfbDisplay

    _HAS_XVFB = True
except ImportError:
    _HAS_XVFB = False


def _human_sleep(min_s: float = 2.0, max_s: float = 5.0) -> None:
    """Zufällige Pause wie ein echter Mensch."""
    time.sleep(random.uniform(min_s, max_s))


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
        # scan_from_year aus providers.yml hat Vorrang vor AMAZON_START_YEAR env var
        self.start_year = int(
            config.get("scan_from_year") or os.environ.get("AMAZON_START_YEAR", "2009")
        )
        self.urls = self.DOMAINS.get(self.domain, self.DOMAINS["amazon.de"])
        # Incremental-Modus: nur letzte 30 Tage scannen (für tägliche Läufe)
        self.incremental = os.environ.get("AMAZON_INCREMENTAL", "").lower() in (
            "1",
            "true",
            "yes",
        )

    # ──────────────────────────────────────────────────────────────
    # Öffentliche Hauptmethode
    # ──────────────────────────────────────────────────────────────

    def fetch_invoices(self) -> list[Invoice]:
        invoices: list[Invoice] = []

        if _CDP_URL:
            invoices = self._fetch_via_cdp()
        else:
            invoices = self._fetch_local()

        return invoices

    def _fetch_via_cdp(self) -> list[Invoice]:
        """
        CDP-Modus: Verbindet sich mit dem chrome-desktop Container.
        Der Nutzer hat sich dort einmalig manuell bei Amazon eingeloggt.
        Diese Session wird direkt weiterverwendet – kein automatischer Login nötig.
        """
        invoices: list[Invoice] = []
        logger.info("CDP-Modus: Verbinde mit Chrome auf %s", _CDP_URL)

        import urllib.request

        cdp_url = _CDP_URL

        # Wait until Chrome is ready (container start may take a moment)
        for attempt in range(30):
            try:
                urllib.request.urlopen(f"{cdp_url}/json/version", timeout=2)
                break
            except Exception:
                if attempt == 0:
                    logger.info("Waiting for Chrome CDP (%s)...", cdp_url)
                time.sleep(2)
        else:
            logger.error("Chrome CDP unreachable after 60s: %s", cdp_url)
            return []

        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp(cdp_url)
                logger.info(
                    "Chrome CDP verbunden: %d Context(s) vorhanden",
                    len(browser.contexts),
                )
            except Exception as e:
                logger.error("CDP connection failed: %s", e)
                return []

            # Bestehenden Context verwenden (hat Amazon-Session) oder neuen erstellen
            if browser.contexts:
                context = browser.contexts[0]
                logger.info("Bestehende Browser-Session übernommen")
            else:
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    locale="de-DE",
                    viewport={"width": 1280, "height": 900},
                )
                logger.info("Neuer Browser-Context erstellt")

            page = context.new_page()
            # Viewport explizit setzen – verhindert dass Amazon die mobile Website zeigt
            # (Chrome-Container kann kleines Standard-Fenster haben)
            page.set_viewport_size({"width": 1280, "height": 900})

            try:
                if not self._ensure_logged_in(page):
                    logger.error(
                        "Amazon Login fehlgeschlagen.\n"
                        "→ Öffne http://<server>:6080/vnc.html und logge dich manuell ein."
                    )
                    return []

                self._save_cookies(context)

                invoice_map = self._get_invoice_map(page)
                logger.info("Rechnungen gefunden: %d", len(invoice_map))

                for order_id, info in invoice_map.items():
                    invoice = self._download_pdf(
                        page,
                        order_id,
                        info["pdf_url"],
                        info.get("date"),
                        info.get("year"),
                        info.get("title"),
                    )
                    if invoice:
                        invoices.append(invoice)

            except Exception as e:
                logger.exception("Fehler im CDP-Modus: %s", e)
            finally:
                page.close()
                # Browser NICHT schließen – bleibt für den Nutzer sichtbar

        return invoices

    def _fetch_local(self) -> list[Invoice]:
        """
        Fallback-Modus: Startet einen eigenen Chromium-Browser.
        Wird verwendet wenn CHROME_CDP_URL nicht gesetzt ist.
        """
        invoices: list[Invoice] = []

        display = None
        use_headless = True
        if _HAS_XVFB:
            try:
                display = _XvfbDisplay(visible=False, size=(1280, 900))
                display.start()
                use_headless = False
                logger.info("Xvfb gestartet – Browser läuft als headless=False")
            except Exception as e:
                logger.warning(
                    "Xvfb konnte nicht gestartet werden: %s – fallback auf headless=True",
                    e,
                )

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=use_headless,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                context = self._create_context(browser)
                page = context.new_page()
                stealth_sync(page)
                self._setup_virtual_authenticator(page)

                try:
                    if not self._ensure_logged_in(page):
                        logger.error(
                            "Amazon Login fehlgeschlagen – überspringe Provider"
                        )
                        return []

                    self._save_cookies(context)

                    invoice_map = self._get_invoice_map(page)
                    logger.info("Rechnungen gefunden: %d", len(invoice_map))

                    for order_id, info in invoice_map.items():
                        invoice = self._download_pdf(
                            page,
                            order_id,
                            info["pdf_url"],
                            info.get("date"),
                            info.get("year"),
                            info.get("title"),
                        )
                        if invoice:
                            invoices.append(invoice)

                except Exception as e:
                    logger.exception("Unerwarteter Fehler beim Amazon-Fetch: %s", e)
                finally:
                    browser.close()
        finally:
            if display:
                try:
                    display.stop()
                except Exception:
                    pass

        return invoices

    # ──────────────────────────────────────────────────────────────
    # Login & Session
    # ──────────────────────────────────────────────────────────────

    def _setup_virtual_authenticator(self, page: Page) -> None:
        """Registriert einen virtuellen FIDO-Authenticator via CDP.
        Verhindert, dass Amazon einen Passkey-Dialog zeigt der die Automation blockiert."""
        try:
            client = page.context.new_cdp_session(page)
            client.send("WebAuthn.enable")
            client.send(
                "WebAuthn.addVirtualAuthenticator",
                {
                    "options": {
                        "protocol": "ctap2",
                        "transport": "internal",
                        "hasResidentKey": True,
                        "hasUserVerification": True,
                        "isUserVerified": True,
                        "automaticPresenceSimulation": True,
                    }
                },
            )
            logger.debug("Virtual Authenticator konfiguriert")
        except Exception as e:
            logger.warning("Virtual Authenticator konnte nicht gesetzt werden: %s", e)

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
        """Prüft Login-Status über die Startseite (menschliches Navigationsverhalten)."""
        # Zuerst zur Startseite – wie ein echter Nutzer
        page.goto(self.urls["base"], wait_until="domcontentloaded", timeout=45000)
        _human_sleep(2, 4)

        # Bereits eingeloggt? (Nav zeigt "Hallo, ..." oder "Konto und Listen")
        already_in = page.evaluate(
            """() => {
                const nav = document.querySelector('#nav-link-accountList-nav-line-1, #nav-tools');
                return nav ? nav.innerText : '';
            }"""
        )
        logger.info(
            "Nav-Text nach Startseite: %r", already_in[:80] if already_in else ""
        )

        # "Hallo, anmelden" = NICHT eingeloggt; "Hallo, Marcel" = eingeloggt
        is_logged_in = (
            bool(already_in)
            and ("hallo" in already_in.lower() or "hello" in already_in.lower())
            and "anmelden" not in already_in.lower()
            and "sign in" not in already_in.lower()
        )

        if is_logged_in:
            logger.info("Amazon: bereits eingeloggt (Cookies)")
            # Zur Bestellseite navigieren
            page.goto(self.urls["orders"], wait_until="domcontentloaded", timeout=45000)
            _human_sleep(2, 3)
            return True

        logger.info("Amazon: Login notwendig...")
        otp_state.notify_login_required()
        result = self._do_login(page)
        otp_state.notify_login_done(result)
        return result

    def _do_login(self, page: Page) -> bool:
        try:
            # Startseite (falls nicht schon dort)
            if self.urls["base"] not in page.url:
                page.goto(
                    self.urls["base"], wait_until="domcontentloaded", timeout=45000
                )
                _human_sleep(2, 3)

            logger.info("Navigiere zur Login-Seite über Menü: %s", page.url[-60:])

            # Screenshot für Debugging
            try:
                page.screenshot(path="/app/data/login_debug.png")
                logger.info("Debug-Screenshot gespeichert: /app/data/login_debug.png")
            except Exception:
                pass

            # Klick auf "Konto und Listen" / "Account & Lists" → Login-Seite
            # Amazon zeigt entweder direkt Login oder ein Hover-Menü
            signin_link = None
            for sel in [
                'a[data-nav-role="signin"]',
                "#nav-link-accountList",
                'a[href*="ap/signin"]',
                "#nav-cvs-submitSignIn",
            ]:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=2000):
                        signin_link = el
                        break
                except Exception:
                    continue

            if signin_link:
                signin_link.click()
                _human_sleep(2, 3)
                page.wait_for_load_state("networkidle", timeout=30000)
            else:
                # Direkt zur Login-URL
                page.goto(
                    f"{self.urls['login']}?openid.return_to={self.urls['orders']}",
                    wait_until="networkidle",
                    timeout=45000,
                )
                _human_sleep(2, 3)

            if not self._is_login_page(page):
                logger.info("Bereits eingeloggt nach Menü-Klick")
                page.goto(
                    self.urls["orders"], wait_until="domcontentloaded", timeout=45000
                )
                return True

            logger.info("Login-Formular erkannt: %s", page.url[-80:])

            # Warten bis das Formular vollständig geladen ist
            try:
                page.wait_for_load_state("load", timeout=15000)
            except Exception:
                pass
            _human_sleep(1, 2)

            # Screenshot für Debugging
            try:
                page.screenshot(path="/app/data/login_form_debug.png")
                logger.info("Formular-Screenshot: /app/data/login_form_debug.png")
            except Exception:
                pass

            # Diagnose: welche Felder gibt es?
            diag = page.evaluate(
                """() => ({
                    emailCount: document.querySelectorAll('#ap_email, input[name="email"], input[type="email"]').length,
                    emailVisible: !!document.querySelector('#ap_email'),
                    title: document.title,
                    bodySnippet: document.body.innerText.substring(0, 200)
                })"""
            )
            logger.info(
                "Formular-Diagnose: title=%r emailCount=%d emailVisible=%s",
                diag.get("title"),
                diag.get("emailCount", 0),
                diag.get("emailVisible"),
            )
            if diag.get("emailCount", 0) == 0:
                logger.warning(
                    "Kein E-Mail-Feld gefunden! Seite: %s",
                    diag.get("bodySnippet", "")[:150],
                )

            # E-Mail per JS setzen (umgeht Visibility-Check)
            page.evaluate(
                f"""() => {{
                    const sel = '#ap_email, input[name="email"], input[type="email"], input[type="text"]';
                    const el = document.querySelector(sel);
                    if (!el) return;
                    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                    setter.call(el, {json.dumps(self.email)});
                    el.dispatchEvent(new Event('input', {{bubbles: true}}));
                    el.dispatchEvent(new Event('change', {{bubbles: true}}));
                    el.dispatchEvent(new Event('blur', {{bubbles: true}}));
                }}"""
            )
            _human_sleep(0.5, 1.5)

            # "Weiter" / "Continue" klicken
            continued = False
            for sel in [
                "#continue",
                '[name="continue"]',
                'input[type="submit"]',
                'button[type="submit"]',
            ]:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=2000):
                        btn.click()
                        continued = True
                        break
                except Exception:
                    continue
            if not continued:
                page.keyboard.press("Return")

            _human_sleep(2, 4)
            try:
                page.wait_for_load_state("load", timeout=15000)
            except Exception:
                pass

            # Passwort per JS setzen
            page.evaluate(
                f"""() => {{
                    const el = document.querySelector('#ap_password, input[name="password"], input[type="password"]');
                    if (!el) return;
                    el.removeAttribute('hidden');
                    el.style.cssText = '';
                    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                    setter.call(el, {json.dumps(self.password)});
                    el.dispatchEvent(new Event('input', {{bubbles: true}}));
                    el.dispatchEvent(new Event('change', {{bubbles: true}}));
                }}"""
            )
            _human_sleep(0.5, 1.5)

            # "Anmelden" / "Sign in" klicken
            signed = False
            for sel in [
                "#signInSubmit",
                '[name="signIn"]',
                'input[type="submit"]',
                'button[type="submit"]',
            ]:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=2000):
                        btn.click()
                        signed = True
                        break
                except Exception:
                    continue
            if not signed:
                page.keyboard.press("Return")

            _human_sleep(3, 5)

            # 2FA / OTP (SMS oder Authenticator) – optional, falls aktiv
            try:
                if page.locator("#auth-mfa-otpcode").is_visible(timeout=3000):
                    otp = os.environ.get("AMAZON_OTP_CODE", "")
                    if not otp:
                        logger.warning(
                            "⚠️  Amazon verlangt 2FA – warte auf Code über Web-UI (5 Min)..."
                        )
                        otp = otp_state.request_otp(timeout=300)
                    if otp:
                        page.locator("#auth-mfa-otpcode").fill(otp)
                        remember = page.locator("#auth-rememberme-checkbox")
                        if remember.is_visible(timeout=1000):
                            remember.check()
                        page.locator("#auth-signin-button").click()
                        _human_sleep(3, 5)
                    else:
                        logger.error("Kein OTP eingegeben – Login abgebrochen")
                        return False
            except Exception:
                pass  # Kein 2FA nötig

            # "Angemeldet bleiben?" Dialog
            for keep_sel in ["#remember_me", 'input[name="rememberMe"]']:
                try:
                    el = page.locator(keep_sel)
                    if el.is_visible(timeout=2000):
                        el.check()
                        _human_sleep(0.5, 1)
                        break
                except Exception:
                    pass

            # Nach Login zur Bestellseite
            page.goto(self.urls["orders"], wait_until="domcontentloaded", timeout=45000)
            _human_sleep(2, 3)

            if "your-orders" in page.url or "/orders" in page.url:
                logger.info("Amazon Login erfolgreich!")
                return True

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

    def _parse_amazon_date(self, date_text: str | None) -> str | None:
        """Parst deutsches Amazon-Datum wie '8. Dezember 2024' → '2024-12-08'."""
        if not date_text:
            return None
        try:
            parts = date_text.lower().replace(".", "").split()
            if len(parts) >= 3:
                day = int(parts[0])
                month = _GERMAN_MONTHS.get(parts[1].strip(","))
                year = int(parts[-1])
                if month and 1 <= day <= 31 and 2000 <= year <= 2100:
                    return f"{year}-{month:02d}-{day:02d}"
        except Exception:
            pass
        return None

    def _get_invoice_map(self, page: Page) -> dict[str, dict]:
        """
        Iteriert durch alle Jahre seit AMAZON_START_YEAR.
        Vergangene Jahre werden nach dem ersten Scan übersprungen (year-skip).
        Im Incremental-Modus werden nur die letzten 30 Tage gescannt.
        Gibt dict[order_id → {pdf_url, date, year}] zurück.
        """
        result: dict[str, dict] = {}
        current_year = datetime.date.today().year

        # ── Incremental-Modus: nur letzte 30 Tage ──────────────────────────────
        if self.incremental:
            logger.info("Incremental-Modus: Scanne nur letzte 30 Tage (last30)")
            self._scan_order_filter(page, "last30", result, current_year)
            logger.info("Incremental: %d Rechnungen gefunden", len(result))
            return result

        # ── Vollständiger Scan: alle Jahre seit start_year ──────────────────────
        # PAPERFLOW_YEARS_FILTER: kommaseparierte Jahre für gezielten Scan (aus UI)
        _years_filter = os.environ.get("PAPERFLOW_YEARS_FILTER", "").strip()
        if _years_filter:
            try:
                years = [int(y.strip()) for y in _years_filter.split(",") if y.strip()]
                logger.info("Jahr-Filter aktiv: %s", years)
            except ValueError:
                years = list(range(current_year, self.start_year - 1, -1))
        else:
            years = list(range(current_year, self.start_year - 1, -1))
        skipped = sum(
            1
            for y in years
            if y < current_year and database.is_year_complete(self.provider_name, y)
        )
        logger.info(
            "Scanne %d Jahre (%d–%d), %d bereits abgeschlossen → überspringe",
            len(years),
            self.start_year,
            current_year,
            skipped,
        )

        for year in years:
            time_filter = f"year-{year}"

            # Vergangene Jahre überspringen die bereits vollständig gescannt wurden
            if year < current_year and database.is_year_complete(
                self.provider_name, year
            ):
                logger.info("Jahr %d: bereits gescannt – überspringe", year)
                continue

            found = self._scan_order_filter(page, time_filter, result, year)
            logger.info(
                "Jahr %d: %d neue Rechnungen (gesamt: %d)", year, found, len(result)
            )

            # Vergangene Jahre nach erstem Scan als "abgeschlossen" markieren
            if year < current_year:
                database.mark_year_complete(self.provider_name, year, found)

        return result

    def _navigate_to_filter(self, page: Page, time_filter: str) -> bool:
        """
        Setzt den Jahresfilter per Dropdown-Select – genau wie ein echter Nutzer.
        Falls nicht auf der Bestellseite, erst dorthin navigieren.
        Fallback auf URL-Navigation falls kein Dropdown gefunden.
        """
        orders_url = self.urls["orders"]

        # Falls wir nicht auf der Bestellseite sind, dorthin navigieren
        if orders_url.split("?")[0] not in page.url:
            page.goto(orders_url, wait_until="domcontentloaded", timeout=45000)
            _human_sleep(2, 3)

        if self._is_login_page(page):
            return False

        logger.info("_navigate_to_filter: aktuelle URL = %s", page.url[-80:])

        # Dropdown per Select-Option setzen (wie echter Nutzer)
        try:
            select = page.locator("select#time-filter")
            count = select.count()
            logger.info("select#time-filter: count=%d", count)
            if count > 0 and select.first.is_visible(timeout=5000):
                logger.info("Dropdown sichtbar – setze Option: %s", time_filter)
                select.first.select_option(value=time_filter)
                _human_sleep(2, 4)
                page.wait_for_load_state("networkidle", timeout=30000)
                logger.info(
                    "Nach Dropdown-Select: URL=%s | login=%s",
                    page.url[-80:],
                    self._is_login_page(page),
                )
                return not self._is_login_page(page)
            else:
                logger.warning(
                    "Dropdown nicht sichtbar (count=%d) – versuche URL-Navigation",
                    count,
                )
        except Exception as e:
            logger.warning(
                "Dropdown-Select fehlgeschlagen: %s – versuche URL-Navigation", e
            )

        # Fallback: direkt per URL
        url = f"{orders_url}?timeFilter={time_filter}"
        logger.info("URL-Navigation: %s", url[-80:])
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        _human_sleep(2, 3)
        logger.info(
            "Nach URL-Navigation: URL=%s | login=%s",
            page.url[-80:],
            self._is_login_page(page),
        )
        return not self._is_login_page(page)

    def _scan_order_filter(
        self,
        page: Page,
        time_filter: str,
        result: dict[str, dict],
        year: int | None = None,
    ) -> int:
        """
        Lädt eine gefilterte Bestellliste, liest alle Popover-URLs aus dem DOM
        und holt per fetch() direkt die PDF-Links – ohne Popup-Klick.
        Paginiert automatisch. Gibt die Anzahl neu gefundener Rechnungen zurück.
        """
        if not self._navigate_to_filter(page, time_filter):
            logger.warning(
                "Session abgelaufen bei %s – versuche Re-Login...", time_filter
            )
            otp_state.notify_login_required()
            success = self._do_login(page)
            otp_state.notify_login_done(success)
            if not success:
                logger.error("Re-Login fehlgeschlagen – überspringe %s", time_filter)
                return 0
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

            # Sammle alle Popover-URLs + Bestellnummern + Datum in einem JS-Call
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
                        if (!m) continue;
                        // Bestelldatum extrahieren
                        let dateText = null;
                        if (card) {
                            const spans = card.querySelectorAll('span, div');
                            for (const s of spans) {
                                const t = s.innerText ? s.innerText.trim() : '';
                                if (/^\\d+\\.\\s+\\w+\\s+\\d{4}$/.test(t)) {
                                    dateText = t;
                                    break;
                                }
                            }
                        }
                        // Produkttitel extrahieren (erstes Produkt in der Bestellung)
                        let titleText = null;
                        if (card) {
                            const titleEl = card.querySelector(
                                '.yohtmlc-product-title, ' +
                                '.a-fixed-left-grid-inner .a-col-right a.a-link-normal, ' +
                                'a[class*="product-title"], ' +
                                '.product-image+* a'
                            );
                            if (titleEl) {
                                titleText = titleEl.innerText.trim().replace(/\\s+/g, ' ').substring(0, 80);
                            }
                        }
                        results.push({ orderId: m[0], popoverUrl: a.href, dateText, titleText });
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
                order_id = item.get("orderId") or item.get("order_id")
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
                    # Relative URLs zu absoluten machen (z.B. /documents/download/...)
                    if pdf_url.startswith("/"):
                        pdf_url = f"{self.urls['base']}{pdf_url}"
                    date_iso = self._parse_amazon_date(item.get("dateText"))
                    title = item.get("titleText") or None
                    result[order_id] = {
                        "pdf_url": pdf_url,
                        "date": date_iso,
                        "year": year,
                        "title": title,
                    }
                    found_new += 1
                    logger.debug(
                        "PDF: %s → %s | Datum: %s", order_id, pdf_url[:60], date_iso
                    )
                else:
                    logger.debug(
                        "Kein PDF für %s (kein Rechnungs-Link im Popover)", order_id
                    )

            # Nächste Seite?
            next_btn = page.locator("ul.a-pagination li.a-last a")
            if next_btn.count() == 0 or not next_btn.is_visible():
                break
            next_btn.click()
            _human_sleep(2, 4)

            if page_num > 500:
                logger.warning("Seitenlimit (500) erreicht für %s", time_filter)
                break

        return found_new

    # ──────────────────────────────────────────────────────────────
    # PDF direkt herunterladen
    # ──────────────────────────────────────────────────────────────

    def _download_pdf(
        self,
        page: Page,
        order_id: str,
        pdf_url: str,
        invoice_date: str | None = None,
        year: int | None = None,
        product_title: str | None = None,
    ) -> Invoice | None:
        """
        Lädt das PDF via Browser-fetch() herunter.
        Nutzt alle Session-Cookies und Browser-Header – zuverlässiger als
        page.context.request.get() welches manchmal HTML-Redirects bekommt.
        """
        output_path = self.download_dir / f"amazon_{order_id}.pdf"
        extra_tags = [str(year)] if year else []
        title = (
            f"Amazon – {product_title}"
            if product_title
            else f"Amazon Rechnung {order_id}"
        )

        # Cache prüfen: existierende Datei muss echtes PDF sein
        if output_path.exists() and output_path.stat().st_size > 1000:
            if output_path.read_bytes()[:4] == b"%PDF":
                logger.debug("Bereits vorhanden: %s", order_id)
                return Invoice(
                    invoice_id=order_id,
                    file_path=output_path,
                    title=title,
                    date=invoice_date,
                    extra_tags=extra_tags,
                )
            else:
                logger.info("Gecachte HTML-Datei gelöscht, lade neu: %s", order_id)
                output_path.unlink()

        try:
            # JS-fetch im Browser-Kontext: nutzt ALLE Session-Cookies + Browser-Header
            # Gibt PDF-Bytes als Base64-String zurück
            result = page.evaluate(
                """async (url) => {
                    try {
                        const resp = await fetch(url, {
                            credentials: 'include',
                            headers: { 'Accept': 'application/pdf,application/octet-stream,*/*' }
                        });
                        if (!resp.ok) return { ok: false, status: resp.status };
                        const buf = await resp.arrayBuffer();
                        const bytes = new Uint8Array(buf);
                        // Magic-Bytes prüfen
                        const magic = String.fromCharCode(bytes[0], bytes[1], bytes[2], bytes[3]);
                        if (magic !== '%PDF') return { ok: true, status: resp.status, magic: magic };
                        // Base64 in Chunks (verhindert Stack-Overflow bei großen PDFs)
                        let binary = '';
                        for (let i = 0; i < bytes.length; i += 8192) {
                            binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
                        }
                        return { ok: true, status: resp.status, data: btoa(binary) };
                    } catch(e) {
                        return { ok: false, status: 0, error: String(e) };
                    }
                }""",
                pdf_url,
            )

            if not result or not result.get("ok"):
                status = result.get("status", "?") if result else "?"
                err = result.get("error", "")
                logger.warning(
                    "HTTP %s für %s%s", status, order_id, f" – {err}" if err else ""
                )
                return None

            b64_data = result.get("data")
            if not b64_data:
                magic = result.get("magic", "?")
                logger.warning("Kein PDF für %s – Magic-Bytes: %r", order_id, magic)
                return None

            pdf_bytes = base64.b64decode(b64_data)
            output_path.write_bytes(pdf_bytes)
            logger.info(
                "✓ PDF heruntergeladen: %s (%d KB) | Datum: %s",
                order_id,
                len(pdf_bytes) // 1024,
                invoice_date or "unbekannt",
            )
            return Invoice(
                invoice_id=order_id,
                file_path=output_path,
                title=title,
                date=invoice_date,
                extra_tags=extra_tags,
            )

        except Exception as e:
            logger.error("Download fehlgeschlagen %s: %s", order_id, e)

        return None
