"use strict";

const PAGE_SIZE = 25;
const $ = (sel) => document.querySelector(sel);
const state = { page: 0, q: "", stock: "", lowStock: 2, settings: null };

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null && c !== false) el.append(c);
  return el;
}

/* ---------------------------------------------------------------- api */
class ApiError extends Error {
  constructor(message, status, detail) { super(message); this.status = status; this.detail = detail; }
}

async function api(path, { method = "GET", body, file, contentType } = {}) {
  const headers = { "X-Sklad": "1" };
  if (file) headers["Content-Type"] = contentType || file.type || "application/octet-stream";
  else if (body) headers["Content-Type"] = "application/json";
  const res = await fetch(path, {
    method,
    credentials: "same-origin",
    headers,
    body: file ?? (body ? JSON.stringify(body) : undefined),
  });
  if (res.status === 401) { lock(); throw new ApiError("Сессия истекла", 401); }
  if (!res.ok) {
    let message = res.statusText;
    let raw = null;
    try {
      raw = (await res.json()).detail;
      if (Array.isArray(raw)) message = raw.map((d) => d.msg).join("; ");
      else if (typeof raw === "string") message = raw;
    } catch { /* not json */ }
    throw new ApiError(message, res.status, raw);
  }
  return res.status === 204 ? null : res.json();
}

/* ---------------------------------------------------------------- ui bits */
let toastTimer;
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("err", err);
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2500);
}

function lock() {
  $("#app").hidden = true;
  $("#logout").hidden = true;
  $("#locked").hidden = false;
  if (new URLSearchParams(location.search).has("denied")) $("#denied").hidden = false;
}

/* ---------------------------------------------------------------- theme */
const THEMES = ["", "light", "dark"]; // "" = follow the OS
const THEME_ICON = { "": "🖥", light: "☀️", dark: "🌙" };
const THEME_TITLE = { "": "Тема: как в системе", light: "Тема: светлая", dark: "Тема: тёмная" };

function applyTheme(t) {
  if (t) document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  const b = $("#theme");
  b.textContent = THEME_ICON[t];
  b.title = b.ariaLabel = THEME_TITLE[t];
  try { t ? localStorage.setItem("theme", t) : localStorage.removeItem("theme"); } catch { /* private mode */ }
}

function cycleTheme() {
  const cur = document.documentElement.dataset.theme ?? "";
  applyTheme(THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length]);
}

/* ---------------------------------------------------------------- stats */
function setStock(stock) {
  state.stock = stock;
  state.page = 0;
  document.querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("on", b.dataset.stock === stock));
  loadProducts();
}

async function loadStats() {
  const s = await api("/api/stats");
  state.lowStock = s.low_stock;
  const card = (label, value, cls = "", stock = null) => stock === null
    ? h("div", { class: `stat ${cls}` }, h("b", {}, String(value)), h("span", {}, label))
    : h("button", { class: `stat ${cls}`, type: "button", onclick: () => setStock(stock) },
        h("b", {}, String(value)), h("span", {}, label));
  $("#stats").replaceChildren(
    card("позиций", s.positions, "", ""),
    card("единиц на складе", s.units),
    card("нет в наличии", s.depleted, s.depleted ? "danger" : "", "depleted"),
    card(`мало (≤${s.low_stock} шт.)`, s.low, s.low ? "warn" : "", "low"),
    card("с фото", s.with_photo),
    card("на Avito", s.on_avito),
  );
}

/* ---------------------------------------------------------------- registry */
function stockOf(p) {
  if (p.quantity === 0) return "depleted";
  if (p.quantity <= state.lowStock) return "low";
  return "ok";
}

function avitoLink(url) {
  if (!url) return h("span", { class: "muted" }, "—");
  let label = "ссылка";
  try { label = new URL(url).pathname.match(/(\d{6,})/)?.[1] ?? label; } catch { /* keep */ }
  return h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, label);
}

const photoUrl = (p) => `/api/products/${p.id}/photo?v=${p.photo_key}`;
const MAX_PHOTO = 10 * 1024 * 1024;

function productRow(p) {
  const out = h("output", {}, String(p.quantity));
  const tr = h("tr", { dataset: { id: p.id, stock: stockOf(p) } },
    h("td", {}, p.has_photo
      ? h("img", { class: "thumb", src: photoUrl(p), alt: "", loading: "lazy" })
      : h("div", { class: "thumb-none" }, "·")),
    h("td", { class: "name", title: "Открыть", onclick: () => openEdit(p) }, p.name, p.description ? h("small", {}, p.description) : null),
    h("td", {}, p.sku ?? h("span", { class: "muted" }, "—")),
    h("td", {}, avitoLink(p.avito_url)),
    h("td", { class: "num" },
      h("span", { class: "qty" },
        h("button", { class: "btn btn-icon", type: "button", "aria-label": "Минус один",
          onclick: () => adjust(p, -1, tr, out) }, "−"),
        out,
        h("button", { class: "btn btn-icon", type: "button", "aria-label": "Плюс один",
          onclick: () => adjust(p, +1, tr, out) }, "+"))),
    h("td", { class: "row-actions" },
      h("button", { class: "btn btn-ghost btn-icon", type: "button", title: "Изменить", onclick: () => openEdit(p) }, "✎"),
      h("button", { class: "btn btn-ghost btn-icon", type: "button", title: "Удалить", onclick: () => confirmDelete(p) }, "🗑")),
  );
  return tr;
}

function renderRows(items) {
  $("#rows").replaceChildren(...items.map(productRow));
  $("#empty").hidden = items.length > 0;
}

function setPager(total) {
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  $("#pageinfo").textContent = `${state.page + 1} / ${pages} · ${total} поз.`;
  $("#prev").disabled = state.page === 0;
  $("#next").disabled = state.page >= pages - 1;
  return pages;
}

const MATCHED = { avito: "Найдено по ссылке Avito", sku: "Точное совпадение по артикулу" };

async function loadProducts() {
  // Avito links can't be matched as substrings — use the bot's search cascade
  if (state.q.startsWith("http")) {
    const res = await api(`/api/search?q=${encodeURIComponent(state.q)}`);
    renderRows(res.items);
    $("#matched").textContent = MATCHED[res.matched_by] ?? "";
    $("#matched").hidden = !MATCHED[res.matched_by];
    setPager(res.items.length);
    return;
  }
  $("#matched").hidden = true;
  const params = new URLSearchParams({ page: state.page, size: PAGE_SIZE });
  if (state.q) params.set("q", state.q);
  if (state.stock) params.set("stock", state.stock);
  const res = await api(`/api/products?${params}`);
  const pages = setPager(res.total);
  if (state.page >= pages && state.page > 0) { state.page = pages - 1; return loadProducts(); }
  renderRows(res.items);
}

const refreshAll = () => Promise.all([loadProducts(), loadStats()]);

async function adjust(p, delta, tr, out) {
  if (p.quantity + delta < 0) return;
  try {
    const upd = await api(`/api/products/${p.id}/adjust`, { method: "POST", body: { delta } });
    p.quantity = upd.quantity;
    out.textContent = String(upd.quantity);
    tr.dataset.stock = stockOf(p);
    tr.classList.remove("flash"); void tr.offsetWidth; tr.classList.add("flash");
    loadStats();
  } catch (e) { toast(e.message, true); }
}

/* ---------------------------------------------------------------- dialogs */
let editing = null;

function openEdit(p = null) {
  editing = p;
  const f = $("#edit-form");
  f.reset();
  $("#edit-error").hidden = true;
  $("#edit-title").textContent = p ? "Редактирование" : "Новый товар";
  if (p) for (const k of ["name", "description", "sku", "quantity", "avito_url"]) f.elements[k].value = p[k] ?? "";
  const img = $("#edit-photo");
  img.hidden = !p?.has_photo;
  if (p?.has_photo) img.src = photoUrl(p); else img.removeAttribute("src");
  $("#photo-remove-wrap").hidden = !p?.has_photo;
  $("#history").hidden = !p;
  $("#edit-delete").hidden = !p;
  $("#history").open = false;
  $("#history-list").replaceChildren();
  if (p) loadHistory(p.id);
  $("#edit").showModal();
  f.elements.name.focus();
}

const SOURCE = { bot: "бот", web: "веб", import: "импорт" };
const fmtDate = (iso) => new Date(iso).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

async function loadHistory(id) {
  try {
    const items = await api(`/api/products/${id}/history`);
    $("#history-list").replaceChildren(...(items.length ? items.map((m) =>
      h("li", {},
        h("span", { class: "muted" }, fmtDate(m.created_at)),
        h("span", { class: m.delta > 0 ? "plus" : "minus" }, (m.delta > 0 ? "+" : "") + m.delta),
        h("span", {}, `→ ${m.quantity_after}`),
        h("span", { class: "muted" }, SOURCE[m.source] ?? m.source))) : [h("li", { class: "muted" }, "Изменений пока нет")]));
  } catch { /* history is optional */ }
}

async function submitEdit(ev) {
  if (ev.submitter?.value !== "ok") return;
  ev.preventDefault();
  const f = $("#edit-form");
  if (!f.reportValidity()) return;
  const body = Object.fromEntries(["name", "description", "sku", "avito_url"].map((k) => [k, f.elements[k].value]));
  body.quantity = Number(f.elements.quantity.value);
  const file = f.elements.photo.files[0];
  if (file && file.size > MAX_PHOTO) {
    $("#edit-error").textContent = "Фото больше 10 МБ";
    $("#edit-error").hidden = false;
    return;
  }
  const btn = ev.submitter;
  btn.disabled = true;
  try {
    const saved = editing
      ? await api(`/api/products/${editing.id}`, { method: "PATCH", body })
      : await api("/api/products", { method: "POST", body });
    editing = saved; // a retry after a failed upload must not create a duplicate
    if (file) await api(`/api/products/${saved.id}/photo`, { method: "PUT", file });
    else if (f.elements.photo_remove.checked) await api(`/api/products/${saved.id}/photo`, { method: "DELETE" });
    $("#edit").close();
    toast("Сохранено");
    await refreshAll();
  } catch (e) {
    $("#edit-error").textContent = e.message;
    $("#edit-error").hidden = false;
    loadProducts();
  } finally {
    btn.disabled = false;
  }
}

function confirmDelete(p) {
  const dlg = $("#confirm");
  $("#confirm-text").textContent = `«${p.name}» будет удалён безвозвратно.`;
  dlg.returnValue = "";
  dlg.onclose = async () => {
    if (dlg.returnValue !== "ok") return;
    try {
      await api(`/api/products/${p.id}`, { method: "DELETE" });
      toast("Товар удалён");
      await refreshAll();
    } catch (e) { toast(e.message, true); }
  };
  dlg.showModal();
}

/* ---------------------------------------------------------------- settings */
function openSettings() {
  const f = $("#settings-form");
  f.elements.notify_low_stock.checked = state.settings.notify_low_stock;
  f.elements.low_stock_threshold.value = state.settings.low_stock_threshold;
  $("#settings-error").hidden = true;
  $("#settings").showModal();
}

async function submitSettings(ev) {
  if (ev.submitter?.value !== "ok") return;
  ev.preventDefault();
  const f = $("#settings-form");
  if (!f.reportValidity()) return;
  try {
    state.settings = await api("/api/settings", {
      method: "PATCH",
      body: {
        notify_low_stock: f.elements.notify_low_stock.checked,
        low_stock_threshold: Number(f.elements.low_stock_threshold.value),
      },
    });
    state.lowStock = state.settings.low_stock_threshold;
    $("#settings").close();
    toast("Настройки сохранены");
    await refreshAll();
  } catch (e) {
    $("#settings-error").textContent = e.message;
    $("#settings-error").hidden = false;
  }
}

/* ---------------------------------------------------------------- import */
async function importCsv(file) {
  const errs = $("#import-errors");
  errs.replaceChildren();
  try {
    const r = await api("/api/import.csv", { method: "POST", file, contentType: "text/csv" });
    $("#import-title").textContent = "Импорт выполнен";
    $("#import-summary").textContent = `Создано: ${r.created}, обновлено: ${r.updated}, без изменений: ${r.unchanged}.`;
    await refreshAll();
  } catch (e) {
    $("#import-title").textContent = "Импорт отменён";
    if (e.detail?.errors) {
      $("#import-summary").textContent = `Ничего не изменено. Ошибок: ${e.detail.total}.`;
      errs.replaceChildren(...e.detail.errors.map((x) => h("li", {}, x)));
    } else {
      $("#import-summary").textContent = e.message;
    }
  }
  $("#import-result").showModal();
}

/* ---------------------------------------------------------------- wiring */
function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function wire() {
  $("#theme").addEventListener("click", cycleTheme);
  $("#settings-btn").addEventListener("click", openSettings);
  $("#settings-form").addEventListener("submit", submitSettings);
  $("#edit-delete").addEventListener("click", () => { const p = editing; $("#edit").close(); confirmDelete(p); });
  $("#import-file").addEventListener("change", (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file) importCsv(file);
  });
  $("#add-btn").addEventListener("click", () => openEdit());
  $("#edit-form").addEventListener("submit", submitEdit);
  $("#prev").addEventListener("click", () => { state.page--; loadProducts(); });
  $("#next").addEventListener("click", () => { state.page++; loadProducts(); });
  $("#q").addEventListener("input", debounce((e) => {
    state.q = e.target.value.trim(); state.page = 0;
    loadProducts().catch((err) => toast(err.message, true));
  }, 250));
  for (const b of document.querySelectorAll(".seg-btn")) b.addEventListener("click", () => setStock(b.dataset.stock));
  $("#logout").addEventListener("click", async () => {
    try { await api("/api/logout", { method: "POST" }); } finally { lock(); }
  });
}

async function boot() {
  applyTheme(document.documentElement.dataset.theme ?? "");
  wire();
  let me;
  try { me = await api("/api/me"); } catch { return; }
  if (location.search) history.replaceState(null, "", "/");
  $("#who").textContent = me.username ? `@${me.username}` : `#${me.id}`;
  state.settings = me.settings;
  state.lowStock = me.settings.low_stock_threshold;
  $("#logout").hidden = false;
  $("#settings-btn").hidden = false;
  $("#app").hidden = false;
  await refreshAll();
}

boot();
