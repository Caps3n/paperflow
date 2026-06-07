#!/bin/bash
# Fix-Script: PDF-Download via JS-fetch + Jahr-Skip-Optimierung
set -e
cd "$(dirname "$0")"

echo "=== 1/6: Git-Lock entfernen ==="
rm -f .git/HEAD.lock .git/index.lock 2>/dev/null || true

echo "=== 2/6: Git commit ==="
git add app/providers/amazon.py app/database.py app/paperless_client.py app/web.py app/ui.html app/main.py app/state.py fix_and_rebuild.sh
git commit -m "feat: product title; date+year tags; incremental scan; parallel uploads; progress bar; error categories; correspondent dropdown" || echo "(nichts zu committen)"
git push origin main || echo "(Push fehlgeschlagen – manuell später erledigen)"

echo "=== 3/6: Schlechte HTML-Dateien aus downloads/ entfernen ==="
BAD=0
for f in downloads/amazon/*.pdf; do
    [ -f "$f" ] || continue
    if ! head -c 4 "$f" | grep -q "%PDF"; then
        rm "$f"
        BAD=$((BAD+1))
    fi
done
echo "  $BAD fehlerhafte HTML-'PDF'-Dateien gelöscht"

echo "=== 4/6: Fehlgeschlagene DB-Einträge zurücksetzen (werden neu versucht) ==="
CONTAINER=$(docker compose ps -q invoice-fetcher 2>/dev/null || true)
if [ -n "$CONTAINER" ]; then
    docker exec "$CONTAINER" sqlite3 /app/data/invoices.db \
        "UPDATE invoices SET status='pending', paperless_id=NULL WHERE status='failed';" \
        2>/dev/null && echo "  DB-Reset erfolgreich" || echo "  DB-Reset übersprungen (Container läuft nicht)"
else
    echo "  Container nicht aktiv – DB-Reset nach Neustart automatisch"
fi

echo "=== 5/6: Container neu bauen ==="
docker compose build invoice-fetcher

echo "=== 6/6: Container neu starten ==="
docker compose up -d invoice-fetcher

echo ""
echo "✓ Fertig!"
echo "  Logs:     http://192.168.178.37:8085/api/logs?lines=50"
echo "  Verlauf:  http://192.168.178.37:8085"
