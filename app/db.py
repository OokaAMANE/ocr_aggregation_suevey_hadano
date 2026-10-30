"""SQLite による回答データの保存。1件 = 回答者1人（用紙1〜複数ページ）。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# 状態の意味
#   batch       : バッチ読み取り中（半額。結果は通常1時間以内、最大24時間）
#   queued      : AI読み取り待ち
#   processing  : AI読み取り中
#   auto        : AI読み取り済み（気になる点なし・未確認）
#   review      : 要確認（AIが自信なし、または単一回答に複数チェック等）
#   confirmed   : 担当者が確認済み
#   error       : 読み取り失敗
STATUSES = ["batch", "queued", "processing", "auto", "review", "confirmed", "error"]
COUNTABLE = ("auto", "review", "confirmed")

_lock = threading.Lock()


def data_dir() -> Path:
    d = Path(os.environ.get("SURVEY_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    (d / "images").mkdir(parents=True, exist_ok=True)
    return d


def _db_path() -> Path:
    return data_dir() / "survey.db"


@contextmanager
def connect():
    with _lock:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init():
    with connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS responses (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                survey_id   TEXT NOT NULL,
                batch       TEXT NOT NULL DEFAULT '',
                source      TEXT NOT NULL DEFAULT '',
                status      TEXT NOT NULL,
                answers     TEXT NOT NULL,
                uncertain   TEXT NOT NULL DEFAULT '[]',
                notes       TEXT NOT NULL DEFAULT '',
                error       TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_responses_survey ON responses(survey_id, status);
            CREATE TABLE IF NOT EXISTS pages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                response_id INTEGER NOT NULL REFERENCES responses(id) ON DELETE CASCADE,
                page_no     INTEGER NOT NULL,
                filename    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pages_response ON pages(response_id);
            CREATE TABLE IF NOT EXISTS ocr_batches (
                id          TEXT PRIMARY KEY,
                survey_id   TEXT NOT NULL,
                status      TEXT NOT NULL,
                total       INTEGER NOT NULL,
                succeeded   INTEGER NOT NULL DEFAULT 0,
                errored     INTEGER NOT NULL DEFAULT 0,
                processing  INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL,
                ended_at    TEXT NOT NULL DEFAULT ''
            );
            """
        )
        cols = {r[1] for r in c.execute("PRAGMA table_info(responses)")}
        if "ocr_batch" not in cols:
            # バッチ読み取りで送信済みのバッチID（未送信は空文字）
            c.execute("ALTER TABLE responses ADD COLUMN ocr_batch TEXT NOT NULL DEFAULT ''")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["answers"] = json.loads(d["answers"])
    d["uncertain"] = json.loads(d["uncertain"])
    return d


def create_response(survey_id: str, batch: str, source: str, status: str, answers: dict) -> int:
    now = _now()
    with connect() as c:
        cur = c.execute(
            "INSERT INTO responses (survey_id, batch, source, status, answers, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (survey_id, batch, source, status, json.dumps(answers, ensure_ascii=False), now, now),
        )
        return cur.lastrowid


def add_page(response_id: int, page_no: int, filename: str) -> int:
    with connect() as c:
        cur = c.execute(
            "INSERT INTO pages (response_id, page_no, filename) VALUES (?, ?, ?)",
            (response_id, page_no, filename),
        )
        return cur.lastrowid


def update_response(response_id: int, **fields):
    if not fields:
        return
    for k in ("answers", "uncertain"):
        if k in fields:
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE responses SET {cols} WHERE id = ?", (*fields.values(), response_id))


def get_response(response_id: int) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM responses WHERE id = ?", (response_id,)).fetchone()
        if not r:
            return None
        d = _row(r)
        d["pages"] = [
            dict(p)
            for p in c.execute(
                "SELECT id, page_no, filename FROM pages WHERE response_id = ? ORDER BY page_no",
                (response_id,),
            )
        ]
        return d


def list_responses(survey_id: str, statuses: list[str] | None = None) -> list[dict]:
    sql = "SELECT * FROM responses WHERE survey_id = ?"
    args: list = [survey_id]
    if statuses:
        sql += f" AND status IN ({','.join('?' * len(statuses))})"
        args += statuses
    sql += " ORDER BY id"
    with connect() as c:
        rows = [_row(r) for r in c.execute(sql, args)]
        counts = dict(
            c.execute(
                "SELECT response_id, COUNT(*) FROM pages WHERE response_id IN "
                "(SELECT id FROM responses WHERE survey_id = ?) GROUP BY response_id",
                (survey_id,),
            ).fetchall()
        )
    for r in rows:
        r["page_count"] = counts.get(r["id"], 0)
    return rows


def status_counts(survey_id: str) -> dict[str, int]:
    with connect() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) FROM responses WHERE survey_id = ? GROUP BY status", (survey_id,)
        ).fetchall()
    out = {s: 0 for s in STATUSES}
    out.update(dict(rows))
    return out


def response_ids_with_status(status: str) -> list[int]:
    with connect() as c:
        return [r[0] for r in c.execute("SELECT id FROM responses WHERE status = ? ORDER BY id", (status,))]


def get_page(page_id: int) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
        return dict(r) if r else None


def page_files(response_id: int) -> list[str]:
    with connect() as c:
        return [
            r[0]
            for r in c.execute(
                "SELECT filename FROM pages WHERE response_id = ? ORDER BY page_no", (response_id,)
            )
        ]


def delete_response(response_id: int) -> list[str]:
    files = page_files(response_id)
    with connect() as c:
        c.execute("DELETE FROM responses WHERE id = ?", (response_id,))
    return files


def next_to_review(survey_id: str, after_id: int) -> int | None:
    """確認画面の「次へ」用。要確認 → 未確認の順で、指定IDより後ろの最初の1件。"""
    with connect() as c:
        for status in ("review", "auto"):
            r = c.execute(
                "SELECT id FROM responses WHERE survey_id = ? AND status = ? AND id > ? ORDER BY id LIMIT 1",
                (survey_id, status, after_id),
            ).fetchone() or c.execute(
                "SELECT id FROM responses WHERE survey_id = ? AND status = ? AND id != ? ORDER BY id LIMIT 1",
                (survey_id, status, after_id),
            ).fetchone()
            if r:
                return r[0]
    return None


# ---------------------------------------------------------------- バッチ読み取り


def unsent_batch_responses() -> list[int]:
    """バッチ読み取り待ちのうち、まだ API に送信していない回答。"""
    with connect() as c:
        return [
            r[0]
            for r in c.execute("SELECT id FROM responses WHERE status = 'batch' AND ocr_batch = '' ORDER BY id")
        ]


def add_batch(batch_id: str, survey_id: str, response_ids: list[int]):
    with connect() as c:
        c.execute(
            "INSERT INTO ocr_batches (id, survey_id, status, total, processing, created_at)"
            " VALUES (?, ?, 'in_progress', ?, ?, ?)",
            (batch_id, survey_id, len(response_ids), len(response_ids), _now()),
        )
        c.executemany(
            "UPDATE responses SET ocr_batch = ? WHERE id = ?", [(batch_id, rid) for rid in response_ids]
        )


def update_batch(batch_id: str, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE ocr_batches SET {cols} WHERE id = ?", (*fields.values(), batch_id))


def open_batches() -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM ocr_batches WHERE status = 'in_progress' ORDER BY created_at")]


def list_batches(survey_id: str, limit: int = 10) -> list[dict]:
    with connect() as c:
        return [
            dict(r)
            for r in c.execute(
                "SELECT * FROM ocr_batches WHERE survey_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (survey_id, limit),
            )
        ]


def batch_response_ids(batch_id: str) -> set[int]:
    """そのバッチの結果を待っている回答（途中で削除・再読み取りされたものは除く）。"""
    with connect() as c:
        return {
            r[0]
            for r in c.execute(
                "SELECT id FROM responses WHERE ocr_batch = ? AND status = 'batch'", (batch_id,)
            )
        }
