from parser import ParsedMessage


def format_alert(channel_id: str, message: ParsedMessage, keywords: list[str],
                 labels: list[str] | None = None, severity: str | None = None) -> str:
    header = (f"Источник: {message.channel_name or channel_id}\n"
              f"Время: {message.datetime or ''}\n"
              f"Метки: {', '.join(labels or [])}\n"
              f"Найдено: {', '.join(keywords)}\n")
    if message.url:
        header += f"{message.url}\n"
    header = header[:1200]
    available = 4000 - len(header) - 2
    text = message.text
    if len(text) > available:
        text = text[:available - 1] + "…"
    return header + "\n" + text
