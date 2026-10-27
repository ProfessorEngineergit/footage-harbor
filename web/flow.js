/* Node-based flow editor. Pointer events only, works with mouse, trackpad and touch. */
"use strict";

const FE_TYPES = {
  trigger: { label: "Karte eingesteckt", kind: "Auslöser", icon: "trigger", desc: "Startpunkt. Legt fest, für welche Karten dieser Flow gilt." },
  filter: { label: "Dateien filtern", kind: "Auswahl", icon: "filter", desc: "Nur bestimmte Dateiarten weitergeben. Alles andere bleibt unberührt auf der Karte." },
  condition: { label: "Bedingung", kind: "Verzweigung", icon: "condition", desc: "Teilt den Ablauf: oberer Ausgang bei Ja, unterer bei Nein." },
  backup: { label: "Sichern", kind: "Ziel", icon: "backup", desc: "Kopiert die Originale an ein Ziel und prüft danach den Inhalt jeder Datei." },
  cleanup: { label: "Karte aufräumen", kind: "Löschen", icon: "cleanup", desc: "Löscht nur Dateien, die an den Zielen davor nachweislich korrekt angekommen sind." },
  notify: { label: "Benachrichtigen", kind: "Meldung", icon: "notify", desc: "Schickt nach dem Lauf eine kurze Nachricht, z. B. an ntfy oder einen Webhook." },
  eject: { label: "Auswerfen", kind: "Abschluss", icon: "eject", desc: "Hängt die Karte nach einem fehlerfreien Lauf sicher aus." },
};
const FE_KINDS = [["photo", "Fotos"], ["raw", "RAW"], ["video", "Videos"], ["audio", "Audio"], ["sidecar", "Begleitdateien"], ["other", "Andere"]];
const FE_TOKENS = ["{year}", "{month}", "{day}", "{date}", "{capture_date}", "{card}", "{label}", "{kind}", "{import}", "{path}", "{folder}", "{name}"];
const FE_FIELDS = { label: ["Label der Karte", "text"], name: ["Name der Karte", "text"], files: ["Anzahl Dateien", "num"], size_gb: ["Datenmenge (GB)", "num"], registered: ["Karte benannt (ja/nein)", "text"] };
const GRID = 10;

class FlowEditor {
  static starter(destId) {
    return {
      nodes: [
        { id: "n1", type: "trigger", x: 80, y: 180, config: { match: "any", start: "auto" } },
        { id: "n2", type: "backup", x: 380, y: 180, config: { destination_id: destId || null, template: "{year}/{date} {card}/{path}" } },
        { id: "n3", type: "eject", x: 680, y: 180, config: {} },
      ],
      edges: [{ id: "e1", from: "n1", fromPort: "out", to: "n2" }, { id: "e2", from: "n2", fromPort: "out", to: "n3" }],
    };
  }

  static triggerText(cfg, cards) {
    const m = cfg.match || "any";
    let t = { any: "Jede Karte", unknown: "Unbekannte Karten" }[m];
    if (m === "label") t = cfg.pattern ? `Label „${cfg.pattern}“` : "Label …";
    if (m === "card") {
      const c = (cards || []).find((x) => x.id === cfg.card_id);
      t = c ? (c.name || c.label) : "Bestimmte Karte";
    }
    return t + (cfg.start === "manual" ? " · per Klick" : "");
  }

  constructor(root, o) {
    this.root = root;
    this.o = o;
    this.flow = JSON.parse(JSON.stringify(o.flow));
    this.g = this.flow.graph;
    this.g.nodes = this.g.nodes || [];
    this.g.edges = this.g.edges || [];
    this.dests = o.dests;
    this.cards = o.cards;
    this.slots = o.slots || [];
    this.sel = null;
    this.view = { x: 0, y: 0, k: 1 };
    this.undo = [];
    this.redo = [];
    this.saveState = this.flow.id ? "saved" : "new";
    this.dry = null;
    this.pointers = new Map();
    this.build();
    this.renderAll();
    this.onKey = this.onKey.bind(this);
    document.addEventListener("keydown", this.onKey);
    this.fitted = false;
    this.ro = new ResizeObserver(() => {
      if (!this.fitted && this.wrap.clientWidth > 0) { this.fitted = true; this.fit(); } else this.applyView();
    });
    this.ro.observe(this.wrap);
  }

  destroy() {
    document.removeEventListener("keydown", this.onKey);
    this.ro.disconnect();
    clearTimeout(this.saveTimer);
    if (this.saveState === "dirty" && this.flow.id) this.save();
  }

  setSlots(slots) {
    const before = this.slots.filter((s) => s.card).map((s) => s.key).join();
    this.slots = slots;
    if (before !== slots.filter((s) => s.card).map((s) => s.key).join() && !this.sel) this.renderInspector();
  }

  // ------------------------------------------------------------- skeleton
  build() {
    const I = (n) => ICONS[n];
    this.root.innerHTML = `
      <div class="ed-bar">
        <button class="btn ghost icon sm fe-list" title="Flow-Liste ein-/ausblenden">${I("menu")}</button>
        <div class="title"><input class="fe-name" maxlength="60" spellcheck="false"><span class="fe-save faint" style="font-size:12px"></span></div>
        <label style="display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--text-2)"><span class="toggle"><input type="checkbox" class="fe-enabled"><i></i></span>Aktiv</label>
        <button class="btn ghost icon sm fe-undo" title="Rückgängig (⌘Z)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/></svg></button>
        <button class="btn ghost icon sm fe-redo" title="Wiederholen (⇧⌘Z)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="m15 14 5-5-5-5"/><path d="M20 9H9.5a5.5 5.5 0 0 0 0 11H13"/></svg></button>
        <button class="btn ghost icon sm danger fe-del" title="Flow löschen">${I("trash")}</button>
        <button class="btn primary sm fe-savebtn">Speichern</button>
      </div>
      <div class="ed-body">
        <div class="canvas-wrap">
          <div class="world"><svg class="edges" width="1" height="1"></svg><div class="nodes"></div></div>
          <div class="palette"><small>Schritte</small>${Object.entries(FE_TYPES).filter(([t]) => t !== "trigger").map(([t, d]) =>
            `<div class="pal-item" data-type="${t}" title="${d.desc}"><span class="node-ico">${I(d.icon)}</span><span>${d.label}</span></div>`).join("")}</div>
          <div class="zoom"><button class="btn ghost icon sm fe-zout">${I("minus")}</button><span class="fe-zl">100%</span><button class="btn ghost icon sm fe-zin">${I("plus")}</button><button class="btn ghost icon sm fe-fit" title="Alles zeigen">${I("fit")}</button></div>
          <div class="hint">Punkt rechts ziehen = verbinden · Doppelklick = Schritt einfügen</div>
        </div>
        <aside class="inspector"></aside>
      </div>`;
    const q = (s) => this.root.querySelector(s);
    this.wrap = q(".canvas-wrap");
    this.world = q(".world");
    this.svg = q(".edges");
    this.nodesEl = q(".nodes");
    this.insp = q(".inspector");
    this.nameIn = q(".fe-name");
    this.nameIn.value = this.flow.name || "";
    this.nameIn.oninput = () => { this.flow.name = this.nameIn.value; this.touch(false); };
    const en = q(".fe-enabled");
    en.checked = !!this.flow.enabled;
    en.onchange = () => { this.flow.enabled = en.checked ? 1 : 0; this.touch(false); };
    q(".fe-savebtn").onclick = () => this.save(true);
    q(".fe-del").onclick = () => this.flow.id ? this.o.onDelete(this.flow.id) : (location.hash = "#/flows");
    q(".fe-undo").onclick = () => this.doUndo();
    q(".fe-redo").onclick = () => this.doRedo();
    q(".fe-zin").onclick = () => this.zoomAt(1.2);
    q(".fe-zout").onclick = () => this.zoomAt(1 / 1.2);
    q(".fe-fit").onclick = () => this.fit();
    q(".fe-list").onclick = () => { this.o.openList(); requestAnimationFrame(() => this.applyView()); };
    this.wrap.addEventListener("pointerdown", (e) => this.onDown(e));
    this.wrap.addEventListener("wheel", (e) => this.onWheel(e), { passive: false });
    this.wrap.addEventListener("dblclick", (e) => {
      if (e.target === this.wrap || e.target === this.world) this.popMenu(e.clientX, e.clientY, null);
    });
    this.root.querySelectorAll(".pal-item").forEach((p) => p.addEventListener("pointerdown", (e) => this.paletteDrag(e, p.dataset.type)));
    this.updateSaveUi();
  }

  // ---------------------------------------------------------- persistence
  snapshot() {
    this.undo.push(JSON.stringify(this.g));
    if (this.undo.length > 80) this.undo.shift();
    this.redo = [];
  }
  doUndo() {
    if (!this.undo.length) return;
    this.redo.push(JSON.stringify(this.g));
    this.g = this.flow.graph = JSON.parse(this.undo.pop());
    this.sel = null;
    this.renderAll();
    this.touch();
  }
  doRedo() {
    if (!this.redo.length) return;
    this.undo.push(JSON.stringify(this.g));
    this.g = this.flow.graph = JSON.parse(this.redo.pop());
    this.sel = null;
    this.renderAll();
    this.touch();
  }
  touch(graphChanged = true) {
    this.saveState = "dirty";
    this.updateSaveUi();
    if (graphChanged) this.dry = null;
    clearTimeout(this.saveTimer);
    if (this.flow.id) this.saveTimer = setTimeout(() => this.save(), 900);
  }
  async save(manual = false) {
    clearTimeout(this.saveTimer);
    this.saveState = "saving";
    this.updateSaveUi();
    try {
      const id = await this.o.onSave({ id: this.flow.id, name: this.flow.name || "Flow", enabled: !!this.flow.enabled, graph: this.g });
      this.flow.id = id;
      this.saveState = "saved";
      if (manual) this.o.toast("Flow gespeichert", "ok");
    } catch (e) {
      this.saveState = "dirty";
      this.o.toast(e.message, "err");
    }
    this.updateSaveUi();
  }
  updateSaveUi() {
    const s = this.root.querySelector(".fe-save");
    const b = this.root.querySelector(".fe-savebtn");
    if (!s) return;
    s.textContent = { saved: "Gespeichert", saving: "Speichert …", dirty: this.flow.id ? "Ungespeichert" : "", new: "Noch nicht gespeichert" }[this.saveState];
    s.style.color = this.saveState === "dirty" ? "var(--warn)" : "";
    b.style.display = this.flow.id && this.saveState !== "dirty" && this.saveState !== "new" ? "none" : "";
    this.root.querySelector(".fe-undo").disabled = !this.undo.length;
    this.root.querySelector(".fe-redo").disabled = !this.redo.length;
  }

  // ----------------------------------------------------------------- view
  applyView() {
    const { x, y, k } = this.view;
    this.world.style.transform = `translate(${x}px, ${y}px) scale(${k})`;
    const gs = 24 * k;
    this.wrap.style.backgroundImage = `radial-gradient(circle at 1px 1px, #252727 1.1px, transparent 1.3px)`;
    this.wrap.style.backgroundSize = `${gs}px ${gs}px`;
    this.wrap.style.backgroundPosition = `${x}px ${y}px`;
    const zl = this.root.querySelector(".fe-zl");
    if (zl) zl.textContent = `${Math.round(k * 100)}%`;
  }
  toWorld(cx, cy) {
    const r = this.wrap.getBoundingClientRect();
    return { x: (cx - r.left - this.view.x) / this.view.k, y: (cy - r.top - this.view.y) / this.view.k };
  }
  zoomAt(f, cx, cy) {
    const r = this.wrap.getBoundingClientRect();
    cx = cx ?? r.left + r.width / 2;
    cy = cy ?? r.top + r.height / 2;
    const k = Math.min(2, Math.max(0.3, this.view.k * f));
    const px = cx - r.left, py = cy - r.top;
    this.view.x = px - ((px - this.view.x) * k) / this.view.k;
    this.view.y = py - ((py - this.view.y) * k) / this.view.k;
    this.view.k = k;
    this.applyView();
  }
  fit() {
    const r = this.wrap.getBoundingClientRect();
    if (!this.g.nodes.length || !r.width) return this.applyView();
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of this.g.nodes) {
      const el = this.nodeEl(n.id);
      x0 = Math.min(x0, n.x); y0 = Math.min(y0, n.y);
      x1 = Math.max(x1, n.x + 236); y1 = Math.max(y1, n.y + (el ? el.offsetHeight : 120));
    }
    const padL = 30;
    const w = x1 - x0, h = y1 - y0;
    const k = Math.min(1, Math.max(0.45, Math.min((r.width - padL - 60) / w, (r.height - 260) / h)));
    this.view.k = k;
    this.view.x = padL + (r.width - padL - w * k) / 2 - x0 * k;
    this.view.y = (r.height - h * k) / 2 - y0 * k;
    this.applyView();
  }
  onWheel(e) {
    e.preventDefault();
    if (e.ctrlKey || e.metaKey) {
      this.zoomAt(Math.exp(-e.deltaY * 0.01), e.clientX, e.clientY);
    } else {
      this.view.x -= e.deltaX;
      this.view.y -= e.deltaY;
      this.applyView();
    }
  }

  // -------------------------------------------------------------- render
  nodeEl(id) { return this.nodesEl.querySelector(`[data-id="${CSS.escape(id)}"]`); }
  node(id) { return this.g.nodes.find((n) => n.id === id); }
  dest(id) { return this.dests.find((d) => d.id === id); }

  renderAll() {
    this.nodesEl.innerHTML = "";
    for (const n of this.g.nodes) this.nodesEl.append(this.makeNode(n));
    this.analyse();
    this.renderEdges();
    this.renderInspector();
    this.applyView();
    this.updateSaveUi();
  }

  makeNode(n) {
    const t = FE_TYPES[n.type];
    const el = document.createElement("div");
    el.className = "node";
    el.dataset.id = n.id;
    el.dataset.type = n.type;
    el.style.left = n.x + "px";
    el.style.top = n.y + "px";
    el.innerHTML = `
      <div class="node-h"><span class="node-ico">${ICONS[t.icon]}</span><div class="node-t"><small>${t.kind}</small><b>${t.label}</b></div></div>
      <div class="node-b"></div>
      ${n.type !== "trigger" ? `<span class="port in" data-port="in"></span>` : ""}
      ${n.type === "condition"
        ? `<span class="port out yes" data-port="yes"></span><span class="port-lbl yes">Ja</span><span class="port out no" data-port="no"></span><span class="port-lbl no">Nein</span>`
        : `<span class="port out" data-port="out"></span>`}`;
    this.fillNode(el, n);
    return el;
  }

  summary(n) {
    const c = n.config || {};
    const esc2 = (s) => String(s ?? "").replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
    switch (n.type) {
      case "trigger": return `<div>${esc2(FlowEditor.triggerText(c, this.cards))}</div>`;
      case "filter": {
        const k = (c.kinds || []).map((x) => (FE_KINDS.find((y) => y[0] === x) || [0, x])[1]);
        return `<div>${k.length ? esc2(k.join(", ")) : "Alle Dateien"}${c.include ? ` + ${esc2(c.include)}` : ""}</div>${c.exclude ? `<div class="faint">ohne ${esc2(c.exclude)}</div>` : ""}`;
      }
      case "condition": {
        const f = FE_FIELDS[c.field || "label"];
        const op = { matches: "passt auf", gt: ">", lt: "<" }[c.op || "matches"];
        return `<div>${esc2(f ? f[0] : c.field)} ${op}</div><div class="mono">${esc2(c.value || "–")}</div>`;
      }
      case "backup": {
        const d = this.dest(c.destination_id);
        return d ? `<div>${esc2(d.name)}</div><div class="mono">${esc2(c.template || "")}</div>` : `<div class="warnline">Kein Ziel gewählt</div>`;
      }
      case "cleanup": {
        const req = c.require === "count" ? `an mind. ${c.count || 1} Zielen` : "an allen Zielen davor";
        return `<div>Nach Prüfung ${req}</div><div class="faint">${c.confirm === false ? "ohne Rückfrage" : "mit Bestätigung"}</div>`;
      }
      case "notify": {
        let host = "";
        try { host = c.url ? new URL(c.url).host + new URL(c.url).pathname : ""; } catch (_) { host = c.url; }
        return host ? `<div class="mono">${esc2(host)}</div>` : `<div class="warnline">Keine Adresse</div>`;
      }
      case "eject": return `<div>Sicher aushängen</div>`;
    }
    return "";
  }

  fillNode(el, n) {
    el.querySelector(".node-b").innerHTML = this.summary(n);
    let st = el.querySelector(".node-stat");
    const d = this.dry && this.dry.steps.find((s) => s.id === n.id);
    if (d) {
      if (!st) { st = document.createElement("div"); st.className = "node-stat"; el.querySelector(".node-b").after(st); }
      const nfm = new Intl.NumberFormat("de-DE");
      st.textContent = n.type === "backup"
        ? `${nfm.format(d.files)} Dateien · ${d.already ? nfm.format(d.already) + " schon da" : "alle neu"}`
        : `${nfm.format(d.files)} Dateien`;
    } else if (st) st.remove();
    el.classList.toggle("sel", !!this.sel && this.sel.kind === "node" && this.sel.id === n.id);
  }

  refreshNode(id) {
    const n = this.node(id), el = this.nodeEl(id);
    if (n && el) this.fillNode(el, n);
  }

  portPos(nodeId, port) {
    const n = this.node(nodeId), el = this.nodeEl(nodeId);
    if (!n || !el) return { x: 0, y: 0 };
    const p = el.querySelector(`[data-port="${port}"]`);
    if (!p) return { x: n.x, y: n.y + 29 };
    return { x: n.x + p.offsetLeft + 7, y: n.y + p.offsetTop + 7 };
  }

  curve(a, b) {
    const dx = Math.max(50, Math.abs(b.x - a.x) * 0.5);
    return `M${a.x},${a.y} C${a.x + dx},${a.y} ${b.x - dx},${b.y} ${b.x},${b.y}`;
  }

  renderEdges() {
    const parts = [];
    for (const e of this.g.edges) {
      const a = this.portPos(e.from, e.fromPort || "out");
      const b = this.portPos(e.to, "in");
      const d = this.curve(a, b);
      const cls = [this.sel && this.sel.kind === "edge" && this.sel.id === e.id ? "sel" : "", e.fromPort === "yes" ? "yes" : e.fromPort === "no" ? "no" : ""].join(" ");
      parts.push(`<g class="${cls}" data-edge="${e.id}"><path class="hit" d="${d}"/><path class="e" d="${d}"/></g>`);
    }
    if (this.ghost) parts.push(`<path class="ghost" d="${this.curve(this.ghost.a, this.ghost.b)}"/>`);
    this.svg.innerHTML = parts.join("");
    this.world.querySelectorAll(".edge-x").forEach((x) => x.remove());
    if (this.sel && this.sel.kind === "edge") {
      const e = this.g.edges.find((x) => x.id === this.sel.id);
      if (e) {
        const a = this.portPos(e.from, e.fromPort || "out"), b = this.portPos(e.to, "in");
        const btn = document.createElement("button");
        btn.className = "edge-x";
        btn.title = "Verbindung entfernen";
        btn.innerHTML = ICONS.x;
        btn.style.left = (a.x + b.x) / 2 + "px";
        btn.style.top = (a.y + b.y) / 2 + "px";
        btn.addEventListener("pointerdown", (ev) => { ev.stopPropagation(); this.removeEdge(e.id); });
        this.world.append(btn);
      }
    }
  }

  // reachable from trigger (both branches), issues per node
  analyse() {
    const trig = this.g.nodes.find((n) => n.type === "trigger");
    const reach = new Set();
    const preds = {};
    for (const e of this.g.edges) (preds[e.to] = preds[e.to] || []).push(e.from);
    if (trig) {
      const stack = [trig.id];
      while (stack.length) {
        const id = stack.pop();
        if (reach.has(id)) continue;
        reach.add(id);
        this.g.edges.filter((e) => e.from === id).forEach((e) => stack.push(e.to));
      }
    }
    const ancestors = (id) => {
      const res = new Set(), st = [...(preds[id] || [])];
      while (st.length) { const p = st.pop(); if (!res.has(p)) { res.add(p); st.push(...(preds[p] || [])); } }
      return res;
    };
    const issues = [];
    const bad = new Set();
    for (const n of this.g.nodes) {
      const t = FE_TYPES[n.type].label;
      if (!reach.has(n.id)) issues.push(["warn", `„${t}“ ist nicht mit dem Auslöser verbunden und wird nie ausgeführt.`, n.id]);
      if (n.type === "backup" && !this.dest(n.config.destination_id)) { issues.push(["err", "Ein Sicherungsschritt hat kein Ziel.", n.id]); bad.add(n.id); }
      if (n.type === "notify" && !(n.config.url || "").startsWith("http")) { issues.push(["warn", "Benachrichtigung ohne gültige Adresse.", n.id]); bad.add(n.id); }
      if (n.type === "cleanup") {
        const up = [...ancestors(n.id)].map((id) => this.node(id)).filter((x) => x && x.type === "backup");
        if (!up.length) { issues.push(["err", "„Karte aufräumen“ braucht vorher mindestens einen Sicherungsschritt.", n.id]); bad.add(n.id); }
        else {
          const ids = new Set(up.map((u) => u.config.destination_id));
          const need = n.config.require === "count" ? Math.min(+n.config.count || 1, ids.size) : ids.size;
          if (need < 2) issues.push(["warn", "Aufräumen nach nur einer Kopie. Empfohlen sind zwei unabhängige Ziele (z. B. SSD + Cloud).", n.id]);
        }
      }
      if (n.type === "condition") {
        const outs = this.g.edges.filter((e) => e.from === n.id).map((e) => e.fromPort);
        if (!outs.includes("yes") && !outs.includes("no")) issues.push(["warn", "Bedingung ohne Ausgang.", n.id]);
      }
    }
    if (!this.g.nodes.some((n) => n.type === "backup" && reach.has(n.id))) issues.unshift(["warn", "Dieser Flow sichert nichts – es fehlt ein Schritt „Sichern“.", null]);
    for (const n of this.g.nodes) {
      const el = this.nodeEl(n.id);
      if (!el) continue;
      el.classList.toggle("unreached", !reach.has(n.id));
      el.classList.toggle("bad", bad.has(n.id));
    }
    this.issues = issues;
  }

  // -------------------------------------------------------------- inspector
  renderInspector() {
    const esc2 = (s) => String(s ?? "").replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
    const n = this.sel && this.sel.kind === "node" ? this.node(this.sel.id) : null;
    if (!n) {
      const withCard = this.slots.filter((s) => s.card);
      this.insp.innerHTML = `
        <h3>Ablauf</h3><p>Verbinde die Schritte von links nach rechts. Ein Schritt bekommt die Dateien vom Schritt davor.</p>
        ${this.issues.length ? `<h2>Hinweise</h2><ul class="issues">${this.issues.map(([k, t, id]) => `<li class="${k}" ${id ? `data-goto="${id}" style="cursor:pointer"` : ""}>${esc2(t)}</li>`).join("")}</ul>` : `<div class="pill ok" style="margin-bottom:4px"><i></i>Keine Probleme gefunden</div>`}
        <div class="sep"></div>
        <h2>Testlauf</h2>
        <p class="faint" style="font-size:12.5px;margin:-4px 0 12px">Zeigt, was mit einer eingesteckten Karte passieren würde. Es wird nichts kopiert oder gelöscht.</p>
        ${withCard.length ? `<div style="display:flex;gap:6px"><select class="input fe-dry-slot">${withCard.map((s) => `<option value="${esc2(s.key)}">Slot ${s.number} · ${esc2(s.card.name || s.card.label)}</option>`).join("")}</select><button class="btn sm fe-dry" style="height:36px">${ICONS.play}</button></div>` : `<div class="faint" style="font-size:12.5px">Steck eine Karte ein, um den Flow zu testen.</div>`}
        <div class="fe-dry-out"></div>
        <div class="sep"></div>
        <h2>Tastatur</h2>
        <dl class="kv" style="font-size:12px"><dt>Löschen</dt><dd class="mono">Entf</dd><dt>Duplizieren</dt><dd class="mono">⌘D</dd><dt>Rückgängig</dt><dd class="mono">⌘Z</dd><dt>Schritt einfügen</dt><dd>Doppelklick</dd><dt>Zoomen</dt><dd>⌘ + Scrollen</dd></dl>`;
      this.insp.querySelectorAll("[data-goto]").forEach((li) => li.onclick = () => this.select({ kind: "node", id: li.dataset.goto }));
      const btn = this.insp.querySelector(".fe-dry");
      if (btn) btn.onclick = () => this.runDry(this.insp.querySelector(".fe-dry-slot").value);
      if (this.dry) this.renderDry();
      return;
    }
    const t = FE_TYPES[n.type];
    const c = n.config;
    let form = "";
    const seg = (key, opts, val) => `<div class="seg wide" data-seg="${key}">${opts.map(([v, l]) => `<button type="button" data-v="${v}" class="${String(val) === String(v) ? "on" : ""}">${l}</button>`).join("")}</div>`;
    switch (n.type) {
      case "trigger":
        form = `<div class="field"><span>Gilt für</span>${seg("match", [["any", "Jede"], ["unknown", "Unbekannte"], ["label", "Label"], ["card", "Karte"]], c.match || "any")}</div>
          ${c.match === "label" ? `<label class="field"><span>Label oder Name</span><input class="input mono" data-k="pattern" value="${esc2(c.pattern || "")}" placeholder="SONY*, DJI_??"><small>Mehrere mit Komma. * und ? als Platzhalter.</small></label>` : ""}
          ${c.match === "card" ? `<label class="field"><span>Karte</span><select class="input" data-k="card_id" data-num>${this.cards.length ? this.cards.map((x) => `<option value="${x.id}" ${x.id === c.card_id ? "selected" : ""}>${esc2(x.name || x.label)}</option>`).join("") : `<option>Noch keine Karten bekannt</option>`}</select></label>` : ""}
          <div class="field"><span>Start</span>${seg("start", [["auto", "Automatisch"], ["manual", "Per Klick"]], c.start || "auto")}<small>${c.start === "manual" ? "Die Karte wartet im Dock, bis du „Jetzt sichern“ drückst." : "Beginnt sofort nach dem Einstecken."}</small></div>`;
        break;
      case "filter":
        form = `<div class="field"><span>Dateiarten</span>${FE_KINDS.map(([k, l]) => `<label class="check"><input type="checkbox" data-kind="${k}" ${(c.kinds || []).includes(k) ? "checked" : ""}><span class="box"></span><span><b>${l}</b></span></label>`).join("")}<small>Nichts angehakt = alle Dateien.</small></div>
          <label class="field"><span>Zusätzlich diese Endungen</span><input class="input mono" data-k="include" value="${esc2(c.include || "")}" placeholder="pdf, txt"></label>
          <label class="field"><span>Ausschließen</span><input class="input mono" data-k="exclude" value="${esc2(c.exclude || "")}" placeholder="lrv, thm"><small>Ausgeschlossene Dateien bleiben unverändert auf der Karte.</small></label>`;
        break;
      case "condition": {
        const f = FE_FIELDS[c.field || "label"];
        form = `<label class="field"><span>Wenn</span><select class="input" data-k="field">${Object.entries(FE_FIELDS).map(([k, [l]]) => `<option value="${k}" ${k === (c.field || "label") ? "selected" : ""}>${l}</option>`).join("")}</select></label>
          ${f[1] === "num" ? `<div class="field"><span>ist</span>${seg("op", [["gt", "größer als"], ["lt", "kleiner als"]], c.op === "lt" ? "lt" : "gt")}</div>` : `<div class="field"><span>passt auf</span></div>`}
          <label class="field"><input class="input mono" data-k="value" value="${esc2(c.value || "")}" placeholder="${f[1] === "num" ? "100" : "SONY*, A7*"}"></label>
          <p class="faint" style="font-size:12.5px;margin:0">Ja → oberer Ausgang (grün). Nein → unterer Ausgang (rot).</p>`;
        break;
      }
      case "backup": {
        const d = this.dest(c.destination_id);
        const ex = { year: "2026", month: "10", day: "27", date: new Date().toISOString().slice(0, 10), capture_date: "2026-10-27", card: "Sony A", label: "SONY_A", kind: "RAW", import: "0042", path: "DCIM/100MSDCF/DSC01234.ARW", folder: "DCIM/100MSDCF", name: "DSC01234.ARW" };
        const prev = (c.template || "").replace(/\{(\w+)\}/g, (m, k) => ex[k] ?? m);
        form = `<label class="field"><span>Ziel</span><select class="input" data-k="destination_id" data-num><option value="">Ziel wählen …</option>${this.dests.map((x) => `<option value="${x.id}" ${x.id === c.destination_id ? "selected" : ""}>${esc2(x.name)}</option>`).join("")}</select>
            <small>${this.dests.length ? "" : "Noch keine Ziele. "}<a href="#/storage">Ziel hinzufügen</a></small></label>
          <label class="field"><span>Ordnerstruktur</span><input class="input mono" data-k="template" value="${esc2(c.template || "")}"></label>
          <div class="tokens">${FE_TOKENS.map((tk) => `<button type="button" data-token="${tk}">${tk}</button>`).join("")}</div>
          <div class="field" style="margin-top:12px"><span>Beispiel</span><div class="preview fe-prev">${esc2(prev)}</div></div>
          <div class="sep"></div>
          <h2>Prüfung</h2><p class="faint" style="font-size:12.5px;margin:0">${!d ? "Wähle ein Ziel." : d.kind === "local"
            ? "Jede Kopie wird nach dem Schreiben vollständig zurückgelesen und per SHA-256 mit dem Original verglichen."
            : "Nach dem Upload wird die Prüfsumme des Anbieters verglichen. Bietet der Anbieter keine passende an, wird die Datei wieder heruntergeladen und verglichen."}
            Gleicher Name mit anderem Inhalt wird nie überschrieben.</p>`;
        break;
      }
      case "cleanup":
        form = `<div class="field"><span>Löschen, wenn geprüft an</span>${seg("require", [["all", "allen Zielen davor"], ["count", "mindestens …"]], c.require || "all")}</div>
          ${c.require === "count" ? `<label class="field"><span>Anzahl Ziele</span><input class="input" type="number" min="1" max="5" data-k="count" data-num value="${c.count || 2}"></label>` : ""}
          <label class="check" style="margin-bottom:12px"><input type="checkbox" data-bool="confirm" ${c.confirm !== false ? "checked" : ""}><span class="box"></span><span><b>Vorher bestätigen</b><small>Im Dock erscheint „Löschen freigeben“.</small></span></label>
          <div class="sep"></div><h2>Was immer gilt</h2>
          <ul class="issues" style="font-size:12px">
            <li style="color:var(--text-2)">Gelöscht werden nur Dateien, deren Kopien in diesem Lauf inhaltlich geprüft wurden.</li>
            <li style="color:var(--text-2)">Jede Datei auf der Karte wird direkt vor dem Löschen erneut gehasht.</li>
            <li style="color:var(--text-2)">Ausgefilterte, neue oder veränderte Dateien bleiben. Ordner bleiben. Die Karte wird nie formatiert.</li>
            <li style="color:var(--text-2)">Wird die Karte gezogen, stoppt alles sofort.</li></ul>`;
        break;
      case "notify":
        form = `<label class="field"><span>Adresse</span><input class="input mono" data-k="url" value="${esc2(c.url || "")}" placeholder="https://ntfy.sh/mein-harbor"><small>Es wird eine kurze Textnachricht per POST gesendet – passt zu ntfy, Gotify-Proxys und den meisten Webhooks.</small></label>`;
        break;
      case "eject":
        form = `<p class="faint" style="font-size:12.5px;margin:0">Nach einem fehlerfreien Lauf wird die Karte ausgehängt. Danach kann sie ohne Risiko entnommen werden. Gab es Fehler, bleibt sie eingebunden.</p>`;
        break;
    }
    this.insp.innerHTML = `
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px"><span class="node-ico" style="width:30px;height:30px;border-radius:8px;display:grid;place-items:center;background:var(--panel-3)">${ICONS[t.icon]}</span><h3>${t.label}</h3></div>
      <p>${t.desc}</p>${form}
      ${n.type !== "trigger" ? `<div class="sep"></div><div style="display:flex;gap:6px"><button class="btn sm fe-dup">${ICONS.duplicate} Duplizieren</button><button class="btn sm ghost danger fe-rm">${ICONS.trash} Entfernen</button></div>` : ""}`;
    this.bindInspector(n);
  }

  bindInspector(n) {
    const c = n.config;
    const changed = (rerender) => {
      this.refreshNode(n.id);
      this.analyse();
      this.renderEdges();
      this.touch();
      if (rerender) this.renderInspector();
    };
    let snapped = false;
    const snapOnce = () => { if (!snapped) { this.snapshot(); snapped = true; } };
    this.insp.querySelectorAll("[data-seg]").forEach((sg) => sg.querySelectorAll("button").forEach((b) => b.onclick = () => {
      snapOnce();
      c[sg.dataset.seg] = b.dataset.v;
      if (sg.dataset.seg === "match" && b.dataset.v === "card" && !c.card_id && this.cards[0]) c.card_id = this.cards[0].id;
      changed(true);
    }));
    this.insp.querySelectorAll("[data-k]").forEach((inp) => {
      const ev = inp.tagName === "SELECT" ? "change" : "input";
      inp.addEventListener(ev, () => {
        snapOnce();
        c[inp.dataset.k] = "num" in inp.dataset ? (inp.value === "" ? null : +inp.value) : inp.value;
        if (inp.dataset.k === "template") this.updatePreview(c);
        changed(inp.tagName === "SELECT");
      });
    });
    this.insp.querySelectorAll("[data-kind]").forEach((cb) => cb.onchange = () => {
      snapOnce();
      const set = new Set(c.kinds || []);
      cb.checked ? set.add(cb.dataset.kind) : set.delete(cb.dataset.kind);
      c.kinds = FE_KINDS.map(([k]) => k).filter((k) => set.has(k));
      changed(false);
    });
    this.insp.querySelectorAll("[data-bool]").forEach((cb) => cb.onchange = () => { snapOnce(); c[cb.dataset.bool] = cb.checked; changed(false); });
    this.insp.querySelectorAll("[data-token]").forEach((b) => b.onclick = () => {
      const inp = this.insp.querySelector('[data-k="template"]');
      const pos = inp.selectionStart ?? inp.value.length;
      inp.value = inp.value.slice(0, pos) + b.dataset.token + inp.value.slice(inp.selectionEnd ?? pos);
      inp.dispatchEvent(new Event("input"));
      inp.focus();
    });
    const dup = this.insp.querySelector(".fe-dup");
    if (dup) dup.onclick = () => this.duplicate(n.id);
    const rm = this.insp.querySelector(".fe-rm");
    if (rm) rm.onclick = () => this.removeNode(n.id);
  }

  updatePreview(c) {
    const el = this.insp.querySelector(".fe-prev");
    if (!el) return;
    const ex = { year: "2026", month: "10", day: "27", date: new Date().toISOString().slice(0, 10), capture_date: "2026-10-27", card: "Sony A", label: "SONY_A", kind: "RAW", import: "0042", path: "DCIM/100MSDCF/DSC01234.ARW", folder: "DCIM/100MSDCF", name: "DSC01234.ARW" };
    el.textContent = (c.template || "").replace(/\{(\w+)\}/g, (m, k) => ex[k] ?? m);
  }

  async runDry(slot) {
    const out = this.insp.querySelector(".fe-dry-out");
    out.innerHTML = `<div class="faint" style="font-size:12.5px;margin-top:10px">Liest die Karte …</div>`;
    try {
      this.dry = await this.o.dryRun(this.g, slot);
      this.renderDry();
      this.g.nodes.forEach((n) => this.refreshNode(n.id));
      this.renderEdges();
    } catch (e) {
      out.innerHTML = `<div style="color:var(--err);font-size:12.5px;margin-top:10px">${e.message}</div>`;
    }
  }
  renderDry() {
    const out = this.insp.querySelector(".fe-dry-out");
    if (!out || !this.dry) return;
    const nfm = new Intl.NumberFormat("de-DE");
    const bytes = (b) => (b >= 1e9 ? `${(b / 1e9).toFixed(1).replace(".", ",")} GB` : `${Math.round(b / 1e6)} MB`);
    out.innerHTML = `<ul class="dry"><li><span>${this.dry.card}</span><span class="num">${nfm.format(this.dry.total)} Dateien</span></li>
      ${this.dry.steps.filter((s) => s.type !== "trigger").map((s) => `<li><span>${FE_TYPES[s.type].label}${s.destination ? " · " + s.destination : ""}</span><span class="num muted">${nfm.format(s.files)} · ${bytes(s.bytes)}</span></li>`).join("")}
      ${this.dry.unreached.length ? `<li><span class="faint">Nicht erreicht</span><span class="faint">${this.dry.unreached.length} Schritte</span></li>` : ""}</ul>`;
  }

  // ------------------------------------------------------------- mutations
  uid(p) { return p + Math.random().toString(36).slice(2, 8); }

  addNode(type, x, y, connectFrom) {
    this.snapshot();
    const defaults = {
      filter: { kinds: [], include: "", exclude: "" },
      condition: { field: "label", op: "matches", value: "" },
      backup: { destination_id: (this.dests[0] || {}).id || null, template: this.o.template || "{year}/{date} {card}/{path}" },
      cleanup: { require: "all", count: 2, confirm: true },
      notify: { url: "" },
      eject: {},
    };
    const n = { id: this.uid("n"), type, x: Math.round(x / GRID) * GRID, y: Math.round(y / GRID) * GRID, config: JSON.parse(JSON.stringify(defaults[type] || {})) };
    this.g.nodes.push(n);
    this.nodesEl.append(this.makeNode(n));
    if (connectFrom) this.g.edges.push({ id: this.uid("e"), from: connectFrom.node, fromPort: connectFrom.port, to: n.id });
    this.select({ kind: "node", id: n.id });
    this.touch();
    return n;
  }

  removeNode(id) {
    const n = this.node(id);
    if (!n || n.type === "trigger") return;
    this.snapshot();
    // keep the chain intact: A → X → B becomes A → B
    const ins = this.g.edges.filter((e) => e.to === id);
    const outs = this.g.edges.filter((e) => e.from === id);
    this.g.edges = this.g.edges.filter((e) => e.to !== id && e.from !== id);
    if (ins.length === 1 && outs.length === 1 && n.type !== "condition") {
      const a = ins[0], b = outs[0];
      if (!this.g.edges.some((e) => e.from === a.from && e.fromPort === a.fromPort && e.to === b.to))
        this.g.edges.push({ id: this.uid("e"), from: a.from, fromPort: a.fromPort, to: b.to });
    }
    this.g.nodes = this.g.nodes.filter((x) => x.id !== id);
    this.sel = null;
    this.renderAll();
    this.touch();
  }

  duplicate(id) {
    const n = this.node(id);
    if (!n || n.type === "trigger") return;
    this.snapshot();
    const copy = { ...JSON.parse(JSON.stringify(n)), id: this.uid("n"), x: n.x + 30, y: n.y + 150 };
    this.g.nodes.push(copy);
    this.nodesEl.append(this.makeNode(copy));
    this.select({ kind: "node", id: copy.id });
    this.touch();
  }

  removeEdge(id) {
    this.snapshot();
    this.g.edges = this.g.edges.filter((e) => e.id !== id);
    this.sel = null;
    this.analyse();
    this.renderEdges();
    this.renderInspector();
    this.touch();
  }

  createsCycle(from, to) {
    const stack = [to], seen = new Set();
    while (stack.length) {
      const id = stack.pop();
      if (id === from) return true;
      if (seen.has(id)) continue;
      seen.add(id);
      this.g.edges.filter((e) => e.from === id).forEach((e) => stack.push(e.to));
    }
    return false;
  }

  connect(from, port, to) {
    if (from === to) return false;
    if (this.g.edges.some((e) => e.from === from && (e.fromPort || "out") === port && e.to === to)) return false;
    if (this.createsCycle(from, to)) { this.o.toast("Kreise sind nicht möglich – ein Flow läuft immer vorwärts.", "err"); return false; }
    this.snapshot();
    this.g.edges.push({ id: this.uid("e"), from, fromPort: port, to });
    this.analyse();
    this.renderEdges();
    this.touch();
    return true;
  }

  select(sel) {
    this.sel = sel;
    this.g.nodes.forEach((n) => { const el = this.nodeEl(n.id); if (el) el.classList.toggle("sel", !!sel && sel.kind === "node" && sel.id === n.id); });
    this.renderEdges();
    this.renderInspector();
  }

  // -------------------------------------------------------------- pointer
  onDown(e) {
    if (e.button !== undefined && e.button > 1) return;
    if (e.target.closest(".palette, .zoom, .popmenu, .edge-x")) return;
    this.closePop();
    this.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (this.pointers.size === 2) return this.startPinch();

    const port = e.target.closest(".port");
    const nodeEl = e.target.closest(".node");
    const edgeG = e.target.closest("[data-edge]");
    if (port && port.dataset.port !== "in") return this.startConnect(e, nodeEl.dataset.id, port.dataset.port);
    if (port && port.dataset.port === "in") {
      const existing = this.g.edges.filter((x) => x.to === nodeEl.dataset.id);
      if (existing.length) {
        const ed = existing[existing.length - 1];
        this.snapshot();
        this.g.edges = this.g.edges.filter((x) => x.id !== ed.id);
        return this.startConnect(e, ed.from, ed.fromPort || "out", true);
      }
      return;
    }
    if (nodeEl) return this.startDrag(e, nodeEl.dataset.id);
    if (edgeG) { this.select({ kind: "edge", id: edgeG.dataset.edge }); return; }
    this.startPan(e);
  }

  track(e, move, up) {
    const mv = (ev) => { if (ev.pointerId === e.pointerId) { this.pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY }); move(ev); } };
    const end = (ev) => {
      if (ev.pointerId !== e.pointerId) return;
      this.pointers.delete(ev.pointerId);
      window.removeEventListener("pointermove", mv);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", end);
      up(ev);
    };
    window.addEventListener("pointermove", mv);
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
  }

  startPan(e) {
    const sx = e.clientX, sy = e.clientY, vx = this.view.x, vy = this.view.y;
    let moved = false;
    this.wrap.classList.add("panning");
    this.track(e, (ev) => {
      if (this.pinching) return;
      if (Math.abs(ev.clientX - sx) + Math.abs(ev.clientY - sy) > 3) moved = true;
      this.view.x = vx + ev.clientX - sx;
      this.view.y = vy + ev.clientY - sy;
      this.applyView();
    }, () => {
      this.wrap.classList.remove("panning");
      if (!moved && this.sel) this.select(null);
    });
  }

  startPinch() {
    const [a, b] = [...this.pointers.values()];
    const d0 = Math.hypot(a.x - b.x, a.y - b.y), k0 = this.view.k;
    this.pinching = true;
    const mv = () => {
      const [p, q] = [...this.pointers.values()];
      if (!p || !q) return;
      const f = (Math.hypot(p.x - q.x, p.y - q.y) / d0) * k0 / this.view.k;
      this.zoomAt(f, (p.x + q.x) / 2, (p.y + q.y) / 2);
    };
    const up = () => {
      if (this.pointers.size < 2) {
        this.pinching = false;
        window.removeEventListener("pointermove", mv);
        window.removeEventListener("pointerup", up);
      }
    };
    window.addEventListener("pointermove", mv);
    window.addEventListener("pointerup", up);
  }

  startDrag(e, id) {
    const n = this.node(id);
    const el = this.nodeEl(id);
    const start = this.toWorld(e.clientX, e.clientY);
    const ox = n.x, oy = n.y;
    let moved = false;
    if (!this.sel || this.sel.id !== id) this.select({ kind: "node", id });
    const before = JSON.stringify(this.g);
    this.track(e, (ev) => {
      if (this.pinching) return;
      const p = this.toWorld(ev.clientX, ev.clientY);
      const nx = Math.round((ox + p.x - start.x) / GRID) * GRID;
      const ny = Math.round((oy + p.y - start.y) / GRID) * GRID;
      if (nx !== n.x || ny !== n.y) {
        moved = true;
        n.x = nx; n.y = ny;
        el.style.left = nx + "px";
        el.style.top = ny + "px";
        this.renderEdges();
      }
    }, () => {
      if (moved) {
        this.undo.push(before);
        this.redo = [];
        this.saveState = "dirty";
        this.touch(false);
      }
    });
  }

  startConnect(e, fromId, port, reattach = false) {
    e.stopPropagation();
    const a = this.portPos(fromId, port);
    this.ghost = { a, b: this.toWorld(e.clientX, e.clientY) };
    this.renderEdges();
    let hot = null;
    this.track(e, (ev) => {
      this.ghost.b = this.toWorld(ev.clientX, ev.clientY);
      const t = document.elementFromPoint(ev.clientX, ev.clientY);
      const target = t && (t.closest(".port.in") || (t.closest(".node") && t.closest(".node").querySelector(".port.in")));
      if (hot && hot !== target) hot.classList.remove("hot");
      hot = target && target.closest(".node").dataset.id !== fromId ? target : null;
      if (hot) {
        hot.classList.add("hot");
        const tid = hot.closest(".node").dataset.id;
        this.ghost.b = this.portPos(tid, "in");
      }
      this.renderEdges();
    }, (ev) => {
      this.ghost = null;
      if (hot) {
        hot.classList.remove("hot");
        const ok = this.connect(fromId, port, hot.closest(".node").dataset.id);
        if (!ok && reattach) { this.analyse(); this.renderEdges(); this.touch(); }
      } else {
        const t = document.elementFromPoint(ev.clientX, ev.clientY);
        if (reattach) { this.analyse(); this.renderEdges(); this.renderInspector(); this.touch(); }
        else if (t && this.wrap.contains(t) && !t.closest(".node, .palette, .zoom")) this.popMenu(ev.clientX, ev.clientY, { node: fromId, port });
        else this.renderEdges();
      }
      this.renderEdges();
    });
  }

  paletteDrag(e, type) {
    e.preventDefault();
    const sx = e.clientX, sy = e.clientY;
    let ghost = null;
    this.track(e, (ev) => {
      if (!ghost && Math.abs(ev.clientX - sx) + Math.abs(ev.clientY - sy) > 5) {
        ghost = document.createElement("div");
        ghost.className = "node";
        ghost.dataset.type = type;
        ghost.style.cssText = "position:fixed;pointer-events:none;opacity:.85;z-index:40;width:236px";
        ghost.innerHTML = `<div class="node-h"><span class="node-ico">${ICONS[FE_TYPES[type].icon]}</span><div class="node-t"><small>${FE_TYPES[type].kind}</small><b>${FE_TYPES[type].label}</b></div></div>`;
        document.body.append(ghost);
      }
      if (ghost) { ghost.style.left = ev.clientX - 40 + "px"; ghost.style.top = ev.clientY - 20 + "px"; }
    }, (ev) => {
      if (ghost) {
        ghost.remove();
        const r = this.wrap.getBoundingClientRect();
        if (ev.clientX > r.left && ev.clientX < r.right && ev.clientY > r.top && ev.clientY < r.bottom) {
          const p = this.toWorld(ev.clientX - 40, ev.clientY - 20);
          this.addNode(type, p.x, p.y, null);
        }
      } else {
        this.addAfterSelection(type);
      }
    });
  }

  // click on palette: append after the selected node (or the last node in the chain) and connect
  addAfterSelection(type) {
    let anchor = this.sel && this.sel.kind === "node" ? this.node(this.sel.id) : null;
    if (!anchor) {
      const hasOut = new Set(this.g.edges.map((e) => e.from));
      anchor = this.g.nodes.filter((n) => !hasOut.has(n.id)).sort((a, b) => b.x - a.x)[0] || this.g.nodes[0];
    }
    if (!anchor) return this.addNode(type, 100, 100, null);
    const outs = this.g.edges.filter((e) => e.from === anchor.id && (e.fromPort || "out") === "out");
    const port = anchor.type === "condition" ? "yes" : "out";
    if (outs.length === 1 && anchor.type !== "condition") {
      // insert between anchor and its single successor, push the rest to the right
      const next = outs[0].to;
      const created = this.addNode(type, anchor.x + 300, anchor.y, { node: anchor.id, port });
      const shift = (id, seen = new Set()) => {
        if (seen.has(id)) return;
        seen.add(id);
        this.node(id).x += 300;
        this.g.edges.filter((e) => e.from === id).forEach((e) => shift(e.to, seen));
      };
      shift(next);
      this.g.edges = this.g.edges.filter((e) => e !== outs[0]);
      this.g.edges.push({ id: this.uid("e"), from: created.id, fromPort: "out", to: next });
      this.renderAll();
      this.select({ kind: "node", id: created.id });
      return;
    }
    this.addNode(type, anchor.x + 300, anchor.y + (outs.length ? 170 : 0), { node: anchor.id, port });
    this.renderEdges();
  }

  popMenu(cx, cy, from) {
    this.closePop();
    const r = this.wrap.getBoundingClientRect();
    const m = document.createElement("div");
    m.className = "popmenu";
    m.style.left = Math.min(cx - r.left, r.width - 210) + "px";
    m.style.top = Math.min(cy - r.top, r.height - 260) + "px";
    m.innerHTML = Object.entries(FE_TYPES).filter(([t]) => t !== "trigger").map(([t, d]) =>
      `<div class="pal-item" data-type="${t}"><span class="node-ico">${ICONS[d.icon]}</span><span>${d.label}</span></div>`).join("");
    m.querySelectorAll(".pal-item").forEach((it) => it.onclick = () => {
      const p = this.toWorld(cx, cy - 26);
      this.addNode(it.dataset.type, p.x, p.y, from);
      this.closePop();
      this.renderEdges();
    });
    this.wrap.append(m);
    this.pop = m;
  }
  closePop() { if (this.pop) { this.pop.remove(); this.pop = null; } }

  onKey(e) {
    if (!document.body.contains(this.root)) return;
    const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);
    const mod = e.metaKey || e.ctrlKey;
    if (mod && e.key.toLowerCase() === "s") { e.preventDefault(); this.save(true); return; }
    if (typing) return;
    if (mod && e.key.toLowerCase() === "z") { e.preventDefault(); e.shiftKey ? this.doRedo() : this.doUndo(); return; }
    if (mod && e.key.toLowerCase() === "d" && this.sel && this.sel.kind === "node") { e.preventDefault(); this.duplicate(this.sel.id); return; }
    if ((e.key === "Delete" || e.key === "Backspace") && this.sel) {
      e.preventDefault();
      this.sel.kind === "node" ? this.removeNode(this.sel.id) : this.removeEdge(this.sel.id);
    }
    if (e.key === "Escape") { this.closePop(); this.select(null); }
  }
}
