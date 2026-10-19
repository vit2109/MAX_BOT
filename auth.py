"""Small, process-local web sessions and an append-only access journal."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from config import ROOT, env_setting

COOKIE_NAME = "max_web_session"
SESSION_SECONDS = 12 * 60 * 60
ACCESS_LOG = ROOT / "data" / "access.jsonl"
log = logging.getLogger("max_monitor")
_lock = threading.Lock()
_sessions: dict[str, tuple[float, bytes]] = {}


def configured() -> bool:
    return bool(env_setting("MAX_WEB_USER") and env_setting("MAX_WEB_PASSWORD"))


def _credential_tag() -> bytes:
    username = env_setting("MAX_WEB_USER")
    password = env_setting("MAX_WEB_PASSWORD")
    return hashlib.sha256(f"{username}\0{password}".encode()).digest()


def authenticate(username: str, password: str) -> str | None:
    expected_user = env_setting("MAX_WEB_USER")
    expected_password = env_setting("MAX_WEB_PASSWORD")
    if not expected_user or not expected_password:
        return None
    valid_user = hmac.compare_digest(username.encode(), expected_user.encode())
    valid_password = hmac.compare_digest(password.encode(), expected_password.encode())
    if not (valid_user and valid_password):
        return None
    token = secrets.token_urlsafe(32)
    with _lock:
        now = time.time()
        for key, value in list(_sessions.items()):
            if value[0] <= now:
                del _sessions[key]
        _sessions[token] = (now + SESSION_SECONDS, _credential_tag())
    return token


def authenticated(token: str | None) -> bool:
    if not token or not configured():
        return False
    with _lock:
        session = _sessions.get(token)
        if session is None:
            return False
        if session[0] <= time.time() or not hmac.compare_digest(session[1], _credential_tag()):
            _sessions.pop(token, None)
            return False
        return True


def logout(token: str | None) -> None:
    if token:
        with _lock:
            _sessions.pop(token, None)


def record_access(event: str, ip: str, *, username: str | None = None) -> None:
    entry = {"time": datetime.now(timezone.utc).isoformat(), "event": event, "ip": ip}
    if username is not None:
        entry["username"] = username[:100]
    try:
        with _lock:
            ACCESS_LOG.parent.mkdir(parents=True, exist_ok=True)
            with ACCESS_LOG.open("a", encoding="utf-8") as file:
                file.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("Не удалось записать журнал доступа")
