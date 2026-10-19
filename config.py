from __future__ import annotations

import configparser
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from matcher import KeywordGroup, flatten_keywords, groups_from_payload
from channel_links import normalize_source
from parser import load_channel_ids, normalize_recipient


def _bundle_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BUNDLE_DIR = _bundle_dir()
ROOT = _app_dir()

_USER_FILES = (
    "config.ini",
    "channels.txt",
    "keywords.txt",
    "keywords.json",
    "recipients.txt",
)


def _ensure_user_files() -> None:
    if BUNDLE_DIR.resolve() == ROOT.resolve():
        return
    for name in _USER_FILES:
        dest = ROOT / name
        src = BUNDLE_DIR / name
        if not dest.exists() and src.exists():
            dest.write_bytes(src.read_bytes())


_ensure_user_files()


def _local_env(root: Path) -> dict[str, str]:
    """Read the simple KEY=value settings used by the project's .env template."""
    path = root / ".env"
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key or key.startswith("#"):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def env_setting(name: str, *, root: Path = ROOT, default: str = "") -> str:
    """Process environment takes precedence over .env beside the application."""
    value = os.environ.get(name)
    if value is None:
        value = _local_env(root).get(name, default)
    return value.strip()


@dataclass(frozen=True)
class Settings:
    interval_seconds: int
    channel_timeout: int
    min_keyword_hits: int
    send_enabled: bool
    send_only_critical: bool
    channels: list[str]
    keywords: list[str]
    keyword_groups: list[KeywordGroup]
    recipients: list[str]
    db_path: Path
    output_dir: Path
    api_token: str = field(default="", repr=False)
    receive_mode: str = "polling"
    webhook_secret: str = field(default="", repr=False)


def load_settings(
    *,
    root: Path = ROOT,
    config_path: Path | None = None,
    channels_path: Path | None = None,
    keywords_path: Path | None = None,
    recipients_path: Path | None = None,
) -> Settings:
    config_path = config_path or root / "config.ini"
    parser = configparser.ConfigParser()
    if config_path.exists():
        parser.read(config_path, encoding="utf-8")
    monitor = parser["monitor"] if parser.has_section("monitor") else {}

    channels_file = channels_path or root / "channels.txt"
    keywords_file = keywords_path or root / "keywords.txt"
    recipients_file = recipients_path or root / "recipients.txt"
    groups = _load_keyword_groups(root, keywords_file)

    return Settings(
        interval_seconds=_int(monitor, "interval_seconds", 30),
        channel_timeout=_int(monitor, "channel_timeout", 30),
        min_keyword_hits=_int(monitor, "min_keyword_hits", 1),
        send_enabled=_bool(monitor, "send_enabled", True),
        send_only_critical=_bool(monitor, "send_only_critical", False),
        channels=load_channel_ids(channels_file) if channels_file.exists() else [],
        keywords=flatten_keywords(groups),
        keyword_groups=groups,
        recipients=[normalize_recipient(item) for item in _load_lines(recipients_file)],
        db_path=root / "data" / "messages.sqlite",
        output_dir=root / "output",
        api_token=env_setting("MAX_BOT_TOKEN", root=root),
        receive_mode=env_setting("MAX_RECEIVE_MODE", root=root, default="polling").lower(),
        webhook_secret=env_setting("MAX_WEBHOOK_SECRET", root=root),
    )


def settings_payload(settings: Settings | None = None) -> dict:
    current = settings or load_settings()
    return {
        "interval_seconds": current.interval_seconds,
        "channel_timeout": current.channel_timeout,
        "receive_mode": current.receive_mode,
        "token_configured": bool(current.api_token),
        "min_keyword_hits": current.min_keyword_hits,
        "send_enabled": current.send_enabled,
        "send_only_critical": current.send_only_critical,
        "channels": list(current.channels),
        "keywords": list(current.keywords),
        "keyword_groups": [
            {
                "id": group.id,
                "name": group.name,
                "role": group.role,
                "keywords": list(group.keywords),
            }
            for group in current.keyword_groups
        ],
        "recipients": list(current.recipients),
    }


def save_settings(
    *,
    interval_seconds: int,
    channel_timeout: int,
    min_keyword_hits: int,
    send_enabled: bool,
    channels: list[str],
    recipients: list[str],
    send_only_critical: bool = False,
    keywords: list[str] | None = None,
    keyword_groups: list[dict] | None = None,
    root: Path = ROOT,
) -> Settings:
    channels = _clean_ids(channels)
    recipients = list(dict.fromkeys(normalize_recipient(item) for item in _clean_lines(recipients)))
    if set(channels) & {item.removeprefix("chat:") for item in recipients if item.startswith("chat:")}:
        raise ValueError("Источник и чат рассылки не должны совпадать (защита от зацикливания)")
    groups = groups_from_payload(keyword_groups, fallback_keywords=_clean_lines(keywords or []))
    if interval_seconds < 1 or interval_seconds > 3600:
        raise ValueError("interval_seconds должен быть от 1 до 3600")
    if channel_timeout < 5 or channel_timeout > 300:
        raise ValueError("channel_timeout должен быть от 5 до 300")
    if min_keyword_hits < 1 or min_keyword_hits > 20:
        raise ValueError("min_keyword_hits должен быть от 1 до 20")

    _write_ini(
        root / "config.ini",
        interval_seconds=interval_seconds,
        channel_timeout=channel_timeout,
        min_keyword_hits=min_keyword_hits,
        send_enabled=send_enabled,
        send_only_critical=send_only_critical,
    )
    _write_lines(
        root / "channels.txt",
        channels,
        "# id канала или полный URL веб-версии, по одному в строке",
    )
    _write_lines(
        root / "keywords.txt",
        flatten_keywords(groups),
        "# Плоский список всех ключевых слов. Источник групп — keywords.json",
    )
    _write_keyword_groups(root / "keywords.json", groups)
    _write_lines(
        root / "recipients.txt",
        recipients,
        "# Адресаты Bot API: chat:-123456789 или user:123456789",
    )
    return load_settings(root=root)


def _clean_ids(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in _clean_lines(values):
        normalized = normalize_source(item)
        if normalized and normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return result


def _clean_lines(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        for raw in str(value).replace("\r", "").split("\n"):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            result.append(line)
    return result


def _write_lines(path: Path, values: list[str], header: str) -> None:
    body = "\n".join(values)
    path.write_text(f"{header}\n{body}\n" if body else f"{header}\n", encoding="utf-8")


def _write_ini(
    path: Path,
    *,
    interval_seconds: int,
    channel_timeout: int,
    min_keyword_hits: int,
    send_enabled: bool,
    send_only_critical: bool = False,
) -> None:
    enabled = "true" if send_enabled else "false"
    only_critical = "true" if send_only_critical else "false"
    path.write_text(
        (
            "[monitor]\n"
            "; Базовая пауза при временных ошибках API, секунды\n"
            f"interval_seconds = {interval_seconds}\n"
            f"channel_timeout = {channel_timeout}\n"
            "; Сколько ключевых слов должно совпасть, чтобы отправить сообщение\n"
            f"min_keyword_hits = {min_keyword_hits}\n"
            "; Отправка совпадений через официальный Bot API MAX\n"
            f"send_enabled = {enabled}\n"
            "; Слать только критические аварийные сообщения\n"
            f"send_only_critical = {only_critical}\n"
        ),
        encoding="utf-8",
    )


def _load_keyword_groups(root: Path, keywords_file: Path) -> list[KeywordGroup]:
    json_path = root / "keywords.json"
    raw_groups: list[dict] = []
    if json_path.exists():
        data = json.loads(json_path.read_text(encoding="utf-8"))
        raw_groups = data if isinstance(data, list) else data.get("groups", [])
    fallback = _load_lines(keywords_file) if keywords_file.exists() else []
    return groups_from_payload(raw_groups, fallback_keywords=fallback)


def _write_keyword_groups(path: Path, groups: list[KeywordGroup]) -> None:
    payload = {
        "groups": [
            {
                "id": group.id,
                "name": group.name,
                "role": group.role,
                "keywords": list(group.keywords),
            }
            for group in groups
        ]
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    values: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        values.append(line)
    return values


def _int(section, key: str, default: int) -> int:
    try:
        return int(section.get(key, default))
    except (TypeError, ValueError):
        return default


def _bool(section, key: str, default: bool) -> bool:
    raw = str(section.get(key, default)).strip().lower()
    return raw in {"1", "true", "yes", "on"}
