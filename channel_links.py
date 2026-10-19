"""Resolve public MAX channel links to Bot API chat IDs when settings are saved."""
from __future__ import annotations

import re
import ssl
from urllib.parse import urlparse

import httpx

from parser import normalize_channel_id


_SLUG = re.compile(r"/[A-Za-z0-9_][A-Za-z0-9_-]{0,127}/?")
_CHANNEL_ID = re.compile(r"\bchannelId:(\d{1,20})\b")
_CANONICAL = re.compile(r'\bcanonical:"(https://max\.ru/[^"?#]+)"')
_RESOLVE_ERROR = (
    "Не удалось определить chat_id по публичной ссылке MAX. "
    "Проверьте адрес или укажите числовой ID из события bot_added."
)


def normalize_source(value: str, *, transport: httpx.BaseTransport | None = None) -> str:
    """Keep numeric links local; resolve a public max.ru/<name> link once on save."""
    try:
        return normalize_channel_id(value)
    except ValueError:
        parsed = urlparse(value.strip())
        if parsed.scheme != "https" or parsed.netloc != "max.ru" or not _SLUG.fullmatch(parsed.path):
            raise
        return resolve_public_channel_id(parsed.path.strip("/"), transport=transport)


def resolve_public_channel_id(slug: str, *, transport: httpx.BaseTransport | None = None) -> str:
    url = f"https://max.ru/{slug}"
    try:
        with httpx.Client(timeout=10, follow_redirects=False,
                          verify=ssl.create_default_context(), transport=transport) as client:
            with client.stream("GET", url, headers={"Accept": "text/html"}) as response:
                if response.status_code != 200 or "text/html" not in response.headers.get("content-type", ""):
                    raise ValueError(_RESOLVE_ERROR)
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 256_000:
                        raise ValueError(_RESOLVE_ERROR)
    except httpx.HTTPError:
        raise ValueError(_RESOLVE_ERROR) from None

    page = body.decode("utf-8", errors="replace")
    ids = set(_CHANNEL_ID.findall(page))
    canonical = set(_CANONICAL.findall(page))
    if len(ids) != 1 or {url.casefold()} != {item.casefold() for item in canonical}:
        raise ValueError(_RESOLVE_ERROR)
    # Public pages expose the positive channelId; Bot API channel chat_id is negative.
    return normalize_channel_id("-" + ids.pop())
