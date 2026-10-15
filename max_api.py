"""Synchronous client for the official MAX Bot API."""
from __future__ import annotations

import time
import httpx

API_URL = "https://platform-api2.max.ru"


class MaxAPIError(RuntimeError):
    def __init__(self, status: int, retry_after: float = 1):
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"MAX API: HTTP {status}")


class MaxAPI:
    def __init__(self, token: str, timeout: int = 30, *, transport=None):
        if not token:
            raise ValueError("Задайте переменную окружения MAX_BOT_TOKEN")
        self.client = httpx.Client(base_url=API_URL, headers={"Authorization": token},
                                   timeout=timeout, transport=transport, follow_redirects=False)
        self._last_send: dict[str, float] = {}

    def close(self):
        self.client.close()

    def request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise MaxAPIError(503) from None
        if response.is_error:
            try:
                delay = min(300, max(1, float(response.headers.get("Retry-After", "1"))))
            except ValueError:
                delay = 1
            raise MaxAPIError(response.status_code, delay)
        try:
            data = response.json()
        except ValueError:
            raise MaxAPIError(502) from None
        if not isinstance(data, dict):
            raise MaxAPIError(502)
        if data.get("success") is False:
            raise MaxAPIError(400)
        return data

    def me(self) -> dict:
        return self.request("GET", "/me")

    def chat(self, channel_id: str) -> dict:
        return self.request("GET", f"/chats/{channel_id}")

    def updates(self, marker: str | None, timeout: int = 20) -> dict:
        params = {"types": "message_created", "limit": 100, "timeout": timeout}
        if marker is not None:
            params["marker"] = marker
        return self.request("GET", "/updates", params=params, timeout=timeout + 10)

    def subscribe(self, url: str, secret: str) -> dict:
        return self.request("POST", "/subscriptions", json={
            "url": url, "update_types": ["message_created"], "secret": secret})

    def send_text(self, recipient: str, text: str) -> None:
        kind, number = recipient.split(":", 1)
        delay = 0.55 - (time.monotonic() - self._last_send.get(recipient, 0))
        if delay > 0:
            time.sleep(delay)
        self._last_send[recipient] = time.monotonic()
        result = self.request("POST", "/messages", params={f"{kind}_id": int(number)},
                              json={"text": text[:4000]})
        if not (result.get("message") or {}).get("body", {}).get("mid"):
            raise MaxAPIError(502)
