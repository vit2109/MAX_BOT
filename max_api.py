"""Synchronous client for the official MAX Bot API."""
from __future__ import annotations

import time
import socket
import ssl
import os
import httpx

from config import ROOT

API_URL = "https://platform-api2.max.ru"


def tls_verify() -> str | bool:
    """Use a local complete CA bundle unless the process selected one explicitly."""
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return True
    local_bundle = ROOT / "certs" / "ca-bundle.pem"
    return str(local_bundle) if local_bundle.is_file() else True


class MaxAPIError(RuntimeError):
    def __init__(self, status: int | None, retry_after: float = 1, *,
                 reason: str = "", endpoint: str = ""):
        self.status = status
        self.retry_after = retry_after
        message = f"MAX API: HTTP {status}" if status is not None else "MAX API: ошибка соединения"
        if reason:
            message += f" — {reason}"
        if endpoint:
            message += f" ({endpoint})"
        super().__init__(message)


def connection_reason(exc: httpx.HTTPError) -> str:
    """Return fixed diagnostic text, never raw exceptions containing URLs or secrets."""
    causes = []
    current: BaseException | None = exc
    while current is not None and all(current is not previous for previous in causes):
        causes.append(current)
        current = current.__cause__ or current.__context__
    if any(isinstance(cause, ssl.SSLCertVerificationError) or
           "CERTIFICATE_VERIFY_FAILED" in str(cause) for cause in causes):
        return "не пройдена проверка TLS-сертификата; проверьте SSL_CERT_FILE или certs/ca-bundle.pem"
    if isinstance(exc, httpx.TimeoutException):
        return "истекло время ожидания ответа MAX; проверьте подключение к интернету"
    if isinstance(exc, httpx.ProxyError):
        return "ошибка подключения через прокси; проверьте HTTPS_PROXY"
    if any(isinstance(cause, socket.gaierror) for cause in causes):
        return "DNS не разрешает имя сервера MAX; проверьте настройки DNS"
    if isinstance(exc, httpx.ConnectError):
        return "не удалось подключиться к MAX; проверьте сеть, DNS и прокси"
    return "соединение с MAX прервано до получения корректного HTTP-ответа"


class MaxAPI:
    def __init__(self, token: str, timeout: int = 30, *, transport=None):
        if not token:
            raise ValueError("Задайте переменную окружения MAX_BOT_TOKEN")
        self.client = httpx.Client(base_url=API_URL, headers={"Authorization": token},
                                   timeout=timeout, transport=transport, follow_redirects=False,
                                   verify=tls_verify())
        self._last_send: dict[str, float] = {}

    def close(self):
        self.client.close()

    def request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise MaxAPIError(None, reason=connection_reason(exc), endpoint=f"{method} {path}") from None
        if response.is_error:
            try:
                delay = min(300, max(1, float(response.headers.get("Retry-After", "1"))))
            except ValueError:
                delay = 1
            reason = "сервер или шлюз MAX временно недоступен" if response.status_code == 503 else ""
            raise MaxAPIError(response.status_code, delay, reason=reason, endpoint=f"{method} {path}")
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

    def membership(self, channel_id: str) -> dict:
        return self.request("GET", f"/chats/{channel_id}/members/me")

    def subscriptions(self) -> list[dict]:
        result = self.request("GET", "/subscriptions").get("subscriptions")
        if not isinstance(result, list):
            raise MaxAPIError(502)
        return result

    def messages(self, channel_id: str, count: int = 1, *, before: int | None = None) -> list[dict]:
        params = {"chat_id": channel_id, "count": count}
        if before is not None:
            params["from"] = before
        result = self.request("GET", "/messages", params=params).get("messages")
        if not isinstance(result, list):
            raise MaxAPIError(502)
        return result

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
