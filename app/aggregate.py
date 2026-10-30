"""回答データの集計（単純集計・クロス集計）。"""

from __future__ import annotations

from collections import Counter

from .surveys import CHOICE_TYPES, is_answered, norm

NO_ANSWER = "無回答"
INVALID = "複数回答（無効）"


def apply_filters(survey: dict, responses: list[dict], filters: dict[str, str]) -> list[dict]:
    """filters = {設問ID: 選択肢} に一致する回答者だけに絞る。"""
    qs = {q["id"]: q for q in survey["questions"]}
    out = responses
    for qid, value in filters.items():
        if qid not in qs or not value:
            continue
        if value == NO_ANSWER:
            out = [r for r in out if not r["answers"][qid]["selected"]]
        else:
            out = [r for r in out if value in r["answers"][qid]["selected"]]
    return out


def choice_category(q: dict, ans: dict) -> str | None:
    """単一回答設問での回答者の区分（クロス集計の表側に使う）。"""
    sel = ans["selected"]
    if not sel:
        return NO_ANSWER
    if len(sel) > 1:
        return INVALID
    return sel[0]


def _pct(n: int, base: int) -> float:
    return round(n * 100 / base, 1) if base else 0.0


def _choice_counts(q: dict, answers: list[dict]) -> dict:
    total = len(answers)
    counts = Counter()
    no_answer = invalid = 0
    other_texts = []
    for a in answers:
        sel = a["selected"]
        if not sel:
            no_answer += 1
        elif q["type"] == "single" and len(sel) > 1:
            invalid += 1
        else:
            counts.update(sel)
        if a["other_text"]:
            other_texts.append(a["other_text"])
    rows = [{"label": o, "count": counts[o], "pct": _pct(counts[o], total)} for o in q["options"]]
    if q["type"] == "single":
        rows.append({"label": INVALID, "count": invalid, "pct": _pct(invalid, total), "extra": True})
    rows.append({"label": NO_ANSWER, "count": no_answer, "pct": _pct(no_answer, total), "extra": True})
    return {"rows": rows, "answered": total - no_answer, "other_texts": other_texts}


def _freq(values: list[str], limit: int | None = None) -> list[dict]:
    c = Counter(norm(v) for v in values if norm(v))
    return [{"label": k, "count": n} for k, n in c.most_common(limit)]


def summarize_question(q: dict, responses: list[dict]) -> dict:
    answers = [r["answers"][q["id"]] for r in responses]
    total = len(answers)
    out = {k: q[k] for k in ("id", "no", "label", "type") if k in q}
    out["total"] = total
    out["answered"] = sum(1 for a in answers if is_answered(q, a))
    t = q["type"]
    if t in CHOICE_TYPES:
        out.update(_choice_counts(q, answers))
    elif t == "timegrid":
        out["hours"] = q["hours"]
        out["rows"] = [
            {
                "id": row["id"],
                "label": row["label"],
                "counts": [sum(1 for a in answers if h in a[row["id"]]) for h in q["hours"]],
            }
            for row in q["rows"]
        ]
    elif t == "destinations":
        out["purposes"] = []
        for p in q["purposes"]:
            items = [a[p["id"]] for a in answers if a[p["id"]]["checked"]]
            out["purposes"].append(
                {
                    "label": p["label"],
                    "count": len(items),
                    "pct": _pct(len(items), total),
                    "places": _freq([i["place"] for i in items]),
                    "stops": _freq([i["stop"] for i in items]),
                }
            )
    elif t == "trip":
        def hours(key):
            c = Counter(int(a[key][:2]) for a in answers if a[key])
            return [{"label": f"{h}時台", "hour": h, "count": c[h]} for h in sorted(c)]

        out["destinations"] = _freq([a["destination"] for a in answers])
        out["depart_hours"] = hours("depart_time")
        out["return_hours"] = hours("return_time")
        out["entries"] = [
            {"id": r["id"], **r["answers"][q["id"]]} for r in responses if is_answered(q, r["answers"][q["id"]])
        ]
    elif t == "short_text":
        out["values"] = _freq([a["text"] for a in answers])
    else:
        out["texts"] = [
            {"id": r["id"], "text": r["answers"][q["id"]]["text"]}
            for r in responses
            if r["answers"][q["id"]]["text"]
        ]
    return out


def cross_table(survey: dict, by_qid: str, q: dict, responses: list[dict]) -> dict:
    """by_qid（単一回答の属性設問）× q（選択式設問）のクロス集計。"""
    by_q = next(x for x in survey["questions"] if x["id"] == by_qid)
    groups: dict[str, list[dict]] = {c: [] for c in [*by_q["options"], INVALID, NO_ANSWER]}
    for r in responses:
        groups[choice_category(by_q, r["answers"][by_qid])].append(r["answers"][q["id"]])
    columns = [o for o in q["options"]] + ([INVALID] if q["type"] == "single" else []) + [NO_ANSWER]
    rows = []
    for label, answers in groups.items():
        if not answers and label in (INVALID, NO_ANSWER):
            continue
        counts = {r["label"]: r["count"] for r in _choice_counts(q, answers)["rows"]}
        rows.append(
            {
                "label": label,
                "n": len(answers),
                "counts": [counts.get(c, 0) for c in columns],
                "pcts": [_pct(counts.get(c, 0), len(answers)) for c in columns],
            }
        )
    return {"by": by_qid, "by_label": by_q["label"], "columns": columns, "rows": rows}


def summarize(survey: dict, responses: list[dict], cross_by: str | None = None) -> dict:
    questions = []
    for q in survey["questions"]:
        s = summarize_question(q, responses)
        if cross_by and q["type"] in CHOICE_TYPES and q["id"] != cross_by:
            s["cross"] = cross_table(survey, cross_by, q, responses)
        questions.append(s)
    return {"survey_id": survey["id"], "total": len(responses), "questions": questions}
