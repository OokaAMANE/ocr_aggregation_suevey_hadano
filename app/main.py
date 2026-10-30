"""アンケート OCR・集計ツールの Web サーバー（FastAPI）。"""

from __future__ import annotations

import logging
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import aggregate, db, export, images, jobs, ocr, surveys

log = logging.getLogger("survey")
STATIC_DIR = Path(__file__).parent / "static"

OCR_MODES = ("batch", "now", "none")


def _image_dir() -> Path:
    return db.data_dir() / "images"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    jobs.start()  # 前回の起動中に終わらなかった読み取りを再開する
    yield
    jobs.stop()


app = FastAPI(title="アンケート OCR・集計ツール", lifespan=lifespan)


def _survey(survey_id: str) -> dict:
    try:
        return surveys.get_survey(survey_id)
    except KeyError:
        raise HTTPException(404, "様式が見つかりません")


def _response(response_id: int) -> dict:
    r = db.get_response(response_id)
    if not r:
        raise HTTPException(404, "回答が見つかりません")
    return r


# ---------------------------------------------------------------- 様式


@app.get("/api/config")
def config():
    return {"ocr_available": ocr.available(), "ocr_model": ocr.MODEL}


@app.get("/api/surveys")
def list_surveys():
    return [
        {"id": s["id"], "title": s["title"], "default_pages": s.get("default_pages", 1),
         "counts": db.status_counts(s["id"])}
        for s in surveys.list_surveys()
    ]


@app.get("/api/surveys/{survey_id}")
def get_survey(survey_id: str):
    return _survey(survey_id)


# ---------------------------------------------------------------- 取り込み


@app.post("/api/surveys/{survey_id}/upload")
async def upload(
    survey_id: str,
    files: list[UploadFile] = File(...),
    batch: str = Form(""),
    pages_per_response: int = Form(1),
    ocr_mode: str = Form("batch"),
):
    """ocr_mode: batch=バッチ読み取り（半額）、now=すぐ読み取る、none=読み取らない（手入力）"""
    survey = _survey(survey_id)
    if not 1 <= pages_per_response <= 10:
        raise HTTPException(400, "1人あたりのページ数は1〜10で指定してください")
    if ocr_mode not in OCR_MODES:
        raise HTTPException(400, "読み取り方法が不正です")
    if ocr_mode != "none" and not ocr.available():
        raise HTTPException(400, "AI読み取りが設定されていません（ANTHROPIC_API_KEY が未設定）")

    # 全ファイルをページ画像として保存する（変換は重いので別スレッドで行い、画面の応答を止めない）
    saved: list[list[tuple[str, int, str]]] = []  # ファイルごとの [(ファイル名, ページ番号, 保存名)]
    try:
        for f in files:
            data = await f.read()
            name = f.filename or ""
            try:
                saved.append(await run_in_threadpool(_save_file_pages, name, data))
            except Exception:
                raise HTTPException(400, f"画像として読み込めないファイルです: {name}")
    except HTTPException:
        for pages in saved:
            for _, _, fn in pages:
                (_image_dir() / fn).unlink(missing_ok=True)
        raise
    total = sum(len(p) for p in saved)
    if not total:
        raise HTTPException(400, "ページがありません")

    # 複数ページのファイル（PDF など）はファイルの中だけで pages_per_response 枚ずつ区切る。
    # 1ページだけの画像ファイルは、選んだ順に続けて区切る（表・裏を別ファイルにした場合）。
    singles = [p[0] for p in saved if len(p) == 1]
    chunks = [(p, True) for p in saved if len(p) > 1] + ([(singles, False)] if singles else [])

    created, warnings = [], []
    for pages, from_one_file in chunks:
        if len(pages) % pages_per_response:
            label = pages[0][0] if from_one_file else "1ページの画像ファイル"
            warnings.append(
                f"{label}: {len(pages)}ページは1人あたり{pages_per_response}ページで割り切れません。"
                "ページの抜け・重複がないか確認してください。"
            )
        for start in range(0, len(pages), pages_per_response):
            group = pages[start : start + pages_per_response]
            source = (
                f"{group[0][0]} p.{','.join(str(g[1]) for g in group)}"
                if from_one_file
                else " / ".join(g[0] for g in group)
            )
            rid = db.create_response(survey_id, batch.strip(), source, "review", surveys.blank_answers(survey))
            for page_no, (_, _, fn) in enumerate(group, 1):
                db.add_page(rid, page_no, fn)
            created.append(rid)
    if ocr_mode == "batch":
        jobs.enqueue_batch(created)
    elif ocr_mode == "now":
        for rid in created:
            jobs.enqueue(rid)
    return {"created": created, "pages": total, "warning": "\n".join(warnings)}


def _save_file_pages(name: str, data: bytes) -> list[tuple[str, int, str]]:
    out = []
    try:
        for i, im in enumerate(images.iter_pages(name, data), 1):
            out.append((name, i, images.save_page(im, _image_dir())))
    except Exception:
        for _, _, fn in out:
            (_image_dir() / fn).unlink(missing_ok=True)
        raise
    return out


@app.post("/api/surveys/{survey_id}/responses")
def create_manual(survey_id: str):
    """用紙画像なしで手入力の回答を1件追加する。"""
    survey = _survey(survey_id)
    rid = db.create_response(survey_id, "", "手入力", "review", surveys.blank_answers(survey))
    return {"id": rid}


# ---------------------------------------------------------------- 回答


@app.get("/api/surveys/{survey_id}/responses")
def list_responses(survey_id: str, status: str = ""):
    survey = _survey(survey_id)
    rows = db.list_responses(survey_id, [s for s in status.split(",") if s] or None)
    qs = surveys.question_map(survey)
    out = []
    for r in rows:
        a = r["answers"]
        out.append(
            {
                "id": r["id"], "status": r["status"], "batch": r["batch"], "source": r["source"],
                "uncertain": [qs[q]["no"] for q in r["uncertain"] if q in qs], "notes": r["notes"],
                "error": r["error"], "page_count": r["page_count"], "updated_at": r["updated_at"],
                "attrs": {q["id"]: "／".join(a[q["id"]]["selected"]) for q in survey["questions"] if q.get("attribute")},
            }
        )
    return {"responses": out, "counts": db.status_counts(survey_id)}


@app.get("/api/responses/{response_id}")
def get_response(response_id: int):
    r = _response(response_id)
    r["next_id"] = db.next_to_review(r["survey_id"], response_id)
    return r


class ResponseUpdate(BaseModel):
    answers: dict
    status: str | None = None
    batch: str | None = None


@app.put("/api/responses/{response_id}")
def update_response(response_id: int, body: ResponseUpdate):
    r = _response(response_id)
    survey = _survey(r["survey_id"])
    fields: dict = {"answers": surveys.normalize_answers(survey, body.answers)}
    if body.status:
        if body.status not in ("review", "confirmed"):
            raise HTTPException(400, "状態が不正です")
        fields["status"] = body.status
    if body.batch is not None:
        fields["batch"] = body.batch.strip()
    db.update_response(response_id, **fields)
    return {"ok": True, "next_id": db.next_to_review(r["survey_id"], response_id)}


@app.post("/api/responses/{response_id}/ocr")
def rerun_ocr(response_id: int):
    r = _response(response_id)
    if not ocr.available():
        raise HTTPException(400, "AI読み取りが設定されていません")
    if not r["pages"]:
        raise HTTPException(400, "用紙画像がありません")
    jobs.enqueue(response_id)
    return {"ok": True}


@app.post("/api/surveys/{survey_id}/confirm-auto")
def confirm_auto(survey_id: str):
    """AI読み取りで気になる点がなかった回答をまとめて確認済みにする。"""
    _survey(survey_id)
    rows = db.list_responses(survey_id, ["auto"])
    for r in rows:
        db.update_response(r["id"], status="confirmed")
    return {"confirmed": len(rows)}


@app.post("/api/surveys/{survey_id}/retry-errors")
def retry_errors(survey_id: str, mode: str = "batch"):
    _survey(survey_id)
    if not ocr.available():
        raise HTTPException(400, "AI読み取りが設定されていません")
    ids = [r["id"] for r in db.list_responses(survey_id, ["error"]) if r["page_count"]]
    if mode == "batch":
        jobs.enqueue_batch(ids)
    else:
        for rid in ids:
            jobs.enqueue(rid)
    return {"queued": len(ids)}


@app.get("/api/surveys/{survey_id}/batches")
def list_batches(survey_id: str):
    _survey(survey_id)
    return {"batches": db.list_batches(survey_id), "unsent": len(db.unsent_batch_responses())}


@app.post("/api/batches/check")
def check_batches():
    """バッチの送信・結果確認を今すぐ行う。"""
    jobs.wake()
    return {"ok": True}


@app.delete("/api/responses/{response_id}")
def delete_response(response_id: int):
    _response(response_id)
    for f in db.delete_response(response_id):
        (_image_dir() / f).unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/pages/{page_id}/image")
def page_image(page_id: int):
    p = db.get_page(page_id)
    if not p:
        raise HTTPException(404)
    return FileResponse(_image_dir() / p["filename"], media_type="image/jpeg",
                        headers={"Cache-Control": "no-cache"})


@app.post("/api/pages/{page_id}/rotate")
def rotate_page(page_id: int, degrees: int = 90):
    p = db.get_page(page_id)
    if not p:
        raise HTTPException(404)
    images.rotate(_image_dir() / p["filename"], degrees % 360)
    return {"ok": True}


# ---------------------------------------------------------------- 集計・出力


def _target(survey: dict, request: Request) -> tuple[list[dict], str]:
    """クエリ ?scope=confirmed&f_gender=男性 ... から集計対象の回答を決める。"""
    scope = request.query_params.get("scope", "all")
    statuses = ["confirmed"] if scope == "confirmed" else list(db.COUNTABLE)
    rows = db.list_responses(survey["id"], statuses)
    filters = {k[2:]: v for k, v in request.query_params.items() if k.startswith("f_") and v}
    rows = aggregate.apply_filters(survey, rows, filters)
    qs = surveys.question_map(survey)
    label = "、".join(f"{qs[k]['label']}={v}" for k, v in filters.items() if k in qs)
    return rows, label


@app.get("/api/surveys/{survey_id}/summary")
def summary(survey_id: str, request: Request, cross: str = ""):
    survey = _survey(survey_id)
    rows, _ = _target(survey, request)
    qs = surveys.question_map(survey)
    cross_by = cross if cross in qs and qs[cross]["type"] == "single" else None
    return aggregate.summarize(survey, rows, cross_by)


def _download(data: bytes, filename: str, media_type: str) -> Response:
    quoted = urllib.parse.quote(filename)
    return Response(data, media_type=media_type,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"})


@app.get("/api/surveys/{survey_id}/export.xlsx")
def export_xlsx(survey_id: str, request: Request):
    survey = _survey(survey_id)
    rows, label = _target(survey, request)
    return _download(export.to_xlsx(survey, rows, label), f"{survey['title']}_集計.xlsx",
                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/surveys/{survey_id}/export.csv")
def export_csv(survey_id: str, request: Request):
    survey = _survey(survey_id)
    rows, _ = _target(survey, request)
    return _download(export.to_csv(survey, rows), f"{survey['title']}_回答データ.csv", "text/csv")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
