# Changelog

All notable changes to paperflow will be documented here.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.0.10] - 2026-09-02

### Fixed
- Klarna: purchases stopped showing up entirely (all discovery strategies found 0 transactions) after Klarna moved the purchase list from `/manage-payments` to a new `/manage-payments/purchases-and-returns` page — confirmed via real screenshots. Transaction detail URLs are unaffected (still `/manage-payments/transactions/internal/...`), so this is a one-line navigation fix: point the scanner at the new list URL while keeping the existing detail-URL base and download flow (still a `blob:` PDF opened via the same "..." → "Auszug herunterladen" menu, already handled by the existing code) unchanged.

---

## [1.0.09] - 2026-08-09

### Fixed
- HP Instant Ink: 1.0.08's HTTP-fetch fix confirmed working in production (88 invoices downloaded in one run), but rows later in a page would intermittently fail with "Element is not attached to the DOM" — earlier downloads on the same page apparently cause the row list to re-render, invalidating the element references collected when the page was first scanned. Rows are now re-located (fresh element handle) immediately before each click instead of reusing the handle from the initial page scan.

---

## [1.0.08] - 2026-08-09

### Fixed
- HP Instant Ink: 1.0.07's download-event listener still timed out — confirmed by both a production log and a user screenshot showing the real cause: the new tab navigates straight to a PDF URL that Chrome's built-in viewer *displays* rather than downloads, so no Playwright `download` event ever fires on either tab. Now captures the new tab's URL directly and fetches the PDF bytes over HTTP through the shared browser context (cookies/session included) instead of waiting for a download event at all.

---

## [1.0.07] - 2026-08-09

### Fixed
- HP Instant Ink: every download timed out after 30s waiting for a download event, confirmed by a real production log (accordion fix from 1.0.05 worked — 11 pages detected, 10 rows found on page 1 — but every single row then failed to download). The "Herunterladen" link most likely opens a new tab that triggers the actual download itself, which the previous code never watched for. Now listens for a download event on both the current tab and any newly opened tab simultaneously, whichever fires first.

---

## [1.0.06] - 2026-08-09

No functional changes — version re-sync to match the tag published for this deployment.

---

## [1.0.05] - 2026-08-09

### Fixed
- HP Instant Ink: the "Druck- und Zahlungsverlauf" section is a collapsed accordion by default — its invoice table (and the "X von Y" pagination count) only rendered after clicking the section header, which the provider never did. Confirmed from a real production log (0 rows found, 1 page detected) and its diagnostic dump. Now clicks the section's button right after the login check, before parsing anything.
- CI: `ruff` was installed unpinned, so a newer ruff release could silently ship a broader default rule set and start failing on pre-existing code. Added `pyproject.toml` pinning `[tool.ruff.lint] select` to ruff's prior default (E4, E7, E9, F) so this can't drift again on a future ruff upgrade.

---

## [1.0.04] - 2026-07-04

### Added
- **HP Instant Ink provider** — downloads individual invoice PDFs from the "Druck- und Zahlungsverlauf" (print/payment history) at `portal.hpsmart.com`, using the same manual CDP login as IKEA/Klarna. Deliberately downloads each row's own "Herunterladen" link rather than the page's "Alle Rechnungen herunterladen" button, which bundles the entire history into one combined PDF. Stops paginating once a page yields no new (not-yet-processed) invoices, since the history is sorted newest-first. The exact pagination control ("next page") is best-effort, since it wasn't visible in the screenshots used to build this — logs a diagnostic dump of the page's buttons/links if it doesn't match; the core row-parsing and per-invoice download were built directly against real screenshots of the actual page.

---

## [1.0.03] - 2026-07-04

### Removed
- **PayPal provider** — not currently supported. After fixing the passkey-blocked automated login (reactivated via manual CDP login) and reworking the provider around PayPal's actual asynchronous report-generation flow, production testing found that PayPal requires a fresh 2FA step-up confirmation on *every single visit* to the account-statements/reports area, with no "remember this device" option, regardless of how recently the main account login happened. That can't be automated without defeating the point of 2FA, so the provider is removed again until there's a viable way around it. `app/providers/paypal.py` is deleted; its config section in `config/providers.yml` is commented out (with the reason) rather than deleted outright, so it's easy to pick back up.

---

## [1.0.02] - 2026-07-04

### Fixed
- Crash when `AMAZON_START_YEAR` is set to an empty string (e.g. left blank in the web UI's Settings form) instead of being unset — `int("")` raised `ValueError`, which `load_providers()` didn't catch, crashing the entire scan for every provider. Fixed the env-var read and broadened the loader's exception handling so a single misconfigured provider can't take down the others.

---

## [1.0.01] - 2026-07-04

### Added
- **PayPal provider reactivated** — uses the same manual CDP login as IKEA/Klarna (log in once via noVNC), avoiding the automated-login passkey block that got the previous PayPal provider removed. PayPal's account statements are generated **asynchronously**: a report is requested for a given month and typically only becomes downloadable on a later run, so paperflow requests missing months and then picks up the finished PDF once PayPal marks it ready — the same "check again next run" pattern already used for Klarna's in-progress payments. The exact selectors for requesting a *new* report are best-effort (no live PayPal access while building this) and log diagnostic output if they don't match; downloading an already-ready report was verified against real production log output.

### Changed
- All CDP providers (Amazon, IKEA, Klarna, PayPal) now share a single `wait_for_cdp()` helper instead of duplicating the "wait for Chrome to be ready" loop
- Klarna: failed downloads are now recorded in the database like IKEA — permanently unavailable statements (no download button found) are marked `no_pdf` and skipped for good, while transient failures (timeouts, invalid PDF) are retried on the next run instead of being silently retried forever without any visibility in the history page
- Klarna: detects a session that expired specifically for `/manage-payments` (root login check can pass while this page still redirects to login) and fails fast with a re-login message instead of grinding through ~65s of timeouts

### Fixed
- Amazon provider failed to import (`playwright-stealth` API mismatch), crashing the entire fetch run for every provider when Amazon was enabled
- Provider loader now catches all `ImportError`s instead of only `ModuleNotFoundError`, so one broken provider module can no longer take down the others
- IKEA: transient download failures (timeouts, network errors) were permanently blacklisted as `no_pdf` and never retried; only orders with no receipt button are now treated as permanent
- IKEA: old orders that serve a JPG photo of the receipt instead of a PDF are now downloaded and uploaded correctly (previously rejected as "no valid PDF")
- Paperless upload now sends the correct `Content-Type` based on the file extension instead of always `application/pdf`
- README/`.env.example` documented `IKEA_EMAIL`/`IKEA_PASSWORD` even though IKEA is CDP-only (manual login) and never reads those variables — removed
- PayPal: the original implementation assumed an instant, synchronous PDF download at a `statement/download` URL that doesn't reflect PayPal's actual (asynchronous, report-request-based) flow, and treated "our selectors didn't find a link" as permanent proof no statement exists — both fixed before release based on real production testing

---

## [1.0.0] - 2026-06-08 — Stable Release

### Security
- **HTTP Basic Auth** — web UI and all API endpoints are now protected when `UI_PASSWORD` is set; leave empty to disable (backwards-compatible)
- **Configurable credentials** — `UI_USER` (default: `admin`) and `UI_PASSWORD` can be set in Settings → Sicherheit
- **Provider syntax check** — uploaded custom provider `.py` files are now validated with `ast.parse()` before saving; broken files are rejected immediately
- `cookies-raw` endpoint excluded from auth (called from external browser context for Amazon cookie injection)

### Added
- **Product title extraction** — Paperless title shows the actual Amazon product name
- **Date extraction** — order date is parsed from the order page and set as `created_date` in Paperless
- **Year tags** — every invoice is automatically tagged with its year (e.g. `2024`)
- **Year-skip optimization** — past years that are fully scanned are skipped on future runs
- **Incremental scan mode** — `AMAZON_INCREMENTAL=true` scans only the last 30 days
- **Parallel uploads** — multiple PDFs uploaded simultaneously (`UPLOAD_WORKERS=3`)
- **Progress bar** — live upload progress (phase, count, current invoice) in the web UI
- **Error categories** — invoice history shows `no PDF`, `Download ✗`, or `Upload ✗` badges
- **Correspondent dropdown** — select Paperless-NGX correspondent from a live list (fixes wrong auto-assignment)
- **Exact correspondent matching** — prevents wrong correspondent assignment (e.g. "AIG" instead of "Amazon")
- **CDP browser mode** — connects to a persistent Chrome via Remote Debugging Protocol
- **chrome-desktop container** — dedicated Chrome + noVNC container (one-time manual login, session persists)
- **JS fetch PDF download** — uses browser's `fetch()` with full cookie access for reliable PDF downloads
- **Invoice history** — Verlauf page with status filters, bulk select, delete, and retry
- **Bulk operations** — select/delete/retry multiple invoices at once
- **Paperless-NGX proxy endpoints** — `/api/paperless/correspondents` and `/api/paperless/tags`
- **Shared progress state** — `app/state.py` module for real-time scan progress between worker and UI

### Changed
- Web UI port changed from `8080` to `8085`
- Version bumped to `1.0.0`

### Fixed
- PDF downloads returning HTML instead of PDF (cached stale URLs now detected and re-fetched)
- Wrong correspondent assigned in Paperless (non-exact name match caused "AIG" assignment)
- Re-login handled gracefully when session expires mid-scan

---

## [0.0.1] - 2024-06-06 — Initial Release

### Added
- Amazon provider — downloads invoices via headless Chromium (Playwright)
- Paperless-NGX REST API client — uploads PDFs with tags, correspondent, and date
- SQLite database — tracks processed invoices, prevents duplicates
- Plugin architecture — add new providers by dropping a single `.py` file
- Web interface (port `8080`) with:
  - Dashboard: stats, last run status, manual trigger
  - Settings: edit all credentials in-browser (saved to `.env`)
  - Providers: enable/disable, edit tags, upload custom provider scripts
  - Logs: live log output with auto-refresh
- Docker-first setup — runs fully headless, no display required
- Custom provider upload — validate, store, and activate `.py` files via web UI
