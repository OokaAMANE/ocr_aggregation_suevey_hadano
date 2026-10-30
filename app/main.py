"""アンケート OCR・集計ツールの Web サーバー（FastAPI）。"""

from __future__ import annotations

import logging
import os
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import aggregate, db, export, images, ocr, surveys

log = logging.getLogger("survey")
STATIC_DIR = Path(__file__).parent / "static"

_executor = ThreadPoolExecutor(max_workers=int(os.environ.get("OCR_CONCURRENCY", "3")))


def _image_dir() -> Path:
    return db.data_dir() / "images"


def run_ocr(response_id: int):
    r = db.get_response(response_id)
    if not r or r["status"] not in ("queued", "processing"):
        return
    db.update_response(response_id, status="processing", error="")
    try:
        survey = surveys.get_survey(r["survey_id"])
        result = ocr.read_form(survey, [_image_dir() / f for f in db.page_files(response_id)])
    except ocr.OCRError as e:
        db.update_response(response_id, status="error", error=str(e))
        return
    except Exception as e:  # 想定外の失敗でも1件のエラーとして記録し、他の読み取りは続ける
        log.exception("OCR failed for response %s", response_id)
        db.update_response(response_id, status="error", error=f"読み取りに失敗しました: {e}")
        return
    uncertain = sorted(set(result["uncertain"]) | set(surveys.find_problems(survey, result["answers"])))
    db.update_response(
        response_id,
        answers=result["answers"],
        uncertain=uncertain,
        notes=result["notes"],
        status="review" if uncertain or result["notes"] else "auto",
    )


def enqueue(response_id: int):
    db.update_response(response_id, status="queued", error="")
    _executor.submit(run_ocr, response_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    # 前回の起動中に終わらなかった読み取りを再開する
    if ocr.available():
        for rid in db.response_ids_with_status("processing") + db.response_ids_with_status("queued"):
            enqueue(rid)
    yield
    _executor.shutdown(wait=False, cancel_futures=True)


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
    run_ocr_now: bool = Form(True),
):
    survey = _survey(survey_id)
    if not 1 <= pages_per_response <= 10:
        raise HTTPException(400, "1人あたりのページ数は1〜10で指定してください")
    if run_ocr_now and not ocr.available():
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
            rid = db.create_response(
                survey_id, batch.strip(), source, "queued" if run_ocr_now else "review",
                surveys.blank_answers(survey),
            )
            for page_no, (_, _, fn) in enumerate(group, 1):
                db.add_page(rid, page_no, fn)
            created.append(rid)
    if run_ocr_now:
        for rid in created:
            _executor.submit(run_ocr, rid)
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
    enqueue(response_id)
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
def retry_errors(survey_id: str):
    _survey(survey_id)
    if not ocr.available():
        raise HTTPException(400, "AI読み取りが設定されていません")
    rows = [r for r in db.list_responses(survey_id, ["error"]) if r["page_count"]]
    for r in rows:
        enqueue(r["id"])
    return {"queued": len(rows)}


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
