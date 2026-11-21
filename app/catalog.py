"""The chat models OpenRouter offers, for the Chat model field's suggestions.

OpenRouter lists its models publicly (no key needed). The list is kept for a
while, since it changes a few times a week at most; when it cannot be had, the
dashboard keeps its own few suggestions.
"""

import asyncio
import time

import httpx

from app.openrouter import DEFAULT_BASE_URL, new_client

CATALOG_KEEP_SECONDS = 6 * 60 * 60
# A list that could not be had is asked for again after this long, not on every page load.
CATALOG_RETRY_SECONDS = 5 * 60


class ModelCatalog:
    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, client: httpx.AsyncClient | None = None):
        self._url = f"{base_url}/models"
        self._client = client or new_client(timeout=15)
        self._models: list[dict] = []
        self._asked_at = 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self) -> list[dict]:
        """[{"id", "name"}] of each model that reads and writes text, by ID; [] when unknown."""
        async with self._lock:
            keep = CATALOG_KEEP_SECONDS if self._models else CATALOG_RETRY_SECONDS
            if self._asked_at and time.monotonic() - self._asked_at < keep:
                return self._models
            self._asked_at = time.monotonic()
            try:
                response = await self._client.get(self._url)
                data = response.json()["data"] if response.status_code == 200 else None
            except (httpx.HTTPError, ValueError, LookupError, TypeError):
                data = None
            if isinstance(data, list):
                found = [model for model in map(_chat_model, data) if model]
                if found:
                    self._models = sorted(found, key=lambda model: model["id"])
            return self._models


def _chat_model(entry: object) -> dict | None:
    """The model as a suggestion, or None when it cannot write a chat reply."""
    if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
        return None
    architecture = entry.get("architecture") if isinstance(entry.get("architecture"), dict) else {}
    inputs = architecture.get("input_modalities") or ["text"]
    outputs = architecture.get("output_modalities") or ["text"]
    if "text" not in inputs or "text" not in outputs:
        return None
    name = entry.get("name") if isinstance(entry.get("name"), str) else ""
    return {"id": entry["id"][:200], "name": name[:120] or entry["id"][:200]}
