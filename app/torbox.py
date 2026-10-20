from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger("torbox")


class TorBoxError(Exception):
    """TorBox answered, but not usefully."""


class TorBoxUnavailable(TorBoxError):
    """TorBox or the network is having a bad minute; worth trying again."""


class TorBoxNoLink(TorBoxError):
    """TorBox answered for this file, but there is no playable link."""


class TorBoxRateLimited(TorBoxError):
    def __init__(self, retry_after: int):
        super().__init__("TorBox is rate limiting the addon")
        self.retry_after = retry_after


# Which endpoint and parameter name each collection uses. The three APIs are
# deliberately near-identical; this is the whole difference.
KINDS: dict[str, tuple[str, str]] = {
    "torrents": ("torrents", "torrent_id"),
    "usenet": ("usenet", "usenet_id"),
    "webdl": ("webdl", "web_id"),
}


class TorBox:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.torbox.app",
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
            transport=transport,
            follow_redirects=True,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "bitchord-torbox-addon/1.0",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        delay = 1.0
        for attempt in range(3):
            try:
                response = await self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                if attempt == 2:
                    # Do not stringify the exception: requestdl carries the API
                    # key in its query string, and httpx puts the URL in it.
                    raise TorBoxUnavailable(
                        f"TorBox request failed ({type(exc).__name__})"
                    ) from exc
                await asyncio.sleep(delay)
                delay *= 2
                continue

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After") or delay)
                if attempt == 2:
                    raise TorBoxRateLimited(retry_after)
                await asyncio.sleep(max(1, retry_after))
                delay *= 2
                continue
            if response.status_code >= 500:
                if attempt == 2:
                    raise TorBoxUnavailable(f"TorBox returned HTTP {response.status_code}")
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if response.status_code in (401, 403):
                raise TorBoxError("TorBox rejected the API key")
            if response.status_code >= 400:
                raise TorBoxError(f"TorBox returned HTTP {response.status_code}")

            try:
                body = response.json()
            except ValueError as exc:
                raise TorBoxUnavailable("TorBox returned a non-JSON response") from exc
            if isinstance(body, dict) and body.get("success") is False:
                detail = body.get("detail") or body.get("error") or "request failed"
                raise TorBoxError(str(detail))
            return body.get("data") if isinstance(body, dict) else body

        raise TorBoxUnavailable("TorBox did not answer")

    async def list_items(self, kind: str, page_size: int = 1000, max_pages: int = 50) -> list[dict]:
        """Every item in one of the account's three collections."""
        api_name, _ = KINDS[kind]
        items: list[dict] = []
        offset = 0
        for _ in range(max_pages):
            page = await self._get(
                f"/v1/api/{api_name}/mylist",
                {"offset": offset, "limit": page_size, "bypass_cache": "false"},
            )
            if not isinstance(page, list):
                raise TorBoxError(f"Unexpected {kind} list response")
            items.extend(item for item in page if isinstance(item, dict))
            if len(page) < page_size:
                break
            offset += page_size
        return items

    async def download_link(self, kind: str, item_id: int, file_id: int) -> str:
        """A CDN URL for one file, valid for about three hours."""
        api_name, id_param = KINDS[kind]
        data = await self._get(
            f"/v1/api/{api_name}/requestdl",
            {
                "token": self._api_key,
                id_param: item_id,
                "file_id": file_id,
                "redirect": "false",
            },
        )
        if not isinstance(data, str) or not data.startswith(("http://", "https://")):
            raise TorBoxNoLink("TorBox did not return a download link")
        return data
