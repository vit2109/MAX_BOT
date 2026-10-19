"""MAX API payloads and identifiers; no HTML parsing."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


@dataclass
class ParsedMessage:
    index: int | None
    text: str
    time: str | None
    date_label: str | None
    datetime: str | None
    views: str | None
    channel_name: str | None
    message_id: str | None = None
    url: str | None = None


def message_from_api(raw: dict, channel_name: str | None = None) -> ParsedMessage:
    body = raw.get("body") or {}
    mid = body.get("mid")
    timestamp = raw.get("timestamp")
    text = str(body.get("text") or "")
    link = raw.get("link") or {}
    if link.get("type") == "forward":
        forwarded = link.get("message") or {}
        forwarded_text = str(forwarded.get("text") or "")
        if forwarded_text:
            text = "\n\n".join(part for part in (text, forwarded_text) if part)
        if not mid and forwarded.get("mid") and isinstance(timestamp, (int, float)):
            # MAX may omit the wrapper body for a pure forward. Keep separate forwards distinct.
            mid = f"forward:{forwarded['mid']}:{timestamp}"
    if not isinstance(mid, str) or not mid or not isinstance(timestamp, (int, float)):
        raise ValueError("Сообщение API без body.mid или timestamp")
    when = datetime.fromtimestamp(timestamp / 1000, timezone.utc)
    stats = raw.get("stat") or {}
    return ParsedMessage(
        index=None, text=text,
        time=when.strftime("%H:%M"), date_label=when.date().isoformat(),
        datetime=when.isoformat(timespec="seconds"),
        views=str(stats["views"]) if "views" in stats else None,
        channel_name=channel_name, message_id=mid, url=raw.get("url"),
    )


def message_uid(channel_id: str, message: ParsedMessage) -> str:
    if message.message_id:
        return f"max:{channel_id}:{message.message_id}"
    normalized = re.sub(r"\s+", " ", message.text.strip().lower())
    basis = "|".join([channel_id, message.datetime or "", message.time or "", normalized])
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def normalize_channel_id(value: str) -> str:
    value = value.strip()
    if value.startswith(("https://", "http://")):
        parsed = urlparse(value)
        if parsed.hostname not in {"web.max.ru", "max.ru"}:
            raise ValueError("Ожидается числовой ID MAX или ссылка max.ru с числовым ID")
        value = parsed.path.strip("/")
        if value and not re.fullmatch(r"-?[1-9]\d*", value):
            raise ValueError(
                "Публичная ссылка max.ru/имя_канала не содержит chat_id. "
                "Добавьте бота в канал и укажите числовой chat_id из события bot_added "
                "(или ссылку web.max.ru/-123456789)."
            )
    if not re.fullmatch(r"-?[1-9]\d*", value):
        raise ValueError("ID MAX должен быть ненулевым целым числом; «0 / избранное» Bot API не поддерживает")
    return str(int(value))


def normalize_recipient(value: str) -> str:
    value = value.strip()
    for prefix in ("user:", "chat:"):
        if value.startswith(prefix):
            number = normalize_channel_id(value[len(prefix):])
            if prefix == "user:" and int(number) < 1:
                raise ValueError("user: требует положительный user_id")
            return prefix + number
    return "chat:" + normalize_channel_id(value)


def load_channel_ids(path: Path) -> list[str]:
    return list(dict.fromkeys(normalize_channel_id(line) for line in path.read_text(encoding="utf-8").splitlines()
                              if line.strip() and not line.strip().startswith("#")))
