"""Read-only diagnostics: never consumes polling updates, sends messages or changes subscriptions."""
from config import load_settings
from max_api import MaxAPI, MaxAPIError


def main() -> int:
    settings = load_settings()
    api = MaxAPI(settings.api_token, settings.channel_timeout)
    errors = 0
    try:
        bot = api.me()
        print(f"Бот: user_id={bot['user_id']}; режим={settings.receive_mode}")
        subscriptions = api.subscriptions()
        print(f"Webhook-подписок: {len(subscriptions)}")
        if settings.receive_mode == "polling" and subscriptions:
            print("ОШИБКА: polling включён одновременно с Webhook; выберите один способ получения событий")
            errors += 1
        if settings.receive_mode == "webhook" and not subscriptions:
            print("ОШИБКА: Webhook не зарегистрирован; входящие события доставляться не будут")
            errors += 1
        if not settings.channels:
            print("ОШИБКА: список источников пуст")
            errors += 1
        for channel_id in settings.channels:
            try:
                chat = api.chat(channel_id)
                try:
                    member = api.membership(channel_id)
                except MaxAPIError as exc:
                    if exc.status not in {403, 404}:
                        raise
                    print(f"Источник {channel_id}: членство не подтверждено (HTTP {exc.status}); проверяем чтение постов отдельно")
                else:
                    print(f"Источник {channel_id}: статус={chat.get('status')}; администратор={member.get('is_admin')}; права={member.get('permissions')}")
                history = api.messages(channel_id)
                print(f"  GET /messages доступен, записей={len(history)}; опрос постов разрешён (текст не выводится)")
            except MaxAPIError as exc:
                errors += 1
                print(f"  Источник {channel_id}: {exc}")
                if exc.status in {403, 404}:
                    print("  Проверьте, что именно этот бот добавлен в источник и назначен администратором.")
        print("Мониторинг загружает до 100 последних постов без рассылки истории и затем получает новые сообщения.")
        return int(errors > 0)
    except (ValueError, MaxAPIError) as exc:
        print(str(exc))
        return 1
    finally:
        api.close()


if __name__ == "__main__":
    raise SystemExit(main())
