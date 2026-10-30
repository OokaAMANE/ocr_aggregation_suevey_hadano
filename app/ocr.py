"""Claude（画像認識）で手書きアンケート用紙を読み取り、様式どおりの JSON にする。"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import anthropic

from . import images, surveys

MODEL = os.environ.get("OCR_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("OCR_EFFORT", "high")

SYSTEM_PROMPT = """あなたは自治体が実施した紙のアンケートの回答を、手書き用紙の画像からデータ化する担当者です。
画像は1人分の回答用紙（1〜複数ページ）です。印刷された設問文・選択肢・記入例は回答ではありません。回答者が書き込んだ内容だけを読み取ってください。

読み取りのルール:
- □へのチェックは ✓・レ点・×・■（塗りつぶし）・〇囲み・選択肢の文字への〇や下線など、書き方を問わず「選んだ」とみなす。
- 選んだ印と隣の□のどちらの選択肢か紛らわしいときは、印が最も近い選択肢を選び、その設問IDを uncertain に入れる。
- 消した跡（二重線・塗りつぶして消したもの）は選んでいないものとして扱う。
- 記述欄は書かれた文字をそのまま書き写す。補ったり要約したりしない。読めない文字は「〓」にし、その設問を uncertain に入れる。
- 無回答の設問は空のまま（selected は空配列、文字列は空文字）にする。推測で埋めない。
- 単一回答の設問で複数の選択肢に印がある場合は、印のあるものをすべて selected に入れ、uncertain に入れる。
- 設問の分岐（「⇒問４へ」など）に従っていない回答も、書かれているとおりに読み取る。
- 判読に自信がない設問、印がかすれている設問は uncertain に入れる。
- notes には、担当者が確認すべき点（欄外の書き込み、ページの欠け・向き、別様式の疑いなど）を日本語で簡潔に書く。なければ空文字。

"""


class OCRError(Exception):
    pass


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=4)
    return _client


def read_form(survey: dict, page_paths: list[Path]) -> dict:
    """用紙画像を読み取り {"answers", "uncertain", "notes"} を返す。answers は正規化済み。"""
    content: list[dict] = []
    for i, path in enumerate(page_paths, 1):
        content.append({"type": "text", "text": f"{i}ページ目:"})
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.standard_b64encode(images.for_ocr(path)).decode("ascii"),
                },
            }
        )
    content.append({"type": "text", "text": "この用紙の回答を、指定の JSON 形式で読み取ってください。"})

    try:
        resp = _get_client().beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            # 安全フィルタで断られた場合は、サーバー側で推奨モデルに自動で切り替えて再実行する
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT + surveys.describe_survey(survey),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": content}],
            output_config={
                "effort": EFFORT,
                "format": {"type": "json_schema", "schema": surveys.ocr_schema(survey)},
            },
        )
    except anthropic.AuthenticationError as e:
        raise OCRError("APIキーが無効です。ANTHROPIC_API_KEY を確認してください。") from e
    except anthropic.RateLimitError as e:
        raise OCRError("API の利用上限に達しました。しばらくしてから再読み取りしてください。") from e
    except anthropic.BadRequestError as e:
        raise OCRError(f"API リクエストエラー: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise OCRError(f"API エラー（{e.status_code}）: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise OCRError("API に接続できません。ネットワークを確認してください。") from e

    if resp.stop_reason == "refusal":
        raise OCRError("AI が読み取りを辞退しました。手入力してください。")
    if resp.stop_reason == "max_tokens":
        raise OCRError("読み取り結果が長すぎて途中で切れました。再読み取りしてください。")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise OCRError("AI から読み取り結果が返りませんでした。")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise OCRError("AI の読み取り結果を解析できませんでした。") from e

    qids = {q["id"] for q in survey["questions"]}
    return {
        "answers": surveys.normalize_answers(survey, data.get("answers")),
        "uncertain": [q for q in data.get("uncertain") or [] if q in qids],
        "notes": str(data.get("notes") or "").strip(),
    }
