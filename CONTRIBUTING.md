# Contributing to paperflow

Thank you for your interest! The most valuable contribution you can make is **writing a provider** for a service you use.

---

## Writing a Provider

1. **Fork** the repo and create a branch: `git checkout -b provider/myprovider`

2. Create `app/providers/myprovider.py`:

```python
from app.providers import BaseProvider, Invoice
from pathlib import Path

class MyproviderProvider(BaseProvider):
    provider_name = "myprovider"  # lowercase, matches filename

    def fetch_invoices(self) -> list[Invoice]:
        """
        Download all invoices and return them as Invoice objects.
        Already-processed invoices are filtered by the caller — 
        return everything you find, duplicates are handled automatically.
        """
        invoices = []
        # ... your download logic ...
        return invoices
```

3. Add an entry to `config/providers.yml.example` (if you add one)

4. Open a **Pull Request** — describe which service it covers and how login/auth works

---

## Rules

- **No hardcoded credentials** — always read from `os.environ`
- **Idempotent** — `fetch_invoices()` may be called multiple times; always return all found invoices, the DB handles deduplication
- **Headless** — providers must work without a display (use Playwright headless mode)
- Keep dependencies minimal — prefer stdlib and already-listed packages

---

## Reporting Bugs

Open an issue and include:
- paperflow version
- Provider name
- Anonymized log output (remove personal data)
- What you expected vs. what happened
