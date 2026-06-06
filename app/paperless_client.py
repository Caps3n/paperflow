"""
Paperless-NGX REST API Client.
Dokumentiert unter: https://docs.paperless-ngx.com/api/
"""

import logging
import os
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


class PaperlessClient:
    def __init__(self):
        self.base_url = os.environ["PAPERLESS_URL"].rstrip("/")
        self.token = os.environ["PAPERLESS_TOKEN"]
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Token {self.token}",
            }
        )

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/{path.lstrip('/')}"

    def test_connection(self) -> bool:
        """Prüft ob Paperless-NGX erreichbar ist."""
        try:
            r = self.session.get(self._url("documents/"), timeout=10)
            r.raise_for_status()
            logger.info("Paperless-NGX Verbindung OK: %s", self.base_url)
            return True
        except Exception as e:
            logger.error("Paperless-NGX nicht erreichbar: %s", e)
            return False

    def upload_document(
        self,
        file_path: Path,
        title: str | None = None,
        tags: list[str] | None = None,
        correspondent: str | None = None,
        created_date: str | None = None,
    ) -> int | None:
        """
        Lädt ein Dokument zu Paperless-NGX hoch.
        Gibt die Paperless-Dokument-ID zurück, oder None bei Fehler.
        """
        tag_ids = []
        if tags:
            for tag_name in tags:
                tag_id = self._get_or_create_tag(tag_name)
                if tag_id:
                    tag_ids.append(tag_id)

        correspondent_id = None
        if correspondent:
            correspondent_id = self._get_or_create_correspondent(correspondent)

        with open(file_path, "rb") as f:
            data = {}
            if title:
                data["title"] = title
            if tag_ids:
                for tid in tag_ids:
                    # Paperless erwartet mehrere 'tags' Felder
                    pass
            if correspondent_id:
                data["correspondent"] = correspondent_id
            if created_date:
                data["created"] = created_date

            files = {"document": (file_path.name, f, "application/pdf")}

            # Tags als mehrfache Form-Felder
            form_data = list(data.items())
            for tid in tag_ids:
                form_data.append(("tags", tid))

            try:
                r = self.session.post(
                    self._url("documents/post_document/"),
                    files=files,
                    data=form_data,
                    timeout=120,
                )
                r.raise_for_status()
                # Paperless gibt die Task-ID zurück, nicht direkt die Dokument-ID
                task_id = r.text.strip().strip('"')
                logger.info(
                    "Dokument hochgeladen, Task-ID: %s (%s)", task_id, file_path.name
                )
                # Wir geben task_id als String zurück (wird in DB gespeichert)
                return task_id
            except requests.HTTPError as e:
                logger.error("Upload fehlgeschlagen: %s – %s", e, r.text[:200])
                return None

    def _get_or_create_tag(self, name: str) -> int | None:
        """Gibt existierenden Tag zurück oder erstellt einen neuen."""
        try:
            r = self.session.get(self._url("tags/"), params={"name": name}, timeout=10)
            r.raise_for_status()
            results = r.json().get("results", [])
            if results:
                return results[0]["id"]
            # Tag existiert nicht → erstellen
            r = self.session.post(
                self._url("tags/"),
                json={"name": name},
                timeout=10,
            )
            r.raise_for_status()
            return r.json()["id"]
        except Exception as e:
            logger.warning("Tag '%s' konnte nicht gesetzt werden: %s", name, e)
            return None

    def _get_or_create_correspondent(self, name: str) -> int | None:
        """Gibt existierenden Korrespondenten zurück oder erstellt einen neuen."""
        try:
            r = self.session.get(
                self._url("correspondents/"), params={"name": name}, timeout=10
            )
            r.raise_for_status()
            results = r.json().get("results", [])
            if results:
                return results[0]["id"]
            r = self.session.post(
                self._url("correspondents/"),
                json={"name": name},
                timeout=10,
            )
            r.raise_for_status()
            return r.json()["id"]
        except Exception as e:
            logger.warning("Korrespondent '%s' nicht gesetzt: %s", name, e)
            return None
