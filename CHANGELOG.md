# Changelog

All notable changes to paperflow will be documented here.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.0.0] - 2026-06-07 — Stable Release

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
