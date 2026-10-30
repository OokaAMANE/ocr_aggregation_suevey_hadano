"""AI 読み取りの実行（その場で読む通常読み取りと、半額のバッチ読み取り）。

通常読み取り : 1件ずつ API を呼ぶ。数十秒で結果が出る。
バッチ読み取り: Message Batches API にまとめて送る。料金は半額、結果は通常1時間以内（最大24時間）。
              送信状況は DB（responses.ocr_batch / ocr_batches）に残すので、途中でツールを
              再起動しても未送信分の送信と結果の受け取りを再開できる。
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import anthropic

from . import db, ocr, surveys

log = logging.getLogger("survey")

_executor = ThreadPoolExecutor(max_workers=int(os.environ.get("OCR_CONCURRENCY", "3")))

POLL_SECONDS = int(os.environ.get("OCR_BATCH_POLL_SECONDS", "60"))
# 1バッチの上限は 256MB。画像を base64 で埋め込むため、余裕を見てこの大きさで分けて送る
MAX_BATCH_BYTES = int(os.environ.get("OCR_BATCH_MAX_MB", "100")) * 1024 * 1024
_REQUEST_OVERHEAD = 64 * 1024  # 指示文・スキーマなど画像以外の分の見積もり

_batch_lock = threading.Lock()
_stop = threading.Event()
_wake = threading.Event()


def _image_dir() -> Path:
    return db.data_dir() / "images"


def _pages(response_id: int) -> list[Path]:
    return [_image_dir() / f for f in db.page_files(response_id)]


def store_result(response_id: int, survey: dict, result: dict):
    uncertain = sorted(set(result["uncertain"]) | set(surveys.find_problems(survey, result["answers"])))
    db.update_response(
        response_id,
        answers=result["answers"],
        uncertain=uncertain,
        notes=result["notes"],
        error="",
        status="review" if uncertain or result["notes"] else "auto",
    )


# ---------------------------------------------------------------- 通常読み取り


def run_ocr(response_id: int):
    r = db.get_response(response_id)
    if not r or r["status"] not in ("queued", "processing"):
        return
    db.update_response(response_id, status="processing", error="")
    try:
        survey = surveys.get_survey(r["survey_id"])
        result = ocr.read_form(survey, _pages(response_id))
    except ocr.OCRError as e:
        db.update_response(response_id, status="error", error=str(e))
        return
    except Exception as e:  # 想定外の失敗でも1件のエラーとして記録し、他の読み取りは続ける
        log.exception("OCR failed for response %s", response_id)
        db.update_response(response_id, status="error", error=f"読み取りに失敗しました: {e}")
        return
    store_result(response_id, survey, result)


def enqueue(response_id: int):
    db.update_response(response_id, status="queued", error="", ocr_batch="")
    _executor.submit(run_ocr, response_id)


# ---------------------------------------------------------------- バッチ読み取り


def enqueue_batch(response_ids: list[int]):
    """回答をバッチ読み取り待ちにする。実際の送信は submit_pending() が行う。"""
    for rid in response_ids:
        db.update_response(rid, status="batch", error="", ocr_batch="")
    _wake.set()


def _send(client, survey_id: str, requests: list[dict], response_ids: list[int]):
    try:
        batch = client.messages.batches.create(requests=requests)
    except anthropic.APIError as e:
        msg = "バッチの送信に失敗しました: " + ocr.api_error_message(e)
        for rid in response_ids:
            db.update_response(rid, status="error", error=msg)
        return
    db.add_batch(batch.id, survey_id, response_ids)
    log.info("submitted batch %s (%d requests)", batch.id, len(requests))


def submit_pending():
    """未送信のバッチ読み取り待ちを、様式ごと・大きさの上限ごとにまとめて送信する。"""
    with _batch_lock:
        pending = db.unsent_batch_responses()
        if not pending:
            return
        client = ocr._get_client()
        by_survey: dict[str, list[int]] = {}
        for rid in pending:
            r = db.get_response(rid)
            if r:
                by_survey.setdefault(r["survey_id"], []).append(rid)
        for survey_id, rids in by_survey.items():
            survey = surveys.get_survey(survey_id)
            requests: list[dict] = []
            ids: list[int] = []
            size = 0
            for rid in rids:
                try:
                    params = ocr.request_params(survey, _pages(rid))
                except Exception as e:
                    db.update_response(rid, status="error", error=f"用紙画像を読み込めません: {e}")
                    continue
                req_size = _REQUEST_OVERHEAD + sum(
                    len(c["source"]["data"]) for c in params["messages"][0]["content"] if c["type"] == "image"
                )
                if requests and size + req_size > MAX_BATCH_BYTES:
                    _send(client, survey_id, requests, ids)
                    requests, ids, size = [], [], 0
                requests.append({"custom_id": f"r{rid}", "params": params})
                ids.append(rid)
                size += req_size
            if requests:
                _send(client, survey_id, requests, ids)


def _result_error(result) -> str:
    t = result.type
    if t == "expired":
        return "バッチの処理期限（24時間）を過ぎました。再読み取りしてください。"
    if t == "canceled":
        return "バッチが取り消されました。再読み取りしてください。"
    err = getattr(getattr(result, "error", None), "error", None)
    detail = getattr(err, "message", "") or getattr(err, "type", "")
    return f"読み取りに失敗しました（バッチ）: {detail}".rstrip(": ")


def poll_batches():
    """送信済みバッチの進み具合を確認し、終わったバッチの結果を回答に反映する。"""
    with _batch_lock:
        open_batches = db.open_batches()
        if not open_batches:
            return
        client = ocr._get_client()
        for b in open_batches:
            try:
                info = client.messages.batches.retrieve(b["id"])
            except anthropic.APIError:
                log.exception("failed to retrieve batch %s", b["id"])
                continue
            counts = info.request_counts
            db.update_batch(
                b["id"],
                succeeded=counts.succeeded,
                errored=counts.errored + counts.canceled + counts.expired,
                processing=counts.processing,
            )
            if info.processing_status != "ended":
                continue
            waiting = db.batch_response_ids(b["id"])
            survey = surveys.get_survey(b["survey_id"])
            try:
                for item in client.messages.batches.results(b["id"]):
                    try:
                        rid = int(item.custom_id.removeprefix("r"))
                    except ValueError:
                        continue
                    if rid not in waiting:  # 結果待ちの間に削除・再読み取りされた回答
                        continue
                    waiting.discard(rid)
                    if item.result.type == "succeeded":
                        try:
                            store_result(rid, survey, ocr.parse_message(survey, item.result.message))
                        except ocr.OCRError as e:
                            db.update_response(rid, status="error", error=str(e))
                    else:
                        db.update_response(rid, status="error", error=_result_error(item.result))
            except anthropic.APIError:
                log.exception("failed to read results of batch %s", b["id"])
                continue
            for rid in waiting:
                db.update_response(rid, status="error", error="バッチの結果に含まれていませんでした。再読み取りしてください。")
            db.update_batch(b["id"], status="ended", processing=0, ended_at=datetime.now().isoformat(timespec="seconds"))


def _loop():
    while not _stop.is_set():
        for step in (submit_pending, poll_batches):
            try:
                step()
            except Exception:
                log.exception("batch step %s failed", step.__name__)
        _wake.wait(POLL_SECONDS)
        _wake.clear()


def wake():
    """送信・結果確認をすぐに行う（画面の「今すぐ確認」ボタンなど）。"""
    _wake.set()


def start():
    """起動時: 中断していた通常読み取りを再開し、バッチの送信・結果確認を始める。"""
    if not ocr.available():
        return
    for rid in db.response_ids_with_status("processing") + db.response_ids_with_status("queued"):
        enqueue(rid)
    _stop.clear()
    threading.Thread(target=_loop, name="ocr-batch", daemon=True).start()


def stop():
    _stop.set()
    _wake.set()
    _executor.shutdown(wait=False, cancel_futures=True)
