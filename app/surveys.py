"""アンケート様式（テンプレート）の読み込みと、回答データの形を定義するモジュール。

様式は app/surveys/*.json に1ファイル1様式で定義する。設問の type は次のいずれか。

- single       : 単一回答（□にチェック）        answer = {"selected": [str], "other_text": str}
- multi        : 複数回答（□にチェック）        answer = {"selected": [str], "other_text": str}
- timegrid     : 時間帯の表に〇をつける         answer = {row_id: [hour, ...]}
- destinations : 種別ごとの目的地・最寄り停留所  answer = {purpose_id: {"checked": bool, "place": str, "stop": str}}
- trip         : 希望する便（目的地・出発/到着時刻） answer = {"destination": str, "depart_time": "HH:MM", "return_time": "HH:MM"}
- short_text   : 短い記述（件数を集計する）      answer = {"text": str}
- text         : 自由記述（一覧で表示する）      answer = {"text": str}

single も selected を配列で持つ。複数チェックされた用紙をそのまま記録し、集計時に「複数回答（無効）」として扱うため。
"""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

SURVEY_DIR = Path(__file__).parent / "surveys"

CHOICE_TYPES = {"single", "multi"}
TEXT_TYPES = {"text", "short_text"}


def norm(s: Any) -> str:
    """全角英数を半角にするなど表記ゆれを吸収して前後の空白を除く。"""
    if s is None:
        return ""
    return unicodedata.normalize("NFKC", str(s)).strip()


@lru_cache(maxsize=None)
def _load_all() -> dict[str, dict]:
    surveys = {}
    for path in sorted(SURVEY_DIR.glob("*.json")):
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        surveys[s["id"]] = s
    return dict(sorted(surveys.items(), key=lambda kv: kv[1].get("order", 999)))


def list_surveys() -> list[dict]:
    return list(_load_all().values())


def get_survey(survey_id: str) -> dict:
    try:
        return _load_all()[survey_id]
    except KeyError:
        raise KeyError(f"unknown survey: {survey_id}") from None


def question_map(survey: dict) -> dict[str, dict]:
    return {q["id"]: q for q in survey["questions"]}


# ---------------------------------------------------------------- 回答の形


def blank_answer(q: dict) -> dict:
    t = q["type"]
    if t in CHOICE_TYPES:
        return {"selected": [], "other_text": ""}
    if t == "timegrid":
        return {r["id"]: [] for r in q["rows"]}
    if t == "destinations":
        return {p["id"]: {"checked": False, "place": "", "stop": ""} for p in q["purposes"]}
    if t == "trip":
        return {"destination": "", "depart_time": "", "return_time": ""}
    if t in TEXT_TYPES:
        return {"text": ""}
    raise ValueError(f"unknown question type: {t}")


def blank_answers(survey: dict) -> dict:
    return {q["id"]: blank_answer(q) for q in survey["questions"]}


_TIME_RE = re.compile(r"^(\d{1,2})[:時](\d{1,2})?")


def norm_time(s: Any) -> str:
    """'9:5'・'9時30'・'午後1時' を 'HH:MM' に揃える。読めなければ空文字。"""
    s = norm(s)
    pm = s.startswith("午後")
    s = s.removeprefix("午前").removeprefix("午後").strip()
    m = _TIME_RE.match(s)
    if not m:
        return ""
    h, mnt = int(m.group(1)), int(m.group(2) or 0)
    if pm and h < 12:
        h += 12
    if not (0 <= h <= 23 and 0 <= mnt <= 59):
        return ""
    return f"{h:02d}:{mnt:02d}"


def normalize_answer(q: dict, raw: Any) -> dict:
    """OCR 結果や画面からの入力を、様式で定義された正しい形に揃える。

    未知の選択肢や範囲外の時刻は捨てる（様式にない値を集計に混ぜないため）。
    """
    ans = blank_answer(q)
    if not isinstance(raw, dict):
        return ans
    t = q["type"]
    if t in CHOICE_TYPES:
        opts = {norm(o): o for o in q["options"]}
        sel = raw.get("selected") or []
        if isinstance(sel, str):
            sel = [sel]
        picked = {opts[norm(v)] for v in sel if norm(v) in opts}
        ans["selected"] = [o for o in q["options"] if o in picked]
        ans["other_text"] = norm(raw.get("other_text"))
    elif t == "timegrid":
        hours = set(q["hours"])
        for r in q["rows"]:
            vals = raw.get(r["id"]) or []
            got = set()
            for v in vals:
                try:
                    h = int(v)
                except (TypeError, ValueError):
                    continue
                if h in hours:
                    got.add(h)
            ans[r["id"]] = sorted(got)
    elif t == "destinations":
        for p in q["purposes"]:
            d = raw.get(p["id"]) or {}
            if not isinstance(d, dict):
                continue
            place, stop = norm(d.get("place")), norm(d.get("stop"))
            ans[p["id"]] = {
                # 名称だけ書いて□にチェックし忘れた用紙も「その種別の回答あり」とみなす
                "checked": bool(d.get("checked")) or bool(place or stop),
                "place": place,
                "stop": stop,
            }
    elif t == "trip":
        ans["destination"] = norm(raw.get("destination"))
        ans["depart_time"] = norm_time(raw.get("depart_time"))
        ans["return_time"] = norm_time(raw.get("return_time"))
    elif t in TEXT_TYPES:
        ans["text"] = str(raw.get("text") or "").strip()
    return ans


def normalize_answers(survey: dict, raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {q["id"]: normalize_answer(q, raw.get(q["id"])) for q in survey["questions"]}


def is_answered(q: dict, ans: dict) -> bool:
    t = q["type"]
    if t in CHOICE_TYPES:
        return bool(ans["selected"])
    if t == "timegrid":
        return any(ans[r["id"]] for r in q["rows"])
    if t == "destinations":
        return any(ans[p["id"]]["checked"] for p in q["purposes"])
    if t == "trip":
        return any(ans.values())
    return bool(ans["text"])


def find_problems(survey: dict, answers: dict) -> list[str]:
    """人の確認が必要そうな設問IDを返す（単一回答への複数チェックなど）。"""
    problems = []
    for q in survey["questions"]:
        a = answers[q["id"]]
        if q["type"] == "single" and len(a["selected"]) > 1:
            problems.append(q["id"])
    return problems


# ---------------------------------------------------------------- AI 読み取り用のスキーマと説明


def _answer_schema(q: dict) -> dict:
    t = q["type"]
    if t in CHOICE_TYPES:
        return {
            "type": "object",
            "properties": {
                "selected": {"type": "array", "items": {"type": "string", "enum": q["options"]}},
                "other_text": {"type": "string"},
            },
            "required": ["selected", "other_text"],
            "additionalProperties": False,
        }
    if t == "timegrid":
        hour_list = {"type": "array", "items": {"type": "integer", "enum": q["hours"]}}
        return {
            "type": "object",
            "properties": {r["id"]: hour_list for r in q["rows"]},
            "required": [r["id"] for r in q["rows"]],
            "additionalProperties": False,
        }
    if t == "destinations":
        item = {
            "type": "object",
            "properties": {
                "checked": {"type": "boolean"},
                "place": {"type": "string"},
                "stop": {"type": "string"},
            },
            "required": ["checked", "place", "stop"],
            "additionalProperties": False,
        }
        return {
            "type": "object",
            "properties": {p["id"]: item for p in q["purposes"]},
            "required": [p["id"] for p in q["purposes"]],
            "additionalProperties": False,
        }
    if t == "trip":
        return {
            "type": "object",
            "properties": {
                "destination": {"type": "string"},
                "depart_time": {"type": "string"},
                "return_time": {"type": "string"},
            },
            "required": ["destination", "depart_time", "return_time"],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    }


def ocr_schema(survey: dict) -> dict:
    qids = [q["id"] for q in survey["questions"]]
    return {
        "type": "object",
        "properties": {
            "answers": {
                "type": "object",
                "properties": {q["id"]: _answer_schema(q) for q in survey["questions"]},
                "required": qids,
                "additionalProperties": False,
            },
            "uncertain": {"type": "array", "items": {"type": "string", "enum": qids}},
            "notes": {"type": "string"},
        },
        "required": ["answers", "uncertain", "notes"],
        "additionalProperties": False,
    }


def describe_question(q: dict) -> str:
    head = f"- {q['id']}（{q['no']}）{q['label']}"
    t = q["type"]
    lines = [head]
    if t in CHOICE_TYPES:
        kind = "単一回答" if t == "single" else "複数回答"
        lines.append(f"  形式: {kind}。選択肢: " + " / ".join(q["options"]))
        if q.get("other_option"):
            lines.append(f"  「{q['other_option']}（　）」の括弧内の記述は other_text に書き写す。")
    elif t == "timegrid":
        rows = "、".join(f"{r['id']}=「{r['label']}」の行" for r in q["rows"])
        lines.append(
            f"  形式: 時間帯の表。列は {q['hours'][0]}時〜{q['hours'][-1]}時。{rows}。"
            "〇印・チェック・塗りつぶし等がある列の時（整数）を列挙する。"
            "1つの印が複数列にまたがる場合や、矢印・線で範囲が示されている場合は、覆われている列をすべて含める。"
        )
    elif t == "destinations":
        ps = "、".join(f"{p['id']}=「{p['label']}」" for p in q["purposes"])
        lines.append(
            f"  形式: 種別ごとの列がある表（{ps}）。各列について、種別の□にチェックがあれば checked=true、"
            "「目的地の名称」欄の記述を place、「目的地の最寄り停留所名」欄の記述を stop に書き写す。"
            "見出しにある記入例（〇〇病院、渋沢駅、渋沢中学校入口など印刷された例）は回答ではない。"
        )
    elif t == "trip":
        lines.append(
            "  形式: 記入欄。行き先（目的地）を destination、行きの出発時刻を depart_time、"
            "帰りの到着時刻を return_time に入れる。時刻は午前・午後の〇印を考慮して24時間表記の HH:MM にする"
            "（例: 午後1時 → 13:00）。午前・午後に印がない場合は数字のまま HH:MM にして uncertain に含める。"
        )
    elif t == "short_text":
        lines.append("  形式: 短い記述欄。書かれた内容をそのまま text に書き写す。")
    else:
        lines.append("  形式: 自由記述欄。書かれた文章をそのまま text に書き写す（改行は \\n）。")
    for key in ("hint", "ocr_hint"):
        if q.get(key):
            lines.append(f"  補足: {q[key]}")
    return "\n".join(lines)


def describe_survey(survey: dict) -> str:
    return f"様式名: {survey['title']}\n\n設問一覧:\n" + "\n".join(
        describe_question(q) for q in survey["questions"]
    )
