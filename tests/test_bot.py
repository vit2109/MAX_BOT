from dataclasses import replace
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from config import ROOT, load_settings, save_settings, settings_payload
from matcher import match_message
from max_api import MaxAPI, MaxAPIError
from monitor import StopFlag, drain_outbox, process_update, rematch_existing, run_monitor
from parser import message_from_api, message_uid, normalize_channel_id, normalize_recipient
from store import MessageStore


@pytest.fixture
def settings(tmp_path):
    base = load_settings()
    return replace(base, channels=["-123"], recipients=["user:42", "chat:-456"],
                   db_path=tmp_path / "test.sqlite", output_dir=tmp_path / "output", api_token="test-token")


@pytest.fixture
def store(settings):
    db = MessageStore(settings.db_path)
    yield db
    db.close()


def event(mid="m1", text="В Москве авария на ВОЛС, интернет не работает", sender=99):
    return {"update_type": "message_created", "timestamp": 1700000000000,
            "message": {"sender": {"user_id": sender}, "recipient": {"chat_id": -123},
                        "timestamp": 1700000000000, "body": {"mid": mid, "text": text}}}


@pytest.mark.parametrize("text,severity", [
    ("МТС открыл новый офис в Москве", "none"),
    ("В Москве пожар на складе", "none"),
    ("Дрон атаковал склад в Москве", "none"),
    ("МТС работает без сбоев в Москве", "none"),
    ("Сбоев не зафиксировано, интернет работает", "none"),
    ("Авария на базовой станции МТС в Московской области", "critical"),
    ("ВОЛС повреждена, у жителей нет интернета", "critical"),
    ("Мобильный интернет не работает в Твери", "active"),
    ("У Ростелекома массовый сбой в Москве", "critical"),
    ("Сбой Мегафона устранён, связь восстановлена", "normal"),
    ("Связь МТС восстановлена", "normal"),
    ("Был сбой интернета. Связь восстановлена", "normal"),
    ("Сбой МТС ещё не устранён, связь не восстановлена", "critical"),
    ("Интернет частично восстановлен, перебои сохраняются", "critical"),
    ("Связь МТС восстановлена, но интернет не работает", "active"),
    ("Сбой интернета будет устранён завтра", "critical"),
    ("Плановые работы: отключение интернета в Москве", "active"),
    ("На базовых станциях произошла авария", "critical"),
    ("В интернате произошёл пожар", "none"),
    ("МТС сообщает о сбоях в сети", "critical"),
    ("Сбой интернета. Восстановление ожидается завтра", "critical"),
    ("Интернет пока не восстановлен", "active"),
    ("Плановые работы на ВОЛС. Произошла авария, обрыв кабеля", "critical"),
    ("В деревне нет связи", "critical"),
    ("Связь восстановлена", "normal"),
])
def test_incident_classification(settings, text, severity):
    result = match_message(text, settings.keyword_groups)
    assert result.severity == severity
    assert result.matched == (severity != "none")


def test_ids_and_stable_message_id():
    assert normalize_channel_id("https://web.max.ru/-123?x=1") == "-123"
    assert normalize_recipient("user:42") == "user:42"
    assert normalize_recipient("-123") == "chat:-123"
    for value in ["0", "foo", "https://example.com/-123"]:
        with pytest.raises(ValueError):
            normalize_channel_id(value)
    raw = event()["message"]
    message = message_from_api(raw)
    uid = message_uid("-123", message)
    raw["body"]["text"] = "edited"
    assert message_uid("-123", message_from_api(raw)) == uid
    assert message.datetime.endswith("+00:00")


def test_forward_text_and_reply_context():
    raw = event(text="Комментарий")["message"]
    raw["link"] = {"type": "forward", "message": {"mid": "original", "text": "Авария ВОЛС"}}
    assert "Авария ВОЛС" in message_from_api(raw).text
    raw["body"] = None
    forwarded = message_from_api(raw)
    assert forwarded.text == "Авария ВОЛС"
    assert forwarded.message_id.startswith("forward:original:")
    raw["body"] = {"mid": "reply", "text": "Спасибо"}
    raw["link"]["type"] = "reply"
    assert message_from_api(raw).text == "Спасибо"


def test_replay_and_partial_delivery(settings, store):
    process_update(event(), store, settings, bot_id=1)
    process_update(event(), store, settings, bot_id=1)
    assert len(store.recent_messages()) == 1
    assert len(store.pending_deliveries()) == 2

    class API:
        def send_text(self, recipient, text):
            if recipient == "chat:-456":
                raise MaxAPIError(500)

    drain_outbox(API(), store, settings, StopFlag())
    assert not store.recent_matches()[0]["sent"]
    store.close()
    reopened = MessageStore(settings.db_path)
    try:
        rows = reopened._conn.execute("SELECT recipient, sent FROM outbox ORDER BY recipient").fetchall()
        assert rows == [("chat:-456", 0), ("user:42", 1)]
        reopened._conn.execute("UPDATE outbox SET next_attempt=0")
        reopened._conn.commit()
        sent = []

        class RetryAPI:
            def send_text(self, recipient, text):
                sent.append(recipient)

        drain_outbox(RetryAPI(), reopened, settings, StopFlag())
        assert sent == ["chat:-456"]
        assert reopened.recent_matches()[0]["sent"]
    finally:
        reopened.close()
        # Fixture owns the original connection; close is idempotent.


def test_crash_between_insert_and_queue(settings, store):
    from datetime import datetime
    update = event()
    store.insert_new("-123", [message_from_api(update["message"])], datetime.now().astimezone())
    process_update(update, store, settings, bot_id=1)
    assert len(store.pending_deliveries()) == 2


def test_inbox_cursor_restart_and_dedup(settings, store):
    store.enqueue_updates([event()], "123", "marker:1")
    store.enqueue_updates([event()], "124", "marker:1")
    assert store.marker("marker:1") == "124"
    assert store.marker("marker:2") is None
    assert len(store.pending_updates()) == 1
    key, update = store.pending_updates()[0]
    process_update(update, store, settings, bot_id=1)
    store.finish_update(key)
    store.enqueue_updates([event()])
    assert store.pending_updates() == []


def test_own_messages_and_other_chats_ignored(settings, store):
    process_update(event(sender=1), store, settings, bot_id=1)
    foreign = event()
    foreign["message"]["recipient"]["chat_id"] = -999
    process_update(foreign, store, settings, bot_id=1)
    assert store.recent_messages() == []


def test_paused_recipients_do_not_starve_current_delivery(settings, store):
    process_update(event(), store, settings, bot_id=1)
    uid = store.recent_matches()[0]["uid"]
    # An old recipient backlog precedes a new valid recipient in the queue.
    store.queue_alert(uid, [f"user:{n}" for n in range(100, 210)], "old")
    store.queue_alert(uid, ["user:999"], "current")
    sent = []

    class API:
        def send_text(self, recipient, text): sent.append(recipient)

    drain_outbox(API(), store, replace(settings, recipients=["user:999"]), StopFlag())
    assert sent == ["user:999"]


def test_request_contract_and_secret_redaction():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/updates":
            return httpx.Response(200, json={"updates": [], "marker": 42})
        return httpx.Response(200, json={"message": {"body": {"mid": "sent"}}})

    api = MaxAPI("secret-token", transport=httpx.MockTransport(handler))
    try:
        api.updates("41", timeout=0)
        api.send_text("user:7", "plain text")
        assert requests[0].headers["Authorization"] == "secret-token"
        assert "access_token" not in str(requests[0].url)
        assert requests[0].url.params["marker"] == "41"
        assert requests[1].url.params["user_id"] == "7"
        assert json.loads(requests[1].content) == {"text": "plain text"}
    finally:
        api.close()

    def fail(request):
        return httpx.Response(429, headers={"Retry-After": "45"}, text="secret-token")

    api = MaxAPI("secret-token", transport=httpx.MockTransport(fail))
    try:
        with pytest.raises(MaxAPIError) as error:
            api.me()
        assert error.value.retry_after == 45
        assert "secret-token" not in str(error.value)
    finally:
        api.close()


def test_polling_once_recovers_durable_inbox(settings, store, monkeypatch):
    import monitor
    store.enqueue_updates([event()])

    class API:
        def __init__(self, *args): pass
        def me(self): return {"user_id": 1}
        def chat(self, channel_id): return {"title": "Источник"}
        def updates(self, marker, timeout): return {"updates": [], "marker": 123}
        def send_text(self, recipient, text): pass
        def close(self): pass

    monkeypatch.setattr(monitor, "MaxAPI", API)
    run_monitor(settings, store, once=True)
    assert store.marker("marker:1") == "123"
    assert store.pending_updates() == []
    assert store.recent_matches()[0]["sent"]


def test_webhook_auth_and_persistence(settings, monkeypatch):
    import server
    configured = replace(settings, receive_mode="webhook", webhook_secret="test-secret")
    monkeypatch.setattr(server, "load_settings", lambda: configured)
    with TestClient(server.app) as client:
        assert client.post("/webhook", json=event()).status_code == 403
        headers = {"X-Max-Bot-Api-Secret": "test-secret"}
        assert client.post("/webhook", json=event(), headers=headers).status_code == 200
        assert client.post("/webhook", json=event(), headers=headers).status_code == 200
        assert client.post("/webhook", content="broken", headers=headers).status_code == 400
        assert client.post("/webhook", json=[], headers=headers).status_code == 400
        assert client.post("/webhook", content="x" * 1_000_001, headers=headers).status_code == 413
    db = MessageStore(settings.db_path)
    try:
        assert len(db.pending_updates()) == 1
    finally:
        db.close()


def test_panel_feed_and_preview(settings, monkeypatch):
    import server
    monkeypatch.setattr(server, "load_settings", lambda: settings)
    with TestClient(server.app) as client:
        assert client.get("/").status_code == 200
        response = client.get("/api/feed")
        assert response.status_code == 200
        assert "test-token" not in response.text
        payload = settings_payload(settings)
        payload["text"] = "МТС открыл офис в Москве"
        assert client.post("/api/preview", json=payload).json()["matched"] is False
        payload["text"] = "МТС: авария, интернет не работает"
        result = client.post("/api/preview", json=payload).json()
        assert result["matched"] and result["would_send"]


def test_settings_roundtrip_and_token_not_exposed(settings, tmp_path):
    assert "test-token" not in json.dumps(settings_payload(settings))
    assert "test-token" not in repr(settings)
    payload = settings_payload(settings)
    for name in ["receive_mode", "token_configured"]:
        payload.pop(name)
    saved = save_settings(**payload, root=tmp_path)
    assert saved.channels == settings.channels
    assert saved.recipients == settings.recipients
    assert saved.keyword_groups == settings.keyword_groups
    with pytest.raises(ValueError):
        save_settings(**{**payload, "recipients": ["chat:-123"]}, root=tmp_path)


def test_rematch_clears_false_alarms_without_sending(settings, store):
    from datetime import datetime
    message = message_from_api(event(text="МТС открыл офис в Москве")["message"])
    uid = store.insert_new("-123", [message], datetime.now().astimezone())[0][0]
    store.mark_matched(uid, ["Москва"], ["критическое"], "critical")
    rematch_existing(store, settings)
    assert store.recent_matches() == []
    assert store.pending_deliveries() == []
