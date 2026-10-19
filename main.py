from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from pathlib import Path

from config import ROOT, load_settings
from monitor import run_monitor
from store import MessageStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Мониторинг каналов MAX. По умолчанию открывается веб-интерфейс FastAPI."
    )
    parser.add_argument("--cli", action="store_true", help="Запуск без веб-интерфейса.")
    parser.add_argument("--register-webhook", metavar="HTTPS_URL", help="Зарегистрировать Webhook и завершить.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--interval", type=int, choices=range(1, 3601), metavar="SECONDS", default=None, help="Базовая пауза при ошибках API.")
    parser.add_argument("--once", action="store_true", help="Один пакет событий API (только --cli).")
    parser.add_argument("--no-send", action="store_true", help="Не отправлять совпадения получателям.")
    parser.add_argument("--log-file", type=Path, default=ROOT / "output" / "monitor.log")
    return parser.parse_args()


def setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


def run_cli(args: argparse.Namespace) -> int:
    settings = load_settings()
    if args.interval is not None:
        settings = replace(settings, interval_seconds=args.interval)
    if args.no_send:
        settings = replace(settings, send_enabled=False)
    if not settings.channels:
        logging.error("Список источников пуст. Добавьте каналы в настройках.")
        return 1
    store = MessageStore(settings.db_path)
    try:
        def current_settings():
            current = load_settings()
            if args.interval is not None:
                current = replace(current, interval_seconds=args.interval)
            if args.no_send:
                current = replace(current, send_enabled=False)
            return current
        run_monitor(settings, store, once=args.once, settings_loader=current_settings)
    except KeyboardInterrupt:
        logging.info("Остановлено пользователем.")
    except (ValueError, RuntimeError) as exc:
        logging.error("%s", exc)
        return 1
    finally:
        store.close()
    return 0


def main() -> int:
    args = parse_args()
    setup_logging(args.log_file)
    if args.register_webhook:
        import re
        from urllib.parse import urlparse
        from max_api import MaxAPI

        settings = load_settings()
        url = urlparse(args.register_webhook)
        if url.scheme != "https" or not url.hostname or url.port not in {None, 443}:
            logging.error("Webhook требует публичный HTTPS URL на порту 443")
            return 1
        if not re.fullmatch(r"[A-Za-z0-9_-]{5,256}", settings.webhook_secret):
            logging.error("MAX_WEBHOOK_SECRET: 5–256 символов A-Z, a-z, 0-9, _ или -")
            return 1
        api = None
        try:
            api = MaxAPI(settings.api_token, settings.channel_timeout)
            api.subscribe(args.register_webhook, settings.webhook_secret)
            logging.info("Webhook зарегистрирован")
        except (ValueError, RuntimeError) as exc:
            logging.error("%s", exc)
            return 1
        finally:
            if api is not None:
                api.close()
        return 0
    if args.cli:
        return run_cli(args)
    from server import run_web

    run_web(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
