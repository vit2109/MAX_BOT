from __future__ import annotations

import logging
import threading
import hmac
import json
from contextlib import asynccontextmanager
import asyncio

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import BUNDLE_DIR, ROOT, load_settings, save_settings, settings_payload
from matcher import groups_from_payload, preview_match
from monitor import StopFlag, rematch_existing, run_monitor
from store import MessageStore

log = logging.getLogger("max_monitor")
WEB_DIR = BUNDLE_DIR / "web"
if not (WEB_DIR / "index.html").exists():
    WEB_DIR = ROOT / "web"


class MonitorService:
    def __init__(self) -> None:
        self.state = "off"
        self.status = "Готов к запуску"
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = StopFlag()
        self.session = self._empty_session()

    def snapshot(self) -> dict:
        settings = load_settings()
        store = MessageStore(settings.db_path)
        try:
            db_stats = store.stats(settings.keywords)
            matches = store.recent_matches(80)
            messages = store.recent_messages(160)
        finally:
            store.close()
        with self._lock:
            session = dict(self.session)
            state = self.state
            status = self.status
        return {
            "state": state,
            "status": status,
            "channels_listed": len(settings.channels),
            "send_enabled": settings.send_enabled and bool(settings.recipients),
            "interval_seconds": settings.interval_seconds,
            "session": session,
            "stats": db_stats,
            "matches": matches,
            "messages": messages,
        }

    def start(self) -> dict:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise HTTPException(status_code=409, detail="Мониторинг уже запущен")
            self._stop = StopFlag()
            self.session = self._empty_session()
            self.state = "starting"
            self.status = "Запуск мониторинга…"
            thread = threading.Thread(target=self._run, name="max-monitor", daemon=True)
            self._thread = thread
            thread.start()
        return {"ok": True, "state": "starting"}

    def stop(self) -> dict:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self.state = "off"
                self.status = "Остановлен"
                return {"ok": True, "state": "off"}
            self._stop.stop()
            self.status = "Остановка мониторинга…"
        return {"ok": True, "state": "stopping"}

    def _run(self) -> None:
        store = None
        try:
            settings = load_settings()
            if not settings.channels:
                self._set_state("error", "Файл channels.txt пуст.")
                return
            store = MessageStore(settings.db_path)
            run_monitor(
                settings,
                store,
                stop=self._stop,
                on_match=self._on_match,
                on_status=self._on_status,
                on_state=self._on_state,
                on_channel=self._on_channel,
                on_cycle=self._on_cycle,
            )
        except Exception as exc:
            log.exception("Мониторинг остановился с ошибкой")
            self._set_state("error", f"Ошибка: {exc}")
        finally:
            if store is not None:
                store.close()
            with self._lock:
                if self.state != "error":
                    self.state = "off"
                    if not self.status.startswith("Ошибка"):
                        self.status = "Остановлен"

    def _on_match(self, _alert: str) -> None:
        with self._lock:
            self.session["matches"] += 1

    def _on_status(self, message: str) -> None:
        with self._lock:
            self.status = message

    def _on_state(self, state: str) -> None:
        with self._lock:
            self.state = state

    def _on_channel(self, payload: dict) -> None:
        with self._lock:
            self.session["channels_processed"] += 1
            self.session["messages_visible"] += int(payload.get("visible") or 0)
            self.session["messages_new"] += int(payload.get("new") or 0)
            self.session["current_channel"] = payload.get("channel_id")

    def _on_cycle(self) -> None:
        with self._lock:
            self.session["rounds"] += 1

    def _set_state(self, state: str, status: str) -> None:
        with self._lock:
            self.state = state
            self.status = status

    @staticmethod
    def _empty_session() -> dict:
        return {
            "channels_processed": 0,
            "messages_visible": 0,
            "messages_new": 0,
            "matches": 0,
            "rounds": 0,
            "current_channel": None,
        }


service = MonitorService()


@asynccontextmanager
async def lifespan(_app):
    yield
    service.stop()
    thread = service._thread
    if thread and thread.is_alive():
        await asyncio.to_thread(thread.join, 35)


app = FastAPI(title="MAX монитор", lifespan=lifespan)

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    page = WEB_DIR / "index.html"
    if not page.exists():
        raise HTTPException(status_code=500, detail="Не найден web/index.html")
    return FileResponse(page)


@app.get("/api/feed")
def feed() -> dict:
    return service.snapshot()


class SettingsPayload(BaseModel):
    interval_seconds: int = Field(ge=1, le=3600)
    channel_timeout: int = Field(ge=5, le=300)
    min_keyword_hits: int = Field(ge=1, le=20)
    send_enabled: bool = True
    send_only_critical: bool = False
    channels: list[str] = []
    keywords: list[str] = []
    keyword_groups: list[dict] = []
    recipients: list[str] = []


@app.get("/api/settings")
def get_settings() -> dict:
    return settings_payload()


@app.put("/api/settings")
def put_settings(payload: SettingsPayload) -> dict:
    try:
        saved = save_settings(
            interval_seconds=payload.interval_seconds,
            channel_timeout=payload.channel_timeout,
            min_keyword_hits=payload.min_keyword_hits,
            send_enabled=payload.send_enabled,
            send_only_critical=payload.send_only_critical,
            channels=payload.channels,
            keywords=payload.keywords,
            keyword_groups=payload.keyword_groups,
            recipients=payload.recipients,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rematched = 0
    store = MessageStore(saved.db_path)
    try:
        rematched = rematch_existing(store, saved)
    finally:
        store.close()
    return {"ok": True, "settings": settings_payload(saved), "rematched": rematched}


class PreviewPayload(BaseModel):
    text: str = ""
    min_keyword_hits: int = Field(default=1, ge=1, le=20)
    send_enabled: bool = True
    send_only_critical: bool = False
    keyword_groups: list[dict] = []
    recipients: list[str] = []


@app.post("/api/preview")
def preview(payload: PreviewPayload) -> dict:
    groups = groups_from_payload(payload.keyword_groups)
    return preview_match(
        payload.text,
        groups,
        payload.min_keyword_hits,
        send_enabled=payload.send_enabled,
        send_only_critical=payload.send_only_critical,
        has_recipients=bool(payload.recipients),
    )


@app.post("/api/start")
def start() -> dict:
    return service.start()


@app.post("/api/stop")
def stop() -> dict:
    return service.stop()


@app.post("/webhook")
async def webhook(request: Request) -> dict:
    settings = load_settings()
    if settings.receive_mode != "webhook" or not settings.webhook_secret:
        raise HTTPException(status_code=503, detail="Webhook не настроен")
    secret = request.headers.get("X-Max-Bot-Api-Secret", "")
    if not hmac.compare_digest(secret.encode(), settings.webhook_secret.encode()):
        raise HTTPException(status_code=403, detail="Неверный секрет Webhook")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 1_000_000:
            raise HTTPException(status_code=413, detail="Слишком большое событие")
    try:
        update = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Некорректный JSON") from None
    if not isinstance(update, dict) or not isinstance(update.get("update_type"), str):
        raise HTTPException(status_code=400, detail="Некорректное событие MAX")
    store = MessageStore(settings.db_path)
    try:
        # Acknowledge only after durable persistence; worker may be stopped at this point.
        store.enqueue_updates([update])
    finally:
        store.close()
    return {"ok": True}


def run_web(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    log.info("Веб-интерфейс: http://%s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")
