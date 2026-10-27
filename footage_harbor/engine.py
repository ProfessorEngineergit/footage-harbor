"""The station: watches readers, runs flows, keeps receipts, cleans up safely.

Deletion rule (never relaxed by a flow): a file on a card is only deleted if
  * the flow contains a cleanup step reachable from the trigger,
  * every required destination upstream of that step holds a copy whose
    content was verified during this import (read-back or provider hash),
  * the file on the card still hashes to the same SHA-256 right before
    deletion,
  * the card has not been removed since the import started.
Anything unknown means: keep the file.
"""
import fnmatch
import hashlib
import json
import os
import re
import threading
import time
import traceback
import urllib.request

from .devices import free_space

KINDS = {
    "photo": {"jpg", "jpeg", "heic", "heif", "png", "tif", "tiff", "webp", "hif"},
    "raw": {"arw", "cr2", "cr3", "nef", "nrw", "raf", "rw2", "orf", "dng", "srw", "pef", "x3f",
            "3fr", "iiq", "rwl", "gpr"},
    "video": {"mp4", "mov", "mxf", "avi", "mts", "m2ts", "mkv", "braw", "r3d", "insv", "360",
              "crm", "m4v", "3gp", "hevc"},
    "audio": {"wav", "mp3", "aac", "m4a", "flac", "bwf", "aif", "aiff", "ogg", "opus"},
    "sidecar": {"xmp", "thm", "lrv", "srt", "xml", "cpi", "bdm", "mpl", "bim", "lrf", "smi",
                "ppn", "dat", "log"},
}
KIND_FOLDER = {"photo": "Fotos", "raw": "RAW", "video": "Videos", "audio": "Audio",
               "sidecar": "Begleitdateien", "other": "Andere"}
SKIP_DIRS = {"System Volume Information", ".Spotlight-V100", ".Trashes", ".fseventsd",
             ".TemporaryItems", "$RECYCLE.BIN", "LOST.DIR", ".DocumentRevisions-V100"}
DEFAULT_TEMPLATE = "{year}/{date} {card}/{path}"
CHUNK = 4 * 1024 * 1024


def kind_of(name):
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    for k, exts in KINDS.items():
        if ext in exts:
            return k
    return "other"


def safe_part(s):
    s = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "-", str(s)).strip(" .")
    return s or "_"


class Cancelled(Exception):
    pass


class CardGone(Exception):
    pass


class Slot:
    def __init__(self, key, number, name=""):
        self.key = key
        self.number = number
        self.name = name
        self.media = None
        self.card_id = None
        self.state = "empty"
        self.message = ""
        self.import_id = None
        self.progress = None
        self.pending_cleanup = None
        self.decision = None
        self.wake = threading.Event()
        self.cancel = False
        self.thread = None
        self.generation = 0
        self.seen = time.time()


class Station:
    def __init__(self, db, backend, cloud, data_dir, mount_root):
        self.db = db
        self.backend = backend
        self.cloud = cloud
        self.data_dir = data_dir
        self.mount_root = mount_root
        self.slots = {}
        self.lock = threading.RLock()
        self.listeners = []
        self.version = 0
        self._stop = False
        self._load_slots()

    # ------------------------------------------------------------------ events
    def changed(self):
        with self.lock:
            self.version += 1
            for ev in list(self.listeners):
                ev.set()

    def _load_slots(self):
        for key, meta in (self.db.get("slots", {}) or {}).items():
            self.slots[key] = Slot(key, meta["number"], meta.get("name", ""))

    def _save_slots(self):
        self.db.set("slots", {k: {"number": s.number, "name": s.name} for k, s in self.slots.items()})

    def _slot_for(self, key):
        if key not in self.slots:
            n = max([s.number for s in self.slots.values()] + [0]) + 1
            self.slots[key] = Slot(key, n)
            self._save_slots()
        return self.slots[key]

    # ---------------------------------------------------------------- watcher
    def run_watcher(self, interval=1.5):
        while not self._stop:
            try:
                self.poll()
            except Exception:
                traceback.print_exc()
            time.sleep(interval)

    def poll(self):
        media = self.backend.scan()
        present = {m.slot_key: m for m in media}
        with self.lock:
            for key, _model in self.backend.empty_reader_slots():
                self._slot_for(key)
            for key, m in present.items():
                slot = self._slot_for(key)
                if slot.media is None or slot.media.key != m.key:
                    if slot.media is not None:
                        self._removed(slot)
                    self._inserted(slot, m)
            for slot in self.slots.values():
                if slot.media is not None and slot.key not in present:
                    self._removed(slot)

    def _inserted(self, slot, m):
        slot.media = m
        slot.generation += 1
        slot.cancel = False
        slot.decision = None
        slot.pending_cleanup = None
        slot.progress = None
        slot.import_id = None
        slot.seen = time.time()
        slot.state = "mounting"
        slot.message = "Karte wird eingebunden"
        self.changed()
        try:
            mp = os.path.join(self.mount_root, f"slot{slot.number}")
            self.backend.mount(m, mp)
        except Exception as e:
            slot.state, slot.message = "error", f"Karte nicht lesbar: {e}"
            self.db.event("error", f"Slot {slot.number}: Karte nicht lesbar ({e})")
            self.changed()
            return
        card = self._identify(m, slot)
        slot.card_id = card["id"]
        flow = self.flow_for(card)
        name = card["name"] or card["label"] or "Karte"
        self.db.event("info", f"{name} in Slot {slot.number} eingesteckt", card_id=card["id"])
        if not flow:
            slot.state, slot.message = "ready", "Kein passender Flow – Karte bleibt unverändert"
        elif self._trigger(flow).get("config", {}).get("start", "auto") == "auto":
            slot.state = "ready"
            self.start(slot.key)
            return
        else:
            slot.state, slot.message = "ready", f"Bereit für „{flow['name']}“"
        self.changed()

    def _removed(self, slot):
        if slot.state in ("running", "confirm"):
            slot.cancel = True
            slot.wake.set()
        name = ""
        if slot.card_id:
            card = self.db.one("SELECT * FROM cards WHERE id=?", (slot.card_id,))
            name = card and (card["name"] or card["label"])
            self.db.x("UPDATE cards SET last_seen=? WHERE id=?", (time.time(), slot.card_id))
        if slot.media is not None:
            try:
                self.backend.unmount(slot.media)
            except Exception:
                pass
        self.db.event("info", f"{name or 'Karte'} aus Slot {slot.number} entnommen", card_id=slot.card_id)
        slot.media = None
        slot.card_id = None
        slot.state = "empty"
        slot.message = ""
        slot.progress = None
        slot.pending_cleanup = None
        slot.generation += 1
        self.changed()

    def _identify(self, m, slot):
        fp = m.fingerprint()
        now = time.time()
        card = self.db.one("SELECT * FROM cards WHERE fingerprint=?", (fp,))
        if not card and m.label:
            # Re-formatted in camera: same label and size, new volume id.
            card = self.db.one("SELECT * FROM cards WHERE label=? AND size=? ORDER BY last_seen DESC",
                               (m.label, m.size))
            if card:
                self.db.x("UPDATE cards SET fingerprint=?, fs_uuid=? WHERE id=?", (fp, m.fs_uuid, card["id"]))
                self.db.event("info", f"{card['name'] or card['label']}: neu formatiert erkannt", card_id=card["id"])
        if not card:
            cid = self.db.x("INSERT INTO cards(fingerprint,label,name,fs_uuid,fstype,size,first_seen,last_seen,last_slot) "
                            "VALUES(?,?,?,?,?,?,?,?,?)",
                            (fp, m.label, "", m.fs_uuid, m.fstype, m.size, now, now, slot.key))
            card = self.db.one("SELECT * FROM cards WHERE id=?", (cid,))
        self.db.x("UPDATE cards SET last_seen=?, last_slot=?, size=?, fstype=? WHERE id=?",
                  (now, slot.key, m.size, m.fstype, card["id"]))
        return self.db.one("SELECT * FROM cards WHERE id=?", (card["id"],))

    # ------------------------------------------------------------------ flows
    def flows(self):
        rows = self.db.q("SELECT * FROM flows ORDER BY priority, id")
        for r in rows:
            r["graph"] = json.loads(r["graph"])
        return rows

    @staticmethod
    def _trigger(flow):
        for n in flow["graph"].get("nodes", []):
            if n["type"] == "trigger":
                return n
        return {}

    def flow_for(self, card):
        flows = [f for f in self.flows() if f["enabled"]]
        if card.get("flow_id"):
            for f in flows:
                if f["id"] == card["flow_id"]:
                    return f
        for f in flows:
            cfg = self._trigger(f).get("config", {})
            match = cfg.get("match", "any")
            if match == "any":
                return f
            if match == "unknown" and not card["registered"]:
                return f
            if match == "label" and cfg.get("pattern"):
                pats = [p.strip() for p in cfg["pattern"].split(",") if p.strip()]
                if any(fnmatch.fnmatch(card["label"].upper(), p.upper()) or
                       fnmatch.fnmatch((card["name"] or "").upper(), p.upper()) for p in pats):
                    return f
            if match == "card" and cfg.get("card_id") == card["id"]:
                return f
        return None

    def resolve(self, graph, ctx):
        """Walks the graph from the trigger, evaluating conditions.
        Returns (ordered_nodes, preds) for reachable nodes."""
        nodes = {n["id"]: n for n in graph.get("nodes", [])}
        trig = next((n for n in nodes.values() if n["type"] == "trigger"), None)
        if not trig:
            return [], {}
        out = {}
        for e in graph.get("edges", []):
            if e["from"] in nodes and e["to"] in nodes:
                out.setdefault(e["from"], []).append(e)
        reach, preds, stack = {trig["id"]}, {}, [trig["id"]]
        while stack:
            nid = stack.pop()
            node = nodes[nid]
            port = None
            if node["type"] == "condition":
                port = "yes" if self._condition(node.get("config", {}), ctx) else "no"
            for e in out.get(nid, []):
                if port and e.get("fromPort", "out") != port:
                    continue
                preds.setdefault(e["to"], []).append(nid)
                if e["to"] not in reach:
                    reach.add(e["to"])
                    stack.append(e["to"])
        # topological order, ties by x so the canvas reads left to right
        indeg = {n: len([p for p in preds.get(n, []) if p in reach]) for n in reach}
        order, ready = [], [trig["id"]]
        while ready:
            ready.sort(key=lambda i: (nodes[i].get("x", 0), nodes[i].get("y", 0)))
            nid = ready.pop(0)
            order.append(nodes[nid])
            for e in out.get(nid, []):
                t = e["to"]
                if t in reach and nid in preds.get(t, []):
                    indeg[t] -= 1
                    if indeg[t] == 0:
                        ready.append(t)
        return order, preds

    @staticmethod
    def _condition(cfg, ctx):
        field, op, val = cfg.get("field", "label"), cfg.get("op", "matches"), str(cfg.get("value", ""))
        actual = ctx.get(field, "")
        if op == "matches":
            pats = [p.strip() for p in val.split(",") if p.strip()] or ["*"]
            return any(fnmatch.fnmatch(str(actual).upper(), p.upper()) for p in pats)
        try:
            a, b = float(actual), float(val.replace(",", "."))
        except ValueError:
            return False
        return a > b if op == "gt" else a < b

    @staticmethod
    def _filter(cfg, files):
        kinds = set(cfg.get("kinds") or [])
        excl = {e.strip().lower().lstrip(".") for e in (cfg.get("exclude") or "").split(",") if e.strip()}
        incl = {e.strip().lower().lstrip(".") for e in (cfg.get("include") or "").split(",") if e.strip()}
        res = []
        for f in files:
            ext = f["rel"].rsplit(".", 1)[-1].lower() if "." in f["rel"] else ""
            if ext in excl:
                continue
            if (kinds and f["kind"] in kinds) or ext in incl or (not kinds and not incl):
                res.append(f)
        return res

    # --------------------------------------------------------------- commands
    def start(self, slot_key, flow_id=None):
        with self.lock:
            slot = self.slots.get(slot_key)
            if not slot or not slot.media or slot.state in ("running", "confirm", "mounting"):
                return False
            card = self.db.one("SELECT * FROM cards WHERE id=?", (slot.card_id,))
            flow = None
            if flow_id:
                flow = next((f for f in self.flows() if f["id"] == int(flow_id)), None)
            flow = flow or self.flow_for(card)
            if not flow:
                slot.state, slot.message = "ready", "Kein passender Flow"
                self.changed()
                return False
            slot.state, slot.message, slot.cancel = "running", "Startet", False
            slot.thread = threading.Thread(target=self._run_import, args=(slot, card, flow), daemon=True)
            slot.thread.start()
            self.changed()
            return True

    def decide_cleanup(self, slot_key, approve):
        slot = self.slots.get(slot_key)
        if slot and slot.state == "confirm":
            slot.decision = bool(approve)
            slot.wake.set()
            return True
        return False

    def eject(self, slot_key):
        slot = self.slots.get(slot_key)
        if not slot or not slot.media or slot.state in ("running", "confirm"):
            return False
        self.backend.unmount(slot.media, power_off=True)
        slot.state, slot.message = "ejected", "Kann entnommen werden"
        self.changed()
        return True

    def cancel(self, slot_key):
        slot = self.slots.get(slot_key)
        if slot and slot.state in ("running", "confirm"):
            slot.cancel = True
            slot.decision = False
            slot.wake.set()
            return True
        return False

    # ----------------------------------------------------------------- import
    def _check(self, slot, gen):
        if slot.cancel or slot.generation != gen:
            raise Cancelled()

    def _inventory(self, root):
        files = []
        root = os.path.realpath(root)
        hidden = bool(self.db.get("include_hidden", False))
        for dp, dns, fns in os.walk(root, followlinks=False):
            dns[:] = sorted(d for d in dns if d not in SKIP_DIRS and (hidden or not d.startswith(".")))
            for fn in sorted(fns):
                if fn.startswith("._") or fn == ".DS_Store" or (fn.startswith(".") and not hidden):
                    continue
                p = os.path.join(dp, fn)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if not os.path.isfile(p) or os.path.islink(p):
                    continue
                rel = os.path.relpath(p, root).replace(os.sep, "/")
                files.append({"rel": rel, "abs": p, "size": st.st_size, "mtime": st.st_mtime,
                              "kind": kind_of(fn), "sha256": "", "md5": "", "sha1": ""})
        return files

    def _ctx(self, card, slot, files):
        return {"label": card["label"], "name": card["name"] or card["label"],
                "files": len(files), "size_gb": round(sum(f["size"] for f in files) / 1e9, 2),
                "weekday": time.strftime("%a"), "slot": slot.number,
                "registered": "ja" if card["registered"] else "nein"}

    def plan(self, flow, card, slot, files):
        """Shared by real imports and dry-runs."""
        ctx = self._ctx(card, slot, files)
        order, preds = self.resolve(flow["graph"], ctx)
        sel = {}
        for node in order:
            if node["type"] == "trigger":
                cur = files
            else:
                seen, cur = set(), []
                for p in preds.get(node["id"], []):
                    for f in sel.get(p, []):
                        if f["rel"] not in seen:
                            seen.add(f["rel"])
                            cur.append(f)
                if node["type"] == "filter":
                    cur = self._filter(node.get("config", {}), cur)
            sel[node["id"]] = cur
        return order, preds, sel

    def _ancestors(self, nid, preds):
        res, stack = set(), list(preds.get(nid, []))
        while stack:
            p = stack.pop()
            if p not in res:
                res.add(p)
                stack.extend(preds.get(p, []))
        return res

    def dry_run(self, flow, slot_key):
        slot = self.slots.get(slot_key)
        if not slot or not slot.media or not slot.media.mountpoint:
            raise ValueError("In diesem Slot steckt keine Karte")
        card = self.db.one("SELECT * FROM cards WHERE id=?", (slot.card_id,))
        files = self._inventory(slot.media.mountpoint)
        self._prefill_hashes(card["id"], files)
        order, preds, sel = self.plan(flow, card, slot, files)
        dests = {d["id"]: d for d in self.destinations()}
        steps = []
        for node in order:
            s = sel[node["id"]]
            info = {"id": node["id"], "type": node["type"], "files": len(s),
                    "bytes": sum(f["size"] for f in s)}
            if node["type"] == "backup":
                did = node.get("config", {}).get("destination_id")
                d = dests.get(did)
                info["destination"] = d["name"] if d else None
                known = 0
                for f in s:
                    if f["sha256"] and self.db.one("SELECT 1 FROM receipts WHERE sha256=? AND destination_id=?",
                                                   (f["sha256"], did)):
                        known += 1
                info["already"] = known
            steps.append(info)
        return {"card": card["name"] or card["label"], "total": len(files), "steps": steps,
                "unreached": [n["id"] for n in flow["graph"].get("nodes", [])
                              if n["id"] not in {o["id"] for o in order}]}

    def _prefill_hashes(self, card_id, files):
        for f in files:
            row = self.db.one("SELECT sha256, md5, sha1 FROM files WHERE card_id=? AND rel_path=? AND size=? "
                              "AND mtime=? AND sha256!='' ORDER BY id DESC LIMIT 1",
                              (card_id, f["rel"], f["size"], f["mtime"]))
            if row:
                f.update(row)
                f["known"] = True

    def destinations(self):
        rows = self.db.q("SELECT * FROM destinations ORDER BY id")
        for r in rows:
            r["config"] = json.loads(r["config"])
        return rows

    def _render(self, template, f, card, imp_id, started):
        t = time.localtime(f["mtime"])
        st = time.localtime(started)
        rel = f["rel"]
        folder, name = (rel.rsplit("/", 1) if "/" in rel else ("", rel))
        base, ext = (name.rsplit(".", 1) if "." in name else (name, ""))
        vals = {
            "card": safe_part(card["name"] or card["label"] or f"Karte {card['id']}"),
            "label": safe_part(card["label"] or "KARTE"),
            "date": time.strftime("%Y-%m-%d", st),
            "time": time.strftime("%H%M", st),
            "capture_date": time.strftime("%Y-%m-%d", t),
            "year": time.strftime("%Y", t), "month": time.strftime("%m", t), "day": time.strftime("%d", t),
            "import": f"{imp_id:04d}", "path": rel, "folder": folder, "name": name,
            "base": base, "ext": ext.lower(), "kind": KIND_FOLDER[f["kind"]],
        }
        out = re.sub(r"\{(\w+)\}", lambda m: vals.get(m.group(1), m.group(0)), template or DEFAULT_TEMPLATE)
        parts = [safe_part(p) if p not in ("", ".", "..") else None for p in out.split("/")]
        parts = [p for p in parts if p]
        if not parts:
            parts = [safe_part(name)]
        return "/".join(parts)

    def _hash_copy(self, src, dst_tmp, f, slot, gen, prog):
        h256, hmd5, hsha1 = hashlib.sha256(), hashlib.md5(), hashlib.sha1()
        st0 = os.stat(src)
        with open(src, "rb") as fi, (open(dst_tmp, "wb") if dst_tmp else _Null()) as fo:
            while True:
                self._check(slot, gen)
                try:
                    buf = fi.read(CHUNK)
                except OSError as e:
                    raise CardGone(str(e))
                if not buf:
                    break
                h256.update(buf); hmd5.update(buf); hsha1.update(buf)
                fo.write(buf)
                prog(len(buf))
            if dst_tmp:
                fo.flush()
                os.fsync(fo.fileno())
        st1 = os.stat(src)
        if (st0.st_size, st0.st_mtime) != (st1.st_size, st1.st_mtime):
            raise RuntimeError("Datei hat sich während des Lesens verändert")
        return h256.hexdigest(), hmd5.hexdigest(), hsha1.hexdigest()

    @staticmethod
    def _sha256(path, check=None):
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                if check:
                    check()
                buf = fh.read(CHUNK)
                if not buf:
                    break
                h.update(buf)
        return h.hexdigest()

    def _receipt(self, f, dest_id, path, method, imp_id):
        self.db.x("INSERT INTO receipts(sha256,size,destination_id,path,method,import_id,verified_at) "
                  "VALUES(?,?,?,?,?,?,?) ON CONFLICT(sha256,destination_id,path) DO UPDATE SET "
                  "method=excluded.method, import_id=excluded.import_id, verified_at=excluded.verified_at",
                  (f["sha256"], f["size"], dest_id, path, method, imp_id, time.time()))

    def _run_import(self, slot, card, flow):
        gen = slot.generation
        started = time.time()
        imp_id = self.db.x("INSERT INTO imports(card_id,flow_id,flow_name,flow_snapshot,slot,state,phase,started_at) "
                           "VALUES(?,?,?,?,?,?,?,?)",
                           (card["id"], flow["id"], flow["name"], json.dumps(flow["graph"]),
                            str(slot.number), "running", "scan", started))
        slot.import_id = imp_id
        name = card["name"] or card["label"] or "Karte"
        prog = {"phase": "scan", "bytes_done": 0, "bytes_total": 0, "files_total": 0,
                "steps": [], "current": "", "rate": 0}
        slot.progress = prog
        slot.message = "Inhalt wird gelesen"
        self.changed()
        ok_files, deleted, errors = set(), 0, []
        last_emit = [0.0]
        window = [time.time(), 0]

        def tick(n, step=None):
            prog["bytes_done"] += n
            if step is not None:
                step["done"] += n
            window[1] += n
            now = time.time()
            if now - window[0] >= 1.0:
                prog["rate"] = window[1] / (now - window[0])
                window[0], window[1] = now, 0
            if now - last_emit[0] > 0.25:
                last_emit[0] = now
                self.changed()

        try:
            files = self._inventory(slot.media.mountpoint)
            self._prefill_hashes(card["id"], files)
            order, preds, sel = self.plan(flow, card, slot, files)
            dests = {d["id"]: d for d in self.destinations()}
            backups = [n for n in order if n["type"] == "backup"]
            strict = any(n["type"] == "cleanup" for n in order)
            involved = {}
            for n in backups:
                for f in sel[n["id"]]:
                    involved[f["rel"]] = f
            for n in order:
                if n["type"] == "cleanup":
                    for f in sel[n["id"]]:
                        involved.setdefault(f["rel"], f)
            for n in backups:
                d = dests.get(n.get("config", {}).get("destination_id"))
                prog["steps"].append({"node": n["id"], "kind": d["kind"] if d else "missing",
                                      "label": d["name"] if d else "Ziel fehlt",
                                      "done": 0, "total": sum(f["size"] for f in sel[n["id"]]),
                                      "files": len(sel[n["id"]]), "files_done": 0, "skipped": 0,
                                      "state": "waiting", "method": ""})
            prog["files_total"] = len(involved)
            prog["bytes_total"] = sum(s["total"] for s in prog["steps"])
            self.db.x("UPDATE imports SET files_total=?, bytes_total=?, phase='copy' WHERE id=?",
                      (len(involved), sum(f["size"] for f in involved.values()), imp_id))
            file_ids = {}
            for f in involved.values():
                file_ids[f["rel"]] = self.db.x(
                    "INSERT INTO files(import_id,card_id,rel_path,size,mtime,sha256,md5,sha1) VALUES(?,?,?,?,?,?,?,?)",
                    (imp_id, card["id"], f["rel"], f["size"], f["mtime"], f["sha256"], f["md5"], f["sha1"]))

            verified = {}       # node_id -> set(rel) verified in this import
            local_copy = {}     # rel -> absolute path of a verified local copy
            new_count = 0

            for i, node in enumerate(order):
                self._check(slot, gen)
                t = node["type"]
                cfg = node.get("config", {})
                if t == "backup":
                    step = next(s for s in prog["steps"] if s["node"] == node["id"])
                    dest = dests.get(cfg.get("destination_id"))
                    if not dest:
                        step["state"] = "error"
                        errors.append("Ein Sicherungsschritt hat kein Ziel")
                        continue
                    step["state"] = "running"
                    prog["phase"] = "copy" if dest["kind"] == "local" else "upload"
                    slot.message = f"{'Kopiert' if dest['kind'] == 'local' else 'Lädt'} nach {dest['name']}"
                    self.changed()
                    done = set()
                    for f in sel[node["id"]]:
                        self._check(slot, gen)
                        prog["current"] = f["rel"]
                        try:
                            rel_target = self._render(cfg.get("template"), f, card, imp_id, started)
                            if dest["kind"] == "local":
                                fresh = self._backup_local(dest, rel_target, f, slot, gen, imp_id,
                                                           lambda n: tick(n, step), local_copy, strict)
                            else:
                                fresh = self._backup_cloud(dest, rel_target, f, slot, gen, imp_id,
                                                           lambda n: tick(n, step), local_copy, step, strict)
                            if fresh:
                                new_count += 1
                            else:
                                step["skipped"] += 1
                            done.add(f["rel"])
                            self.db.x("UPDATE files SET sha256=?, md5=?, sha1=? WHERE id=?",
                                      (f["sha256"], f["md5"], f["sha1"], file_ids[f["rel"]]))
                        except (Cancelled, CardGone):
                            raise
                        except Exception as e:
                            errors.append(f"{f['rel']} → {dest['name']}: {e}")
                            self.db.x("UPDATE files SET state='failed', note=? WHERE id=?",
                                      (str(e)[:300], file_ids[f["rel"]]))
                        step["files_done"] += 1
                    verified[node["id"]] = done
                    step["state"] = "done" if len(done) == len(sel[node["id"]]) else "partial"
                    self.changed()

                elif t == "cleanup":
                    anc = self._ancestors(node["id"], preds)
                    req_nodes = [n["id"] for n in backups if n["id"] in anc]
                    need = len(req_nodes) if cfg.get("require", "all") == "all" else \
                        min(int(cfg.get("count") or 1), len(req_nodes))
                    if not req_nodes or need < 1:
                        errors.append("Aufräumen übersprungen: keine Sicherung davor")
                        continue
                    candidates = []
                    for f in sel[node["id"]]:
                        hits = sum(1 for r in req_nodes if f["rel"] in verified.get(r, set()))
                        if hits >= need and f["sha256"]:
                            candidates.append(f)
                    kept = len(sel[node["id"]]) - len(candidates)
                    if not candidates:
                        continue
                    if cfg.get("confirm", True):
                        slot.pending_cleanup = {"files": len(candidates),
                                                "bytes": sum(f["size"] for f in candidates), "kept": kept}
                        slot.state, slot.message = "confirm", "Wartet auf Freigabe zum Aufräumen"
                        prog["phase"] = "confirm"
                        self.db.x("UPDATE imports SET state='confirm', phase='confirm' WHERE id=?", (imp_id,))
                        self.changed()
                        slot.wake.clear()
                        while slot.decision is None:
                            slot.wake.wait(1)
                            self._check(slot, gen)
                        approved, slot.decision = slot.decision, None
                        slot.pending_cleanup = None
                        slot.state = "running"
                        self.db.x("UPDATE imports SET state='running' WHERE id=?", (imp_id,))
                        if not approved:
                            self.db.event("info", f"{name}: Aufräumen abgelehnt, Dateien bleiben", card["id"], imp_id)
                            continue
                    prog["phase"] = "cleanup"
                    slot.message = "Räumt die Karte auf"
                    self.changed()
                    deleted += self._cleanup(slot, gen, candidates, file_ids, imp_id)

                elif t == "notify":
                    self._notify(cfg, name, imp_id, new_count, deleted, errors)

                elif t == "eject":
                    if not errors:
                        prog["phase"] = "eject"
                        self.backend.sync(slot.media)

            wanted = {n["id"]: {f["rel"] for f in sel[n["id"]]} for n in backups}
            for rel in involved:
                if all(rel in verified.get(nid, set()) for nid, rels in wanted.items() if rel in rels):
                    ok_files.add(rel)
            for rel in ok_files:
                self.db.x("UPDATE files SET state='secured' WHERE id=? AND state='pending'", (file_ids[rel],))
            state = "done" if not errors else "partial"
            summary = {"new": new_count, "deleted": deleted, "errors": errors[:50],
                       "steps": prog["steps"], "duration": time.time() - started}
            self.db.x("UPDATE imports SET state=?, phase='done', finished_at=?, files_new=?, files_deleted=?, "
                      "error=?, summary=? WHERE id=?",
                      (state, time.time(), new_count, deleted, "\n".join(errors[:5]), json.dumps(summary), imp_id))
            msg = f"{name}: {new_count} neue Dateien gesichert" + (f", {deleted} von der Karte entfernt" if deleted else "")
            self.db.event("ok" if state == "done" else "warn", msg + (f" · {len(errors)} Fehler" if errors else ""),
                          card["id"], imp_id)
            if self.db.get("notify_url") and (errors or self.db.get("notify_on", "all") == "all"):
                if not any(n["type"] == "notify" for n in order):
                    self._notify({"url": self.db.get("notify_url")}, name, imp_id, new_count, deleted, errors)
            prog["phase"] = "done"
            if any(n["type"] == "eject" for n in order) and not errors and slot.media:
                self.backend.unmount(slot.media, power_off=True)
                slot.state, slot.message = "ejected", "Gesichert – kann entnommen werden"
            else:
                slot.state = "done" if not errors else "error"
                slot.message = "Gesichert – kann entnommen werden" if not errors else errors[0]
        except Cancelled:
            self.db.x("UPDATE imports SET state='interrupted', finished_at=?, error=? WHERE id=?",
                      (time.time(), "Karte entnommen oder abgebrochen", imp_id))
            self.db.event("warn", f"{name}: Import unterbrochen – wird beim nächsten Einstecken fortgesetzt",
                          card["id"], imp_id)
            if slot.generation == gen and slot.media:
                slot.state, slot.message = "ready", "Abgebrochen"
        except CardGone as e:
            self.db.x("UPDATE imports SET state='interrupted', finished_at=?, error=? WHERE id=?",
                      (time.time(), f"Karte nicht mehr lesbar: {e}", imp_id))
            self.db.event("warn", f"{name}: Karte während des Imports verloren", card["id"], imp_id)
        except Exception as e:
            traceback.print_exc()
            self.db.x("UPDATE imports SET state='failed', finished_at=?, error=? WHERE id=?",
                      (time.time(), str(e), imp_id))
            self.db.event("error", f"{name}: {e}", card["id"], imp_id)
            if slot.generation == gen:
                slot.state, slot.message = "error", str(e)
        finally:
            prog["current"] = ""
            self.changed()

    def _backup_local(self, dest, rel_target, f, slot, gen, imp_id, tick, local_copy, strict):
        root = dest["config"]["path"]
        target = os.path.join(root, rel_target)
        if f.get("sha256"):
            for r in self.db.q("SELECT path FROM receipts WHERE sha256=? AND destination_id=?",
                               (f["sha256"], dest["id"])):
                p = os.path.join(root, r["path"])
                if os.path.isfile(p) and os.path.getsize(p) == f["size"]:
                    # Before a cleanup the existing copy is re-read; otherwise size is enough to skip.
                    if strict and self._sha256(p, lambda: self._check(slot, gen)) != f["sha256"]:
                        continue
                    self._receipt(f, dest["id"], r["path"], "sha256-readback" if strict else "receipt", imp_id)
                    local_copy.setdefault(f["rel"], p)
                    tick(f["size"])
                    return False
        need = f["size"] + 64 * 1024 * 1024
        if free_space(root)["free"] and free_space(root)["free"] < need:
            raise RuntimeError(f"{dest['name']} ist voll")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = os.path.join(os.path.dirname(target), f".fh-{imp_id}-{os.path.basename(target)}.part")
        try:
            sha, md5, sha1 = self._hash_copy(f["abs"], tmp, f, slot, gen, tick)
            f.update(sha256=sha, md5=md5, sha1=sha1)
            final = target
            n = 2
            while os.path.exists(final):
                if os.path.getsize(final) == f["size"] and self._sha256(final) == sha:
                    os.remove(tmp)
                    tmp = None
                    break
                b, e = os.path.splitext(target)
                final = f"{b} ({n}){e}"
                n += 1
            if tmp:
                os.replace(tmp, final)
                tmp = None
                _fsync_dir(os.path.dirname(final))
            if self._sha256(final, lambda: self._check(slot, gen)) != sha:
                raise RuntimeError("Prüfsumme der Kopie stimmt nicht")
            relp = os.path.relpath(final, root).replace(os.sep, "/")
            self._receipt(f, dest["id"], relp, "sha256-readback", imp_id)
            local_copy.setdefault(f["rel"], final)
            return True
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)

    def _backup_cloud(self, dest, rel_target, f, slot, gen, imp_id, tick, local_copy, step, strict):
        remote, root = dest["config"]["remote"], dest["config"].get("root", "").strip("/")
        src = local_copy.get(f["rel"]) or f["abs"]
        if not f.get("sha256"):
            sha, md5, sha1 = self._hash_copy(src, None, f, slot, gen, lambda n: None)
            f.update(sha256=sha, md5=md5, sha1=sha1)
        elif not f.get("md5"):
            sha, md5, sha1 = self._hash_copy(src, None, f, slot, gen, lambda n: None)
            f.update(md5=md5, sha1=sha1)
        for r in self.db.q("SELECT path FROM receipts WHERE sha256=? AND destination_id=?",
                           (f["sha256"], dest["id"])):
            p = _join(root, r["path"])
            if not self.cloud.exists_with_size(remote, p, f["size"]):
                continue
            method = "receipt+size"
            if strict:
                method, ok = self.cloud.remote_hash(remote, p, f)
                if not ok:
                    continue
            self._receipt(f, dest["id"], r["path"], method, imp_id)
            tick(f["size"])
            return False
        path = _join(root, rel_target)
        self.cloud.upload(src, remote, path)
        self._check(slot, gen)
        method, ok = self.cloud.remote_hash(remote, path, f)
        step["method"] = method
        if not ok:
            raise RuntimeError("Inhalt in der Cloud weicht ab")
        self._receipt(f, dest["id"], rel_target, method, imp_id)
        tick(f["size"])
        return True

    def _cleanup(self, slot, gen, candidates, file_ids, imp_id):
        deleted = 0
        self.backend.set_writable(slot.media, True)
        try:
            for f in candidates:
                self._check(slot, gen)
                try:
                    if self._sha256(f["abs"], lambda: self._check(slot, gen)) != f["sha256"]:
                        self.db.x("UPDATE files SET state='kept', note='verändert' WHERE id=?", (file_ids[f["rel"]],))
                        continue
                    os.remove(f["abs"])
                    deleted += 1
                    self.db.x("UPDATE files SET state='deleted' WHERE id=?", (file_ids[f["rel"]],))
                except (Cancelled, CardGone):
                    raise
                except OSError as e:
                    self.db.x("UPDATE files SET state='kept', note=? WHERE id=?", (str(e)[:200], file_ids[f["rel"]]))
            self.backend.sync(slot.media)
        finally:
            try:
                self.backend.set_writable(slot.media, False)
            except Exception:
                pass
        return deleted

    def _notify(self, cfg, name, imp_id, new, deleted, errors):
        url = (cfg.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return
        text = f"{name}: {new} neue Dateien gesichert" + (f", {deleted} entfernt" if deleted else "")
        if errors:
            text += f" · {len(errors)} Fehler"
        try:
            req = urllib.request.Request(url, data=text.encode(), method="POST",
                                         headers={"Title": "Footage Harbor", "Content-Type": "text/plain"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            self.db.event("warn", f"Benachrichtigung fehlgeschlagen: {e}", import_id=imp_id)

    # --------------------------------------------------------------- snapshot
    def snapshot(self):
        cards = {c["id"]: c for c in self.db.q("SELECT * FROM cards")}
        slots = []
        for s in sorted(self.slots.values(), key=lambda s: s.number):
            c = cards.get(s.card_id) if s.card_id else None
            flow = None
            if c:
                f = self.flow_for(c)
                flow = {"id": f["id"], "name": f["name"]} if f else None
            slots.append({
                "key": s.key, "number": s.number, "name": s.name, "state": s.state, "message": s.message,
                "card": _card_public(c) if c else None, "flow": flow,
                "media": {"label": s.media.label, "size": s.media.size, "fstype": s.media.fstype,
                          "used": s.media.size if s.media.fstype == "folder" else _used(s.media.mountpoint)}
                if s.media else None,
                "progress": s.progress, "import_id": s.import_id, "cleanup": s.pending_cleanup,
            })
        return {"slots": slots, "version": self.version}


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def write(self, b):
        pass


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _join(root, path):
    return f"{root}/{path}" if root else path


def _used(mp):
    if not mp:
        return None
    fs = free_space(mp)
    return fs["total"] - fs["free"] if fs["total"] else None


def _card_public(c):
    return {"id": c["id"], "label": c["label"], "name": c["name"], "color": c["color"],
            "registered": bool(c["registered"]), "size": c["size"], "flow_id": c["flow_id"]}
