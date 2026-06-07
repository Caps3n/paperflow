"""
SQLite-Datenbank zum Tracken bereits verarbeiteter Rechnungen.
Verhindert Duplikate beim erneuten Lauf.
"""

import sqlite3
import logging
from datetime import datetime
from pathlib import Path

DB_PATH = Path("/app/data/invoices.db")
logger = logging.getLogger(__name__)


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Erstellt die Tabellen beim ersten Start."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS invoices (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                provider    TEXT NOT NULL,
                invoice_id  TEXT NOT NULL,
                filename    TEXT,
                status      TEXT NOT NULL DEFAULT 'pending',
                paperless_id INTEGER,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                UNIQUE(provider, invoice_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_provider_invoice
            ON invoices(provider, invoice_id)
        """)
        # Migration: error_type Spalte hinzufügen falls nicht vorhanden
        cols = [r[1] for r in conn.execute("PRAGMA table_info(invoices)").fetchall()]
        if "error_type" not in cols:
            conn.execute("ALTER TABLE invoices ADD COLUMN error_type TEXT")
            logger.info("Spalte 'error_type' zur invoices-Tabelle hinzugefügt")
        # Jahre-Tracking: einmal gescannte Vergangenjahre überspringen
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scanned_years (
                provider     TEXT NOT NULL,
                year         INTEGER NOT NULL,
                invoice_count INTEGER NOT NULL DEFAULT 0,
                completed_at TEXT NOT NULL,
                PRIMARY KEY (provider, year)
            )
        """)
        conn.commit()
    logger.info("Datenbank initialisiert: %s", DB_PATH)


def is_processed(provider: str, invoice_id: str) -> bool:
    """Gibt True zurück wenn die Rechnung bereits erfolgreich hochgeladen wurde."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT status FROM invoices WHERE provider=? AND invoice_id=?",
            (provider, invoice_id),
        ).fetchone()
    return row is not None and row["status"] == "uploaded"


def mark_pending(provider: str, invoice_id: str, filename: str) -> None:
    """Trägt eine neu gefundene Rechnung ein (Status: pending)."""
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO invoices
                (provider, invoice_id, filename, status, created_at, updated_at)
            VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            (provider, invoice_id, filename, now, now),
        )
        conn.commit()


def mark_uploaded(provider: str, invoice_id: str, paperless_id: int | None) -> None:
    """Markiert eine Rechnung als erfolgreich zu Paperless hochgeladen."""
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE invoices
            SET status='uploaded', paperless_id=?, updated_at=?
            WHERE provider=? AND invoice_id=?
            """,
            (paperless_id, now, provider, invoice_id),
        )
        conn.commit()


def mark_failed(
    provider: str,
    invoice_id: str,
    reason: str,
    error_type: str = "other",
) -> None:
    """Markiert eine Rechnung als fehlgeschlagen.

    error_type: "no_pdf" | "download_failed" | "upload_failed" | "other"
    """
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE invoices
            SET status='failed', filename=?, error_type=?, updated_at=?
            WHERE provider=? AND invoice_id=?
            """,
            (reason[:500], error_type, now, provider, invoice_id),
        )
        conn.commit()


def get_all_invoices(
    limit: int = 500,
    status: str | None = None,
    provider: str | None = None,
) -> list[dict]:
    """Gibt alle Rechnungen zurück (für Verlauf-Seite)."""
    query = "SELECT * FROM invoices WHERE 1=1"
    params: list = []
    if status:
        query += " AND status=?"
        params.append(status)
    if provider:
        query += " AND provider=?"
        params.append(provider)
    query += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def delete_invoice(db_id: int) -> bool:
    """Löscht einen Eintrag aus der Datenbank (wird beim nächsten Lauf neu verarbeitet)."""
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM invoices WHERE id=?", (db_id,))
        conn.commit()
    return cur.rowcount > 0


def reset_invoice(db_id: int) -> bool:
    """Setzt einen Eintrag auf 'pending' zurück (erneuter Upload-Versuch)."""
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE invoices SET status='pending', paperless_id=NULL, updated_at=? WHERE id=?",
            (now, db_id),
        )
        conn.commit()
    return cur.rowcount > 0


def mark_year_complete(provider: str, year: int, invoice_count: int = 0) -> None:
    """Markiert ein Jahr als vollständig gescannt (wird beim nächsten Lauf übersprungen)."""
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO scanned_years (provider, year, invoice_count, completed_at)
            VALUES (?, ?, ?, ?)
            """,
            (provider, year, invoice_count, now),
        )
        conn.commit()
    logger.debug(
        "Jahr %d (%s) als gescannt markiert (%d Rechnungen)",
        year,
        provider,
        invoice_count,
    )


def is_year_complete(provider: str, year: int) -> bool:
    """True wenn das Jahr bereits vollständig gescannt wurde."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM scanned_years WHERE provider=? AND year=?",
            (provider, year),
        ).fetchone()
    return row is not None


def reset_year(provider: str, year: int) -> bool:
    """Löscht den Jahr-Scan-Status (erzwingt erneutes Scannen beim nächsten Lauf)."""
    with get_connection() as conn:
        cur = conn.execute(
            "DELETE FROM scanned_years WHERE provider=? AND year=?",
            (provider, year),
        )
        conn.commit()
    return cur.rowcount > 0


def get_scanned_years(provider: str | None = None) -> list[dict]:
    """Gibt alle gespeicherten Jahres-Scan-Einträge zurück."""
    with get_connection() as conn:
        if provider:
            rows = conn.execute(
                "SELECT * FROM scanned_years WHERE provider=? ORDER BY year DESC",
                (provider,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM scanned_years ORDER BY provider, year DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    """Gibt eine Übersicht über alle Rechnungen zurück."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT provider, status, COUNT(*) as cnt
            FROM invoices
            GROUP BY provider, status
            """
        ).fetchall()
    stats: dict = {}
    for row in rows:
        p = row["provider"]
        stats.setdefault(p, {})
        stats[p][row["status"]] = row["cnt"]
    return stats
