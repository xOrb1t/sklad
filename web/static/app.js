"use strict";

const PAGE_SIZE = 25;
const $ = (sel) => document.querySelector(sel);
const state = { page: 0, q: "", stock: "", lowStock: 2 };

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
class ApiError extends Error {}

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: { "X-Sklad": "1", ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) { lock(); throw new ApiError("Сессия истекла"); }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = Array.isArray(j.detail) ? j.detail.map((d) => d.msg).join("; ") : j.detail || detail;
    } catch { /* not json */ }
    throw new ApiError(detail);
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

function productRow(p) {
  const out = h("output", {}, String(p.quantity));
  const tr = h("tr", { dataset: { id: p.id, stock: stockOf(p) } },
    h("td", {}, p.has_photo
      ? h("img", { class: "thumb", src: `/api/products/${p.id}/photo`, alt: "", loading: "lazy" })
      : h("div", { class: "thumb-none" }, "·")),
    h("td", { class: "name" }, p.name, p.description ? h("small", {}, p.description) : null),
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
  $("#edit").showModal();
  f.elements.name.focus();
}

async function submitEdit(ev) {
  if (ev.submitter?.value !== "ok") return;
  ev.preventDefault();
  const f = $("#edit-form");
  if (!f.reportValidity()) return;
  const body = Object.fromEntries(["name", "description", "sku", "avito_url"].map((k) => [k, f.elements[k].value]));
  body.quantity = Number(f.elements.quantity.value);
  try {
    if (editing) await api(`/api/products/${editing.id}`, { method: "PATCH", body });
    else await api("/api/products", { method: "POST", body });
    $("#edit").close();
    toast(editing ? "Сохранено" : "Товар добавлен");
    await refreshAll();
  } catch (e) {
    $("#edit-error").textContent = e.message;
    $("#edit-error").hidden = false;
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

/* ---------------------------------------------------------------- wiring */
function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function wire() {
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
  wire();
  let me;
  try { me = await api("/api/me"); } catch { return; }
  if (location.search) history.replaceState(null, "", "/");
  $("#who").textContent = me.username ? `@${me.username}` : `#${me.id}`;
  $("#logout").hidden = false;
  $("#app").hidden = false;
  await refreshAll();
}

boot();
