# Changelog

All notable changes to paperflow will be documented here.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

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
