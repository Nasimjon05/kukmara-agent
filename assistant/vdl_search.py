"""VDL Market (Telegram mini app) catalogue search.

Read-only: only products on sale and in stock, and available dishes from open kitchens.
The API never exposes customers, orders or phone numbers. Any failure returns an empty list.
"""
from __future__ import annotations

import logging
from typing import List, Optional

import httpx

log = logging.getLogger("assistant.vdl")

SEARCH_TYPES = ("market", "food")
MAX_LIMIT = 10
TIMEOUT_SECONDS = 8.0
USER_AGENT = "KukmaraAssistant/1.0"


class VdlSearch:
    def __init__(
        self,
        api_url: str,
        api_key: str,
        *,
        timeout: float = TIMEOUT_SECONDS,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.api_url = api_url
        self._api_key = api_key
        self.timeout = timeout
        self._transport = transport

    def __repr__(self) -> str:
        return f"VdlSearch(api_url={self.api_url!r})"

    async def search(
        self, q: str, type_: str = "market", limit: int = 3, max_price: Optional[int] = None
    ) -> List[dict]:
        params = {
            "q": q,
            "type": type_ if type_ in SEARCH_TYPES else "market",
            "limit": max(1, min(int(limit), MAX_LIMIT)),
        }
        if max_price:
            params["max_price"] = int(max_price)
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
                response = await client.get(
                    self.api_url,
                    params=params,
                    headers={"Authorization": f"Bearer {self._api_key}", "User-Agent": USER_AGENT},
                )
                response.raise_for_status()
                items = response.json()["items"]
        except Exception as exc:  # the assistant must keep working whatever the API does
            log.warning("VDL search failed (q=%r, type=%s): %s: %s", q, params["type"], type(exc).__name__, exc)
            return []
        if not isinstance(items, list):
            log.warning("VDL search returned unexpected data for q=%r", q)
            return []
        return [item for item in items if isinstance(item, dict) and item.get("name")]


def make_vdl_search(settings) -> Optional[VdlSearch]:
    if not settings.vdl_api_key:
        return None
    return VdlSearch(settings.vdl_api_url, settings.vdl_api_key)
