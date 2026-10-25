"""
Ollama Provider – lädt die Abo-Rechnungen von ollama.com herunter.

CDP-Modus:
  Verbindet sich mit dem paperflow-chrome Container via CDP.
  Nutzer loggt sich einmalig manuell auf ollama.com ein → Session bleibt erhalten.
  Setzt CHROME_CDP_URL=http://paperflow-chrome:9222 voraus.

Seite (Stand 2026-10, https://ollama.com/settings/billing, per Screenshot):
  Abschnitt "Invoices" mit einer Tabelle Date | Due | Total | Status und einem
  "View"-Link je Zeile, z.B. "October 11, 2026 · — · $20.00 · Paid · View".

"View" führt auf https://ollama.com/settings/billing/invoices/<n>/in_…
(Stripe-Rechnungs-ID, dient als Rechnungs-ID). Download (best-effort, der
Inhalt dieser Seite war beim Schreiben nicht bekannt):
  1. Zeigt "View" auf eine von Stripe gehostete Rechnung
     (invoice.stripe.com/i/acct_…/…), wird das PDF direkt über die
     zugehörige Stripe-PDF-URL (pay.stripe.com/invoice/acct_…/…/pdf) geholt.
  2. Sonst wird die Rechnungsseite geöffnet und dort ein PDF-Link
     (…/pdf) bzw. der "Download invoice"-Button verwendet.
"""

from __future__ import annotations

import datetime
import logging
import os
import random
import re
import time
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import Page, sync_playwright

from app import database
from app.providers import BaseProvider, Invoice, wait_for_cdp

logger = logging.getLogger("provider.ollama")

BILLING_URL = "https://ollama.com/settings/billing"

_CDP_URL = os.environ.get("CHROME_CDP_URL", "").strip()

_STRIPE_HOSTED_RE = re.compile(
    r"https://invoice\.stripe\.com/i/(acct_[A-Za-z0-9]+)/([A-Za-z0-9_\-]+)"
)
_AMOUNT_PREFIX_RE = re.compile(r"[$€£]\s?(\d[\d.,]*\d|\d)")
_AMOUNT_SUFFIX_RE = re.compile(r"(\d[\d.,]*\d|\d)\s?(?:[$€£]|USD|EUR)")
_INVOICE_ID_RE = re.compile(r"/invoices/(?:[^/]+/)?(in_[A-Za-z0-9]+)")
_MONTHS_EN = {
    m: i
    for i, m in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ],
        start=1,
    )
}
_MONTHS_DE = {
    m: i
    for i, m in enumerate(
        [
            "januar",
            "februar",
            "märz",
            "april",
            "mai",
            "juni",
            "juli",
            "august",
            "september",
            "oktober",
            "november",
            "dezember",
        ],
        start=1,
    )
}
_PAID_WORDS = ("paid", "bezahlt")
_UNPAID_WORDS = ("open", "void", "draft", "uncollectible", "offen", "entwurf")
_VIEW_TEXT_RE = r"^(view|ansehen|anzeigen|rechnung anzeigen|view invoice)$"
_LOAD_MORE_SELECTORS = [
    "button:has-text('Load more')",
    "button:has-text('Show more')",
    "button:has-text('View all')",
    "button:has-text('Mehr anzeigen')",
    "button:has-text('Mehr laden')",
]
_DOWNLOAD_BTN_SELECTORS = [
    "button:has-text('Download invoice')",
    "a:has-text('Download invoice')",
    "button:has-text('Rechnung herunterladen')",
    "a:has-text('Rechnung herunterladen')",
    "button:has-text('Download')",
    "a:has-text('Download')",
]


def _sleep(min_s: float = 1.0, max_s: float = 2.5) -> None:
    time.sleep(random.uniform(min_s, max_s))


def parse_date(text: str) -> str | None:
    """'October 11, 2026' / '11. Oktober 2026' / '11.10.2026' → '2026-10-11'."""
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", text)
    if m and m.group(1).lower() in _MONTHS_EN:
        month, day, year = _MONTHS_EN[m.group(1).lower()], m.group(2), m.group(3)
        return f"{year}-{month:02d}-{int(day):02d}"
    m = re.search(r"(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]+)\s+(\d{4})", text)
    if m and m.group(2).lower() in _MONTHS_DE:
        day, month, year = m.group(1), _MONTHS_DE[m.group(2).lower()], m.group(3)
        return f"{year}-{month:02d}-{int(day):02d}"
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None


def parse_amount_cents(text: str) -> int | None:
    """'$20.00' / '20,00 €' → 2000."""
    m = _AMOUNT_PREFIX_RE.search(text) or _AMOUNT_SUFFIX_RE.search(text)
    if not m:
        return None
    raw = m.group(1)
    digits = re.sub(r"[.,](\d{2})$", r"\1", raw)
    digits = re.sub(r"[.,]", "", digits)
    if not re.search(r"[.,]\d{2}$", raw):
        digits += "00"
    try:
        return int(digits)
    except ValueError:
        return None


def stripe_pdf_url(hosted_url: str) -> str | None:
    """invoice.stripe.com/i/acct_X/TOKEN → pay.stripe.com/invoice/acct_X/TOKEN/pdf"""
    m = _STRIPE_HOSTED_RE.search(hosted_url or "")
    if not m:
        return None
    return f"https://pay.stripe.com/invoice/{m.group(1)}/{m.group(2)}/pdf"


class OllamaProvider(BaseProvider):
    provider_name = "ollama"

    def fetch_invoices(self) -> list[Invoice]:
        if not _CDP_URL:
            logger.error("CHROME_CDP_URL nicht gesetzt – Ollama Provider deaktiviert")
            return []
        return self._fetch_via_cdp()

    # ── CDP-Modus ──────────────────────────────────────────────────

    def _fetch_via_cdp(self) -> list[Invoice]:
        invoices: list[Invoice] = []
        logger.info("CDP-Modus: Verbinde mit Chrome auf %s", _CDP_URL)

        if not wait_for_cdp(_CDP_URL, logger):
            return []

        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp(_CDP_URL)
                logger.info(
                    "Chrome CDP verbunden: %d Context(s)", len(browser.contexts)
                )
            except Exception as exc:
                logger.error("CDP-Verbindung fehlgeschlagen: %s", exc)
                return []

            context = (
                browser.contexts[0]
                if browser.contexts
                else browser.new_context(locale="de-DE")
            )
            page = context.new_page()
            try:
                invoices = self._collect_invoices(page)
            except Exception:
                logger.exception("Ollama CDP-Fehler")
            finally:
                page.close()
                # Browser NICHT schließen – Session bleibt erhalten

        return invoices

    # ── Scan-Logik ─────────────────────────────────────────────────

    def _collect_invoices(self, page: Page) -> list[Invoice]:
        page.goto(BILLING_URL, timeout=30_000)
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass

        try:
            page.wait_for_selector("text=Invoices", timeout=15_000)
        except Exception:
            pass
        if re.search(r"/(signin|login|signup)", page.url) or not page.query_selector(
            "text=Invoices"
        ):
            logger.error(
                "Ollama: Nicht eingeloggt oder Rechnungsliste nicht gefunden "
                "(URL: %s).\n"
                "→ Öffne den Browser (noVNC)\n"
                "→ Navigiere zu ollama.com und logge dich ein\n"
                "→ Danach erneut starten",
                page.url,
            )
            self._debug_dump(page, "Login-Check fehlgeschlagen")
            return []

        logger.info("Ollama: Eingeloggt – lese Rechnungsliste")
        self._load_all_rows(page)

        years_filter: set[int] | None = None
        yf = os.environ.get("PAPERFLOW_YEARS_FILTER", "").strip()
        if yf:
            years_filter = {int(y) for y in yf.split(",") if y.strip().isdigit()}
        elif self.scan_from_year:
            years_filter = set(
                range(self.scan_from_year, datetime.date.today().year + 1)
            )

        rows = self._parse_rows(page)
        logger.info("Ollama: %d Rechnungen in der Liste", len(rows))
        if not rows:
            self._debug_dump(page, "Keine Rechnungszeilen gefunden")

        invoices: list[Invoice] = []
        for row in rows:
            date_iso = parse_date(row["text"])
            cents = parse_amount_cents(row["text"])
            if not date_iso:
                logger.warning(
                    "Ollama: Datum nicht erkannt in Zeile: %s", row["text"][:80]
                )
                continue
            if years_filter and int(date_iso[:4]) not in years_filter:
                continue

            status = row["text"].lower()
            if any(w in status for w in _UNPAID_WORDS) and not any(
                w in status for w in _PAID_WORDS
            ):
                logger.info("Ollama: Rechnung %s noch nicht bezahlt – später", date_iso)
                continue

            # "View" → /settings/billing/invoices/<n>/in_… (Stripe-Rechnungs-ID,
            # bestätigt per URL vom Nutzer) – stabiler als Datum+Betrag.
            m_id = _INVOICE_ID_RE.search(row.get("href") or "")
            invoice_id = (
                f"ollama_{m_id.group(1)}"
                if m_id
                else f"ollama_{date_iso}_{cents if cents is not None else 0}"
            )
            if database.is_processed(self.provider_name, invoice_id):
                logger.info("Bereits verarbeitet: %s", invoice_id)
                continue

            pdf_path = self._download_row(page, row, invoice_id)
            if pdf_path:
                amount = f"{cents / 100:.2f}" if cents is not None else None
                invoices.append(
                    Invoice(
                        invoice_id=invoice_id,
                        file_path=pdf_path,
                        title=f"Ollama Rechnung {date_iso}",
                        date=date_iso,
                        amount=amount,
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

        logger.info("Ollama: %d Rechnungen heruntergeladen", len(invoices))
        return invoices

    def _load_all_rows(self, page: Page) -> None:
        for _ in range(20):
            clicked = False
            for sel in _LOAD_MORE_SELECTORS:
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click(timeout=5_000)
                        logger.info("Ollama: Klicke '%s'", sel)
                        _sleep(1, 2)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                return

    def _parse_rows(self, page: Page) -> list[dict]:
        """Sucht alle "View"-Links und liest den Text ihrer Tabellenzeile."""
        return page.evaluate(
            """
            (viewRe) => {
                const re = new RegExp(viewRe, 'i');
                const views = [...document.querySelectorAll('a, button')]
                    .filter(el => re.test((el.textContent || '').trim()));
                return views.map((el, index) => {
                    let row = el.parentElement;
                    for (let k = 0; k < 6 && row; k++) {
                        const t = row.innerText || '';
                        if (/\\d{4}/.test(t) && /[$€£]|USD|EUR/.test(t)) break;
                        row = row.parentElement;
                    }
                    return {
                        index,
                        href: el.getAttribute('href') || '',
                        target: el.getAttribute('target') || '',
                        text: row ? row.innerText.replace(/\\s+/g, ' ').trim() : '',
                    };
                });
            }
            """,
            _VIEW_TEXT_RE,
        )

    # ── Download ───────────────────────────────────────────────────

    def _download_row(self, page: Page, row: dict, invoice_id: str) -> Path | None:
        out_path = self.download_dir / f"{invoice_id}.pdf"
        context = page.context
        href = urljoin(page.url, row["href"]) if row.get("href") else ""

        pdf_url = stripe_pdf_url(href)
        if pdf_url and self._fetch_pdf(context, pdf_url, out_path, invoice_id):
            return out_path

        detail = None
        own_page = False
        try:
            if href and not href.startswith("javascript"):
                detail = context.new_page()
                own_page = True
                detail.goto(href, timeout=30_000)
            else:
                view = page.locator("a, button").filter(
                    has_text=re.compile(_VIEW_TEXT_RE, re.IGNORECASE)
                )
                try:
                    with context.expect_page(timeout=10_000) as new_page_info:
                        view.nth(row["index"]).click()
                    detail = new_page_info.value
                    own_page = True
                except Exception:
                    detail = page  # Navigation im selben Tab
            try:
                detail.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass
            logger.info("Ollama: Rechnungsseite %s", detail.url[:80])
            return self._download_from_detail(detail, out_path, invoice_id)
        except Exception as exc:
            logger.warning("Ollama: Rechnung %s nicht geöffnet: %s", invoice_id, exc)
            return None
        finally:
            if detail is not None and own_page:
                try:
                    detail.close()
                except Exception:
                    pass
            elif detail is page and BILLING_URL not in page.url:
                page.goto(BILLING_URL, timeout=30_000)
                try:
                    page.wait_for_selector("text=Invoices", timeout=15_000)
                except Exception:
                    pass

    def _download_from_detail(
        self, detail: Page, out_path: Path, invoice_id: str
    ) -> Path | None:
        context = detail.context

        # Die Rechnungsseite ist selbst schon das PDF?
        for url in (stripe_pdf_url(detail.url), detail.url):
            if url and (url.endswith("/pdf") or ".pdf" in url):
                if self._fetch_pdf(context, url, out_path, invoice_id):
                    return out_path

        # PDF-Link auf der Seite
        for link in detail.query_selector_all("a[href*='/pdf'], a[href$='.pdf']"):
            url = urljoin(detail.url, link.get_attribute("href") or "")
            if url and self._fetch_pdf(context, url, out_path, invoice_id):
                return out_path

        # "Download invoice"-Button → Browser-Download
        for sel in _DOWNLOAD_BTN_SELECTORS:
            try:
                btn = detail.query_selector(sel)
                if not btn or not btn.is_visible():
                    continue
                with detail.expect_download(timeout=30_000) as dl_info:
                    btn.click()
                download = dl_info.value
                tmp = out_path.with_suffix(".part")
                download.save_as(tmp)
                try:
                    download.delete()
                except Exception:
                    pass
                data = tmp.read_bytes()
                tmp.unlink(missing_ok=True)
                if data[:4] == b"%PDF":
                    out_path.write_bytes(data)
                    logger.info(
                        "Rechnung gespeichert: %s (%d bytes)", out_path.name, len(data)
                    )
                    return out_path
                logger.warning("Ollama: Download via '%s' ist kein PDF", sel)
            except Exception as exc:
                logger.debug("Ollama: Download via '%s' fehlgeschlagen: %s", sel, exc)

        self._debug_dump(detail, f"Kein PDF auf der Rechnungsseite ({invoice_id})")
        return None

    def _fetch_pdf(self, context, url: str, out_path: Path, invoice_id: str) -> bool:
        try:
            response = context.request.get(url, timeout=30_000)
        except Exception as exc:
            logger.debug("Ollama: PDF-Request %s fehlgeschlagen: %s", url[:80], exc)
            return False
        if not response.ok:
            logger.debug("Ollama: PDF-Request %s → HTTP %d", url[:80], response.status)
            return False
        data = response.body()
        if len(data) > 500 and data[:4] == b"%PDF":
            out_path.write_bytes(data)
            logger.info("Rechnung gespeichert: %s (%d bytes)", out_path.name, len(data))
            return True
        return False

    def _debug_dump(self, page: Page, context: str) -> None:
        try:
            texts = page.evaluate(
                """
                () => [...document.querySelectorAll('button, a')]
                    .map(el => el.tagName + ':' + (el.textContent || '').trim().slice(0, 40)
                        + (el.getAttribute('href') ? ' → ' + el.getAttribute('href').slice(0, 80) : ''))
                    .filter(t => t.length > 3)
                    .slice(0, 50)
                """
            )
            logger.info("DEBUG Ollama (%s) URL: %s", context, page.url)
            logger.info("DEBUG Ollama (%s): %s", context, texts)
        except Exception as de:
            logger.debug("DEBUG-Dump fehlgeschlagen (%s): %s", context, de)
