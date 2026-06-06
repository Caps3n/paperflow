# Changelog

All notable changes to paperflow will be documented here.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

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
