from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime

from alerts import format_alert
from config import Settings, load_settings
from matcher import match_message
from max_api import MaxAPI, MaxAPIError
from parser import message_from_api, message_uid
from store import MessageStore, append_jsonl, message_record

log = logging.getLogger("max_monitor")


class StopFlag:
    def __init__(self):
        self._event = threading.Event()

    def stop(self):
        self._event.set()

    def stopped(self):
        return self._event.is_set()

    def wait(self, seconds):
        return self._event.wait(seconds)


def rematch_existing(store: MessageStore, settings: Settings) -> int:
    # Recompute both positives and negatives: changed rules must clear old false alarms.
    rows = store._conn.execute("SELECT uid, text FROM messages").fetchall()
    count = 0
    with store._conn:
        for uid, text in rows:
            result = match_message(text, settings.keyword_groups, settings.min_keyword_hits)
            store._conn.execute(
                "UPDATE messages SET matched=?, matched_keywords=?, labels=?, severity=? WHERE uid=?",
                (int(result.matched), ", ".join(result.all_hits), ", ".join(result.labels), result.severity, uid))
            count += int(result.matched)
    return count


def process_update(update: dict, store: MessageStore, settings: Settings, bot_id: int,
                   on_match=None, on_channel=None, channel_names: dict | None = None) -> None:
    if update.get("update_type") != "message_created":
        return
    raw = update.get("message") or {}
    channel_id = str((raw.get("recipient") or {}).get("chat_id") or "")
    if channel_id not in settings.channels:
        return
    if (raw.get("sender") or {}).get("user_id") == bot_id:
        return
    message = message_from_api(raw, channel_name=(channel_names or {}).get(channel_id, channel_id))
    uid = message_uid(channel_id, message)
    now = datetime.now().astimezone()
    items = store.insert_new(channel_id, [message], now)
    store.mark_bootstrapped(channel_id, now)
    if items:
        append_jsonl(settings.output_dir / "messages.jsonl", message_record(channel_id, uid, message))
    if on_channel:
        on_channel({"channel_id": channel_id, "visible": 1, "new": len(items)})
    # Replay analysis even for an inserted message: a crash may have occurred before queue_alert.
    result = match_message(message.text, settings.keyword_groups, settings.min_keyword_hits)
    if not result.matched:
        return
    store.mark_matched(uid, result.all_hits, result.labels, result.severity)
    alert = format_alert(channel_id, message, result.all_hits, result.labels, result.severity)
    if settings.send_enabled and (not settings.send_only_critical or result.severity == "critical"):
        recipients = [r for r in settings.recipients if r != f"chat:{channel_id}"]
        store.queue_alert(uid, recipients, alert)
    if items:
        record = message_record(channel_id, uid, message, keywords=result.all_hits)
        record.update(labels=result.labels, severity=result.severity)
        append_jsonl(settings.output_dir / "matches.jsonl", record)
        if on_match:
            on_match(alert)


def drain_outbox(api: MaxAPI, store: MessageStore, settings: Settings, stop: StopFlag) -> None:
    if not settings.send_enabled:
        return
    allowed = [r for r in settings.recipients if not (r.startswith("chat:") and r[5:] in settings.channels)]
    for uid, recipient, text in store.pending_deliveries(allowed, settings.send_only_critical):
        if stop.stopped():
            return
        try:
            api.send_text(recipient, text)
        except MaxAPIError as exc:
            store.delivery_failed(uid, recipient, max(30, exc.retry_after))
            log.warning("Отправка в %s отложена: %s", recipient, exc)
            if exc.status in {401, 403}:
                raise
            if exc.status == 429:
                return
        else:
            store.delivery_sent(uid, recipient)


def run_monitor(settings: Settings, store: MessageStore, *, stop: StopFlag | None = None,
                once: bool = False, on_match=None, on_status=None, on_state=None,
                on_channel=None, on_cycle=None,
                settings_loader: Callable[[], Settings] = load_settings) -> None:
    stop = stop or StopFlag()
    if settings.receive_mode not in {"polling", "webhook"}:
        raise ValueError("MAX_RECEIVE_MODE: допустимы polling или webhook")
    if settings.receive_mode == "webhook" and not settings.webhook_secret:
        raise ValueError("Для webhook задайте MAX_WEBHOOK_SECRET")
    api = MaxAPI(settings.api_token, settings.channel_timeout)
    try:
        rematch_existing(store, settings)
        bot_id = None
        channel_names = {}
        if on_state:
            on_state("on")
        failures = 0

        def process_pending():
            for key, update in store.pending_updates():
                if stop.stopped():
                    break
                try:
                    process_update(update, store, settings, bot_id, on_match, on_channel, channel_names)
                except (ValueError, TypeError, AttributeError, OverflowError):
                    log.warning("Некорректное событие API %s; пропущено", key)
                store.finish_update(key)

        while not stop.stopped():
            try:
                if bot_id is None:
                    bot_id = int(api.me()["user_id"])
                marker_key = f"marker:{bot_id}"
                for channel_id in settings.channels:
                    if channel_id not in channel_names:
                        channel_names[channel_id] = api.chat(channel_id).get("title") or channel_id
                # Work persisted before an interruption must not wait for a successful poll.
                process_pending()
                drain_outbox(api, store, settings, stop)
                if settings.receive_mode == "polling":
                    data = api.updates(store.marker(marker_key), timeout=0 if once else 20)
                    updates = data.get("updates")
                    marker = data.get("marker")
                    if not isinstance(updates, list) or any(not isinstance(u, dict) for u in updates):
                        raise MaxAPIError(502)
                    store.enqueue_updates(updates, marker, marker_key)
                process_pending()
                drain_outbox(api, store, settings, stop)
                failures = 0
                status = f"MAX API ({settings.receive_mode}): источников {len(settings.channels)}"
                if on_status:
                    on_status(status)
                if on_cycle:
                    on_cycle()
            except MaxAPIError as exc:
                if exc.status in {400, 401, 403, 404, 405, 409}:
                    raise
                failures += 1
                delay = min(300, max(exc.retry_after, settings.interval_seconds, 2 ** min(failures, 6)))
                log.warning("%s; повтор через %s с", exc, delay)
                if on_status:
                    on_status(f"{exc}; повтор через {delay} с")
                if once:
                    raise
                stop.wait(delay)
            if once or stop.stopped():
                break
            stop.wait(1 if settings.receive_mode == "webhook" else 0.2)
            current = settings_loader()
            if current.api_token != settings.api_token or current.receive_mode != settings.receive_mode:
                raise ValueError("Изменён токен или режим API: перезапустите мониторинг")
            settings = current
    finally:
        api.close()
        if on_state:
            on_state("off")
