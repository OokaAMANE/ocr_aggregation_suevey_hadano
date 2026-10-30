"use strict";

const STATUS = {
  batch: "バッチ読取中",
  queued: "読取待ち",
  processing: "読取中",
  auto: "読取済（未確認）",
  review: "要確認",
  confirmed: "確認済",
  error: "エラー",
};

const state = {
  config: { ocr_available: false },
  surveys: [],
  surveyId: null,
  survey: null, // 現在の様式の定義
  listFilter: "",
  summary: { scope: "all", filters: {}, cross: "" },
};

let pollTimer = null;

// ---------------------------------------------------------------- 共通

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = `エラー (${res.status})`;
    try {
      const body = await res.json();
      if (body.detail) msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}

const jsonOpts = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function toast(msg, bad = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (bad ? " bad" : "");
  el.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (el.hidden = true), bad ? 6000 : 2500);
}

function badge(status) {
  return `<span class="badge st-${status}">${esc(STATUS[status] || status)}</span>`;
}

function stopPolling() {
  clearTimeout(pollTimer);
  pollTimer = null;
}

function store(key, val) {
  try { localStorage.setItem(key, val); } catch (_) {}
}
function load(key) {
  try { return localStorage.getItem(key); } catch (_) { return null; }
}

// ---------------------------------------------------------------- 起動・ルーティング

async function init() {
  [state.config, state.surveys] = await Promise.all([api("/api/config"), api("/api/surveys")]);
  $("#ocr-warning").hidden = state.config.ocr_available;
  const sel = $("#survey-select");
  sel.innerHTML = state.surveys.map((s) => `<option value="${esc(s.id)}">${esc(s.title)}</option>`).join("");
  const saved = load("surveyId");
  state.surveyId = state.surveys.some((s) => s.id === saved) ? saved : state.surveys[0].id;
  sel.value = state.surveyId;
  sel.addEventListener("change", async () => {
    state.surveyId = sel.value;
    store("surveyId", sel.value);
    state.summary = { scope: state.summary.scope, filters: {}, cross: "" };
    await loadSurvey();
    if (location.hash.startsWith("#/review")) location.hash = "#/list";
    else route();
  });
  await loadSurvey();
  window.addEventListener("hashchange", route);
  route();
}

async function loadSurvey() {
  state.survey = await api(`/api/surveys/${state.surveyId}`);
}

function route() {
  stopPolling();
  const [, page, arg] = (location.hash || "#/list").split("/");
  $$(".tabs a").forEach((a) => a.classList.toggle("active", a.dataset.tab === (page === "review" ? "list" : page)));
  const view = $("#view");
  if (page === "import") return renderImport(view);
  if (page === "review") return renderReview(view, Number(arg));
  if (page === "summary") return renderSummary(view);
  return renderList(view);
}

// ---------------------------------------------------------------- ① 取り込み

function renderImport(view) {
  const s = state.surveys.find((x) => x.id === state.surveyId);
  const ocrOk = state.config.ocr_available;
  const mode = ocrOk ? load("ocrMode") || "batch" : "none";
  view.innerHTML = `
    <h2>用紙の取り込み</h2>
    <div class="panel">
      <p class="muted" style="margin-top:0">
        スキャンした回答用紙（PDF・JPEG・PNG・TIFF）を選んでください。複数ページの PDF は、先頭から
        「1人あたりのページ数」ごとに1人分として区切ります。様式が正しいか、上部の「様式」で確認してから取り込んでください。
      </p>
      <div class="drop" id="drop">
        <strong>ここにファイルをドラッグ＆ドロップ</strong>
        <span class="muted">またはクリックしてファイルを選択</span>
        <input type="file" id="file" multiple accept=".pdf,.jpg,.jpeg,.png,.tif,.tiff,image/*" hidden>
      </div>
      <ul class="file-list" id="file-list"></ul>
      <div class="row" style="margin-top:14px; align-items:flex-end">
        <label class="field">回収区分（任意・例: 第1回回収、〇〇自治会）
          <input type="text" id="batch" style="width:280px" value="${esc(load("batch") || "")}">
        </label>
        <label class="field">1人あたりのページ数
          <select id="ppr">${[1, 2, 3, 4].map((n) => `<option ${n === s.default_pages ? "selected" : ""}>${n}</option>`).join("")}</select>
        </label>
        <span class="spacer"></span>
        <button class="btn primary" id="go" disabled>取り込み開始</button>
      </div>
      <fieldset class="modes">
        <legend>AIによる読み取り</legend>
        ${[
          ["batch", "バッチで読み取る（おすすめ）", "料金が半額。結果は通常1時間以内（最大24時間）に届き、届いた分から一覧に反映されます。"],
          ["now", "すぐに読み取る", "通常料金。1枚あたり数十秒で結果が出ます。少数の試し読みや急ぐとき向け。"],
          ["none", "読み取らない", "画像だけ取り込み、画面から手入力します。"],
        ].map(([v, label, desc]) => `
          <label class="mode"><input type="radio" name="ocr-mode" value="${v}"
            ${v !== "none" && !ocrOk ? "disabled" : ""} ${v === mode ? "checked" : ""}>
            <span><strong>${label}</strong><br><span class="muted">${desc}</span></span></label>`).join("")}
      </fieldset>
    </div>
    <div class="panel">
      <h3>用紙がない回答（Web回答・電話聞き取りなど）</h3>
      <div class="row">
        <span class="muted">画像なしで回答を1件追加し、画面から手入力します。</span>
        <span class="spacer"></span>
        <button class="btn" id="manual">手入力で1件追加</button>
      </div>
    </div>`;

  let files = [];
  const drop = $("#drop"), input = $("#file"), go = $("#go");
  const setFiles = (list) => {
    files = [...list];
    $("#file-list").innerHTML = files.map((f) => `<li>${esc(f.name)} <span class="muted">(${(f.size / 1024 / 1024).toFixed(1)} MB)</span></li>`).join("");
    go.disabled = !files.length;
  };
  drop.addEventListener("click", () => input.click());
  input.addEventListener("change", () => setFiles(input.files));
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); setFiles(e.dataTransfer.files); });

  go.addEventListener("click", async () => {
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    fd.append("batch", $("#batch").value);
    fd.append("pages_per_response", $("#ppr").value);
    const ocrMode = $("input[name=ocr-mode]:checked").value;
    fd.append("ocr_mode", ocrMode);
    store("batch", $("#batch").value);
    if (ocrMode !== "none") store("ocrMode", ocrMode);
    go.disabled = true;
    go.textContent = "取り込み中…";
    try {
      const res = await api(`/api/surveys/${state.surveyId}/upload`, { method: "POST", body: fd });
      toast(`${res.pages}ページ・${res.created.length}人分を取り込みました`);
      if (res.warning) toast(res.warning, true);
      location.hash = "#/list";
    } catch (e) {
      toast(e.message, true);
      go.disabled = false;
      go.textContent = "取り込み開始";
    }
  });

  $("#manual").addEventListener("click", async () => {
    const res = await api(`/api/surveys/${state.surveyId}/responses`, { method: "POST" });
    location.hash = `#/review/${res.id}`;
  });
}

// ---------------------------------------------------------------- ② 回答一覧

async function renderList(view) {
  const [data, batches] = await Promise.all([
    api(`/api/surveys/${state.surveyId}/responses?status=${state.listFilter}`),
    api(`/api/surveys/${state.surveyId}/batches`),
  ]);
  const c = data.counts;
  const total = Object.values(c).reduce((a, b) => a + b, 0);
  const attrQs = state.survey.questions.filter((q) => q.attribute);
  const filters = [["", `すべて ${total}`], ...Object.keys(STATUS).map((k) => [k, `${STATUS[k]} ${c[k]}`])];

  view.innerHTML = `
    <div class="row" style="margin-bottom:12px">
      <h2 style="margin:0">回答一覧</h2>
      <span class="spacer"></span>
      ${c.review + c.auto ? `<a class="btn primary" href="#/review/${firstToReview(data.responses)}">未確認の回答を順に確認</a>` : ""}
      ${c.auto ? `<button class="btn" id="confirm-auto">読取済（未確認）${c.auto}件を一括で確認済にする</button>` : ""}
      ${c.error && state.config.ocr_available ? `<button class="btn" id="retry">エラー${c.error}件をバッチで再読み取り</button>` : ""}
    </div>
    ${renderBatches(batches, c.batch)}
    <div class="panel">
      <div class="chips" style="margin-bottom:12px">
        ${filters.map(([k, label]) => `<button class="chip ${state.listFilter === k ? "on" : ""}" data-f="${k}">${esc(label)}</button>`).join("")}
      </div>
      ${data.responses.length ? `
      <div class="table-wrap"><table class="list">
        <thead><tr><th>No.</th><th>状態</th>${attrQs.map((q) => `<th>${esc(q.label)}</th>`).join("")}<th>要確認の設問・メモ</th><th>回収区分</th><th>ファイル</th></tr></thead>
        <tbody>
          ${data.responses.map((r) => `
            <tr class="clickable" data-id="${r.id}">
              <td>${r.id}</td>
              <td>${badge(r.status)}</td>
              ${attrQs.map((q) => `<td class="nw">${esc(r.attrs[q.id] || "")}</td>`).join("")}
              <td>${r.error ? `<span style="color:var(--bad)">${esc(r.error)}</span>` : esc(r.uncertain.join("、"))}${r.notes ? `<div class="muted">${esc(r.notes)}</div>` : ""}</td>
              <td>${esc(r.batch)}</td>
              <td class="muted">${esc(r.source)}</td>
            </tr>`).join("")}
        </tbody>
      </table></div>` : `<p class="muted">回答はまだありません。「① 取り込み」から用紙を取り込んでください。</p>`}
    </div>`;

  $$(".chip", view).forEach((b) => b.addEventListener("click", () => { state.listFilter = b.dataset.f; renderList(view); }));
  $$("tr.clickable", view).forEach((tr) => tr.addEventListener("click", () => (location.hash = `#/review/${tr.dataset.id}`)));
  $("#confirm-auto")?.addEventListener("click", async () => {
    if (!confirm("AIが気になる点なしと判定した回答を、確認せずに確認済にします。よろしいですか？")) return;
    const res = await api(`/api/surveys/${state.surveyId}/confirm-auto`, { method: "POST" });
    toast(`${res.confirmed}件を確認済にしました`);
    renderList(view);
  });
  $("#retry")?.addEventListener("click", async () => {
    const res = await api(`/api/surveys/${state.surveyId}/retry-errors?mode=batch`, { method: "POST" });
    toast(`${res.queued}件をバッチで再読み取りします`);
    renderList(view);
  });
  $("#check-batches")?.addEventListener("click", async () => {
    await api("/api/batches/check", { method: "POST" });
    toast("結果を確認しています…");
    setTimeout(() => location.hash.startsWith("#/list") && renderList(view), 4000);
  });

  // 読み取り中は自動で表示を更新する（バッチは結果が届くまで時間がかかるので間隔を長めに）
  const wait = c.queued + c.processing > 0 ? 3000 : c.batch > 0 ? 30000 : 0;
  if (wait) pollTimer = setTimeout(() => location.hash.startsWith("#/list") && renderList(view), wait);
}

function renderBatches(data, waiting) {
  const active = data.batches.filter((b) => b.status === "in_progress");
  if (!waiting && !active.length) return "";
  const fmt = (t) => (t ? t.replace("T", " ").slice(5, 16) : "");
  return `<div class="panel">
    <div class="row" style="margin-bottom:8px">
      <h3 style="margin:0">バッチ読み取りの状況</h3>
      <span class="muted">結果は自動で確認しています（1分ごと）。通常1時間以内、最大24時間で届きます。</span>
      <span class="spacer"></span>
      <button class="btn small" id="check-batches">今すぐ確認</button>
    </div>
    ${data.unsent ? `<p class="muted" style="margin:0 0 6px">送信準備中: ${data.unsent}件</p>` : ""}
    ${data.batches.length ? `<div class="table-wrap"><table class="list">
      <thead><tr><th>送信日時</th><th>件数</th><th>状況</th><th>完了</th><th>失敗</th></tr></thead>
      <tbody>${data.batches.map((b) => `<tr>
        <td>${esc(fmt(b.created_at))}</td><td>${b.total}</td>
        <td>${b.status === "ended" ? `<span class="badge st-confirmed">受け取り済</span>` : `<span class="badge st-batch">処理中（残り ${b.processing}件）</span>`}</td>
        <td>${b.succeeded}</td><td>${b.errored || ""}</td></tr>`).join("")}
      </tbody></table></div>` : ""}
  </div>`;
}

function firstToReview(rows) {
  return (rows.find((r) => r.status === "review") || rows.find((r) => r.status === "auto") || {}).id || "";
}

// ---------------------------------------------------------------- 確認・修正

async function renderReview(view, id) {
  let r;
  try {
    r = await api(`/api/responses/${id}`);
  } catch (e) {
    view.innerHTML = `<div class="panel">${esc(e.message)} <a href="#/list">一覧へ戻る</a></div>`;
    return;
  }
  if (r.survey_id !== state.surveyId) {
    state.surveyId = r.survey_id;
    $("#survey-select").value = r.survey_id;
    await loadSurvey();
  }
  const busy = ["batch", "queued", "processing"].includes(r.status);
  const zoom = Number(load("zoom") || 100);

  view.innerHTML = `
    <div class="row" style="margin-bottom:12px">
      <a href="#/list">← 一覧</a>
      <h2 style="margin:0">回答 No.${r.id}</h2>
      ${badge(r.status)}
      <span class="muted">${esc(r.source)}</span>
      <span class="spacer"></span>
      <label class="row muted" style="gap:6px">回収区分 <input type="text" id="batch" value="${esc(r.batch)}" style="padding:4px 8px;border:1px solid var(--line);border-radius:6px"></label>
    </div>
    <div class="review">
      <section class="viewer">
        <div class="viewer-tools">
          <button class="btn small" data-zoom="-20">縮小</button>
          <button class="btn small" data-zoom="20">拡大</button>
          <button class="btn small" data-zoom="0">幅に合わせる</button>
          <span class="spacer"></span>
          ${r.pages.map((p) => `<button class="btn small" data-rotate="${p.id}">${r.pages.length > 1 ? p.page_no + "ページ目を" : ""}回転</button>`).join("")}
        </div>
        <div class="viewer-scroll" id="scroll">
          ${r.pages.length ? r.pages.map((p) => `<img data-page="${p.id}" src="/api/pages/${p.id}/image?v=${Date.now()}" alt="${p.page_no}ページ目">`).join("") : `<div class="no-image">用紙画像なし（手入力）</div>`}
        </div>
      </section>
      <section>
        ${busy ? `<div class="notes">${r.status === "batch"
          ? "バッチで読み取り中です（通常1時間以内）。結果が届くと一覧に反映されます。"
          : "AIが読み取り中です。完了すると自動で表示が更新されます。"}</div>` : ""}
        ${r.error ? `<div class="error-box">${esc(r.error)}</div>` : ""}
        ${r.notes ? `<div class="notes">AIからのメモ: ${esc(r.notes)}</div>` : ""}
        <form id="form">${state.survey.questions.map((q) => renderQuestion(q, r.answers[q.id], r.uncertain.includes(q.id))).join("")}</form>
        <div class="actionbar">
          <button class="btn primary" id="confirm-next" ${busy ? "disabled" : ""}>確認済にして次へ（Ctrl+Enter）</button>
          <button class="btn" id="save" ${busy ? "disabled" : ""}>保存（要確認のまま）</button>
          ${r.pages.length && state.config.ocr_available ? `<button class="btn" id="reocr" ${busy ? "disabled" : ""}>AIで再読み取り</button>` : ""}
          <span class="spacer"></span>
          <button class="btn danger" id="delete">この回答を削除</button>
        </div>
      </section>
    </div>`;

  // 画像の拡大縮小
  const scroll = $("#scroll");
  const applyZoom = (z) => {
    const w = scroll.clientWidth - 16;
    $$("img", scroll).forEach((img) => (img.style.width = z ? `${(w * z) / 100}px` : `${w}px`));
  };
  let z = zoom;
  applyZoom(z);
  $$("[data-zoom]", view).forEach((b) => b.addEventListener("click", () => {
    const d = Number(b.dataset.zoom);
    z = d === 0 ? 100 : Math.min(400, Math.max(40, z + d));
    store("zoom", z);
    applyZoom(z);
  }));
  $$("[data-rotate]", view).forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/pages/${b.dataset.rotate}/rotate?degrees=90`, { method: "POST" });
    const img = $(`img[data-page="${b.dataset.rotate}"]`, view);
    img.src = `/api/pages/${b.dataset.rotate}/image?v=${Date.now()}`;
  }));

  bindQuestionInputs($("#form"));

  const save = async (status) => {
    const body = { answers: collectAnswers($("#form")), batch: $("#batch").value };
    if (status) body.status = status;
    return api(`/api/responses/${id}`, jsonOpts("PUT", body));
  };
  const confirmNext = async () => {
    try {
      const res = await save("confirmed");
      toast(`No.${id} を確認済にしました`);
      location.hash = res.next_id ? `#/review/${res.next_id}` : "#/list";
      if (!res.next_id) toast("未確認の回答はありません");
    } catch (e) { toast(e.message, true); }
  };
  $("#confirm-next").addEventListener("click", confirmNext);
  $("#save").addEventListener("click", async () => {
    try { await save("review"); toast("保存しました"); } catch (e) { toast(e.message, true); }
  });
  $("#reocr")?.addEventListener("click", async () => {
    if (!confirm("現在の入力内容はAIの読み取り結果で上書きされます。再読み取りしますか？")) return;
    await api(`/api/responses/${id}/ocr`, { method: "POST" });
    renderReview(view, id);
  });
  $("#delete").addEventListener("click", async () => {
    if (!confirm(`回答 No.${id} を削除します。元に戻せません。よろしいですか？`)) return;
    await api(`/api/responses/${id}`, { method: "DELETE" });
    toast("削除しました");
    location.hash = r.next_id ? `#/review/${r.next_id}` : "#/list";
  });
  view.onkeydown = (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !busy) { e.preventDefault(); confirmNext(); }
  };

  if (busy) pollTimer = setTimeout(() => location.hash === `#/review/${id}` && renderReview(view, id), r.status === "batch" ? 30000 : 3000);
}

function renderQuestion(q, a, uncertain) {
  const head = `<div class="q-title"><span class="no">${esc(q.no)}</span>${esc(q.label)}${uncertain ? `<span class="q-flag">● 要確認</span>` : ""}</div>
    ${q.hint ? `<div class="q-hint">${esc(q.hint)}</div>` : ""}`;
  let body = "";
  if (q.type === "single" || q.type === "multi") {
    body = `<div class="opts">${q.options.map((o) => {
      const on = a.selected.includes(o);
      return `<label class="opt ${on ? "on" : ""}"><input type="checkbox" data-opt value="${esc(o)}" ${on ? "checked" : ""}>${esc(o)}</label>`;
    }).join("")}</div>
    ${q.other_option ? `<div class="other">「${esc(q.other_option)}」の記述 <input type="text" data-other value="${esc(a.other_text)}"></div>` : ""}
    ${q.type === "single" ? `<div class="warn-line" data-multi-warn ${a.selected.length > 1 ? "" : "hidden"}>単一回答の設問に複数チェックがあります（集計では「複数回答（無効）」になります）</div>` : ""}`;
  } else if (q.type === "timegrid") {
    body = `<div class="table-wrap"><table class="grid-table"><tr><th></th>${q.hours.map((h) => `<th>${h}時</th>`).join("")}</tr>
      ${q.rows.map((row) => `<tr><th>${esc(row.label)}</th>${q.hours.map((h) => {
        const on = a[row.id].includes(h);
        return `<td class="${on ? "on" : ""}"><input type="checkbox" data-row="${row.id}" value="${h}" ${on ? "checked" : ""}></td>`;
      }).join("")}</tr>`).join("")}</table></div>`;
  } else if (q.type === "destinations") {
    body = `<table class="dest-table"><tr><th>種別</th><th>目的地の名称</th><th>最寄り停留所</th></tr>
      ${q.purposes.map((p) => {
        const d = a[p.id];
        return `<tr data-purpose="${p.id}">
          <td><label class="opt ${d.checked ? "on" : ""}"><input type="checkbox" data-checked ${d.checked ? "checked" : ""}>${esc(p.label)}</label></td>
          <td><input type="text" data-place value="${esc(d.place)}"></td>
          <td><input type="text" data-stop value="${esc(d.stop)}"></td></tr>`;
      }).join("")}</table>`;
  } else if (q.type === "trip") {
    body = `<div class="trip-grid">
      <span>目的地</span><input type="text" data-k="destination" value="${esc(a.destination)}">
      <span>行き：停留所を出発</span><input type="time" data-k="depart_time" value="${esc(a.depart_time)}">
      <span>帰り：停留所に到着</span><input type="time" data-k="return_time" value="${esc(a.return_time)}">
    </div>`;
  } else if (q.type === "short_text") {
    body = `<input type="text" data-text value="${esc(a.text)}">`;
  } else {
    body = `<textarea data-text>${esc(a.text)}</textarea>`;
  }
  return `<div class="q ${uncertain ? "uncertain" : ""}" data-q="${q.id}" data-type="${q.type}">${head}${body}</div>`;
}

function bindQuestionInputs(form) {
  form.addEventListener("change", (e) => {
    const label = e.target.closest(".opt");
    if (label) label.classList.toggle("on", e.target.checked);
    const td = e.target.closest(".grid-table td");
    if (td) td.classList.toggle("on", e.target.checked);
    const qEl = e.target.closest(".q");
    const warn = qEl && $("[data-multi-warn]", qEl);
    if (warn) warn.hidden = $$("[data-opt]:checked", qEl).length <= 1;
  });
  // 目的地・停留所を書いたら種別にもチェックを入れる
  form.addEventListener("input", (e) => {
    if (!e.target.matches("[data-place],[data-stop]") || !e.target.value) return;
    const cb = $("[data-checked]", e.target.closest("tr"));
    if (!cb.checked) { cb.checked = true; cb.closest(".opt").classList.add("on"); }
  });
}

function collectAnswers(form) {
  const answers = {};
  for (const qEl of $$(".q", form)) {
    const t = qEl.dataset.type;
    let a;
    if (t === "single" || t === "multi") {
      a = { selected: $$("[data-opt]:checked", qEl).map((i) => i.value), other_text: $("[data-other]", qEl)?.value || "" };
    } else if (t === "timegrid") {
      a = {};
      for (const cb of $$("[data-row]", qEl)) {
        a[cb.dataset.row] = a[cb.dataset.row] || [];
        if (cb.checked) a[cb.dataset.row].push(Number(cb.value));
      }
    } else if (t === "destinations") {
      a = {};
      for (const tr of $$("[data-purpose]", qEl)) {
        a[tr.dataset.purpose] = { checked: $("[data-checked]", tr).checked, place: $("[data-place]", tr).value, stop: $("[data-stop]", tr).value };
      }
    } else if (t === "trip") {
      a = {};
      for (const i of $$("[data-k]", qEl)) a[i.dataset.k] = i.value;
    } else {
      a = { text: $("[data-text]", qEl).value };
    }
    answers[qEl.dataset.q] = a;
  }
  return answers;
}

// ---------------------------------------------------------------- ③ 集計

function summaryQuery() {
  const p = new URLSearchParams({ scope: state.summary.scope });
  for (const [k, v] of Object.entries(state.summary.filters)) if (v) p.set(`f_${k}`, v);
  if (state.summary.cross) p.set("cross", state.summary.cross);
  return p.toString();
}

async function renderSummary(view) {
  const survey = state.survey;
  const attrQs = survey.questions.filter((q) => q.attribute);
  const crossable = survey.questions.filter((q) => q.type === "single");
  const data = await api(`/api/surveys/${state.surveyId}/summary?${summaryQuery()}`);
  const sf = state.summary;

  view.innerHTML = `
    <div class="row" style="margin-bottom:12px">
      <h2 style="margin:0">集計結果</h2>
      <span class="muted">集計対象 <strong style="font-size:18px;color:var(--text)">${data.total}</strong> 人</span>
      <span class="spacer"></span>
      <a class="btn primary" href="/api/surveys/${state.surveyId}/export.xlsx?${summaryQuery()}">Excelで出力</a>
      <a class="btn" href="/api/surveys/${state.surveyId}/export.csv?${summaryQuery()}">回答データCSV</a>
    </div>
    <div class="panel row" style="align-items:flex-end">
      <label class="field">集計対象
        <select id="scope">
          <option value="all" ${sf.scope === "all" ? "selected" : ""}>読み取り済すべて（未確認・要確認を含む）</option>
          <option value="confirmed" ${sf.scope === "confirmed" ? "selected" : ""}>確認済のみ</option>
        </select>
      </label>
      ${attrQs.map((q) => `
        <label class="field">${esc(q.label)}で絞り込み
          <select data-filter="${q.id}">
            <option value="">すべて</option>
            ${q.options.map((o) => `<option ${sf.filters[q.id] === o ? "selected" : ""}>${esc(o)}</option>`).join("")}
            <option ${sf.filters[q.id] === "無回答" ? "selected" : ""}>無回答</option>
          </select>
        </label>`).join("")}
      <label class="field">クロス集計
        <select id="cross">
          <option value="">なし</option>
          ${crossable.map((q) => `<option value="${q.id}" ${sf.cross === q.id ? "selected" : ""}>${esc(q.no === q.label ? q.label : q.no + " " + q.label)}別</option>`).join("")}
        </select>
      </label>
    </div>
    ${data.total ? `<div class="summary-grid">${data.questions.map(renderSummaryCard).join("")}</div>`
      : `<div class="panel muted">集計対象の回答がありません。</div>`}`;

  $("#scope").addEventListener("change", (e) => { sf.scope = e.target.value; renderSummary(view); });
  $("#cross").addEventListener("change", (e) => { sf.cross = e.target.value; renderSummary(view); });
  $$("[data-filter]", view).forEach((s) => s.addEventListener("change", () => { sf.filters[s.dataset.filter] = s.value; renderSummary(view); }));
}

function bars(rows) {
  const max = Math.max(1, ...rows.map((r) => r.count));
  return `<div class="bars">${rows.map((r) => `
    <span class="lbl ${r.extra ? "extra" : ""}">${esc(r.label)}</span>
    <span class="bar-track"><span class="bar-fill ${r.extra ? "extra" : ""}" style="width:${(r.count / max) * 100}%; display:block"></span></span>
    <span class="val">${r.count}${r.pct !== undefined ? ` <small>(${r.pct}%)</small>` : ""}</span>`).join("")}</div>`;
}

function freqList(items, limit = 15) {
  if (!items.length) return `<p class="muted">記入なし</p>`;
  const shown = items.slice(0, limit);
  return `<ol class="freq">${shown.map((x) => `<li>${esc(x.label)}（${x.count}）</li>`).join("")}</ol>
    ${items.length > limit ? `<p class="muted">ほか ${items.length - limit} 件（Excel出力で全件確認できます）</p>` : ""}`;
}

function renderSummaryCard(q) {
  const head = (extra = "") => `<div class="card-head"><h3>${esc(q.no === q.label ? q.label : `${q.no}　${q.label}`)}</h3><span class="n">回答 ${q.answered} / ${q.total}人${extra}</span></div>`;
  if (q.type === "single" || q.type === "multi") {
    const rows = q.rows.filter((r) => !r.extra || r.count);
    let cross = "";
    if (q.cross) {
      const ct = q.cross;
      cross = `<div class="table-wrap"><table class="cross">
        <tr><th>${esc(ct.by_label)}</th><th>人数</th>${ct.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr>
        ${ct.rows.filter((r) => r.n).map((r) => `<tr><td>${esc(r.label)}</td><td>${r.n}</td>${r.counts.map((n, i) => `<td>${n}<small>${r.pcts[i]}%</small></td>`).join("")}</tr>`).join("")}
      </table></div>`;
    }
    return `<div class="card ${q.cross ? "wide" : ""}">${head(q.type === "multi" ? "・複数回答" : "")}${bars(rows)}
      ${q.other_texts.length ? `<div class="other-texts">その他の記述: ${q.other_texts.map(esc).join(" ／ ")}</div>` : ""}${cross}</div>`;
  }
  if (q.type === "timegrid") {
    const max = Math.max(1, ...q.rows.flatMap((r) => r.counts));
    return `<div class="card wide">${head()}<div class="table-wrap"><table class="heat">
      <tr><th></th>${q.hours.map((h) => `<th>${h}時</th>`).join("")}</tr>
      ${q.rows.map((r) => `<tr><th>${esc(r.label)}</th>${r.counts.map((n) => {
        const a = n / max;
        return `<td style="background:rgba(59,125,216,${(a * 0.85).toFixed(2)});color:${a > 0.55 ? "#fff" : "inherit"}">${n || ""}</td>`;
      }).join("")}</tr>`).join("")}
    </table></div><p class="muted">数字は〇をつけた人数</p></div>`;
  }
  if (q.type === "destinations") {
    return `<div class="card wide">${head()}${bars(q.purposes.map((p) => ({ label: p.label, count: p.count, pct: p.pct })))}
      <div class="summary-grid" style="margin-top:14px">
        ${q.purposes.filter((p) => p.count).map((p) => `<div><h3>${esc(p.label)}</h3>
          <div class="row" style="align-items:flex-start;gap:24px">
            <div><div class="muted">目的地</div>${freqList(p.places, 10)}</div>
            <div><div class="muted">最寄り停留所</div>${freqList(p.stops, 10)}</div>
          </div></div>`).join("")}
      </div></div>`;
  }
  if (q.type === "trip") {
    return `<div class="card wide">${head()}<div class="row" style="align-items:flex-start;gap:32px">
      <div style="flex:1;min-width:220px"><h3>目的地</h3>${freqList(q.destinations)}</div>
      <div style="flex:1;min-width:220px"><h3>行き：出発時刻</h3>${q.depart_hours.length ? bars(q.depart_hours) : `<p class="muted">記入なし</p>`}</div>
      <div style="flex:1;min-width:220px"><h3>帰り：到着時刻</h3>${q.return_hours.length ? bars(q.return_hours) : `<p class="muted">記入なし</p>`}</div>
    </div></div>`;
  }
  if (q.type === "short_text") {
    return `<div class="card">${head()}${freqList(q.values, 30)}</div>`;
  }
  return `<div class="card wide">${head()}${q.texts.length
    ? `<ul class="texts">${q.texts.map((t) => `<li><a class="id" href="#/review/${t.id}">No.${t.id}</a>${esc(t.text)}</li>`).join("")}</ul>`
    : `<p class="muted">記入なし</p>`}</div>`;
}

init().catch((e) => {
  $("#view").innerHTML = `<div class="panel error-box">起動に失敗しました: ${esc(e.message)}</div>`;
});
