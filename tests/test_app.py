import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from PIL import Image

from app import aggregate, main, ocr, surveys


class InlineExecutor:
    """読み取りをバックグラウンドではなくその場で実行する（テスト用）。"""

    def submit(self, fn, *args):
        fn(*args)

    def shutdown(self, **kw):
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SURVEY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(main, "_executor", InlineExecutor())
    with TestClient(main.app) as c:
        yield c


def png(color="white") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (400, 560), color).save(buf, "PNG")
    return buf.getvalue()


def pdf(pages: int) -> bytes:
    ims = [Image.new("RGB", (400, 560), "white") for _ in range(pages)]
    buf = io.BytesIO()
    ims[0].save(buf, "PDF", save_all=True, append_images=ims[1:])
    return buf.getvalue()


def answers_for(survey_id: str, **overrides) -> dict:
    a = surveys.blank_answers(surveys.get_survey(survey_id))
    a.update(overrides)
    return a


# ---------------------------------------------------------------- 様式定義


@pytest.mark.parametrize("survey", surveys.list_surveys(), ids=lambda s: s["id"])
def test_survey_definitions_are_consistent(survey):
    ids = [q["id"] for q in survey["questions"]]
    assert len(ids) == len(set(ids))
    for q in survey["questions"]:
        if q["type"] in surveys.CHOICE_TYPES:
            assert len(q["options"]) == len(set(q["options"]))
            if q.get("other_option"):
                assert q["other_option"] in q["options"]
    schema = surveys.ocr_schema(survey)
    assert schema["properties"]["answers"]["required"] == ids
    # 空の回答は正規化しても変わらない
    blank = surveys.blank_answers(survey)
    assert surveys.normalize_answers(survey, blank) == blank


def test_normalize_drops_unknown_values_and_fixes_formats():
    s = surveys.get_survey("taxi_minami")
    raw = {
        "gender": {"selected": ["女性", "宇宙人"], "other_text": ""},
        "age": {"selected": "７０代"},  # 全角・文字列でも受け付ける
        "q3": {"outbound": [8, "9", 99], "return": []},
        "q4": {"hospital": {"checked": False, "place": "秦野赤十字病院", "stop": ""}},
        "q6": {"destination": "Ａスーパー", "depart_time": "9時5", "return_time": "25:00"},
    }
    a = surveys.normalize_answers(s, raw)
    assert a["gender"]["selected"] == ["女性"]
    assert a["age"]["selected"] == ["70代"]
    assert a["q3"] == {"outbound": [8, 9], "return": []}
    assert a["q4"]["hospital"] == {"checked": True, "place": "秦野赤十字病院", "stop": ""}
    assert a["q6"] == {"destination": "Aスーパー", "depart_time": "09:05", "return_time": ""}
    assert surveys.norm_time("午後1時") == "13:00"
    assert surveys.norm_time("午前11:30") == "11:30"


# ---------------------------------------------------------------- 取り込み


def test_upload_splits_pdf_pages_into_respondents(client):
    files = [("files", ("scan.pdf", pdf(4), "application/pdf"))]
    res = client.post(
        "/api/surveys/chimura_new/upload",
        files=files,
        data={"batch": "第1回", "pages_per_response": "2", "run_ocr_now": "false"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["pages"] == 4 and len(body["created"]) == 2 and body["warning"] == ""

    r = client.get(f"/api/responses/{body['created'][1]}").json()
    assert [p["page_no"] for p in r["pages"]] == [1, 2]
    assert r["source"] == "scan.pdf p.3,4"
    assert r["batch"] == "第1回"
    img = client.get(f"/api/pages/{r['pages'][0]['id']}/image")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"


def test_upload_groups_pages_within_each_pdf(client):
    # 1つ目の PDF は1ページ抜けている。2つ目の PDF の区切りはずれない
    files = [
        ("files", ("box01.pdf", pdf(5), "application/pdf")),
        ("files", ("box02.pdf", pdf(4), "application/pdf")),
    ]
    body = client.post(
        "/api/surveys/chimura_new/upload",
        files=files,
        data={"pages_per_response": "2", "run_ocr_now": "false"},
    ).json()
    assert body["pages"] == 9 and len(body["created"]) == 5
    assert "box01.pdf" in body["warning"] and "box02.pdf" not in body["warning"]
    sources = [client.get(f"/api/responses/{i}").json()["source"] for i in body["created"]]
    assert sources == ["box01.pdf p.1,2", "box01.pdf p.3,4", "box01.pdf p.5", "box02.pdf p.1,2", "box02.pdf p.3,4"]


def test_upload_pairs_single_page_images_in_order(client):
    files = [("files", (n, png(), "image/png")) for n in ("1表.png", "1裏.png", "2表.png", "2裏.png")]
    body = client.post(
        "/api/surveys/chimura_new/upload",
        files=files,
        data={"pages_per_response": "2", "run_ocr_now": "false"},
    ).json()
    sources = [client.get(f"/api/responses/{i}").json()["source"] for i in body["created"]]
    assert sources == ["1表.png / 1裏.png", "2表.png / 2裏.png"] and body["warning"] == ""


def test_upload_rejects_ocr_without_api_key(client, monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: False)
    res = client.post(
        "/api/surveys/taxi_oone/upload",
        files=[("files", ("a.png", png(), "image/png"))],
        data={"run_ocr_now": "true"},
    )
    assert res.status_code == 400


def test_ocr_result_sets_status(client, monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: True)

    def fake_read(survey, paths):
        assert len(paths) == 1 and paths[0].exists()
        a = answers_for(survey["id"])
        a["gender"] = {"selected": ["男性"], "other_text": ""}
        return {"answers": a, "uncertain": [], "notes": ""}

    monkeypatch.setattr(ocr, "read_form", fake_read)
    res = client.post(
        "/api/surveys/taxi_oone/upload",
        files=[("files", ("a.png", png(), "image/png"))],
        data={"run_ocr_now": "true"},
    ).json()
    r = client.get(f"/api/responses/{res['created'][0]}").json()
    assert r["status"] == "auto"
    assert r["answers"]["gender"]["selected"] == ["男性"]

    # 単一回答に複数チェック → 要確認
    def fake_read_multi(survey, paths):
        a = answers_for(survey["id"])
        a["age"] = {"selected": ["60代", "70代"], "other_text": ""}
        return {"answers": a, "uncertain": [], "notes": ""}

    monkeypatch.setattr(ocr, "read_form", fake_read_multi)
    client.post(f"/api/responses/{res['created'][0]}/ocr")
    r = client.get(f"/api/responses/{res['created'][0]}").json()
    assert r["status"] == "review" and r["uncertain"] == ["age"]

    # 読み取り失敗はエラーとして記録される
    def fake_fail(survey, paths):
        raise ocr.OCRError("だめでした")

    monkeypatch.setattr(ocr, "read_form", fake_fail)
    client.post(f"/api/responses/{res['created'][0]}/ocr")
    r = client.get(f"/api/responses/{res['created'][0]}").json()
    assert r["status"] == "error" and r["error"] == "だめでした"


# ---------------------------------------------------------------- 確認・集計・出力


def add_manual(client, survey_id, answers, status="confirmed"):
    rid = client.post(f"/api/surveys/{survey_id}/responses").json()["id"]
    res = client.put(f"/api/responses/{rid}", json={"answers": answers, "status": status})
    assert res.status_code == 200, res.text
    return rid


def sel(*values, other=""):
    return {"selected": list(values), "other_text": other}


def test_summary_cross_filter_and_export(client):
    sid = "taxi_nishi_chimura"
    add_manual(client, sid, answers_for(
        sid, gender=sel("男性"), age=sel("70代"), area=sel("千村台"), q2=sel("ほぼ毎日"),
        q3={"outbound": [8, 9], "return": [12]},
        q4={"hospital": {"checked": True, "place": "秦野赤十字病院", "stop": "日赤前"}},
        q5=sel("行きたい目的地がない", "その他", other="坂が多い"),
        q8={"text": "本数を増やしてほしい"},
    ))
    add_manual(client, sid, answers_for(
        sid, gender=sel("女性"), age=sel("70代"), q2=sel("ほぼ毎日", "月に数回程度"),
        q3={"outbound": [8], "return": []},
        q4={"hospital": {"checked": True, "place": "秦野赤十字病院", "stop": ""},
            "shopping": {"checked": True, "place": "", "stop": ""}},
        q6={"destination": "渋沢駅", "depart_time": "13:30", "return_time": ""},
    ))
    add_manual(client, sid, answers_for(sid, gender=sel("女性"), q2=sel("年に数回程度")), status="review")

    s = client.get(f"/api/surveys/{sid}/summary").json()
    assert s["total"] == 3
    q = {x["id"]: x for x in s["questions"]}
    counts = {r["label"]: r["count"] for r in q["q2"]["rows"]}
    assert counts["ほぼ毎日"] == 1 and counts["年に数回程度"] == 1
    assert counts[aggregate.INVALID] == 1  # 単一回答への複数チェック
    assert q["q3"]["rows"][0]["counts"][q["q3"]["hours"].index(8)] == 2
    hosp = q["q4"]["purposes"][0]
    assert hosp["count"] == 2 and hosp["places"] == [{"label": "秦野赤十字病院", "count": 2}]
    assert q["q5"]["other_texts"] == ["坂が多い"]
    assert q["q6"]["depart_hours"] == [{"label": "13時台", "hour": 13, "count": 1}]
    assert q["q8"]["texts"][0]["text"] == "本数を増やしてほしい"

    # 確認済のみ・絞り込み・クロス集計
    assert client.get(f"/api/surveys/{sid}/summary?scope=confirmed").json()["total"] == 2
    assert client.get(f"/api/surveys/{sid}/summary?f_gender=女性").json()["total"] == 2
    s = client.get(f"/api/surveys/{sid}/summary?cross=gender").json()
    ct = next(x for x in s["questions"] if x["id"] == "q2")["cross"]
    male = next(r for r in ct["rows"] if r["label"] == "男性")
    assert male["n"] == 1 and male["counts"][ct["columns"].index("ほぼ毎日")] == 1

    xlsx = client.get(f"/api/surveys/{sid}/export.xlsx")
    assert xlsx.status_code == 200
    wb = load_workbook(io.BytesIO(xlsx.content))
    assert wb.sheetnames == ["単純集計", "クロス_性別", "クロス_年齢", "クロス_自治会区", "自由記述", "回答データ"]
    raw = list(wb["回答データ"].values)
    assert len(raw) == 4 and "問5_その他記述" in raw[0]

    csv_text = client.get(f"/api/surveys/{sid}/export.csv").content.decode("utf-8-sig")
    assert csv_text.splitlines()[0].startswith("No.,状態,回収区分,ファイル,性別")


def test_confirm_auto_and_delete(client, monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "read_form", lambda s, p: {"answers": answers_for(s["id"]), "uncertain": [], "notes": ""})
    created = client.post(
        "/api/surveys/taxi_minami/upload",
        files=[("files", ("a.png", png(), "image/png")), ("files", ("b.png", png(), "image/png"))],
        data={"run_ocr_now": "true"},
    ).json()["created"]
    assert client.post("/api/surveys/taxi_minami/confirm-auto").json() == {"confirmed": 2}
    assert client.delete(f"/api/responses/{created[0]}").json() == {"ok": True}
    counts = client.get("/api/surveys/taxi_minami/responses").json()["counts"]
    assert counts["confirmed"] == 1
