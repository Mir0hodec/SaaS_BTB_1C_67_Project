"use strict";
(() => {
  const TOKEN = document.querySelector('meta[name="panel-token"]').content;
  const $ = (s, el = document) => el.querySelector(s);
  const page = $("#page");
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const nf = new Intl.NumberFormat("ru-RU");
  const money = (v) => v == null ? "—" : "$" + (v < 1 ? v.toFixed(3) : v.toFixed(2)).replace(".", ",");
  const plural = (n, a, b, c) => { const m = Math.abs(n) % 100, k = m % 10; return m > 10 && m < 20 ? c : k > 1 && k < 5 ? b : k === 1 ? a : c; };
  const secs = (s) => s == null ? "—" : s < 60 ? `${Math.round(s)} с` : s < 3600 ? `${Math.floor(s / 60)} мин ${Math.round(s % 60)} с` : `${Math.floor(s / 3600)} ч ${Math.floor(s % 3600 / 60)} мин`;
  const ago = (iso) => {
    if (!iso) return "—";
    const t = typeof iso === "number" ? iso * 1000 : Date.parse(iso);
    const d = (Date.now() - t) / 1000;
    if (d < 10) return "только что";
    if (d < 60) return `${Math.round(d)} с назад`;
    if (d < 3600) return `${Math.round(d / 60)} мин назад`;
    const dt = new Date(t), today = new Date();
    const hm = dt.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
    if (dt.toDateString() === today.toDateString()) return `сегодня в ${hm}`;
    const y = new Date(); y.setDate(y.getDate() - 1);
    if (dt.toDateString() === y.toDateString()) return `вчера в ${hm}`;
    return dt.toLocaleDateString("ru-RU", { day: "numeric", month: "short" }) + ` в ${hm}`;
  };
  const when = (iso) => iso ? new Date(iso).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";
  const COLORS = ["#5b4fe9", "#0ea5e9", "#12a150", "#d97706", "#db2777", "#7c3aed", "#0d9488", "#dc2626"];
  const avatar = (name, id) => {
    const parts = String(name || "?").trim().split(/\s+/);
    const ini = ((parts[0] || "?")[0] + ((parts[1] || "")[0] || "")).toUpperCase();
    let h = 0; for (const ch of String(id || name)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    return `<div class="avatar" style="background:${COLORS[h % COLORS.length]}">${esc(ini)}</div>`;
  };
  const ICON = {
    msg: '<svg viewBox="0 0 24 24"><path d="M4 5h16v11H9l-5 4z"/></svg>',
    clock: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>',
    coin: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M14.5 9.2c-.5-.8-1.4-1.2-2.5-1.2-1.5 0-2.6.8-2.6 2s1.1 1.6 2.6 2 2.6.9 2.6 2.1-1.1 2-2.6 2c-1.2 0-2.1-.5-2.6-1.3M12 6.5V8M12 16v1.5"/></svg>',
    users: '<svg viewBox="0 0 24 24"><circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.6 3.4-5.5 6.5-5.5s5.7 1.9 6.5 5.5"/><circle cx="17" cy="9" r="2.6"/><path d="M16 14.6c2.6-.2 4.8 1.3 5.5 4.4"/></svg>',
    search: '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/></svg>',
    plus: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
    x: '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    refresh: '<svg viewBox="0 0 24 24"><path d="M20 11a8 8 0 00-14.6-4.3L4 8.5M4 4v4.5h4.5M4 13a8 8 0 0014.6 4.3L20 15.5M20 20v-4.5h-4.5"/></svg>',
    inbox: '<svg viewBox="0 0 24 24"><path d="M3 13l3-8h12l3 8v6H3z"/><path d="M3 13h5l1.5 2.5h5L16 13h5"/></svg>',
    play: '<svg viewBox="0 0 24 24"><path d="M7 5l12 7-12 7z"/></svg>',
    pause: '<svg viewBox="0 0 24 24"><path d="M8 5v14M16 5v14"/></svg>',
  };

  async function api(path, { method = "GET", body } = {}) {
    const res = await fetch("/panel/api/" + path, {
      method, headers: { "X-Panel-Token": TOKEN, "Content-Type": "application/json" },
      body: body === undefined ? (method === "GET" ? undefined : "{}") : JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `ошибка ${res.status}`);
    return data;
  }
  async function waitJob(job, onTick) {
    for (;;) {
      if (job.state !== "running") break;
      await new Promise((r) => setTimeout(r, 1200));
      job = await api("jobs/" + job.id);
      onTick && onTick(job);
    }
    if (job.state === "error") throw new Error(job.error);
    return job.result;
  }

  function toast(text, bad = false) {
    const el = document.createElement("div");
    el.className = "toast" + (bad ? " bad" : "");
    el.textContent = text;
    $("#toasts").append(el);
    setTimeout(() => el.remove(), bad ? 7000 : 3500);
  }
  function confirmBox(title, text, okText = "Да", danger = false) {
    return new Promise((resolve) => {
      const m = $("#modal");
      $("#modal-title").textContent = title;
      $("#modal-text").textContent = text;
      const ok = $("#modal-ok");
      ok.textContent = okText;
      ok.className = "btn " + (danger ? "danger" : "primary");
      m.hidden = false;
      const done = (v) => { m.hidden = true; ok.onclick = null; $("#modal-cancel").onclick = null; resolve(v); };
      ok.onclick = () => done(true);
      $("#modal-cancel").onclick = () => done(false);
      m.querySelector(".modal-back").onclick = () => done(false);
    });
  }
  function drawer(html) {
    $("#drawer-body").innerHTML = html;
    $("#drawer").hidden = false;
  }
  document.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) $("#drawer").hidden = true; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { $("#drawer").hidden = true; $("#modal").hidden = true; } });

  // --- тема ---
  const savedTheme = (() => { try { return localStorage.getItem("panel-theme"); } catch { return null; } })();
  const setTheme = (t) => { document.documentElement.dataset.theme = t; try { localStorage.setItem("panel-theme", t); } catch {} };
  setTheme(savedTheme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
  $("#theme").onclick = () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");

  // --- шапка: состояние и пауза ---
  let last = null;
  function header(o) {
    last = o;
    const pill = $("#state-pill");
    let cls = "ok", text = "Работает";
    const r = o.receiver;
    const bad = r.receive === "history" && r.targets.some((t) => t.state === "access_denied" || t.state === "error");
    if (o.paused) { cls = "warn"; text = "На паузе"; }
    else if (o.limit_until) { cls = "warn"; text = "Лимит подписки до " + new Date(o.limit_until).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }); }
    else if (r.receive === "history" && !r.running) { cls = "bad"; text = "Приём не запущен"; }
    else if (bad) { cls = "warn"; text = "Работает, есть предупреждения"; }
    pill.className = "pill " + cls;
    pill.lastElementChild.textContent = text;
    const b = $("#pause-btn");
    b.innerHTML = o.paused ? ICON.play + "Включить" : ICON.pause + "Пауза";
    b.className = "btn " + (o.paused ? "primary" : "");
    const v = o.version || {};
    $("#version").innerHTML = v.commit ? `Версия ${esc(v.commit)} · ${esc(when(v.date))}<br>Работает ${esc(secs(o.uptime))}` : `Работает ${esc(secs(o.uptime))}`;
  }
  $("#pause-btn").onclick = async () => {
    if (!last) return;
    const pausing = !last.paused;
    if (pausing && !(await confirmBox("Поставить бота на паузу?", "Сотрудники будут получать ответ «Помощник временно выключен», пока вы его не включите.", "На паузу"))) return;
    try {
      await api("pause", { method: "POST", body: { paused: pausing } });
      toast(pausing ? "Бот на паузе" : "Бот снова отвечает");
      refreshHeader();
      if (current === "overview") render();
    } catch (e) { toast(e.message, true); }
  };
  async function refreshHeader() { try { header(await api("overview")); } catch {} }

  // --- обзор ---
  const STATE = {
    ok: ["ok", "читается"], waiting: ["", "ждёт очереди"], limit: ["warn", "лимит API"],
    access_denied: ["bad", "нет доступа"], error: ["bad", "ошибка"],
  };
  const STATUS = {
    ok: ["ok", "Ответил"], error: ["bad", "Ошибка"], send_error: ["bad", "Не доставлено"], limit: ["warn", "Лимит"],
    denied: ["", "Нет доступа к базе"], unknown_user: ["", "Не в списке"],
  };
  const chip = (map, key) => { const [c, t] = map[key] || ["", key]; return `<span class="chip ${c}">${esc(t)}</span>`; };

  function chart(series) {
    const W = 640, H = 190, pad = { l: 28, r: 6, t: 10, b: 24 };
    const max = Math.max(4, ...series.map((d) => d.ok + d.error + d.limit));
    const step = (W - pad.l - pad.r) / series.length, bw = Math.min(26, step * 0.62);
    const y = (v) => H - pad.b - (v / max) * (H - pad.t - pad.b);
    let g = "";
    for (let i = 0; i <= 4; i++) {
      const v = Math.round((max / 4) * i), yy = y(v);
      g += `<line class="grid-line" x1="${pad.l}" x2="${W - pad.r}" y1="${yy}" y2="${yy}"/><text class="axis" x="${pad.l - 6}" y="${yy + 3.5}" text-anchor="end">${v}</text>`;
    }
    series.forEach((d, i) => {
      const x = pad.l + i * step + (step - bw) / 2;
      let top = H - pad.b;
      const seg = (n, cls) => { if (!n) return ""; const h = (n / max) * (H - pad.t - pad.b); top -= h; return `<rect class="${cls}" x="${x}" y="${top}" width="${bw}" height="${h}" rx="3"/>`; };
      g += seg(d.ok, "bar-ok") + seg(d.error, "bar-err") + seg(d.limit, "bar-lim");
      const dt = new Date(d.date + "T00:00:00");
      if (i % 2 === series.length % 2 || series.length < 9) g += `<text class="axis" x="${x + bw / 2}" y="${H - 7}" text-anchor="middle">${dt.getDate()}.${String(dt.getMonth() + 1).padStart(2, "0")}</text>`;
      const tip = `${dt.toLocaleDateString("ru-RU", { day: "numeric", month: "long" })}: ответов ${d.ok}, ошибок ${d.error}, лимит ${d.limit}, ${money(d.cost)}`;
      g += `<rect class="hit" x="${pad.l + i * step}" y="${pad.t}" width="${step}" height="${H - pad.t - pad.b}"><title>${esc(tip)}</title></rect>`;
    });
    return `<div class="chart"><svg viewBox="0 0 ${W} ${H}">${g}</svg></div>
      <div class="legend"><span><i style="background:var(--accent)"></i>ответил</span><span><i style="background:var(--bad)"></i>ошибка</span><span><i style="background:var(--warn)"></i>лимит подписки</span></div>`;
  }

  function journalRows(rows, compact = false) {
    if (!rows.length) return `<div class="empty">${ICON.inbox}Вопросов пока не было</div>`;
    return `<div class="table-wrap"><table><thead><tr><th>Когда</th><th>Сотрудник</th><th>Вопрос</th><th>Статус</th>${compact ? "" : '<th class="right">Время</th><th class="right">Цена</th>'}</tr></thead><tbody>
      ${rows.map((r) => `<tr data-id="${r.id}"><td class="muted num" style="white-space:nowrap">${esc(ago(r.created_at))}</td>
        <td><div class="row">${avatar(r.user_name, r.user_key)}<span class="ellipsis" style="max-width:180px">${esc(r.user_name)}</span></div></td>
        <td class="q"><div class="ellipsis">${esc(r.question || "—")}</div></td><td>${chip(STATUS, r.status)}</td>
        ${compact ? "" : `<td class="right num muted">${r.duration_ms ? secs(r.duration_ms / 1000) : "—"}</td><td class="right num muted">${money(r.cost_usd)}</td>`}</tr>`).join("")}
      </tbody></table></div>`;
  }
  function bindRows(root) {
    root.querySelectorAll("tr[data-id]").forEach((tr) => tr.onclick = () => openEntry(tr.dataset.id));
  }
  async function openEntry(id) {
    try {
      const r = await api("journal/" + id);
      drawer(`<div class="row">${avatar(r.user_name, r.user_key)}<div><h3>${esc(r.user_name)}</h3><div class="muted">${esc(new Date(r.created_at).toLocaleString("ru-RU"))}</div></div></div>
        <div class="row" style="gap:8px;margin-top:14px;flex-wrap:wrap">${chip(STATUS, r.status)}
          ${r.duration_ms ? `<span class="chip">${ICON.clock}${esc(secs(r.duration_ms / 1000))}</span>` : ""}
          ${r.cost_usd != null ? `<span class="chip">${esc(money(r.cost_usd))}</span>` : ""}
          ${r.base_id ? `<span class="chip info">база ${esc(r.base_id)}</span>` : ""}
          ${r.files ? `<span class="chip accent">файлов: ${r.files}</span>` : ""}</div>
        <div class="section-label">Вопрос</div><div class="bubble q">${esc(r.question || "—")}</div>
        ${r.answer ? `<div class="section-label">Ответ бота</div><div class="bubble a">${esc(r.answer)}</div>` :
          r.status === "ok" ? `<div class="section-label">Ответ бота</div><div class="muted" style="margin-top:8px">Текст ответа не сохранён (запись до обновления панели), длина — ${nf.format(r.answer_chars || 0)} символов.</div>` : ""}
        ${r.error ? `<div class="section-label">Ошибка</div><div class="bubble err">${esc(r.error)}</div>` : ""}`);
    } catch (e) { toast(e.message, true); }
  }

  async function overview() {
    const o = await api("overview");
    header(o);
    const s = o.stats, r = o.receiver;
    const t = s.today, w = s.week;
    const hist = r.receive === "history";
    const calls = r.calls_last_hour, perHour = r.interval ? Math.round(3600 / r.interval) : 0;
    const meterCls = r.limit_hits_last_hour ? "bad" : calls > 80 ? "warn" : "";
    const nowCard = o.current
      ? `<div class="now-busy"><div class="spinner"></div><div class="grow"><div><b>${esc(o.current.user_name)}</b> <span class="muted">· ${esc(secs(o.current.seconds))}</span></div><div class="muted ellipsis">«${esc(o.current.text)}»</div></div></div>`
      : `<div class="row">${avatarIdle()}<div><b>${o.paused ? "Бот на паузе" : "Свободен"}</b><div class="muted">${o.paused ? "Новые вопросы получат ответ «временно выключен»" : "Ждёт новых сообщений"}</div></div></div>`;
    page.innerHTML = `
      <div class="grid g4">
        ${kpi("Вопросов сегодня", nf.format(t.questions), t.questions ? `${t.ok_rate}% без ошибок · ${t.users} ${plural(t.users, "сотрудник", "сотрудника", "сотрудников")}` : "сегодня ещё не спрашивали", ICON.msg)}
        ${kpi("Время ответа", t.median_seconds != null ? secs(t.median_seconds) : w.median_seconds != null ? secs(w.median_seconds) : "—", "медиана " + (t.median_seconds != null ? "за сегодня" : "за 7 дней"), ICON.clock)}
        ${kpi("Расход сегодня", money(t.cost), "в пересчёте на цены API", ICON.coin)}
        ${kpi("За 7 дней", nf.format(w.questions) + " " + plural(w.questions, "вопрос", "вопроса", "вопросов"), `${money(w.cost)} · лимит подписки: ${w.limits}`, ICON.users)}
      </div>
      <div class="grid g32">
        <div class="card"><h2>Вопросы за 14 дней <span class="hint">наведите на столбец</span></h2>${chart(s.series)}</div>
        <div class="card"><h2>Сейчас</h2>${nowCard}
          ${o.queue ? `<div class="hint" style="margin-top:10px">В очереди: ${o.queue}</div>` : ""}
          <dl class="facts" style="margin-top:18px">
            <dt>Модель</dt><dd>${esc(o.model_name)} · ${esc(effortName(o.effort))}</dd>
            <dt>База знаний</dt><dd>${o.kb.documents ? `${nf.format(o.kb.documents)} ${plural(o.kb.documents, "документ", "документа", "документов")}` : '<span style="color:var(--warn)">не собрана</span>'}</dd>
            <dt>Контрагенты</dt><dd>${o.counterparty ? "проверка включена" : '<span class="muted">не настроена</span>'}</dd>
            <dt>Поиск по коду</dt><dd>${o.onec_code ? "включён" : '<span class="muted">не настроен</span>'}</dd>
          </dl></div>
      </div>
      <div class="grid g32">
        <div class="card"><h2>Последние вопросы <a class="hint" href="#journal">весь журнал →</a></h2><div id="recent">${journalRows(o.recent, true)}</div></div>
        <div class="card"><h2>Приём сообщений ${hist ? `<span class="hint">${r.next_poll_in != null ? "следующая проверка через " + secs(r.next_poll_in) : ""}</span>` : ""}</h2>
          ${hist ? receiverCard(r, calls, perHour, meterCls) : `<div class="muted">Режим: ${esc(r.channel)} / ${esc(r.receive)}</div>`}</div>
      </div>`;
    bindRows($("#recent"));
  }
  const avatarIdle = () => `<div class="avatar" style="background:var(--ok-soft);color:var(--ok)"><svg viewBox="0 0 24 24"><path d="M5 12.5l4.2 4L19 7"/></svg></div>`;
  const kpi = (label, value, sub, icon) => `<div class="card kpi"><div class="glyph">${icon}</div><div class="label">${esc(label)}</div><div class="value num">${esc(value)}</div><div class="sub">${esc(sub)}</div></div>`;
  const effortName = (e) => ({ low: "быстро", medium: "средняя глубина", high: "глубокие рассуждения", max: "максимальная глубина" }[e] || e);

  function receiverCard(r, calls, perHour, meterCls) {
    if (!r.credentials) return `<div class="check-res bad">Не заданы логин и пароль API 1С-Коннекта — бот не может читать сообщения.</div>`;
    const people = r.targets.length ? r.targets.map((t) => `<div class="item row">${avatar(t.name, t.colleague_id)}<div class="grow"><div class="ellipsis"><b>${esc(t.name)}</b></div>
        <div class="hint ellipsis">${t.last_poll_at ? "проверено " + esc(ago(t.last_poll_at)) : "ещё не проверялся"}${t.state === "access_denied" ? " · нужно открыть доступ к переписке в 1С-Коннекте" : t.error && t.state !== "ok" ? " · " + esc(t.error.slice(0, 80)) : ""}</div></div>
        ${chip(STATE, t.state)}</div>`).join("")
      : `<div class="empty">${ICON.users}Бот ещё никого не слушает.<br><a href="#people">Добавить сотрудников →</a></div>`;
    return `<div class="list">${people}</div>
      <div style="margin-top:16px"><div class="row" style="justify-content:space-between;margin-bottom:6px"><span class="hint">Запросов истории за час: <b class="num">${calls}</b> из 100 на учётку API</span><span class="hint">~${perHour}/ч у этого бота</span></div>
      <div class="meter ${meterCls}"><div style="width:${Math.min(100, calls)}%"></div></div>
      ${r.limit_hits_last_hour ? `<div class="hint" style="color:var(--bad);margin-top:6px">1С-Коннект ${r.limit_hits_last_hour} ${plural(r.limit_hits_last_hour, "раз", "раза", "раз")} ответил «лимит исчерпан» за последний час</div>` : ""}</div>`;
  }

  // --- сотрудники ---
  async function people() {
    const data = await api("colleagues");
    const n = data.colleagues.length;
    const minutes = n ? Math.ceil((data.interval * n) / 60) : 0;
    page.innerHTML = `
      <div class="grid g2">
        <div class="card"><h2>Кому отвечает бот <span class="hint">${n ? `ответ придёт в течение ~${minutes} мин` : ""}</span></h2>
          <div class="list" id="chosen">${n ? data.colleagues.map(personRow).join("") : `<div class="empty">${ICON.users}Пока никого нет — найдите сотрудника справа и нажмите «Добавить»</div>`}</div>
          <div class="hint" style="margin-top:14px">Бот читает переписку с каждым по очереди раз в ${data.interval} с. Чем больше людей, тем дольше ждать ответа: 1С-Коннект даёт 100 запросов истории в час на всех. Сообщения, отправленные до добавления, бот пропустит.</div>
        </div>
        <div class="card"><h2>Добавить сотрудника <button class="btn sm ghost" id="dir-refresh" style="margin-left:auto">${ICON.refresh}Обновить список</button></h2>
          <div class="search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/></svg><input type="search" id="dir-q" placeholder="Фамилия, имя, должность или почта" autocomplete="off"></div>
          <div class="list" id="dir" style="margin-top:12px"><div class="empty">Загрузка справочника…</div></div>
          <div class="hint" id="dir-info" style="margin-top:10px"></div>
        </div>
      </div>`;
    page.querySelectorAll("[data-remove]").forEach((b) => b.onclick = () => removePerson(b.dataset.remove, b.dataset.name));
    let timer;
    $("#dir-q").oninput = () => { clearTimeout(timer); timer = setTimeout(() => loadDir(), 200); };
    $("#dir-refresh").onclick = () => loadDir(true);
    loadDir();
  }
  function personRow(p) {
    const st = p.status;
    return `<div class="item row">${avatar(p.name, p.id)}<div class="grow"><div class="ellipsis"><b>${esc(p.name)}</b></div>
      <div class="hint ellipsis">${esc(p.post || p.email || (p.known ? "" : "нет в справочнике 1С-Коннекта"))}${st && st.last_message_at ? " · писал(а) " + esc(when(st.last_message_at)) : ""}</div></div>
      ${p.denied ? '<span class="chip bad">в списке запрета</span>' : st ? chip(STATE, st.state) : ""}
      <button class="icon-btn" title="Убрать" data-remove="${esc(p.id)}" data-name="${esc(p.name)}">${ICON.x}</button></div>`;
  }
  async function loadDir(refresh = false) {
    const q = $("#dir-q").value;
    const list = $("#dir");
    if (refresh) list.innerHTML = `<div class="empty">Загружаю из 1С-Коннекта…</div>`;
    try {
      const d = await api(`directory?q=${encodeURIComponent(q)}&refresh=${refresh ? 1 : 0}`);
      list.innerHTML = d.people.length ? d.people.map((p) => `<div class="item row">${avatar(p.name, p.id)}<div class="grow"><div class="ellipsis"><b>${esc(p.name)}</b></div><div class="hint ellipsis">${esc([p.post, p.email].filter(Boolean).join(" · "))}</div></div>
        ${p.added ? '<span class="chip ok">уже слушает</span>' : `<button class="btn sm primary" data-add="${esc(p.id)}" data-name="${esc(p.name)}">${ICON.plus}Добавить</button>`}</div>`).join("")
        : `<div class="empty">${ICON.search}${d.loaded_at ? "Никого не нашлось" : "Справочник не загрузился — проверьте логин API на вкладке «Проверки»"}</div>`;
      $("#dir-info").textContent = d.loaded_at ? `Найдено: ${d.total}${d.total > d.people.length ? ", показаны первые " + d.people.length : ""}. Справочник обновлён ${ago(d.loaded_at)}.` : "";
      list.querySelectorAll("[data-add]").forEach((b) => b.onclick = () => addPerson(b.dataset.add, b.dataset.name, b));
      if (refresh) toast("Справочник обновлён");
    } catch (e) { list.innerHTML = `<div class="check-res bad">${esc(e.message)}</div>`; }
  }
  async function addPerson(id, name, btn) {
    btn.disabled = true;
    try {
      const r = await api("colleagues", { method: "POST", body: { id } });
      toast(`${name}: бот начнёт читать переписку при следующей проверке` + (r.notes.length ? " (" + r.notes.join("; ") + ")" : ""));
      people();
    } catch (e) { toast(e.message, true); btn.disabled = false; }
  }
  async function removePerson(id, name) {
    if (!(await confirmBox("Убрать сотрудника?", `Бот перестанет читать переписку с «${name}» и отвечать ему.`, "Убрать", true))) return;
    try { await api("colleagues/" + id, { method: "DELETE" }); toast(`${name} убран(а)`); people(); }
    catch (e) { toast(e.message, true); }
  }

  // --- журнал ---
  const jstate = { status: "", user: "", q: "", rows: [], more: false };
  async function journal(append = false) {
    const before = append && jstate.rows.length ? jstate.rows[jstate.rows.length - 1].id : 0;
    const d = await api(`journal?status=${jstate.status}&user=${encodeURIComponent(jstate.user)}&q=${encodeURIComponent(jstate.q)}&before=${before}`);
    jstate.rows = append ? [...jstate.rows, ...d.rows] : d.rows;
    jstate.more = d.more;
    if (!append) {
      page.innerHTML = `<div class="card">
        <div class="toolbar">
          <div class="seg" id="j-status">${[["", "Все"], ["ok", "Ответил"], ["error", "Ошибки"], ["limit", "Лимит"], ["other", "Прочее"]].map(([k, t]) => `<button data-k="${k}" class="${jstate.status === k ? "on" : ""}">${t}</button>`).join("")}</div>
          <select id="j-user"><option value="">Все сотрудники</option>${d.users.map((u) => `<option value="${esc(u.id)}" ${jstate.user === u.id ? "selected" : ""}>${esc(u.name)}</option>`).join("")}</select>
          <div class="search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/></svg><input type="search" id="j-q" placeholder="Поиск по вопросам и ответам" value="${esc(jstate.q)}"></div>
        </div><div id="j-table"></div><div id="j-more" style="text-align:center;margin-top:14px"></div></div>`;
      $("#j-status").querySelectorAll("button").forEach((b) => b.onclick = () => { jstate.status = b.dataset.k; journal(); });
      $("#j-user").onchange = (e) => { jstate.user = e.target.value; journal(); };
      let t; $("#j-q").oninput = (e) => { clearTimeout(t); t = setTimeout(() => { jstate.q = e.target.value; journal(); }, 300); };
    }
    $("#j-table").innerHTML = journalRows(jstate.rows);
    bindRows($("#j-table"));
    $("#j-more").innerHTML = jstate.more ? `<button class="btn" id="j-more-btn">Показать ещё</button>` : "";
    if (jstate.more) $("#j-more-btn").onclick = () => journal(true);
  }

  // --- проверки ---
  const CHECKS = [
    { k: "claude", title: "Claude Code", text: "Запускает один короткий вопрос с настройками бота: версия CLI, вход в подписку, только нужные инструменты. Тратит один небольшой запрос подписки." },
    { k: "connect", title: "API 1С-Коннекта", text: "Проверяет логин и пароль API и заново загружает справочник сотрудников." },
    { k: "counterparty", title: "Проверка контрагента", text: "Запрос в сервис 1С CounterpartyCheck по БИН/ИИН — долг и неподписанные ЭАВР.", input: true },
    { k: "kb", title: "База знаний", text: "Полная переиндексация папки «База знаний». Обычно бот делает это сам, когда файлы меняются." },
  ];
  async function checks() {
    const o = await api("overview");
    header(o);
    page.innerHTML = `<div class="grid g2">${CHECKS.map((c) => `<div class="card"><h2>${esc(c.title)}</h2><div class="hint">${esc(c.text)}</div>
      ${c.k === "kb" ? `<div class="hint" style="margin-top:8px">Сейчас: <b>${nf.format(o.kb.documents)}</b> ${plural(o.kb.documents, "документ", "документа", "документов")}${o.kb.built_at ? ", собрана " + esc(ago(o.kb.built_at)) : ""}${o.kb.root_exists ? "" : " · <span style='color:var(--bad)'>папка не найдена</span>"}</div>` : ""}
      <div class="row" style="margin-top:14px">${c.input ? `<input type="text" id="bin" placeholder="БИН или ИИН, 12 цифр" inputmode="numeric" maxlength="14" style="width:200px">` : ""}
      <button class="btn primary" data-check="${c.k}">${c.k === "kb" ? ICON.refresh + "Переиндексировать" : "Проверить"}</button></div>
      <div id="res-${c.k}">${o.checks[c.k] ? checkResult(c.k, o.checks[c.k].result, o.checks[c.k].at) : ""}</div></div>`).join("")}</div>`;
    page.querySelectorAll("[data-check]").forEach((b) => b.onclick = () => runCheck(b.dataset.check, b));
  }
  async function runCheck(k, btn) {
    const body = k === "counterparty" ? { bin: $("#bin").value } : {};
    const box = $("#res-" + k);
    btn.disabled = true;
    const t0 = Date.now();
    box.innerHTML = `<div class="check-res"><div class="row"><div class="spinner" style="width:18px;height:18px;border-width:2px"></div><span id="tick-${k}">Выполняется…</span></div></div>`;
    try {
      const job = await api("check/" + k, { method: "POST", body });
      const res = await waitJob(job, () => { const el = $("#tick-" + k); if (el) el.textContent = `Выполняется… ${Math.round((Date.now() - t0) / 1000)} с`; });
      box.innerHTML = checkResult(k, res, new Date().toISOString());
    } catch (e) { box.innerHTML = `<div class="check-res bad"><b>Не выполнено.</b> ${esc(e.message)}</div>`; }
    btn.disabled = false;
  }
  function checkResult(k, r, at) {
    if (!r) return "";
    const stamp = `<span class="hint" style="float:right">${esc(ago(at))}</span>`;
    if (k === "claude") return `<div class="check-res ${r.ok ? "ok" : "bad"}">${stamp}<b>${r.ok ? "Claude на связи" : r.limit ? "Лимит подписки исчерпан" : "Ошибка"}</b><div class="hint">${esc(r.version)}</div>
      ${r.answer ? `<div style="margin-top:6px">«${esc(r.answer)}»</div>` : ""}${r.error ? `<pre>${esc(r.error)}</pre>` : ""}
      ${r.tools ? `<div class="hint" style="margin-top:6px">Инструментов в сессии: ${r.tools.length}</div>` : ""}</div>`;
    if (k === "connect") return `<div class="check-res ${r.ok ? "ok" : "bad"}">${stamp}<b>${r.ok ? "API отвечает" : "API не отвечает"}</b>${r.http ? ` · HTTP ${r.http}` : ""}
      ${r.specialists != null ? `<div>Специалистов линий: ${r.specialists}</div>` : ""}${r.directory != null ? `<div>Справочник: ${r.directory} сотрудников</div>` : ""}
      ${r.directory_error ? `<div>Справочник: ${esc(r.directory_error)}</div>` : ""}${r.error ? `<pre>${esc(r.error)}</pre>` : ""}</div>`;
    if (k === "counterparty") return `<div class="check-res ${r.status === "ok" ? "ok" : r.status === "needs_manager" ? "" : "bad"}">${stamp}<b>${esc(r.name || "Контрагент")}</b><div>${esc(r.summary)}</div>${r.error ? `<pre>${esc(r.error)}</pre>` : ""}</div>`;
    if (k === "kb") return `<div class="check-res ok">${stamp}<b>Собрано: ${r.indexed} ${plural(r.indexed, "документ", "документа", "документов")}</b>
      <div class="hint">дублей пропущено: ${r.duplicates}, исключено правилами: ${r.excluded}</div>
      ${r.unsupported && r.unsupported.length ? `<div class="hint">не читаются: ${esc(r.unsupported.join(", "))}</div>` : ""}
      ${r.failed && r.failed.length ? `<pre>${esc(r.failed.join("\n"))}</pre>` : ""}</div>`;
    return "";
  }

  // --- настройки ---
  const MODEL_HINT = {
    "claude-opus-5-5": "самая умная, расходует лимит быстрее всех",
    "claude-sonnet-5-5": "умная и в несколько раз экономнее",
    "claude-haiku-5-5": "самая быстрая и дешёвая",
  };
  const EFFORT = { low: ["Низкая", "быстрые ответы"], medium: ["Средняя", "баланс"], high: ["Высокая", "думает тщательнее"], max: ["Максимальная", "медленнее и дороже"] };
  async function settingsPage() {
    const s = await api("settings");
    const form = { model: s.model, effort: s.effort, interval: s.interval, ack_text: s.ack_text };
    const v = s.version || {};
    const draw = () => {
      const perHour = Math.round(3600 / form.interval);
      const wait = s.colleagues ? Math.ceil((form.interval * s.colleagues) / 60) : 0;
      page.innerHTML = `
        <div class="card"><h2>Модель Claude</h2>
          <div class="choice">${s.models.map((m) => `<button class="opt ${form.model === m.id ? "on" : ""}" data-model="${m.id}"><b>${esc(m.name)}</b><span>${esc(MODEL_HINT[m.id] || "")}</span></button>`).join("")}</div>
          <div class="field" style="margin-top:20px"><label>Глубина рассуждений</label>
          <div class="choice four">${s.efforts.map((e) => `<button class="opt ${form.effort === e ? "on" : ""}" data-effort="${e}"><b>${EFFORT[e][0]}</b><span>${EFFORT[e][1]}</span></button>`).join("")}</div></div>
        </div>
        <div class="card"><h2>Сообщения</h2>
          <div class="field"><label for="interval">Как часто проверять переписку</label>
            <div class="range"><input type="range" id="interval-r" min="30" max="300" step="5" value="${Math.min(300, form.interval)}"><input type="number" id="interval" min="20" max="3600" value="${form.interval}"><span class="muted">сек</span></div>
            <div class="hint">≈ ${perHour} запросов истории в час у этого бота${s.colleagues ? ` · при ${s.colleagues} ${plural(s.colleagues, "сотруднике", "сотрудниках", "сотрудниках")} ответ придёт в течение ~${wait} мин` : ""}. Лимит 1С-Коннекта — 100 в час на учётку API, его делят все, кто ею пользуется.</div></div>
          <div class="field"><label for="ack">Сообщение «принял вопрос»</label>
            <input type="text" id="ack" value="${esc(form.ack_text)}" maxlength="300" placeholder="Пусто — не отправлять">
            <div class="hint">Бот сразу отправляет его сотруднику, пока готовит ответ.</div></div>
          <div class="savebar"><span class="hint" id="dirty"></span><button class="btn primary" id="save">Сохранить</button></div>
        </div>
        <div class="card"><h2>Обслуживание</h2>
          <dl class="facts"><dt>Версия</dt><dd>${v.commit ? `${esc(v.commit)} · ${esc(v.subject)} <span class="muted">(${esc(when(v.date))})</span>` : "неизвестна"}</dd></dl>
          <div class="row" style="margin-top:16px;flex-wrap:wrap">
            <button class="btn" id="update">${ICON.refresh}Обновить с GitHub</button>
            <button class="btn" id="restart" ${s.supervised ? "" : "disabled"}>Перезапустить бота</button>
            ${s.supervised ? "" : '<span class="hint">Перезапуск доступен, когда бот запущен через scripts\\start.py</span>'}
          </div><div id="update-res"></div>
        </div>`;
      page.querySelectorAll("[data-model]").forEach((b) => b.onclick = () => { form.model = b.dataset.model; draw(); });
      page.querySelectorAll("[data-effort]").forEach((b) => b.onclick = () => { form.effort = b.dataset.effort; draw(); });
      $("#interval-r").oninput = (e) => { form.interval = +e.target.value; $("#interval").value = form.interval; markDirty(); };
      $("#interval-r").onchange = () => draw();
      $("#interval").onchange = (e) => { form.interval = Math.max(20, Math.min(3600, +e.target.value || 75)); draw(); };
      $("#ack").oninput = (e) => { form.ack_text = e.target.value; markDirty(); };
      $("#save").onclick = save;
      $("#update").onclick = update;
      $("#restart").onclick = restart;
      markDirty();
    };
    const markDirty = () => {
      const dirty = form.model !== s.model || form.effort !== s.effort || +form.interval !== +s.interval || form.ack_text !== s.ack_text;
      $("#dirty").textContent = dirty ? "Есть несохранённые изменения" : "";
      $("#save").disabled = !dirty;
    };
    const save = async () => {
      try {
        const res = await api("settings", { method: "POST", body: form });
        Object.assign(s, res);
        toast("Сохранено — применится к следующему вопросу");
        draw(); refreshHeader();
      } catch (e) { toast(e.message, true); }
    };
    draw();
  }
  async function update() {
    const box = $("#update-res");
    if (!(await confirmBox("Обновить бота?", "Скачаю новую версию с GitHub. Если она есть — бот перезапустится (около 20 секунд), текущий ответ сотруднику может оборваться.", "Обновить"))) return;
    box.innerHTML = `<div class="check-res"><div class="row"><div class="spinner" style="width:18px;height:18px;border-width:2px"></div>Скачиваю обновление…</div></div>`;
    try {
      const r = await waitJob(await api("update", { method: "POST" }));
      if (!r.changed) { box.innerHTML = `<div class="check-res ok">Уже последняя версия.</div>`; return; }
      box.innerHTML = `<div class="check-res ok"><b>Загружено изменений: ${r.commits.length}</b><pre>${esc(r.commits.join("\n"))}</pre>${r.restarting ? "<div>Бот перезапускается…</div>" : "<div>Перезапустите бота, чтобы изменения заработали.</div>"}</div>`;
      if (r.restarting) waitBack();
    } catch (e) { box.innerHTML = `<div class="check-res bad"><b>Не обновилось.</b><pre>${esc(e.message)}</pre></div>`; }
  }
  async function restart() {
    const busy = last && last.current;
    if (!(await confirmBox("Перезапустить бота?", busy ? "Бот сейчас отвечает сотруднику — этот ответ оборвётся." : "Бот будет недоступен около 20 секунд.", "Перезапустить", !!busy))) return;
    try { await api("restart", { method: "POST" }); toast("Перезапуск…"); waitBack(); } catch (e) { toast(e.message, true); }
  }
  function waitBack() {
    const pill = $("#state-pill");
    pill.className = "pill warn"; pill.lastElementChild.textContent = "Перезапуск…";
    const started = Date.now();
    const tick = async () => {
      await new Promise((r) => setTimeout(r, 2500));
      try { const r = await fetch("/health", { cache: "no-store" }); if (r.ok && Date.now() - started > 4000) { location.reload(); return; } } catch {}
      if (Date.now() - started < 120000) tick(); else toast("Бот не поднялся за 2 минуты — посмотрите окно «Claude CLI bot» на сервере", true);
    };
    tick();
  }

  // --- логи ---
  const lstate = { file: "bridge", health: false, auto: true, filter: "" };
  let logTimer = null;
  async function logs() {
    page.innerHTML = `<div class="card">
      <div class="toolbar">
        <div class="seg" id="l-file"><button data-f="bridge" class="${lstate.file === "bridge" ? "on" : ""}">Бот</button><button data-f="start" class="${lstate.file === "start" ? "on" : ""}">Запуск</button></div>
        <div class="search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/></svg><input type="search" id="l-filter" placeholder="Фильтр строк" value="${esc(lstate.filter)}"></div>
        <label class="row hint" style="gap:6px"><input type="checkbox" id="l-auto" ${lstate.auto ? "checked" : ""}>обновлять</label>
        <label class="row hint" style="gap:6px"><input type="checkbox" id="l-health" ${lstate.health ? "checked" : ""}>служебные запросы</label>
      </div><div class="log" id="log"></div></div>`;
    $("#l-file").querySelectorAll("button").forEach((b) => b.onclick = () => { lstate.file = b.dataset.f; logs(); });
    $("#l-filter").oninput = (e) => { lstate.filter = e.target.value; loadLog(); };
    $("#l-auto").onchange = (e) => { lstate.auto = e.target.checked; };
    $("#l-health").onchange = (e) => { lstate.health = e.target.checked; loadLog(); };
    await loadLog(true);
  }
  async function loadLog(scroll = false) {
    const box = $("#log");
    if (!box) return;
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    const d = await api(`logs?file=${lstate.file}&lines=500&health=${lstate.health ? 1 : 0}`);
    const f = lstate.filter.toLowerCase();
    const lines = d.lines.filter((l) => !f || l.toLowerCase().includes(f));
    box.innerHTML = lines.length ? lines.map((l) => `<div class="${/ERROR|ОШИБКА|Traceback|exception/i.test(l) ? "e" : /WARNING/.test(l) ? "w" : /^INFO:|GET \/|POST \//.test(l) ? "d" : "i"}">${esc(l)}</div>`).join("") : '<div class="d">Пусто</div>';
    if (scroll || atBottom) box.scrollTop = box.scrollHeight;
  }

  // --- маршрутизация ---
  const PAGES = {
    overview: ["Обзор", "Состояние бота и статистика", overview],
    people: ["Сотрудники", "Кому бот отвечает в личных сообщениях 1С-Коннекта", people],
    journal: ["Журнал", "Все вопросы сотрудников и ответы бота", () => journal()],
    checks: ["Проверки", "Проверить связь с Claude, 1С-Коннектом, 1С и базой знаний", checks],
    settings: ["Настройки", "Изменения применяются сразу, перезапуск не нужен", settingsPage],
    logs: ["Логи", "Что бот пишет в журнал работы", logs],
  };
  let current = "overview";
  async function render() {
    const [title, sub, fn] = PAGES[current];
    $("#title").textContent = title;
    $("#subtitle").textContent = sub;
    document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === current));
    try { await fn(); }
    catch (e) { page.innerHTML = `<div class="card"><div class="check-res bad"><b>Не удалось загрузить.</b> ${esc(e.message)}</div></div>`; }
  }
  function route() {
    const p = location.hash.slice(1);
    current = PAGES[p] ? p : "overview";
    page.style.animation = "none"; void page.offsetWidth; page.style.animation = "";
    render();
  }
  window.addEventListener("hashchange", route);
  setInterval(() => {
    if (document.hidden) return;
    if (current === "overview") overview().catch(() => {});
    else if (current === "logs" && lstate.auto) loadLog().catch(() => {});
    else refreshHeader();
  }, 5000);
  route();
  if (current !== "overview") refreshHeader();
})();
