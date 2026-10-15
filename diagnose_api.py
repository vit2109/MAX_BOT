"""Check DNS and verified HTTPS from the runtime, without reading or sending a bot token."""
import socket
import sys

import httpx

from max_api import API_URL, connection_reason
from urllib.parse import urlparse


def main() -> int:
    host = urlparse(API_URL).hostname
    print(f"MAX API: {API_URL}")
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        print(f"DNS: OK ({len(set(item[4][0] for item in addresses))} адресов)")
    except OSError:
        print("DNS: ошибка разрешения имени сервера MAX")
        return 1
    try:
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            response = client.get(API_URL + "/me")
    except httpx.HTTPError as exc:
        print("HTTPS: " + connection_reason(exc))
        return 1
    print(f"HTTPS: HTTP {response.status_code}")
    if response.status_code == 401:
        print("Соединение и TLS работают. HTTP 401 ожидается: диагностика не передаёт токен.")
        return 0
    if response.status_code == 503:
        print("Получен настоящий HTTP 503 от сервера или шлюза; это не ошибка проверки TLS.")
    return 0 if response.is_success else 1


if __name__ == "__main__":
    sys.exit(main())
