from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from parser import ParsedMessage, message_uid


class MessageStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init()

    def close(self) -> None:
        self._conn.close()

    def marker(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM api_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def enqueue_updates(self, updates: list[dict], marker: str | None = None,
                        marker_key: str = "marker") -> None:
        """Persist the complete response before advancing the API cursor."""
        import hashlib
        with self._conn:
            for update in updates:
                payload = json.dumps(update, ensure_ascii=False, sort_keys=True)
                key = hashlib.sha256(payload.encode()).hexdigest()
                self._conn.execute("INSERT OR IGNORE INTO inbox (id, payload) VALUES (?, ?)", (key, payload))
            if marker is not None:
                self._conn.execute("INSERT INTO api_state VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                   (marker_key, str(marker)))

    def pending_updates(self) -> list[tuple[str, dict]]:
        return [(row[0], json.loads(row[1])) for row in self._conn.execute(
            "SELECT id, payload FROM inbox WHERE processed = 0 ORDER BY rowid LIMIT 100")]

    def finish_update(self, key: str) -> None:
        with self._conn:
            self._conn.execute("UPDATE inbox SET processed=1 WHERE id=?", (key,))

    def queue_alert(self, uid: str, recipients: list[str], text: str) -> None:
        with self._conn:
            for recipient in recipients:
                self._conn.execute("INSERT OR IGNORE INTO outbox(uid, recipient, text) VALUES (?, ?, ?)",
                                   (uid, recipient, text))

    def pending_deliveries(self, recipients: list[str] | None = None,
                           only_critical: bool = False) -> list[tuple[str, str, str]]:
        conditions = ["o.sent=0", "o.next_attempt <= ?", "m.matched=1"]
        params: list = [datetime.now().timestamp()]
        if recipients is not None:
            if not recipients:
                return []
            conditions.append("o.recipient IN (" + ",".join("?" for _ in recipients) + ")")
            params.extend(recipients)
        if only_critical:
            conditions.append("m.severity='critical'")
        return self._conn.execute(
            "SELECT o.uid, o.recipient, o.text FROM outbox o JOIN messages m ON m.uid=o.uid WHERE "
            + " AND ".join(conditions) + " ORDER BY o.rowid LIMIT 100", params).fetchall()

    def delivery_failed(self, uid: str, recipient: str, delay: float) -> None:
        with self._conn:
            self._conn.execute("UPDATE outbox SET next_attempt=? WHERE uid=? AND recipient=?",
                               (datetime.now().timestamp() + delay, uid, recipient))

    def delivery_sent(self, uid: str, recipient: str) -> None:
        with self._conn:
            self._conn.execute("UPDATE outbox SET sent=1 WHERE uid=? AND recipient=?", (uid, recipient))
            pending = self._conn.execute("SELECT 1 FROM outbox WHERE uid=? AND sent=0", (uid,)).fetchone()
            if pending is None:
                self._conn.execute("UPDATE messages SET sent=1, sent_at=? WHERE uid=?",
                                   (datetime.now().astimezone().isoformat(), uid))

    def is_bootstrapped(self, channel_id: str) -> bool:
        row = self._conn.execute(
            "SELECT bootstrapped FROM channel_state WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
        return bool(row and row[0])

    def mark_bootstrapped(self, channel_id: str, scraped_at: datetime) -> None:
        self._conn.execute(
            """
            INSERT INTO channel_state (channel_id, bootstrapped, last_scraped_at)
            VALUES (?, 1, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                bootstrapped = 1,
                last_scraped_at = excluded.last_scraped_at
            """,
            (channel_id, scraped_at.isoformat(timespec="seconds")),
        )
        self._conn.commit()

    def insert_new(
        self,
        channel_id: str,
        messages: list[ParsedMessage],
        scraped_at: datetime,
    ) -> list[tuple[str, ParsedMessage]]:
        inserted: list[tuple[str, ParsedMessage]] = []
        for message in messages:
            uid = message_uid(channel_id, message)
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO messages (
                    uid, channel_id, channel_name, text, time, date_label,
                    datetime, views, scraped_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uid,
                    channel_id,
                    message.channel_name,
                    message.text,
                    message.time,
                    message.date_label,
                    message.datetime,
                    message.views,
                    scraped_at.isoformat(timespec="seconds"),
                ),
            )
            if cursor.rowcount:
                inserted.append((uid, message))
        self._conn.execute(
            """
            INSERT INTO channel_state (channel_id, bootstrapped, last_scraped_at)
            VALUES (?, 0, ?)
            ON CONFLICT(channel_id) DO UPDATE SET last_scraped_at = excluded.last_scraped_at
            """,
            (channel_id, scraped_at.isoformat(timespec="seconds")),
        )
        self._conn.commit()
        return inserted

    def unmatched_texts(self) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT uid, text FROM messages WHERE IFNULL(matched, 0) = 0"
        ).fetchall()
        return [(row[0], row[1] or "") for row in rows]

    def mark_matched(self, uid: str, keywords: list[str], labels: list[str] | None = None, severity: str | None = None) -> None:
        self._conn.execute(
            """
            UPDATE messages
            SET matched = 1, matched_keywords = ?, labels = ?, severity = ?
            WHERE uid = ?
            """,
            (", ".join(keywords), ", ".join(labels or []), severity or "", uid),
        )
        self._conn.commit()

    def mark_matched_batch(self, items: list[tuple[str, list[str], list[str], str]]) -> None:
        for uid, keywords, labels, severity in items:
            self._conn.execute(
                """
                UPDATE messages
                SET matched = 1, matched_keywords = ?, labels = ?, severity = ?
                WHERE uid = ?
                """,
                (", ".join(keywords), ", ".join(labels or []), severity or "", uid),
            )
        if items:
            self._conn.commit()

    def mark_sent(self, uid: str, sent_at: datetime) -> None:
        self._conn.execute(
            "UPDATE messages SET sent = 1, sent_at = ? WHERE uid = ?",
            (sent_at.isoformat(timespec="seconds"), uid),
        )
        self._conn.commit()

    def recent_matches(self, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT uid, channel_id, channel_name, text, datetime, time, views,
                   matched_keywords, sent, scraped_at, labels, severity
            FROM messages
            WHERE matched = 1
            ORDER BY scraped_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "uid": row[0],
                "channel_id": row[1],
                "channel_name": row[2],
                "text": row[3],
                "datetime": row[4],
                "time": row[5],
                "views": row[6],
                "matched": True,
                "matched_keywords": row[7] or "",
                "sent": bool(row[8]),
                "scraped_at": row[9],
                "labels": row[10] or "",
                "severity": row[11] or "",
            }
            for row in rows
        ]

    def recent_messages(self, limit: int = 200) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT uid, channel_id, channel_name, text, datetime, time, views,
                   matched, matched_keywords, sent, scraped_at, labels, severity
            FROM messages
            ORDER BY scraped_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "uid": row[0],
                "channel_id": row[1],
                "channel_name": row[2],
                "text": row[3],
                "datetime": row[4],
                "time": row[5],
                "views": row[6],
                "matched": bool(row[7]),
                "matched_keywords": row[8] or "",
                "sent": bool(row[9]),
                "scraped_at": row[10],
                "labels": row[11] or "",
                "severity": row[12] or "",
            }
            for row in rows
        ]

    def stats(self, keywords: list[str] | None = None) -> dict:
        total = self._count("SELECT COUNT(*) FROM messages")
        matched = self._count("SELECT COUNT(*) FROM messages WHERE matched = 1")
        sent = self._count("SELECT COUNT(*) FROM messages WHERE sent = 1")
        channels = self._count("SELECT COUNT(*) FROM channel_state")
        bootstrapped = self._count("SELECT COUNT(*) FROM channel_state WHERE bootstrapped = 1")
        keyword_counts = {word: 0 for word in keywords or []}
        for (raw,) in self._conn.execute(
            "SELECT matched_keywords FROM messages WHERE matched = 1 AND matched_keywords IS NOT NULL"
        ):
            for part in str(raw).split(","):
                word = part.strip()
                if not word:
                    continue
                keyword_counts[word] = keyword_counts.get(word, 0) + 1
        channel_rows = self._conn.execute(
            """
            SELECT channel_id,
                   MAX(channel_name),
                   COUNT(*),
                   SUM(matched),
                   SUM(sent)
            FROM messages
            GROUP BY channel_id
            ORDER BY COUNT(*) DESC
            """
        ).fetchall()
        label_counts: dict[str, int] = {}
        for (raw,) in self._conn.execute(
            "SELECT labels FROM messages WHERE matched = 1 AND labels IS NOT NULL AND labels != ''"
        ):
            for part in str(raw).split(","):
                label = part.strip()
                if label:
                    label_counts[label] = label_counts.get(label, 0) + 1
        severity_rows = self._conn.execute(
            """
            SELECT severity, COUNT(*) FROM messages
            WHERE matched = 1 AND severity IS NOT NULL AND severity != ''
            GROUP BY severity
            """
        ).fetchall()
        return {
            "messages_total": total,
            "matches_total": matched,
            "sent_total": sent,
            "channels_in_db": channels,
            "channels_bootstrapped": bootstrapped,
            "keywords": [
                {"word": word, "count": count}
                for word, count in sorted(
                    ((word, count) for word, count in keyword_counts.items() if count > 0),
                    key=lambda item: (-item[1], item[0]),
                )[:12]
            ],
            "labels": [
                {"name": name, "count": count}
                for name, count in sorted(label_counts.items(), key=lambda item: (-item[1], item[0]))
            ],
            "severities": [
                {"name": row[0], "count": int(row[1])}
                for row in severity_rows
            ],
            "channels": [
                {
                    "id": row[0],
                    "name": row[1] or row[0],
                    "messages": int(row[2] or 0),
                    "matches": int(row[3] or 0),
                    "sent": int(row[4] or 0),
                }
                for row in channel_rows
            ],
        }

    def _count(self, sql: str) -> int:
        row = self._conn.execute(sql).fetchone()
        return int(row[0] if row else 0)

    def _init(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                uid TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                channel_name TEXT,
                text TEXT NOT NULL,
                time TEXT,
                date_label TEXT,
                datetime TEXT,
                views TEXT,
                scraped_at TEXT NOT NULL,
                matched INTEGER NOT NULL DEFAULT 0,
                matched_keywords TEXT,
                labels TEXT,
                severity TEXT,
                sent INTEGER NOT NULL DEFAULT 0,
                sent_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id);
            CREATE INDEX IF NOT EXISTS idx_messages_datetime ON messages(datetime);

            CREATE TABLE IF NOT EXISTS channel_state (
                channel_id TEXT PRIMARY KEY,
                bootstrapped INTEGER NOT NULL DEFAULT 0,
                last_scraped_at TEXT
            );
            CREATE TABLE IF NOT EXISTS api_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inbox (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, processed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS outbox (
                uid TEXT NOT NULL, recipient TEXT NOT NULL, text TEXT NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(uid, recipient)
            );
            """
        )
        self._ensure_column("messages", "labels", "TEXT")
        self._ensure_column("messages", "severity", "TEXT")
        self._conn.commit()

    def _ensure_column(self, table: str, name: str, spec: str) -> None:
        columns = [row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")]
        if name not in columns:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def message_record(
    channel_id: str,
    uid: str,
    message: ParsedMessage,
    *,
    keywords: list[str] | None = None,
) -> dict:
    payload = asdict(message)
    payload["uid"] = uid
    payload["channel_id"] = channel_id
    if keywords is not None:
        payload["matched_keywords"] = keywords
    return payload
