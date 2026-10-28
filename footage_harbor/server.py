"""HTTP API, live event stream and static web UI."""
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .cloud import FORM_PROVIDERS, OAUTH_PROVIDERS
from .devices import free_space, local_disks
from .engine import DEFAULT_TEMPLATE

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
SESSION_DAYS = 30


def hash_password(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000)
    return f"{salt}${dk.hex()}"


def check_password(pw, stored):
    try:
        salt, _ = stored.split("$", 1)
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(hash_password(pw, salt), stored)


def default_flow(dest_id):
    return {
        "nodes": [
            {"id": "n1", "type": "trigger", "x": 80, "y": 200, "config": {"match": "any", "start": "auto"}},
            {"id": "n2", "type": "backup", "x": 380, "y": 200,
             "config": {"destination_id": dest_id, "template": DEFAULT_TEMPLATE}},
            {"id": "n3", "type": "eject", "x": 680, "y": 200, "config": {}},
        ],
        "edges": [
            {"id": "e1", "from": "n1", "fromPort": "out", "to": "n2"},
            {"id": "e2", "from": "n2", "fromPort": "out", "to": "n3"},
        ],
    }


class App:
    def __init__(self, db, station, cloud, dev=False):
        self.db = db
        self.station = station
        self.cloud = cloud
        self.dev = dev
        self.sessions = self.db.get("sessions", {}) or {}
        self.started = time.time()
        self.login_attempts = {}

    # ------------------------------------------------------------- sessions
    def new_session(self):
        token = secrets.token_urlsafe(32)
        self.sessions[token] = time.time() + SESSION_DAYS * 86400
        self.sessions = {k: v for k, v in self.sessions.items() if v > time.time()}
        self.db.set("sessions", self.sessions)
        return token

    def valid(self, token):
        return bool(token) and self.sessions.get(token, 0) > time.time()

    def settings(self):
        return {k: self.db.get(k, v) for k, v in SETTINGS.items()}

    def update_status(self):
        try:
            with open(os.path.join(self.station.data_dir, "update.json")) as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            st = {}
        st["available"] = os.path.exists(os.path.join(APP_ROOT, "systemd", "footage-harbor-updater.service"))
        return st

    def setup_done(self):
        return bool(self.db.get("password"))

    # ---------------------------------------------------------------- info
    def station_info(self):
        return {"name": self.db.get("station_name", "Footage Harbor"), "version": installed_version(),
                "hostname": socket.gethostname(), "addresses": _addresses(),
                "uptime": time.time() - self.started, "backend": self.station.backend.name,
                "rclone": self.cloud.version(), "temperature": _cpu_temp(), "dev": self.dev,
                "repo": REPO_URL}

    def destinations_public(self, with_usage=False):
        out = []
        for d in self.station.destinations():
            item = {"id": d["id"], "name": d["name"], "kind": d["kind"], "provider": d["provider"],
                    "config": d["config"]}
            if with_usage:
                if d["kind"] == "local":
                    fs = free_space(d["config"]["path"])
                    item["usage"] = {"total": fs["total"], "free": fs["free"],
                                     "ok": os.path.isdir(d["config"]["path"])}
                else:
                    info = self.cloud.about(d["config"]["remote"]) if self.cloud.available else {"error": "rclone fehlt"}
                    item["usage"] = {"total": info.get("total"), "free": info.get("free"),
                                     "ok": "error" not in info, "error": info.get("error")}
            stats = self.db.one("SELECT COUNT(*) n, COALESCE(SUM(size),0) b, MAX(verified_at) last "
                                "FROM receipts WHERE destination_id=?", (d["id"],))
            item["stats"] = stats
            out.append(item)
        return out


APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def installed_version():
    try:
        with open(os.path.join(APP_ROOT, "VERSION")) as fh:
            return fh.read().strip()[:7] or __version__
    except OSError:
        return f"{__version__}-dev"


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FootageHarbor"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            if os.environ.get("FH_DEBUG"):
                super().log_message(fmt, *args)

        # helpers ---------------------------------------------------------
        def _cookie(self):
            raw = self.headers.get("Cookie", "")
            for part in raw.split(";"):
                k, _, v = part.strip().partition("=")
                if k == "fh_session":
                    return v
            return None

        def _send(self, code, body=b"", ctype="application/json", headers=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def json(self, data, code=200, headers=None):
            self._send(code, json.dumps(data, default=str).encode(), headers=headers)

        def err(self, msg, code=400):
            self.json({"error": msg}, code)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 2_000_000:
                raise ValueError("Anfrage zu groß")
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw or b"{}")

        def authed(self):
            return app.valid(self._cookie())

        # routing ---------------------------------------------------------
        def do_GET(self):
            self.route("GET")

        def do_HEAD(self):
            self.route("GET")

        def do_POST(self):
            self.route("POST")

        def do_PUT(self):
            self.route("PUT")

        def do_DELETE(self):
            self.route("DELETE")

        def route(self, method):
            path = urllib.parse.urlparse(self.path).path
            try:
                if not path.startswith("/api/"):
                    return self.static(path)
                if method != "GET" and self.headers.get("X-Harbor") != "1":
                    return self.err("Fehlender Header", 403)
                public = {("GET", "/api/status"), ("POST", "/api/setup"), ("POST", "/api/login"),
                          ("GET", "/api/internal/update-state")}
                if (method, path) not in public and not self.authed():
                    return self.err("Nicht angemeldet", 401)
                for m, pattern, fn in ROUTES:
                    if m != method:
                        continue
                    match = re.fullmatch(pattern, path)
                    if match:
                        return fn(self, *match.groups())
                self.err("Nicht gefunden", 404)
            except (ValueError, KeyError) as e:
                self.err(str(e) or "Ungültige Anfrage")
            except BrokenPipeError:
                pass
            except Exception as e:
                import traceback
                traceback.print_exc()
                try:
                    self.err(str(e), 500)
                except Exception:
                    pass

        def static(self, path):
            if path in ("", "/"):
                path = "/index.html"
            full = os.path.realpath(os.path.join(WEB_DIR, path.lstrip("/")))
            if not full.startswith(os.path.realpath(WEB_DIR)) or not os.path.isfile(full):
                full = os.path.join(WEB_DIR, "index.html")
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
                ctype += "; charset=utf-8"
            with open(full, "rb") as fh:
                data = fh.read()
            self._send(200, data, ctype, {
                "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                                           "connect-src 'self'; frame-ancestors 'none'"})

        # public ----------------------------------------------------------
        def api_status(self):
            self.json({"setup": app.setup_done(), "authed": self.authed(),
                       "name": app.db.get("station_name", "Footage Harbor"),
                       "disks": local_disks() if not app.setup_done() else [],
                       "suggested_archive": _suggest_archive(app)})

        def api_setup(self):
            if app.setup_done():
                return self.err("Station ist bereits eingerichtet", 409)
            b = self.body()
            pw = b.get("password", "")
            if len(pw) < 8:
                return self.err("Passwort braucht mindestens 8 Zeichen")
            archive = (b.get("archive") or "").strip()
            if archive:
                os.makedirs(archive, exist_ok=True)
                if not os.access(archive, os.W_OK):
                    return self.err("In diesen Ordner kann nicht geschrieben werden")
            app.db.set("password", hash_password(pw))
            app.db.set("station_name", (b.get("name") or "Footage Harbor").strip()[:40])
            if archive and not app.db.one("SELECT 1 FROM destinations"):
                did = app.db.x("INSERT INTO destinations(name,kind,provider,config,created_at) VALUES(?,?,?,?,?)",
                               (b.get("archive_name") or "Archiv", "local", "local", json.dumps({"path": archive}),
                                time.time()))
                app.db.x("INSERT INTO flows(name,enabled,priority,graph,updated_at) VALUES(?,?,?,?,?)",
                         ("Alles ins Archiv", 1, 100, json.dumps(default_flow(did)), time.time()))
            token = app.new_session()
            app.station.changed()
            self.json({"ok": True}, headers={"Set-Cookie": _cookie(token)})

        def api_login(self):
            ip = self.client_address[0]
            hist = [t for t in app.login_attempts.get(ip, []) if t > time.time() - 300]
            if len(hist) >= 10:
                return self.err("Zu viele Versuche – bitte kurz warten", 429)
            b = self.body()
            if not check_password(b.get("password", ""), app.db.get("password")):
                app.login_attempts[ip] = hist + [time.time()]
                time.sleep(0.8)
                return self.err("Passwort stimmt nicht", 401)
            self.json({"ok": True}, headers={"Set-Cookie": _cookie(app.new_session())})

        def api_logout(self):
            app.sessions.pop(self._cookie(), None)
            app.db.set("sessions", app.sessions)
            self.json({"ok": True}, headers={"Set-Cookie": "fh_session=; Path=/; Max-Age=0; SameSite=Strict"})

        # live ------------------------------------------------------------
        def api_events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            ev = threading.Event()
            app.station.listeners.append(ev)
            try:
                last = -1
                while True:
                    if app.station.version != last:
                        last = app.station.version
                        data = json.dumps(app.station.snapshot(), default=str)
                        self.wfile.write(f"data: {data}\n\n".encode())
                        self.wfile.flush()
                    if not ev.wait(15):
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    ev.clear()
                    time.sleep(0.15)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                app.station.listeners.remove(ev)
                self.close_connection = True

        def api_state(self):
            self.json(app.station.snapshot())

        def api_overview(self):
            last = app.db.q("SELECT i.*, c.name card_name, c.label card_label FROM imports i "
                            "LEFT JOIN cards c ON c.id=i.card_id ORDER BY i.id DESC LIMIT 12")
            for r in last:
                r.pop("flow_snapshot", None)
                r["summary"] = json.loads(r["summary"] or "{}")
            events = app.db.q("SELECT * FROM events ORDER BY id DESC LIMIT 40")
            totals = app.db.one("SELECT COUNT(DISTINCT sha256) files, COALESCE(SUM(size),0) bytes FROM "
                                "(SELECT sha256, MAX(size) size FROM receipts GROUP BY sha256)")
            self.json({"station": app.station_info(), "imports": last, "events": events, "totals": totals,
                       "destinations": app.destinations_public(with_usage=False)})

        # slots -----------------------------------------------------------
        def api_slot_action(self, key, action):
            key = urllib.parse.unquote(key)
            b = self.body()
            st = app.station
            ok = {"start": lambda: st.start(key, b.get("flow_id")),
                  "eject": lambda: st.eject(key),
                  "cancel": lambda: st.cancel(key),
                  "approve": lambda: st.decide_cleanup(key, True),
                  "decline": lambda: st.decide_cleanup(key, False)}[action]()
            self.json({"ok": bool(ok)})

        def api_slot_rename(self, key):
            key = urllib.parse.unquote(key)
            slot = app.station.slots.get(key)
            if not slot:
                return self.err("Slot unbekannt", 404)
            slot.name = (self.body().get("name") or "").strip()[:30]
            app.station._save_slots()
            app.station.changed()
            self.json({"ok": True})

        # cards -----------------------------------------------------------
        def api_cards(self):
            rows = app.db.q("SELECT c.*, (SELECT COUNT(*) FROM imports i WHERE i.card_id=c.id) imports, "
                            "(SELECT MAX(started_at) FROM imports i WHERE i.card_id=c.id) last_import, "
                            "(SELECT COUNT(*) FROM files f WHERE f.card_id=c.id AND f.state IN ('secured','deleted')) "
                            "files_secured FROM cards c ORDER BY c.last_seen DESC")
            inserted = {s.card_id: s.number for s in app.station.slots.values() if s.card_id}
            for r in rows:
                r["slot"] = inserted.get(r["id"])
            self.json(rows)

        def api_card_update(self, cid):
            b = self.body()
            card = app.db.one("SELECT * FROM cards WHERE id=?", (int(cid),))
            if not card:
                return self.err("Karte unbekannt", 404)
            name = b.get("name", card["name"]).strip()[:40]
            color = b.get("color", card["color"])
            flow_id = b.get("flow_id", card["flow_id"]) or None
            reg = 1 if b.get("registered", True) else 0
            app.db.x("UPDATE cards SET name=?, color=?, flow_id=?, registered=? WHERE id=?",
                     (name, color, flow_id, reg, int(cid)))
            app.station.changed()
            self.json({"ok": True})

        def api_card_delete(self, cid):
            app.db.x("DELETE FROM cards WHERE id=?", (int(cid),))
            app.station.changed()
            self.json({"ok": True})

        # flows -----------------------------------------------------------
        def api_flows(self):
            self.json(app.station.flows())

        def api_flow_save(self, fid=None):
            b = self.body()
            graph = b.get("graph") or {"nodes": [], "edges": []}
            _validate_graph(graph)
            name = (b.get("name") or "Neuer Flow").strip()[:60]
            if fid:
                app.db.x("UPDATE flows SET name=?, enabled=?, graph=?, updated_at=? WHERE id=?",
                         (name, 1 if b.get("enabled", True) else 0, json.dumps(graph), time.time(), int(fid)))
            else:
                prio = (app.db.one("SELECT MIN(priority) p FROM flows") or {}).get("p") or 100
                fid = app.db.x("INSERT INTO flows(name,enabled,priority,graph,updated_at) VALUES(?,?,?,?,?)",
                               (name, 1, prio - 1, json.dumps(graph), time.time()))
            app.station.changed()
            self.json({"id": int(fid)})

        def api_flow_order(self):
            for i, fid in enumerate(self.body().get("order", [])):
                app.db.x("UPDATE flows SET priority=? WHERE id=?", (i + 1, int(fid)))
            app.station.changed()
            self.json({"ok": True})

        def api_flow_delete(self, fid):
            app.db.x("DELETE FROM flows WHERE id=?", (int(fid),))
            app.db.x("UPDATE cards SET flow_id=NULL WHERE flow_id=?", (int(fid),))
            app.station.changed()
            self.json({"ok": True})

        def api_flow_dryrun(self):
            b = self.body()
            graph = b.get("graph")
            _validate_graph(graph)
            self.json(app.station.dry_run({"graph": graph}, b.get("slot")))

        # destinations ----------------------------------------------------
        def api_destinations(self):
            self.json({"items": app.destinations_public(with_usage=True),
                       "rclone": bool(app.cloud.available),
                       "oauth": {k: v["label"] for k, v in OAUTH_PROVIDERS.items()},
                       "forms": {k: {"label": v["label"], "fields": v["fields"]} for k, v in FORM_PROVIDERS.items()},
                       "disks": local_disks()})

        def _add_dest(self, name, kind, provider, config):
            did = app.db.x("INSERT INTO destinations(name,kind,provider,config,created_at) VALUES(?,?,?,?,?)",
                           (name[:40], kind, provider, json.dumps(config), time.time()))
            app.station.changed()
            return did

        def api_dest_local(self):
            b = self.body()
            path = (b.get("path") or "").strip()
            if not path.startswith("/"):
                return self.err("Bitte einen absoluten Pfad angeben")
            os.makedirs(path, exist_ok=True)
            if not os.access(path, os.W_OK):
                return self.err("Ordner ist nicht beschreibbar")
            self.json({"id": self._add_dest(b.get("name") or os.path.basename(path) or "Speicher",
                                            "local", "local", {"path": path})})

        def api_dest_update(self, did):
            b = self.body()
            d = app.db.one("SELECT * FROM destinations WHERE id=?", (int(did),))
            if not d:
                return self.err("Ziel unbekannt", 404)
            cfg = json.loads(d["config"])
            if "root" in b and d["kind"] == "rclone":
                cfg["root"] = b["root"].strip().strip("/")
            app.db.x("UPDATE destinations SET name=?, config=? WHERE id=?",
                     ((b.get("name") or d["name"]).strip()[:40], json.dumps(cfg), int(did)))
            app.station.changed()
            self.json({"ok": True})

        def api_dest_delete(self, did):
            d = app.db.one("SELECT * FROM destinations WHERE id=?", (int(did),))
            if not d:
                return self.err("Ziel unbekannt", 404)
            if d["kind"] == "rclone":
                remote = json.loads(d["config"]).get("remote")
                still = [x for x in app.station.destinations() if x["id"] != d["id"]
                         and x["config"].get("remote") == remote]
                if not still:
                    app.cloud.delete_remote(remote)
            app.db.x("DELETE FROM destinations WHERE id=?", (int(did),))
            app.station.changed()
            self.json({"ok": True})

        def api_dest_test(self, did):
            d = next((x for x in app.station.destinations() if x["id"] == int(did)), None)
            if not d:
                return self.err("Ziel unbekannt", 404)
            if d["kind"] == "local":
                p = d["config"]["path"]
                probe = os.path.join(p, f".fh-probe-{secrets.token_hex(4)}")
                with open(probe, "wb") as fh:
                    fh.write(b"ok")
                    os.fsync(fh.fileno())
                os.remove(probe)
                return self.json({"ok": True, "message": "Schreiben und Lesen funktioniert"})
            app.cloud.test(d["config"]["remote"], d["config"].get("root", ""))
            feats = app.cloud.features(d["config"]["remote"])
            method = next((h for h in ("sha256", "sha1", "md5") if h in feats["hashes"]), None)
            msg = f"Verbunden · Prüfung über {method.upper()} des Anbieters" if method else \
                "Verbunden · Prüfung durch erneutes Herunterladen"
            self.json({"ok": True, "message": msg})

        def _require_rclone(self):
            if not app.cloud.available:
                raise ValueError("rclone ist nicht installiert – install.sh erneut ausführen")

        def api_connect_nextcloud(self):
            self._require_rclone()
            server = (self.body().get("server") or "").strip()
            if not server:
                return self.err("Bitte die Adresse deiner Nextcloud angeben")
            self.json(app.cloud.start_nextcloud(server, f"Footage Harbor ({app.db.get('station_name', 'Station')})"))

        def api_connect_oauth(self):
            self._require_rclone()
            provider = self.body().get("provider")
            if provider not in OAUTH_PROVIDERS:
                return self.err("Anbieter nicht unterstützt")
            self.json(app.cloud.start_oauth(provider))

        def api_connect_finish(self, sid):
            sess = app.cloud.finish_oauth(sid, self.body().get("url", ""))
            self.json(app.cloud.session_info(sess["id"]))

        def api_connect_status(self, sid):
            info = app.cloud.session_info(sid)
            if not info:
                return self.err("Unbekannt", 404)
            self.json(info)

        def api_connect_save(self, sid):
            """Turns a finished sign-in into a destination."""
            info = app.cloud.session_info(sid)
            if not info or info["state"] != "done":
                return self.err("Anmeldung noch nicht abgeschlossen")
            b = self.body()
            label = {"nextcloud": "Nextcloud", **{k: v["label"] for k, v in OAUTH_PROVIDERS.items()}}
            did = self._add_dest(b.get("name") or label.get(info["provider"], "Cloud"), "rclone", info["provider"],
                                 {"remote": info["remote"], "root": (b.get("root") or "Footage Harbor").strip("/"),
                                  "account": info.get("account", "")})
            app.cloud.sessions.pop(sid, None)
            self.json({"id": did})

        def api_connect_form(self):
            self._require_rclone()
            b = self.body()
            provider = b.get("provider")
            if provider not in FORM_PROVIDERS:
                return self.err("Anbieter nicht unterstützt")
            remote = app.cloud.create_form_remote(provider, b.get("fields") or {})
            try:
                app.cloud.test(remote, b.get("root", ""))
            except Exception as e:
                app.cloud.delete_remote(remote)
                return self.err(f"Verbindung fehlgeschlagen: {e}")
            self.json({"id": self._add_dest(b.get("name") or FORM_PROVIDERS[provider]["label"], "rclone", provider,
                                            {"remote": remote, "root": (b.get("root") or "Footage Harbor").strip("/"),
                                             "account": (b.get("fields") or {}).get("host", "")})})

        def api_connect_paste(self):
            self._require_rclone()
            b = self.body()
            names = app.cloud.paste_config(b.get("config", ""))
            ids = [self._add_dest(n, "rclone", app.cloud.remote_type(n),
                                  {"remote": n, "root": (b.get("root") or "Footage Harbor").strip("/"),
                                   "account": ""}) for n in names]
            self.json({"ids": ids})

        # history ---------------------------------------------------------
        def api_imports(self):
            rows = app.db.q("SELECT i.id, i.card_id, i.flow_name, i.slot, i.state, i.started_at, i.finished_at, "
                            "i.files_total, i.bytes_total, i.files_new, i.files_deleted, i.error, "
                            "c.name card_name, c.label card_label FROM imports i LEFT JOIN cards c ON c.id=i.card_id "
                            "ORDER BY i.id DESC LIMIT 200")
            self.json(rows)

        def api_import(self, iid):
            imp = app.db.one("SELECT * FROM imports WHERE id=?", (int(iid),))
            if not imp:
                return self.err("Import unbekannt", 404)
            imp["summary"] = json.loads(imp["summary"] or "{}")
            imp.pop("flow_snapshot", None)
            files = app.db.q("SELECT * FROM files WHERE import_id=? ORDER BY rel_path", (int(iid),))
            dests = {d["id"]: d["name"] for d in app.station.destinations()}
            for f in files:
                f["copies"] = [{"destination": dests.get(r["destination_id"], "entfernt"), "path": r["path"],
                                "method": r["method"], "verified_at": r["verified_at"]}
                               for r in app.db.q("SELECT * FROM receipts WHERE sha256=? AND sha256!=''",
                                                 (f["sha256"],))]
            imp["files"] = files
            self.json(imp)

        # settings --------------------------------------------------------
        def api_settings(self):
            self.json({"station": app.station_info(), "settings": app.settings(), "update": app.update_status()})

        def api_update_state(self):
            # Only the updater service on this machine may ask.
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                return self.err("Nicht erlaubt", 403)
            busy = any(s.state in ("running", "mounting") for s in app.station.slots.values())
            self.json({"enabled": bool(app.db.get("auto_update", True)), "busy": busy})

        def api_settings_save(self):
            b = self.body()
            if b.get("name"):
                app.db.set("station_name", b["name"].strip()[:40])
            for key, default in SETTINGS.items():
                if key in b:
                    val = b[key]
                    if isinstance(default, bool):
                        val = bool(val)
                    elif isinstance(default, str):
                        val = str(val or "").strip()[:300]
                    if key == "bwlimit" and val and not re.fullmatch(r"[0-9A-Za-z.:, |\-]+", val):
                        return self.err("Bandbreite: z. B. 10M oder 08:00,2M 23:00,off")
                    if key == "notify_url" and val and not val.startswith(("http://", "https://")):
                        return self.err("Benachrichtigung braucht eine http(s)-Adresse")
                    app.db.set(key, val)
            app.cloud.bwlimit = app.db.get("bwlimit", "")
            if b.get("new_password"):
                if not check_password(b.get("password", ""), app.db.get("password")):
                    return self.err("Aktuelles Passwort stimmt nicht", 401)
                if len(b["new_password"]) < 8:
                    return self.err("Passwort braucht mindestens 8 Zeichen")
                app.db.set("password", hash_password(b["new_password"]))
            app.station.changed()
            self.json({"ok": True})

        def api_logout_all(self):
            mine = self._cookie()
            app.sessions = {k: v for k, v in app.sessions.items() if k == mine}
            app.db.set("sessions", app.sessions)
            self.json({"ok": True})

        def api_system(self, action):
            if app.dev:
                return self.err("Im Entwicklungsmodus nicht verfügbar")
            cmd = {"restart": ["systemctl", "restart", "footage-harbor"],
                   "reboot": ["systemctl", "reboot"], "poweroff": ["systemctl", "poweroff"]}[action]
            busy = [s for s in app.station.slots.values() if s.state in ("running", "confirm")]
            if busy:
                return self.err("Es läuft noch ein Import")
            self.json({"ok": True})
            threading.Timer(1.0, lambda: subprocess.Popen(cmd)).start()

        def api_export(self):
            data = {"format": "footage-harbor-config/1", "exported_at": time.time(),
                    "station_name": app.db.get("station_name"), "settings": app.settings(),
                    "flows": app.station.flows(),
                    "destinations": [{k: d[k] for k in ("id", "name", "kind", "provider", "config")}
                                     for d in app.station.destinations()],
                    "cards": app.db.q("SELECT id,label,name,color,flow_id,registered,size FROM cards")}
            body = json.dumps(data, indent=2, default=str).encode()
            self._send(200, body, "application/json", {
                "Content-Disposition": 'attachment; filename="footage-harbor-config.json"'})

    ROUTES = [
        ("GET", r"/api/status", Handler.api_status),
        ("POST", r"/api/setup", Handler.api_setup),
        ("POST", r"/api/login", Handler.api_login),
        ("POST", r"/api/logout", Handler.api_logout),
        ("GET", r"/api/events", Handler.api_events),
        ("GET", r"/api/state", Handler.api_state),
        ("GET", r"/api/overview", Handler.api_overview),
        ("POST", r"/api/slots/([^/]+)/(start|eject|cancel|approve|decline)", Handler.api_slot_action),
        ("PUT", r"/api/slots/([^/]+)", Handler.api_slot_rename),
        ("GET", r"/api/cards", Handler.api_cards),
        ("PUT", r"/api/cards/(\d+)", Handler.api_card_update),
        ("DELETE", r"/api/cards/(\d+)", Handler.api_card_delete),
        ("GET", r"/api/flows", Handler.api_flows),
        ("POST", r"/api/flows", Handler.api_flow_save),
        ("PUT", r"/api/flows/(\d+)", Handler.api_flow_save),
        ("DELETE", r"/api/flows/(\d+)", Handler.api_flow_delete),
        ("POST", r"/api/flows/order", Handler.api_flow_order),
        ("POST", r"/api/flows/dryrun", Handler.api_flow_dryrun),
        ("GET", r"/api/destinations", Handler.api_destinations),
        ("POST", r"/api/destinations/local", Handler.api_dest_local),
        ("PUT", r"/api/destinations/(\d+)", Handler.api_dest_update),
        ("DELETE", r"/api/destinations/(\d+)", Handler.api_dest_delete),
        ("POST", r"/api/destinations/(\d+)/test", Handler.api_dest_test),
        ("POST", r"/api/connect/nextcloud", Handler.api_connect_nextcloud),
        ("POST", r"/api/connect/oauth", Handler.api_connect_oauth),
        ("POST", r"/api/connect/form", Handler.api_connect_form),
        ("POST", r"/api/connect/paste", Handler.api_connect_paste),
        ("GET", r"/api/connect/([\w-]+)", Handler.api_connect_status),
        ("POST", r"/api/connect/([\w-]+)/finish", Handler.api_connect_finish),
        ("POST", r"/api/connect/([\w-]+)/save", Handler.api_connect_save),
        ("GET", r"/api/imports", Handler.api_imports),
        ("GET", r"/api/imports/(\d+)", Handler.api_import),
        ("GET", r"/api/internal/update-state", Handler.api_update_state),
        ("GET", r"/api/settings", Handler.api_settings),
        ("PUT", r"/api/settings", Handler.api_settings_save),
        ("POST", r"/api/logout-all", Handler.api_logout_all),
        ("POST", r"/api/system/(restart|reboot|poweroff)", Handler.api_system),
        ("GET", r"/api/export", Handler.api_export),
    ]
    return Handler


SETTINGS = {"auto_update": True, "bwlimit": "", "notify_url": "", "notify_on": "all", "include_hidden": False,
            "default_template": DEFAULT_TEMPLATE}
REPO_URL = "https://github.com/ProfessorEngineergit/footage-harbor"

NODE_TYPES = {"trigger", "filter", "condition", "backup", "cleanup", "notify", "eject"}


def _validate_graph(graph):
    if not isinstance(graph, dict):
        raise ValueError("Ungültiger Flow")
    nodes = graph.get("nodes") or []
    if len(nodes) > 60:
        raise ValueError("Zu viele Schritte")
    ids = set()
    for n in nodes:
        if n.get("type") not in NODE_TYPES:
            raise ValueError(f"Unbekannter Schritt: {n.get('type')}")
        ids.add(n.get("id"))
    if sum(1 for n in nodes if n["type"] == "trigger") != 1:
        raise ValueError("Ein Flow braucht genau einen Auslöser")
    for e in graph.get("edges") or []:
        if e.get("from") not in ids or e.get("to") not in ids:
            raise ValueError("Verbindung zeigt ins Leere")


def _cookie(token):
    return f"fh_session={token}; Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; SameSite=Strict"


def _addresses():
    addrs = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(addrs)


def _cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as fh:
            return round(int(fh.read().strip()) / 1000, 1)
    except (OSError, ValueError):
        return None


def _suggest_archive(app):
    best = None
    for d in local_disks():
        if d["path"] in ("/",):
            continue
        if not best or d["free"] > best["free"]:
            best = d
    base = best["path"] if best else app.station.data_dir
    return os.path.join(base, "footage-harbor") if best else os.path.join(base, "archive")


def serve(app, host, port):
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    return httpd
