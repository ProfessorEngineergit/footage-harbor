/* Footage Harbor web UI – no build step, no dependencies. */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const I = (name) => ICONS[name] || "";
const GH_ICON = `<svg viewBox="0 0 16 16" fill="currentColor"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0 0 16 8c0-4.42-3.58-8-8-8Z"/></svg>`;

const CARD_COLORS = ["#d7d5cf", "#ff6b2c", "#f2c14e", "#5fd38d", "#8fb8ff", "#c4a0ff", "#ff7aa8", "#6f7372"];
const PROVIDERS = {
  local: { label: "Lokaler Speicher", icon: "disk" },
  nextcloud: { label: "Nextcloud", icon: "cloud" },
  drive: { label: "Google Drive", icon: "cloud" },
  dropbox: { label: "Dropbox", icon: "cloud" },
  box: { label: "Box", icon: "cloud" },
  pcloud: { label: "pCloud", icon: "cloud" },
  webdav: { label: "WebDAV", icon: "server" },
  sftp: { label: "SFTP", icon: "server" },
  smb: { label: "NAS (SMB)", icon: "server" },
  s3: { label: "S3", icon: "server" },
};

// ---------------------------------------------------------------- formatting
const nf = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 1 });
function fmtBytes(b) {
  if (b == null || isNaN(b)) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (b >= 1000 && i < u.length - 1) { b /= 1000; i++; }
  return `${nf.format(b < 10 && i > 1 ? Math.round(b * 10) / 10 : Math.round(b))} ${u[i]}`;
}
function fmtAgo(ts) {
  if (!ts) return "–";
  const d = Date.now() / 1000 - ts;
  if (d < 45) return "gerade eben";
  if (d < 3600) return `vor ${Math.round(d / 60)} Min.`;
  if (d < 86400) return `vor ${Math.round(d / 3600)} Std.`;
  if (d < 86400 * 7) return `vor ${Math.round(d / 86400)} Tg.`;
  return new Date(ts * 1000).toLocaleDateString("de-DE", { day: "2-digit", month: "short", year: "numeric" });
}
function fmtDate(ts) {
  return ts ? new Date(ts * 1000).toLocaleString("de-DE", { day: "2-digit", month: "2-digit", year: "2-digit", hour: "2-digit", minute: "2-digit" }) : "–";
}
function fmtDur(s) {
  if (!isFinite(s) || s <= 0) return "–";
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return `${Math.round(s / 60)} Min.`;
  return `${Math.floor(s / 3600)} Std. ${Math.round((s % 3600) / 60)} Min.`;
}
const cardName = (c) => (c && (c.name || c.label)) || "Unbenannte Karte";

// ---------------------------------------------------------------------- api
async function api(path, opts = {}) {
  const res = await fetch("/api" + path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json", "X-Harbor": "1" },
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    credentials: "same-origin",
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty */ }
  if (res.status === 401 && !path.startsWith("/login") && !path.startsWith("/settings")) {
    S.status.authed = false;
    boot();
    throw new Error("Nicht angemeldet");
  }
  if (!res.ok) throw new Error((data && data.error) || `Fehler ${res.status}`);
  return data;
}
const post = (p, body = {}) => api(p, { method: "POST", body });
const put = (p, body = {}) => api(p, { method: "PUT", body });
const del = (p) => api(p, { method: "DELETE" });

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), kind === "err" ? 6000 : 3200);
}
async function guard(fn, okMsg) {
  try {
    const r = await fn();
    if (okMsg) toast(okMsg, "ok");
    return r;
  } catch (e) {
    toast(e.message, "err");
    throw e;
  }
}

// -------------------------------------------------------------------- modal
function modal({ title, body, foot, wide, onMount }) {
  const scrim = document.createElement("div");
  scrim.className = "scrim";
  scrim.innerHTML = `<div class="modal" style="${wide ? "width:min(680px,100%)" : ""}">
    <header><h3>${esc(title)}</h3><button class="btn ghost icon sm" data-close>${I("x")}</button></header>
    <div class="body">${body}</div>${foot ? `<footer>${foot}</footer>` : ""}</div>`;
  const close = () => { scrim.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  scrim.addEventListener("pointerdown", (e) => { if (e.target === scrim) close(); });
  scrim.querySelector("[data-close]").onclick = close;
  document.addEventListener("keydown", onKey);
  document.body.append(scrim);
  const m = scrim.querySelector(".modal");
  m.close = close;
  if (onMount) onMount(m, close);
  const first = m.querySelector("input, select, textarea");
  if (first) setTimeout(() => first.focus(), 30);
  return m;
}
function confirmBox(title, text, okLabel = "Bestätigen", danger = false) {
  return new Promise((resolve) => {
    modal({
      title, body: `<p class="muted" style="margin:0">${text}</p>`,
      foot: `<button class="btn ghost" data-no>Abbrechen</button><button class="btn ${danger ? "danger" : "primary"}" data-yes>${esc(okLabel)}</button>`,
      onMount(m, close) {
        m.querySelector("[data-no]").onclick = () => { close(); resolve(false); };
        m.querySelector("[data-yes]").onclick = () => { close(); resolve(true); };
      },
    });
  });
}

// -------------------------------------------------------------------- state
const S = {
  status: null, live: { slots: [] }, overview: null, flows: [], dests: [], cards: [],
  sel: null, sse: null, connected: true, route: [], flowEditor: null,
};

function connectLive() {
  if (S.sse) S.sse.close();
  const es = new EventSource("/api/events");
  S.sse = es;
  es.onopen = () => { S.connected = true; renderSideFoot(); };
  es.onerror = () => { S.connected = false; renderSideFoot(); };
  es.onmessage = (ev) => {
    const prev = S.live;
    S.live = JSON.parse(ev.data);
    const finished = prev.slots.some((p) => {
      const n = S.live.slots.find((x) => x.key === p.key);
      return n && p.state !== n.state && ["done", "ejected", "error"].includes(n.state);
    });
    const changedCards = prev.slots.map((s) => s.card && s.card.id).join() !== S.live.slots.map((s) => s.card && s.card.id).join();
    if (S.route[0] === "station") {
      renderStationLive();
      if (finished || changedCards) loadOverview().then(renderStationLower);
    }
    if (S.flowEditor) S.flowEditor.setSlots(S.live.slots);
    renderNavCounts();
  };
}

async function loadOverview() { S.overview = await api("/overview"); }

// -------------------------------------------------------------------- boot
async function boot() {
  S.status = await api("/status");
  if (!S.status.setup) return renderSetup();
  if (!S.status.authed) return renderLogin();
  renderShell();
  connectLive();
  route();
}
window.addEventListener("hashchange", () => S.status && S.status.authed && route());

// ------------------------------------------------------------------- setup
function renderSetup() {
  const disks = (S.status.disks || []).filter((d) => d.total > 2e9);
  const sugg = S.status.suggested_archive;
  $("#app").innerHTML = `
  <div class="auth"><div class="auth-box">
    <div class="brand">${I("logo")}<b>footage harbor</b></div>
    <h1>Station einrichten</h1>
    <p>Einmalig: Name, Zugangspasswort und wohin die Originale zuerst gesichert werden.</p>
    <form id="setup">
      <label class="field"><span>Name der Station</span><input class="input" name="name" value="Footage Harbor" maxlength="40"></label>
      <label class="field"><span>Passwort für diese Oberfläche</span><input class="input" type="password" name="password" minlength="8" autocomplete="new-password" required><small>Mindestens 8 Zeichen. Schützt Karten und Cloud-Zugänge im lokalen Netz.</small></label>
      <div class="field"><span>Lokales Archiv</span>
        ${disks.length ? `<div class="disk-list">${disks.map((d) => `<div class="disk" data-path="${esc(d.path === "/" ? sugg : d.path.replace(/\/$/, "") + "/footage-harbor")}"><b>${esc(d.label)}</b><span>${esc(d.path)} · ${fmtBytes(d.free)} frei</span></div>`).join("")}</div>` : ""}
        <input class="input mono" name="archive" value="${esc(sugg)}">
        <small>Am besten eine SSD. Leer lassen, wenn nur in die Cloud gesichert werden soll.</small>
      </div>
      <button class="btn primary" style="width:100%;height:40px;margin-top:6px">Station einrichten</button>
    </form>
  </div></div>`;
  $$(".disk").forEach((d) => d.onclick = () => {
    $$(".disk").forEach((x) => x.classList.remove("on"));
    d.classList.add("on");
    $("[name=archive]").value = d.dataset.path;
  });
  $("#setup").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    await guard(() => post("/setup", { name: f.get("name"), password: f.get("password"), archive: f.get("archive") }));
    boot();
  };
}

function renderLogin() {
  $("#app").innerHTML = `
  <div class="auth"><div class="auth-box">
    <div class="brand">${I("logo")}<b>footage harbor</b></div>
    <h1>${esc(S.status.name)}</h1>
    <p>Mit dem Passwort der Station anmelden.</p>
    <form id="login">
      <label class="field"><span>Passwort</span><input class="input" type="password" name="password" autocomplete="current-password" required autofocus></label>
      <button class="btn primary" style="width:100%;height:40px">Anmelden</button>
    </form>
  </div></div>`;
  $("#login").onsubmit = async (e) => {
    e.preventDefault();
    await guard(() => post("/login", { password: new FormData(e.target).get("password") }));
    boot();
  };
}

// ------------------------------------------------------------------- shell
const NAV = [
  ["station", "Station", "station"],
  ["flows", "Flows", "flows"],
  ["cards", "Karten", "cards"],
  ["storage", "Ziele", "storage"],
  ["history", "Verlauf", "history"],
  ["settings", "Einstellungen", "settings"],
];
function renderShell() {
  $("#app").innerHTML = `
  <div class="shell">
    <aside class="side">
      <a class="brand" href="#/station">${I("logo")}<b>footage harbor</b></a>
      <nav class="nav">${NAV.map(([k, l, ic]) => `<a href="#/${k}" data-nav="${k}">${I(ic)}<span>${l}</span><em class="count" data-count="${k}"></em></a>`).join("")}</nav>
      <div class="side-foot" id="side-foot"></div>
    </aside>
    <main class="main" id="main"></main>
  </div>`;
  renderSideFoot();
}
function renderSideFoot() {
  const el = $("#side-foot");
  if (!el) return;
  const st = S.overview && S.overview.station;
  el.innerHTML = `
    <div class="row"><span class="live ${S.connected ? "" : "off"}"><i></i>${S.connected ? "Verbunden" : "Getrennt"}</span><span>${st ? esc(st.name) : ""}</span></div>
    ${st && st.addresses.length ? `<div class="row"><span>Adresse</span><span class="mono">${esc(st.addresses[0])}</span></div>` : ""}
    ${st && st.temperature ? `<div class="row"><span>Temperatur</span><span class="num">${nf.format(st.temperature)} °C</span></div>` : ""}
    ${st ? `<a class="gh" href="${esc(st.repo)}" target="_blank" rel="noopener">${GH_ICON}<span>footage-harbor auf GitHub</span></a>` : ""}`;
}
function renderNavCounts() {
  const n = S.live.slots.filter((s) => s.card).length;
  const el = $('[data-count="station"]');
  if (el) el.textContent = n ? n : "";
}

async function route() {
  const parts = (location.hash.replace(/^#\/?/, "") || "station").split("/");
  S.route = parts;
  if (S.flowEditor && parts[0] !== "flows") { S.flowEditor.destroy(); S.flowEditor = null; }
  $$("[data-nav]").forEach((a) => a.classList.toggle("on", a.dataset.nav === parts[0]));
  const views = { station: viewStation, flows: viewFlows, cards: viewCards, storage: viewStorage, history: viewHistory, settings: viewSettings };
  try {
    await (views[parts[0]] || viewStation)(parts.slice(1));
  } catch (e) {
    if (e.message !== "Nicht angemeldet") toast(e.message, "err");
  }
  renderNavCounts();
}

// ----------------------------------------------------------------- station
const STATE_INFO = {
  empty: ["", "Frei"],
  mounting: ["run", "Wird eingebunden"],
  ready: ["idle", "Bereit"],
  running: ["run", "Läuft"],
  confirm: ["warn", "Freigabe nötig"],
  done: ["ok", "Gesichert"],
  ejected: ["ok", "Kann entnommen werden"],
  error: ["err", "Fehler"],
};
const PHASE = { scan: "Liest Inhalt", copy: "Kopiert", upload: "Lädt hoch", confirm: "Wartet auf Freigabe", cleanup: "Räumt auf", eject: "Wirft aus", done: "Fertig" };

function pct(p) { return p && p.bytes_total ? Math.min(100, Math.floor((p.bytes_done / p.bytes_total) * 100)) : 0; }

async function viewStation() {
  if (!S.overview) await loadOverview();
  else loadOverview().then(() => { renderStationLower(); renderSideFoot(); });
  renderSideFoot();
  $("#main").innerHTML = `
  <div class="page">
    <div class="head"><div><h1>Station</h1><p id="st-sub"></p></div></div>
    <div class="station-grid">
      <div>
        <section class="dock"><div class="dock-label"><span class="mono">Kartenplätze</span><span class="mono" id="dock-count"></span></div><div class="bays" id="bays"></div></section>
        <div id="st-lower"></div>
      </div>
      <aside class="detail panel" id="detail"></aside>
    </div>
  </div>`;
  renderStationLive();
  renderStationLower();
}

function renderStationLive() {
  const slots = S.live.slots;
  const bays = $("#bays");
  if (!bays) return;
  if (!S.sel || !slots.find((s) => s.key === S.sel)) {
    const first = slots.find((s) => s.state === "running" || s.state === "confirm") || slots.find((s) => s.card) || slots[0];
    S.sel = first ? first.key : null;
  }
  const inserted = slots.filter((s) => s.card).length;
  const running = slots.filter((s) => s.state === "running").length;
  $("#st-sub").textContent = slots.length === 0
    ? "Noch kein Kartenleser erkannt."
    : `${inserted} von ${slots.length} Plätzen belegt` + (running ? ` · ${running} ${running === 1 ? "Import läuft" : "Imports laufen"}` : "");
  $("#dock-count").textContent = slots.length ? `${String(slots.length).padStart(2, "0")} Slots` : "";
  if (!slots.length) {
    bays.innerHTML = `<div class="empty" style="grid-column:1/-1"><b>Steck eine Karte ein.</b>Kartenleser und Karten erscheinen hier automatisch, sobald sie erkannt werden.</div>`;
  } else {
    bays.innerHTML = slots.map((s) => {
      const [led, label] = STATE_INFO[s.state] || ["", s.state];
      const p = s.progress;
      const c = s.card;
      const micro = s.media && s.media.size && s.media.size < 0; // reader does not tell us; keep SD silhouette
      const state = s.state === "running" && p ? `${PHASE[p.phase] || "Läuft"} · ${pct(p)} %` : (s.state === "ready" ? (s.flow ? `Bereit · ${s.flow.name}` : "Kein Flow passt") : label);
      return `<button class="bay ${s.card ? "" : "empty-bay"} ${S.sel === s.key ? "sel" : ""}" data-slot="${esc(s.key)}">
        <div class="bay-top"><span class="bay-no">SLOT ${String(s.number).padStart(2, "0")}${s.name ? " · " + esc(s.name.toUpperCase()) : ""}</span><i class="led ${led}"></i></div>
        <div class="slot-well">${c ? `<div class="sd ${micro ? "micro" : ""}" style="--c:${esc(c.color || CARD_COLORS[0])}" data-label="${esc((c.label || c.name || "").slice(0, 12))}"></div>` : `<span>leer</span>`}</div>
        <div class="bay-name">${c ? esc(cardName(c)) : "—"}</div>
        <div class="bay-state">${esc(state)}</div>
        ${s.state === "running" ? `<div class="bar"><i style="width:${pct(p)}%"></i></div>` : ""}
      </button>`;
    }).join("");
    $$(".bay", bays).forEach((b) => b.onclick = () => { S.sel = b.dataset.slot; renderStationLive(); });
  }
  renderDetail();
}

function renderDetail() {
  const el = $("#detail");
  if (!el) return;
  const s = S.live.slots.find((x) => x.key === S.sel);
  if (!s) {
    el.innerHTML = `<div class="panel-pad"><div class="empty" style="padding:30px 0"><b>Nichts ausgewählt</b>Wähle einen Kartenplatz.</div></div>`;
    return;
  }
  const c = s.card;
  const [led, label] = STATE_INFO[s.state] || ["", s.state];
  const pillCls = { run: "run", ok: "ok", warn: "warn", err: "err" }[led] || "";
  if (!c) {
    el.innerHTML = `<div class="panel-pad">
      <div class="mono faint">SLOT ${String(s.number).padStart(2, "0")}</div>
      <h3 style="margin-top:6px">${s.name ? esc(s.name) : "Freier Platz"}</h3>
      <p class="muted" style="margin:8px 0 14px">Karte einstecken – der passende Flow startet automatisch.</p>
      <button class="btn sm" data-act="rename-slot">${I("edit")} Platz benennen</button></div>`;
    bindDetail(el, s);
    return;
  }
  const p = s.progress;
  let body = "";
  if (s.state === "running" || s.state === "confirm" || (p && ["done", "ejected", "error"].includes(s.state))) {
    const rate = p && p.rate ? `${fmtBytes(p.rate)}/s` : "";
    const eta = p && p.rate && s.state === "running" ? fmtDur((p.bytes_total - p.bytes_done) / p.rate) : "";
    body += `<div class="panel-pad">
      <div style="display:flex;justify-content:space-between;align-items:flex-end">
        <div class="big-pct">${p ? pct(p) : 0}<small>%</small></div>
        <div style="text-align:right;font-size:12px" class="muted num">${p ? `${fmtBytes(p.bytes_done)} / ${fmtBytes(p.bytes_total)}` : ""}<br>${rate}${eta ? ` · noch ${eta}` : ""}</div>
      </div>
      <div class="bar" style="margin-top:12px"><i style="width:${p ? pct(p) : 0}%"></i></div>
      ${p && p.current ? `<div class="mono faint" style="margin-top:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(p.current)}</div>` : ""}
      <ul class="steps">${(p ? p.steps : []).map((st) => {
        const pc = st.total ? Math.floor((st.done / st.total) * 100) : (st.state === "done" ? 100 : 0);
        const sub = st.state === "waiting" ? "Wartet" : st.state === "running" ? `${st.files_done} / ${st.files} Dateien` :
          st.state === "done" ? `${st.files} geprüft${st.skipped ? ` · ${st.skipped} schon vorhanden` : ""}` : "Unvollständig";
        return `<li class="${st.state === "running" ? "run" : st.state}">
          <span class="ico">${I(st.state === "done" ? "check" : st.kind === "local" ? "disk" : "cloud")}</span>
          <div><div>${esc(st.label)}</div><div class="sub">${sub}${st.method ? ` · ${esc(methodLabel(st.method))}` : ""}</div></div>
          <span class="num muted" style="font-size:12px">${pc} %</span>
          ${st.state === "running" ? `<div class="bar ${st.kind === "local" ? "" : "cloud"}"><i style="width:${pc}%"></i></div>` : ""}
        </li>`;
      }).join("")}</ul>
      ${s.state === "confirm" && s.cleanup ? `<div class="confirm-box"><b>Karte aufräumen?</b>
        <p>${s.cleanup.files} Dateien (${fmtBytes(s.cleanup.bytes)}) sind an allen geforderten Zielen inhaltlich geprüft und können von der Karte gelöscht werden.${s.cleanup.kept ? ` ${s.cleanup.kept} weitere bleiben erhalten.` : ""} Jede Datei wird vor dem Löschen noch einmal mit der Kopie verglichen.</p>
        <div style="display:flex;gap:8px"><button class="btn primary sm" data-act="approve">${I("check")} Löschen freigeben</button><button class="btn sm" data-act="decline">Karte behalten</button></div></div>` : ""}
    </div>`;
  }
  body += `<div class="panel-pad">
    <dl class="kv" style="margin-top:0">
      <dt>Label</dt><dd class="mono">${esc(c.label || "—")}</dd>
      <dt>Belegt</dt><dd>${s.media && s.media.used != null ? `${fmtBytes(s.media.used)} von ${fmtBytes(s.media.size)}` : fmtBytes(s.media && s.media.size)}</dd>
      <dt>Dateisystem</dt><dd>${esc((s.media && s.media.fstype || "").toUpperCase())}</dd>
      <dt>Flow</dt><dd>${s.flow ? `<a href="#/flows/${s.flow.id}">${esc(s.flow.name)}</a>` : "keiner"}</dd>
    </dl>
    <div class="row-actions">
      ${["ready", "done", "error", "ejected"].includes(s.state) && s.state !== "ejected" ? `<button class="btn primary sm" data-act="start">${I("play")} ${s.state === "ready" ? "Jetzt sichern" : "Erneut sichern"}</button>` : ""}
      ${s.state === "running" || s.state === "confirm" ? `<button class="btn sm" data-act="cancel">Abbrechen</button>` : ""}
      ${["ready", "done", "error"].includes(s.state) ? `<button class="btn sm" data-act="eject">${I("eject")} Auswerfen</button>` : ""}
      <button class="btn ghost sm" data-act="card">${I("edit")} Karte</button>
    </div>
  </div>`;
  el.innerHTML = `<div class="panel-pad" style="padding-bottom:${body ? 0 : 18}px">
      <div style="display:flex;justify-content:space-between;align-items:center"><span class="mono faint">SLOT ${String(s.number).padStart(2, "0")}</span><span class="pill ${pillCls}"><i></i>${esc(label)}</span></div>
      <h3 style="margin-top:8px">${esc(cardName(c))}</h3>
      <p class="muted" style="margin:4px 0 ${c.registered ? 18 : 12}px;font-size:13px">${esc(s.message || "")}</p>
      ${!c.registered ? `<div class="confirm-box" style="background:var(--accent-soft);border-color:rgba(255,107,44,.25);margin:0 0 18px"><b style="color:var(--accent)">Neue Karte</b><p style="margin:6px 0 10px">Gib ihr einen Namen und eine Farbe – so erkennst du sie im Dock und kannst ihr einen eigenen Flow zuweisen.</p><button class="btn sm primary" data-act="card">Karte benennen</button></div>` : ""}
    </div>${body}`;
  bindDetail(el, s);
}
function methodLabel(m) {
  return { "sha256-readback": "SHA-256 zurückgelesen", "sha256-download": "heruntergeladen & verglichen", "md5-provider": "MD5 des Anbieters", "sha1-provider": "SHA-1 des Anbieters", "sha256-provider": "SHA-256 des Anbieters", receipt: "schon gesichert", "receipt+size": "schon gesichert" }[m] || m;
}
function bindDetail(el, s) {
  $$("[data-act]", el).forEach((b) => b.onclick = async () => {
    const act = b.dataset.act;
    const k = encodeURIComponent(s.key);
    if (act === "card") return editCard(s.card.id);
    if (act === "rename-slot") {
      const name = prompt("Name für diesen Platz (z. B. „microSD links“)", s.name || "");
      if (name !== null) await guard(() => put(`/slots/${k}`, { name }));
      return;
    }
    if (act === "approve" && !(await confirmBox("Dateien löschen?", `${s.cleanup.files} geprüfte Dateien werden von „${esc(cardName(s.card))}“ gelöscht. Ordner und nicht gesicherte Dateien bleiben.`, "Löschen", true))) return;
    await guard(() => post(`/slots/${k}/${act}`));
  });
}

function renderStationLower() {
  const el = $("#st-lower");
  if (!el || !S.overview) return;
  const o = S.overview;
  const evs = o.events.slice(0, 14);
  el.innerHTML = `
    <div class="stats">
      <div><b>${nf.format(o.totals.files || 0)}</b><span>Originale gesichert</span></div>
      <div><b>${fmtBytes(o.totals.bytes || 0)}</b><span>geprüfte Daten</span></div>
      <div><b>${o.destinations.length}</b><span>${o.destinations.length === 1 ? "Ziel" : "Ziele"} verbunden</span></div>
    </div>
    ${!o.destinations.length ? `<div class="panel panel-pad" style="margin-top:14px;display:flex;justify-content:space-between;align-items:center;gap:12px"><div><b>Noch kein Ziel</b><div class="muted" style="font-size:13px">Ohne Ziel kann nichts gesichert werden.</div></div><a class="btn primary sm" href="#/storage">Ziel hinzufügen</a></div>` : ""}
    <section style="margin-top:28px"><h2>Aktivität</h2>
      ${evs.length ? `<ul class="feed">${evs.map((e) => `<li class="${esc(e.level)}"><i class="dot"></i><span>${esc(e.message)}</span><time>${fmtAgo(e.ts)}</time></li>`).join("")}</ul>` : `<div class="faint" style="font-size:13px">Noch nichts passiert.</div>`}
    </section>`;
}

// ------------------------------------------------------------------- cards
async function viewCards() {
  [S.cards, S.flows] = await Promise.all([api("/cards"), api("/flows")]);
  const flowName = (id) => (S.flows.find((f) => f.id === id) || {}).name;
  $("#main").innerHTML = `
  <div class="page">
    <div class="head"><div><h1>Karten</h1><p>Jede Karte, die je eingesteckt wurde. Benannte Karten können eigene Flows bekommen.</p></div></div>
    <div class="panel">${S.cards.length ? `<table class="table"><thead><tr><th>Karte</th><th>Flow</th><th class="hide-s">Zuletzt gesehen</th><th class="r hide-s">Imports</th><th class="r">Gesichert</th></tr></thead><tbody>
      ${S.cards.map((c) => `<tr class="click" data-id="${c.id}">
        <td><div class="cardcell"><span class="swatch" style="background:${esc(c.color || CARD_COLORS[0])}"></span><div><b>${esc(cardName(c))}</b><span class="mono">${esc(c.label || "ohne Label")} · ${fmtBytes(c.size)}</span></div></div></td>
        <td>${c.flow_id ? esc(flowName(c.flow_id) || "–") : `<span class="faint">automatisch</span>`}</td>
        <td class="hide-s">${c.slot ? `<span class="pill run"><i></i>Slot ${c.slot}</span>` : `<span class="muted">${fmtAgo(c.last_seen)}</span>`}</td>
        <td class="r num hide-s">${c.imports}</td>
        <td class="r num">${nf.format(c.files_secured)} Dateien</td></tr>`).join("")}
    </tbody></table>` : `<div class="empty"><b>Noch keine Karten</b>Sobald eine Karte eingesteckt wird, erscheint sie hier.</div>`}</div>
  </div>`;
  $$("tr[data-id]").forEach((r) => r.onclick = () => editCard(+r.dataset.id));
}

async function editCard(id) {
  const [cards, flows] = await Promise.all([api("/cards"), api("/flows")]);
  const c = cards.find((x) => x.id === id);
  if (!c) return;
  let color = c.color || CARD_COLORS[0];
  modal({
    title: c.registered ? "Karte bearbeiten" : "Neue Karte benennen",
    body: `
      <label class="field"><span>Name</span><input class="input" name="name" value="${esc(c.name || c.label)}" maxlength="40" placeholder="z. B. Sony A – Hauptkamera"></label>
      <div class="field"><span>Farbe</span><div style="display:flex;gap:8px">${CARD_COLORS.map((col) => `<button type="button" class="swatch" data-color="${col}" style="background:${col};width:26px;height:34px;border:0;cursor:pointer;outline:${col === color ? "2px solid var(--text)" : "none"};outline-offset:3px"></button>`).join("")}</div><small>Gleiche Farbe wie der Aufkleber auf der Karte – dann findest du sie im Dock sofort.</small></div>
      <label class="field"><span>Flow</span><select class="input" name="flow"><option value="">Automatisch – erster passender Flow</option>${flows.map((f) => `<option value="${f.id}" ${f.id === c.flow_id ? "selected" : ""}>${esc(f.name)}</option>`).join("")}</select></label>
      <dl class="kv"><dt>Label</dt><dd class="mono">${esc(c.label || "—")}</dd><dt>Kapazität</dt><dd>${fmtBytes(c.size)}</dd><dt>Zuerst gesehen</dt><dd>${fmtDate(c.first_seen)}</dd><dt>Imports</dt><dd>${c.imports}</dd></dl>`,
    foot: `<button class="btn ghost danger" data-forget style="margin-right:auto">Vergessen</button><button class="btn ghost" data-close2>Abbrechen</button><button class="btn primary" data-save>Speichern</button>`,
    onMount(m, close) {
      $$("[data-color]", m).forEach((b) => b.onclick = () => {
        color = b.dataset.color;
        $$("[data-color]", m).forEach((x) => x.style.outline = x.dataset.color === color ? "2px solid var(--text)" : "none");
      });
      m.querySelector("[data-close2]").onclick = close;
      m.querySelector("[data-save]").onclick = async () => {
        await guard(() => put(`/cards/${id}`, { name: $("[name=name]", m).value, color, flow_id: +$("[name=flow]", m).value || null, registered: true }), "Gespeichert");
        close();
        if (S.route[0] === "cards") viewCards();
      };
      m.querySelector("[data-forget]").onclick = async () => {
        if (!(await confirmBox("Karte vergessen?", "Name, Farbe und Flow-Zuordnung werden entfernt. Gesicherte Dateien und Verlauf bleiben erhalten.", "Vergessen", true))) return;
        await guard(() => del(`/cards/${id}`));
        close();
        if (S.route[0] === "cards") viewCards();
      };
    },
  });
}

// ----------------------------------------------------------------- storage
async function viewStorage() {
  const data = await api("/destinations");
  S.dests = data.items;
  $("#main").innerHTML = `
  <div class="page">
    <div class="head"><div><h1>Ziele</h1><p>Wohin die Originale gesichert werden. Anmeldungen laufen im Browser dieses Rechners – die Station bekommt nur eine eigene, widerrufbare Freigabe.</p></div>
      <div class="actions"><button class="btn primary" id="add-dest">${I("plus")} Ziel hinzufügen</button></div></div>
    ${!data.rclone ? `<div class="panel panel-pad" style="margin-bottom:16px;border-color:rgba(242,193,78,.3)"><b style="color:var(--warn)">rclone fehlt</b><div class="muted" style="font-size:13px">Cloud-Ziele brauchen rclone. Auf der Station <span class="mono">sudo ./install.sh</span> erneut ausführen. Lokale Ziele funktionieren trotzdem.</div></div>` : ""}
    ${S.dests.length ? `<div class="dest-grid">${S.dests.map(destCard).join("")}</div>` : `<div class="panel"><div class="empty"><b>Noch kein Ziel</b>Lege einen lokalen Ordner oder eine Cloud an.</div></div>`}
  </div>`;
  $("#add-dest").onclick = () => addDestination(data);
  $$("[data-dest]").forEach((el) => {
    const id = +el.dataset.dest;
    $("[data-test]", el).onclick = async (e) => {
      e.target.closest("button").disabled = true;
      try { const r = await api(`/destinations/${id}/test`, { method: "POST" }); toast(r.message, "ok"); }
      catch (err) { toast(err.message, "err"); }
      e.target.closest("button").disabled = false;
    };
    $("[data-edit]", el).onclick = () => editDestination(S.dests.find((d) => d.id === id));
    $("[data-del]", el).onclick = async () => {
      if (!(await confirmBox("Ziel entfernen?", "Die Station hört auf, dorthin zu sichern. Bereits gesicherte Dateien bleiben am Ziel. Flows mit diesem Ziel müssen angepasst werden.", "Entfernen", true))) return;
      await guard(() => del(`/destinations/${id}`));
      viewStorage();
    };
  });
}
function destCard(d) {
  const p = PROVIDERS[d.provider] || { label: d.provider, icon: "cloud" };
  const u = d.usage || {};
  const used = u.total ? u.total - u.free : null;
  const pc = u.total ? Math.round((used / u.total) * 100) : 0;
  const where = d.kind === "local" ? d.config.path : `${d.config.account ? d.config.account + " · " : ""}/${d.config.root || ""}`;
  return `<div class="panel dest" data-dest="${d.id}">
    <div class="dest-top"><div class="dest-ico">${I(p.icon)}</div><div style="min-width:0"><b>${esc(d.name)}</b><span class="mono">${esc(where)}</span></div>
      <span class="pill ${u.ok === false ? "err" : "ok"}" style="margin-left:auto"><i></i>${u.ok === false ? "Fehler" : "Bereit"}</span></div>
    ${u.ok === false && u.error ? `<div style="font-size:12px;color:var(--err)">${esc(u.error)}</div>` : ""}
    <div>
      <div class="bar ${d.kind === "local" ? "ok" : "cloud"}"><i style="width:${pc}%"></i></div>
      <div class="dest-meta" style="margin-top:8px"><span>${u.total ? `${fmtBytes(u.free)} frei von ${fmtBytes(u.total)}` : "Kapazität unbekannt"}</span><span>${p.label}</span></div>
    </div>
    <div class="dest-meta"><span>${nf.format(d.stats.n)} Dateien · ${fmtBytes(d.stats.b)}</span><span>${d.stats.last ? "zuletzt " + fmtAgo(d.stats.last) : "noch leer"}</span></div>
    <div class="dest-foot"><button class="btn sm" data-test>${I("check")} Testen</button><button class="btn sm ghost" data-edit>${I("edit")} Bearbeiten</button><button class="btn sm ghost danger" data-del style="margin-left:auto">${I("trash")}</button></div>
  </div>`;
}
function editDestination(d) {
  modal({
    title: "Ziel bearbeiten",
    body: `<label class="field"><span>Name</span><input class="input" name="name" value="${esc(d.name)}"></label>
      ${d.kind === "rclone" ? `<label class="field"><span>Ordner im Konto</span><input class="input mono" name="root" value="${esc(d.config.root || "")}"><small>Alle Sicherungen landen unterhalb dieses Ordners.</small></label>` : `<div class="field"><span>Pfad</span><div class="code-hint">${esc(d.config.path)}</div></div>`}`,
    foot: `<button class="btn ghost" data-c>Abbrechen</button><button class="btn primary" data-s>Speichern</button>`,
    onMount(m, close) {
      $("[data-c]", m).onclick = close;
      $("[data-s]", m).onclick = async () => {
        const body = { name: $("[name=name]", m).value };
        if ($("[name=root]", m)) body.root = $("[name=root]", m).value;
        await guard(() => put(`/destinations/${d.id}`, body), "Gespeichert");
        close();
        viewStorage();
      };
    },
  });
}

function addDestination(data) {
  const items = [
    ["local", "Lokaler Speicher", "SSD, Festplatte oder Ordner an der Station", "disk"],
    ["nextcloud", "Nextcloud", "Freigabe direkt im Browser", "cloud"],
    ...Object.entries(data.oauth).map(([k, l]) => [k, l, "Anmelden im Browser", "cloud"]),
    ...Object.entries(data.forms).map(([k, v]) => [k, v.label, "Zugangsdaten eingeben", "server"]),
    ["paste", "rclone-Remote", "OneDrive, iCloud Drive & alles, was rclone kann", "terminal"],
  ];
  modal({
    title: "Ziel hinzufügen", wide: true,
    body: `<div class="provider-grid">${items.map(([k, l, d, ic]) => `<button class="provider" data-p="${k}" ${k !== "local" && !data.rclone ? "disabled style='opacity:.4'" : ""}>${I(ic)}<div><b>${esc(l)}</b><br><span>${esc(d)}</span></div></button>`).join("")}</div>`,
    onMount(m, close) {
      $$("[data-p]", m).forEach((b) => b.onclick = () => {
        close();
        const k = b.dataset.p;
        if (k === "local") return addLocal(data);
        if (k === "nextcloud") return addNextcloud();
        if (data.oauth[k]) return addOAuth(k, data.oauth[k]);
        if (data.forms[k]) return addForm(k, data.forms[k]);
        if (k === "paste") return addPaste();
      });
    },
  });
}
function addLocal(data) {
  const disks = (data.disks || []).filter((d) => d.total > 1e9);
  modal({
    title: "Lokaler Speicher",
    body: `${disks.length ? `<div class="field"><span>Laufwerk</span><div class="disk-list">${disks.map((d) => `<div class="disk" data-path="${esc(d.path.replace(/\/$/, "") + "/footage-harbor")}"><b>${esc(d.label)}</b><span>${esc(d.path)} · ${fmtBytes(d.free)} frei</span></div>`).join("")}</div></div>` : ""}
      <label class="field"><span>Ordner</span><input class="input mono" name="path" placeholder="/mnt/ssd/footage-harbor"></label>
      <label class="field"><span>Name</span><input class="input" name="name" placeholder="z. B. Studio-SSD"></label>
      <p class="faint" style="font-size:12px;margin:0">Zwei Ordner auf demselben Laufwerk sind keine zwei unabhängigen Kopien.</p>`,
    foot: `<button class="btn ghost" data-c>Abbrechen</button><button class="btn primary" data-s>Hinzufügen</button>`,
    onMount(m, close) {
      $$(".disk", m).forEach((d) => d.onclick = () => { $$(".disk", m).forEach((x) => x.classList.remove("on")); d.classList.add("on"); $("[name=path]", m).value = d.dataset.path; });
      $("[data-c]", m).onclick = close;
      $("[data-s]", m).onclick = async () => {
        await guard(() => post("/destinations/local", { path: $("[name=path]", m).value, name: $("[name=name]", m).value }), "Ziel hinzugefügt");
        close(); viewStorage();
      };
    },
  });
}
function finishCloud(sid, label) {
  modal({
    title: `${label} verbunden`,
    body: `<p class="muted" style="margin:0 0 16px">Die Station hat ihre eigene Freigabe erhalten. Du kannst das Browserfenster schließen.</p>
      <label class="field"><span>Name</span><input class="input" name="name" value="${esc(label)}"></label>
      <label class="field"><span>Ordner im Konto</span><input class="input mono" name="root" value="Footage Harbor"><small>Wird bei der ersten Sicherung angelegt.</small></label>`,
    foot: `<button class="btn primary" data-s>Fertig</button>`,
    onMount(m, close) {
      $("[data-s]", m).onclick = async () => {
        await guard(() => post(`/connect/${sid}/save`, { name: $("[name=name]", m).value, root: $("[name=root]", m).value }), "Ziel hinzugefügt");
        close(); viewStorage();
      };
    },
  });
}
function addNextcloud() {
  let timer = null;
  modal({
    title: "Nextcloud verbinden",
    body: `<ol class="steps-guide"><li>Adresse deiner Nextcloud eingeben.</li><li>Im neuen Tab <b>Zugriff gewähren</b> – du bist dort meist schon angemeldet.</li><li>Fertig. Die Station bekommt ein eigenes App-Passwort, das du in Nextcloud jederzeit widerrufen kannst.</li></ol>
      <label class="field"><span>Nextcloud-Adresse</span><input class="input mono" name="server" placeholder="cloud.example.com"></label>
      <div id="nc-wait" class="hidden"><span class="pill run"><i></i>Warte auf Freigabe im Browser …</span></div>`,
    foot: `<button class="btn ghost" data-c>Abbrechen</button><button class="btn primary" data-s>${I("external")} Anmeldung öffnen</button>`,
    onMount(m, close) {
      const stop = () => { clearInterval(timer); close(); };
      $("[data-c]", m).onclick = stop;
      $("[data-s]", m).onclick = async () => {
        const win = window.open("about:blank", "_blank");
        try {
          const r = await post("/connect/nextcloud", { server: $("[name=server]", m).value });
          if (win) win.location = r.url; else window.open(r.url, "_blank");
          $("#nc-wait", m).classList.remove("hidden");
          $("[data-s]", m).disabled = true;
          timer = setInterval(async () => {
            const st = await api(`/connect/${r.id}`).catch(() => null);
            if (!st) return;
            if (st.state === "done") { stop(); finishCloud(r.id, "Nextcloud"); }
            if (st.state === "error") { clearInterval(timer); toast(st.error, "err"); $("[data-s]", m).disabled = false; $("#nc-wait", m).classList.add("hidden"); }
          }, 2000);
        } catch (e) { if (win) win.close(); toast(e.message, "err"); }
      };
    },
  });
}
function addOAuth(provider, label) {
  let sid = null;
  modal({
    title: `${label} verbinden`,
    body: `<ol class="steps-guide">
        <li><b>Anmelden</b> öffnet ${esc(label)} in einem neuen Tab. Melde dich an und erlaube den Zugriff.</li>
        <li>Danach zeigt der Tab eine Fehlerseite („Seite nicht erreichbar“, Adresse beginnt mit <span class="mono">127.0.0.1</span>). <b>Das ist richtig so.</b></li>
        <li>Kopiere die komplette Adresse aus der Adresszeile dieses Tabs und füge sie hier ein.</li></ol>
      <button class="btn" data-open style="width:100%;margin-bottom:16px">${I("external")} Bei ${esc(label)} anmelden</button>
      <label class="field"><span>Adresse aus dem Tab</span><input class="input mono" name="url" placeholder="http://127.0.0.1:53682/?state=…&code=…" disabled></label>`,
    foot: `<button class="btn ghost" data-c>Abbrechen</button><button class="btn primary" data-s disabled>Verbinden</button>`,
    onMount(m, close) {
      $("[data-c]", m).onclick = close;
      $("[data-open]", m).onclick = async () => {
        const win = window.open("about:blank", "_blank");
        try {
          const r = await post("/connect/oauth", { provider });
          sid = r.id;
          if (win) win.location = r.url; else window.open(r.url, "_blank");
          $("[name=url]", m).disabled = false;
          $("[data-s]", m).disabled = false;
          $("[name=url]", m).focus();
        } catch (e) { if (win) win.close(); toast(e.message, "err"); }
      };
      $("[data-s]", m).onclick = async () => {
        $("[data-s]", m).disabled = true;
        try {
          await post(`/connect/${sid}/finish`, { url: $("[name=url]", m).value });
          close(); finishCloud(sid, label);
        } catch (e) { toast(e.message, "err"); $("[data-s]", m).disabled = false; }
      };
    },
  });
}
const FIELD_LABELS = { url: "Adresse", user: "Benutzer", pass: "Passwort", host: "Server", port: "Port", domain: "Domäne", provider: "S3-Anbieter (z. B. AWS, Minio, Wasabi)", access_key_id: "Access Key", secret_access_key: "Secret Key", endpoint: "Endpoint", region: "Region" };
function addForm(provider, spec) {
  modal({
    title: `${spec.label} verbinden`,
    body: spec.fields.map((f) => `<label class="field"><span>${FIELD_LABELS[f] || f}</span><input class="input ${["pass", "secret_access_key"].includes(f) ? "" : "mono"}" name="${f}" ${["pass", "secret_access_key"].includes(f) ? 'type="password"' : ""}></label>`).join("") +
      `<label class="field"><span>Ordner</span><input class="input mono" name="__root" value="${provider === "s3" ? "bucket/footage-harbor" : "Footage Harbor"}"></label>
       <label class="field"><span>Name</span><input class="input" name="__name" value="${esc(spec.label)}"></label>`,
    foot: `<button class="btn ghost" data-c>Abbrechen</button><button class="btn primary" data-s>Verbinden</button>`,
    onMount(m, close) {
      $("[data-c]", m).onclick = close;
      $("[data-s]", m).onclick = async () => {
        const fields = {};
        spec.fields.forEach((f) => fields[f] = $(`[name=${f}]`, m).value);
        $("[data-s]", m).disabled = true;
        try {
          await post("/connect/form", { provider, fields, root: $("[name=__root]", m).value, name: $("[name=__name]", m).value });
          toast("Ziel hinzugefügt", "ok"); close(); viewStorage();
        } catch (e) { toast(e.message, "err"); $("[data-s]", m).disabled = false; }
      };
    },
  });
}
function addPaste() {
  modal({
    title: "rclone-Remote übernehmen", wide: true,
    body: `<ol class="steps-guide">
        <li>Auf deinem Rechner <a href="https://rclone.org/install/" target="_blank" rel="noopener">rclone</a> installieren und <span class="mono">rclone config</span> ausführen – die Anmeldung läuft dort ganz normal im Browser.</li>
        <li>Mit <span class="mono">rclone config show NAME</span> den Abschnitt anzeigen lassen und hier einfügen.</li></ol>
      <label class="field"><span>Konfiguration</span><textarea class="input" name="cfg" placeholder="[onedrive]&#10;type = onedrive&#10;token = {…}"></textarea><small>Für iCloud Drive verlangt Apple etwa alle 30 Tage eine neue Bestätigung.</small></label>
      <label class="field"><span>Ordner im Konto</span><input class="input mono" name="root" value="Footage Harbor"></label>`,
    foot: `<button class="btn ghost" data-c>Abbrechen</button><button class="btn primary" data-s>Übernehmen</button>`,
    onMount(m, close) {
      $("[data-c]", m).onclick = close;
      $("[data-s]", m).onclick = async () => {
        await guard(() => post("/connect/paste", { config: $("[name=cfg]", m).value, root: $("[name=root]", m).value }), "Ziel hinzugefügt");
        close(); viewStorage();
      };
    },
  });
}

// ----------------------------------------------------------------- history
const IMPORT_STATE = { done: ["ok", "Gesichert"], partial: ["warn", "Teilweise"], running: ["run", "Läuft"], confirm: ["warn", "Freigabe"], interrupted: ["warn", "Unterbrochen"], failed: ["err", "Fehlgeschlagen"] };
async function viewHistory(args) {
  if (args[0]) return viewImport(+args[0]);
  const rows = await api("/imports");
  $("#main").innerHTML = `
  <div class="page">
    <div class="head"><div><h1>Verlauf</h1><p>Jeder Import mit Nachweis, wo jede Datei liegt und wie sie geprüft wurde.</p></div></div>
    <div class="panel">${rows.length ? `<table class="table"><thead><tr><th>Zeit</th><th>Karte</th><th class="hide-s">Flow</th><th class="r">Neu</th><th class="r hide-s">Daten</th><th class="r hide-s">Gelöscht</th><th>Status</th></tr></thead><tbody>
      ${rows.map((r) => { const [k, l] = IMPORT_STATE[r.state] || ["", r.state]; return `<tr class="click" data-id="${r.id}">
        <td class="num">${fmtDate(r.started_at)}</td><td><b style="font-weight:550">${esc(r.card_name || r.card_label || "Karte")}</b></td>
        <td class="hide-s muted">${esc(r.flow_name)}</td><td class="r num">${r.files_new} / ${r.files_total}</td>
        <td class="r num hide-s">${fmtBytes(r.bytes_total)}</td><td class="r num hide-s">${r.files_deleted || "–"}</td>
        <td><span class="pill ${k}"><i></i>${l}</span></td></tr>`; }).join("")}
    </tbody></table>` : `<div class="empty"><b>Noch keine Imports</b>Steck eine Karte ein.</div>`}</div>
  </div>`;
  $$("tr[data-id]").forEach((r) => r.onclick = () => location.hash = `#/history/${r.dataset.id}`);
}
async function viewImport(id) {
  const imp = await api(`/imports/${id}`);
  const [k, l] = IMPORT_STATE[imp.state] || ["", imp.state];
  const sum = imp.summary || {};
  const FST = { secured: ["ok", "gesichert"], deleted: ["ok", "gesichert · gelöscht"], kept: ["warn", "behalten"], failed: ["err", "Fehler"], pending: ["", "offen"] };
  $("#main").innerHTML = `
  <div class="page">
    <div class="head"><div><a href="#/history" class="muted" style="font-size:13px;text-decoration:none">← Verlauf</a><h1 style="margin-top:6px">Import #${imp.id}</h1><p>${fmtDate(imp.started_at)} · ${esc(imp.flow_name)} · Slot ${esc(imp.slot)}</p></div>
      <div class="actions"><span class="pill ${k}" style="height:28px"><i></i>${l}</span><button class="btn" id="dl">${I("download")} Bericht (JSON)</button></div></div>
    <div class="stats" style="margin:0 0 22px;grid-template-columns:repeat(4,1fr)">
      <div><b>${imp.files_total}</b><span>Dateien betrachtet</span></div>
      <div><b>${imp.files_new}</b><span>neu gesichert</span></div>
      <div><b>${fmtBytes(imp.bytes_total)}</b><span>Datenmenge</span></div>
      <div><b>${imp.files_deleted}</b><span>von der Karte gelöscht</span></div>
    </div>
    ${(sum.errors || []).length ? `<div class="panel panel-pad" style="margin-bottom:22px;border-color:rgba(255,90,82,.3)"><h2 style="color:var(--err)">Fehler</h2><ul class="issues">${sum.errors.map((e) => `<li class="err">${esc(e)}</li>`).join("")}</ul></div>` : ""}
    ${imp.error && !(sum.errors || []).length ? `<div class="panel panel-pad" style="margin-bottom:22px"><span class="muted">${esc(imp.error)}</span></div>` : ""}
    <h2>Dateien</h2>
    <div class="files"><table class="table"><thead><tr><th>Datei</th><th class="r">Größe</th><th>Status</th><th class="hide-s">Kopien</th></tr></thead><tbody>
      ${imp.files.map((f) => { const [fk, fl] = FST[f.state] || ["", f.state]; return `<tr><td class="mono">${esc(f.rel_path)}</td><td class="r num">${fmtBytes(f.size)}</td><td><span class="pill ${fk}"><i></i>${fl}</span></td>
        <td class="hide-s" style="font-size:12px">${f.copies.map((c) => `<div title="${esc(c.path)}">${esc(c.destination)} <span class="faint">· ${esc(methodLabel(c.method))}</span></div>`).join("") || `<span class="faint">${esc(f.note || "–")}</span>`}</td></tr>`; }).join("")}
    </tbody></table></div>
  </div>`;
  $("#dl").onclick = () => {
    const blob = new Blob([JSON.stringify(imp, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `footage-harbor-import-${imp.id}.json`;
    a.click();
  };
}

// ---------------------------------------------------------------- settings
async function viewSettings() {
  const { station: st, settings: cfg } = await api("/settings");
  S.settings = cfg;
  const rows = [
    ["Rechnername", st.hostname], ["Adresse", st.addresses.join(", ") || "–"], ["Version", st.version],
    ["Kartenquelle", st.backend === "linux" ? "Kartenleser (USB/SD)" : "Ordner (Entwicklungsmodus)"],
    ["rclone", st.rclone || "nicht installiert"], ["Temperatur", st.temperature ? `${nf.format(st.temperature)} °C` : "–"], ["Laufzeit", fmtDur(st.uptime)],
  ];
  const tg = (key, on) => `<span class="toggle"><input type="checkbox" data-set="${key}" ${on ? "checked" : ""}><i></i></span>`;
  $("#main").innerHTML = `
  <div class="page" style="max-width:820px">
    <div class="head"><div><h1>Einstellungen</h1><p>Gilt für die ganze Station. Was pro Karte passiert, legen die Flows fest.</p></div></div>

    <div class="panel panel-pad" style="margin-bottom:16px"><h2>Station</h2>
      <div class="set-row"><div><b>Name</b><small>Erscheint in der Seitenleiste, in Benachrichtigungen und als Gerätename in Nextcloud.</small></div><input class="input" data-set="name" value="${esc(st.name)}" maxlength="40"></div>
    </div>

    <div class="panel panel-pad" style="margin-bottom:16px"><h2>Import</h2>
      <div class="set-row"><div><b>Standard-Ordnerstruktur</b><small>Vorlage für neue „Sichern“-Schritte. Platzhalter wie {year}, {date}, {card}, {path}.</small></div><input class="input mono" data-set="default_template" value="${esc(cfg.default_template)}"></div>
      <div class="set-row"><div><b>Versteckte Dateien mitsichern</b><small>Dateien und Ordner, die mit einem Punkt beginnen. Systemmüll wie .DS_Store und ._-Dateien bleibt immer draußen.</small></div>${tg("include_hidden", cfg.include_hidden)}</div>
    </div>

    <div class="panel panel-pad" style="margin-bottom:16px"><h2>Cloud</h2>
      <div class="set-row"><div><b>Upload-Bandbreite</b><small>Leer = unbegrenzt. <span class="mono">10M</span> begrenzt auf 10 MB/s. Mit Zeitplan: <span class="mono">08:00,2M 23:00,off</span> – tagsüber gedrosselt, nachts volle Leitung.</small></div><input class="input mono" data-set="bwlimit" value="${esc(cfg.bwlimit)}" placeholder="unbegrenzt"></div>
    </div>

    <div class="panel panel-pad" style="margin-bottom:16px"><h2>Benachrichtigungen</h2>
      <div class="set-row"><div><b>Adresse</b><small>Nach jedem Import geht eine kurze Nachricht dorthin, z. B. <span class="mono">https://ntfy.sh/dein-thema</span>. Flows mit eigenem „Benachrichtigen“-Schritt nutzen ihre eigene Adresse.</small></div><input class="input mono" data-set="notify_url" value="${esc(cfg.notify_url)}" placeholder="https://ntfy.sh/…"></div>
      <div class="set-row"><div><b>Wann</b></div><div class="seg" data-seg-set="notify_on"><button data-v="all" class="${cfg.notify_on !== "errors" ? "on" : ""}">Immer</button><button data-v="errors" class="${cfg.notify_on === "errors" ? "on" : ""}">Nur bei Fehlern</button></div></div>
    </div>

    <div class="panel panel-pad" style="margin-bottom:16px"><h2>Zugang</h2>
      <div class="set-row"><div><b>Passwort ändern</b></div><div style="display:flex;flex-direction:column;gap:6px;width:240px"><input class="input" type="password" id="pw-old" placeholder="Aktuelles Passwort" autocomplete="current-password"><input class="input" type="password" id="pw-new" placeholder="Neues Passwort" autocomplete="new-password"><button class="btn sm" id="save-pw">Ändern</button></div></div>
      <div class="set-row"><div><b>Alle anderen Geräte abmelden</b><small>Nur dieser Browser bleibt angemeldet.</small></div><button class="btn sm" id="logout-all">Abmelden</button></div>
      <div class="set-row"><div><b>Dieses Gerät abmelden</b></div><button class="btn sm" id="logout">Abmelden</button></div>
    </div>

    <div class="panel panel-pad" style="margin-bottom:16px"><h2>Daten</h2>
      <div class="set-row"><div><b>Konfiguration exportieren</b><small>Flows, Ziele, Karten und Einstellungen als JSON. Zugangsdaten zu Cloud-Konten sind nicht enthalten.</small></div><a class="btn sm" href="/api/export" download>${I("download")} Exportieren</a></div>
    </div>

    <div class="panel panel-pad" style="margin-bottom:16px"><h2>System</h2>
      <dl class="kv" style="margin-top:0">${rows.map(([a, b]) => `<dt>${a}</dt><dd class="mono">${esc(b)}</dd>`).join("")}</dl>
      <div class="row-actions">
        <button class="btn sm" data-sys="restart" ${st.dev ? "disabled" : ""}>Dienst neu starten</button>
        <button class="btn sm" data-sys="reboot" ${st.dev ? "disabled" : ""}>Station neu starten</button>
        <button class="btn sm danger" data-sys="poweroff" ${st.dev ? "disabled" : ""}>Herunterfahren</button>
      </div>
    </div>

    <div class="panel panel-pad"><h2>Über</h2>
      <p class="muted" style="margin:0 0 12px;font-size:13px">Footage Harbor ist Open Source (MIT). Fehler gefunden oder eine Idee? Gerne auf GitHub melden.</p>
      <a class="btn sm" href="${esc(st.repo)}" target="_blank" rel="noopener">${GH_ICON} GitHub</a>
      <a class="btn sm ghost" href="${esc(st.repo)}/issues/new" target="_blank" rel="noopener">Problem melden</a>
    </div>
  </div>`;
  const saveSet = (body) => guard(() => put("/settings", body), "Gespeichert").then(() => loadOverview()).then(renderSideFoot);
  $$("[data-set]").forEach((el) => {
    el.addEventListener(el.type === "checkbox" ? "change" : "change", () => saveSet({ [el.dataset.set]: el.type === "checkbox" ? el.checked : el.value }));
  });
  $$("[data-seg-set] button").forEach((b) => b.onclick = () => {
    $$("button", b.parentElement).forEach((x) => x.classList.toggle("on", x === b));
    saveSet({ [b.parentElement.dataset.segSet]: b.dataset.v });
  });
  $("#save-pw").onclick = () => guard(() => put("/settings", { password: $("#pw-old").value, new_password: $("#pw-new").value }), "Passwort geändert");
  $("#logout-all").onclick = () => guard(() => post("/logout-all"), "Andere Geräte abgemeldet");
  $("#logout").onclick = async () => { await post("/logout"); if (S.sse) S.sse.close(); boot(); };
  $$("[data-sys]").forEach((b) => b.onclick = async () => {
    const label = { restart: "Dienst neu starten?", reboot: "Station neu starten?", poweroff: "Station herunterfahren?" }[b.dataset.sys];
    const text = b.dataset.sys === "poweroff" ? "Danach ist die Oberfläche nicht mehr erreichbar, bis die Station wieder Strom bekommt." : "Die Oberfläche ist kurz nicht erreichbar und verbindet sich danach von selbst.";
    if (!(await confirmBox(label, text, "Ja", b.dataset.sys === "poweroff"))) return;
    await guard(() => post(`/system/${b.dataset.sys}`), "Wird ausgeführt");
  });
}

// ------------------------------------------------------------------- flows
async function viewFlows(args) {
  let cfg;
  [S.flows, S.dests, S.cards, cfg] = await Promise.all([api("/flows"), api("/destinations").then((d) => d.items), api("/cards"), api("/settings")]);
  S.settings = cfg.settings;
  let current = args[0] === "new" ? null : S.flows.find((f) => f.id === +args[0]) || S.flows[0];
  if (args[0] === "new" || !S.flows.length) current = null;
  $("#main").innerHTML = `
  <div class="page full"><div class="flows" id="flows">
    <aside class="flow-list">
      <h1>Flows</h1><p>Der erste passende Flow gewinnt. Karten mit fest zugewiesenem Flow überspringen die Reihenfolge.</p>
      <div id="flow-items"></div>
      <button class="btn ghost sm" id="new-flow" style="margin:10px 4px">${I("plus")} Neuer Flow</button>
    </aside>
    <section class="editor" id="editor"></section>
  </div></div>`;
  renderFlowList(current && current.id);
  $("#new-flow").onclick = () => location.hash = "#/flows/new";
  if (S.flowEditor) S.flowEditor.destroy();
  const flow = current || { id: null, name: "Neuer Flow", enabled: 1, graph: FlowEditor.starter(S.dests[0] && S.dests[0].id) };
  S.flowEditor = new FlowEditor($("#editor"), {
    flow, dests: S.dests, cards: S.cards, slots: S.live.slots, template: S.settings.default_template,
    onSave: async (data) => {
      const r = data.id ? await put(`/flows/${data.id}`, data) : await post("/flows", data);
      S.flows = await api("/flows");
      renderFlowList(r.id);
      if (!data.id) history.replaceState(null, "", `#/flows/${r.id}`);
      return r.id;
    },
    onDelete: async (id) => {
      if (!(await confirmBox("Flow löschen?", "Karten, denen dieser Flow zugewiesen ist, nutzen danach wieder die automatische Auswahl.", "Löschen", true))) return;
      await guard(() => del(`/flows/${id}`));
      location.hash = "#/flows";
    },
    dryRun: (graph, slot) => post("/flows/dryrun", { graph, slot }),
    toast, confirmBox, openList: () => $("#flows").classList.toggle(innerWidth > 820 ? "list-hidden" : "list-open"),
  });
}
function renderFlowList(activeId) {
  const el = $("#flow-items");
  if (!el) return;
  el.innerHTML = S.flows.map((f, i) => {
    const trig = (f.graph.nodes || []).find((n) => n.type === "trigger") || { config: {} };
    return `<div class="flow-item ${f.id === activeId ? "on" : ""} ${f.enabled ? "" : "off"}" data-id="${f.id}">
      <span class="prio">${i + 1}</span><div style="min-width:0"><b>${esc(f.name)}</b><small>${esc(FlowEditor.triggerText(trig.config, S.cards))}</small></div>
      <span class="ord"><button class="btn ghost sm icon" data-mv="-1" title="Nach oben">${I("up")}</button><button class="btn ghost sm icon" data-mv="1" title="Nach unten">${I("down")}</button></span></div>`;
  }).join("") || `<div class="faint" style="font-size:12.5px;padding:8px">Noch keine Flows.</div>`;
  $$(".flow-item", el).forEach((it) => {
    it.onclick = (e) => {
      if (e.target.closest("[data-mv]")) return;
      $("#flows").classList.remove("list-open");
      location.hash = `#/flows/${it.dataset.id}`;
    };
    $$("[data-mv]", it).forEach((b) => b.onclick = async () => {
      const ids = S.flows.map((f) => f.id);
      const i = ids.indexOf(+it.dataset.id), j = i + +b.dataset.mv;
      if (j < 0 || j >= ids.length) return;
      [ids[i], ids[j]] = [ids[j], ids[i]];
      await guard(() => post("/flows/order", { order: ids }));
      S.flows = await api("/flows");
      renderFlowList(activeId);
    });
  });
}

boot().catch((e) => { document.body.innerHTML = `<div class="auth"><div class="muted">Station nicht erreichbar: ${esc(e.message)}</div></div>`; });
