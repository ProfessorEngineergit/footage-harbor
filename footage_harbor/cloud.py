"""Cloud destinations through rclone, plus browser-based sign-in.

Sign-in never happens on the station itself:
  * Nextcloud uses Login Flow v2: the station asks the server for a login URL,
    the user approves it in the browser of their own computer, the station
    polls and receives a revocable app password.
  * OAuth providers (Google Drive, Dropbox, Box, pCloud …) run `rclone
    authorize` on the station. The provider page opens in the user's browser.
    The provider then redirects to 127.0.0.1 – that address points at the
    user's own computer, so the browser shows an error page. The user pastes
    that address back into the web UI and the station replays it against its
    own local rclone listener. Passwords never touch the station.
"""
import base64
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

OAUTH_PROVIDERS = {
    "drive": {"label": "Google Drive", "backend": "drive", "extra": {"scope": "drive"}},
    "dropbox": {"label": "Dropbox", "backend": "dropbox", "extra": {}},
    "box": {"label": "Box", "backend": "box", "extra": {}},
    "pcloud": {"label": "pCloud", "backend": "pcloud", "extra": {}},
}

FORM_PROVIDERS = {
    "webdav": {"label": "WebDAV", "backend": "webdav",
               "fields": ["url", "user", "pass"], "secret": ["pass"], "fixed": {"vendor": "other"}},
    "sftp": {"label": "SFTP", "backend": "sftp",
             "fields": ["host", "user", "pass", "port"], "secret": ["pass"], "fixed": {}},
    "smb": {"label": "NAS (SMB)", "backend": "smb",
            "fields": ["host", "user", "pass", "domain"], "secret": ["pass"], "fixed": {}},
    "s3": {"label": "S3", "backend": "s3",
           "fields": ["provider", "access_key_id", "secret_access_key", "endpoint", "region"],
           "secret": [], "fixed": {"env_auth": "false"}},
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def _no_redirect_get(url, timeout=10):
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        resp = opener.open(url, timeout=timeout)
        return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


class Rclone:
    def __init__(self, data_dir):
        self.config = os.path.join(data_dir, "rclone.conf")
        self.bin = shutil.which("rclone")
        self._features = {}
        self._about = {}
        self.sessions = {}
        self.lock = threading.Lock()
        self.bwlimit = ""

    @property
    def available(self):
        return bool(self.bin)

    def version(self):
        if not self.bin:
            return None
        try:
            return self.run(["version"], timeout=10).stdout.splitlines()[0]
        except Exception:
            return None

    def run(self, args, timeout=None, check=False):
        if not self.bin:
            raise RuntimeError("rclone ist nicht installiert")
        r = subprocess.run([self.bin, "--config", self.config, *args],
                           capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError(_last_error(r.stderr) or f"rclone {args[0]} fehlgeschlagen")
        return r

    # remotes ----------------------------------------------------------------
    def remote_name(self, base):
        base = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "remote"
        existing = set(self.list_remotes())
        name, i = base, 2
        while name in existing:
            name, i = f"{base}-{i}", i + 1
        return name

    def list_remotes(self):
        if not self.bin:
            return []
        r = self.run(["listremotes"], timeout=10)
        return [l.rstrip(":") for l in r.stdout.split() if l.strip()]

    def create_remote(self, name, backend, params):
        args = ["config", "create", name, backend]
        for k, v in params.items():
            args.append(f"{k}={v}")
        args += ["--non-interactive", "--no-obscure"]
        self.run(args, timeout=60, check=True)

    def obscure(self, value):
        return self.run(["obscure", value], timeout=10, check=True).stdout.strip()

    def delete_remote(self, name):
        if self.bin:
            self.run(["config", "delete", name], timeout=10)

    def paste_config(self, text):
        """Accepts one or more [name] sections from `rclone config` on another computer."""
        sections = re.findall(r"^\[([^\]]+)\]\s*$", text, re.M)
        if not sections:
            raise ValueError("Kein [remote]-Abschnitt gefunden")
        existing = set(self.list_remotes())
        clash = [s for s in sections if s in existing]
        if clash:
            raise ValueError(f"Remote „{clash[0]}“ existiert bereits")
        os.makedirs(os.path.dirname(self.config), exist_ok=True)
        with open(self.config, "a") as f:
            f.write("\n" + text.strip() + "\n")
        os.chmod(self.config, 0o600)
        return sections

    def remote_type(self, name):
        r = self.run(["config", "dump"], timeout=10)
        try:
            return json.loads(r.stdout or "{}").get(name, {}).get("type", "rclone")
        except ValueError:
            return "rclone"

    def features(self, remote):
        if remote in self._features:
            return self._features[remote]
        r = self.run(["backend", "features", f"{remote}:"], timeout=30)
        feats = {}
        try:
            feats = json.loads(r.stdout)
        except ValueError:
            pass
        hashes = [h.lower() for h in feats.get("Hashes") or []]
        self._features[remote] = {"hashes": hashes}
        return self._features[remote]

    def about(self, remote, max_age=600):
        cached = self._about.get(remote)
        if cached and time.time() - cached[0] < max_age:
            return cached[1]
        info = {}
        try:
            r = self.run(["about", f"{remote}:", "--json"], timeout=30)
            if r.returncode == 0:
                info = json.loads(r.stdout)
            else:
                info = {"error": _last_error(r.stderr)}
        except Exception as e:
            info = {"error": str(e)}
        self._about[remote] = (time.time(), info)
        return info

    def test(self, remote, root):
        r = self.run(["lsf", "--max-depth", "1", f"{remote}:{root}"], timeout=60)
        if r.returncode != 0 and "directory not found" not in r.stderr:
            raise RuntimeError(_last_error(r.stderr))
        return True

    # transfer + verification ------------------------------------------------
    def upload(self, src, remote, dst_path):
        extra = ["--bwlimit", self.bwlimit] if self.bwlimit else []
        r = self.run(["copyto", src, f"{remote}:{dst_path}", "--retries", "3",
                      "--low-level-retries", "10", "--no-check-dest", *extra], timeout=None)
        if r.returncode != 0:
            raise RuntimeError(_last_error(r.stderr) or "Upload fehlgeschlagen")

    def remote_hash(self, remote, path, local_hashes):
        """Returns (method, matches). Uses a provider hash when the provider
        stores one we already computed; otherwise downloads and hashes."""
        feats = self.features(remote)
        for algo, key in (("sha256", "sha256"), ("sha1", "sha1"), ("md5", "md5")):
            if algo in feats["hashes"] and local_hashes.get(key):
                r = self.run(["hashsum", algo.upper(), f"{remote}:{path}"], timeout=120)
                got = (r.stdout.split() or [""])[0].lower()
                if r.returncode == 0 and re.fullmatch(r"[0-9a-f]{32,64}", got):
                    return f"{algo}-provider", got == local_hashes[key]
        r = self.run(["hashsum", "SHA256", "--download", f"{remote}:{path}"], timeout=None)
        got = (r.stdout.split() or [""])[0].lower()
        if r.returncode != 0 or not got:
            raise RuntimeError(_last_error(r.stderr) or "Prüfung fehlgeschlagen")
        return "sha256-download", got == local_hashes["sha256"]

    def exists_with_size(self, remote, path, size):
        r = self.run(["lsjson", f"{remote}:{path}"], timeout=60)
        try:
            items = json.loads(r.stdout or "[]")
        except ValueError:
            return False
        return any(i.get("Size") == size and not i.get("IsDir") for i in items)

    # sign-in sessions -------------------------------------------------------
    def start_nextcloud(self, server, device_name):
        server = server.strip().rstrip("/")
        if not re.match(r"^https?://", server):
            server = "https://" + server
        req = urllib.request.Request(server + "/index.php/login/v2", method="POST",
                                     headers={"User-Agent": device_name})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        sid = secrets.token_urlsafe(12)
        sess = {"id": sid, "provider": "nextcloud", "state": "waiting", "server": server,
                "poll": data["poll"], "login_url": data["login"], "created": time.time()}
        self.sessions[sid] = sess
        threading.Thread(target=self._poll_nextcloud, args=(sess,), daemon=True).start()
        return {"id": sid, "url": data["login"]}

    def _poll_nextcloud(self, sess):
        deadline = time.time() + 20 * 60
        while time.time() < deadline and sess["state"] == "waiting":
            time.sleep(2)
            body = urllib.parse.urlencode({"token": sess["poll"]["token"]}).encode()
            try:
                req = urllib.request.Request(sess["poll"]["endpoint"], data=body, method="POST")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    cred = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    continue
                sess.update(state="error", error=f"Nextcloud antwortet mit {e.code}")
                return
            except Exception:
                continue
            try:
                user = cred["loginName"]
                name = self.remote_name("nextcloud-" + user)
                url = cred["server"].rstrip("/") + "/remote.php/dav/files/" + urllib.parse.quote(user) + "/"
                self.create_remote(name, "webdav", {"url": url, "vendor": "nextcloud", "user": user,
                                                    "pass": self.obscure(cred["appPassword"])})
                sess.update(state="done", remote=name, account=f"{user} · {urllib.parse.urlparse(cred['server']).hostname}")
            except Exception as e:
                sess.update(state="error", error=str(e))
            return
        if sess["state"] == "waiting":
            sess.update(state="error", error="Anmeldung abgelaufen")

    def start_oauth(self, provider):
        spec = OAUTH_PROVIDERS[provider]
        with self.lock:
            for s in self.sessions.values():
                if s.get("proc") and s["proc"].poll() is None:
                    s["proc"].kill()
                    s["state"] = "error"
            proc = subprocess.Popen([self.bin, "--config", self.config, "authorize", spec["backend"],
                                     "--auth-no-open-browser"],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        sid = secrets.token_urlsafe(12)
        sess = {"id": sid, "provider": provider, "state": "starting", "proc": proc, "out": [],
                "created": time.time()}
        self.sessions[sid] = sess
        threading.Thread(target=self._read_oauth, args=(sess,), daemon=True).start()
        deadline = time.time() + 15
        while time.time() < deadline and not sess.get("local_url") and proc.poll() is None:
            time.sleep(0.1)
        if not sess.get("local_url"):
            proc.kill()
            raise RuntimeError("rclone authorize startete nicht: " + " ".join(sess["out"][-3:]))
        status, headers, _ = _no_redirect_get(sess["local_url"])
        auth_url = headers.get("Location") or headers.get("location")
        if not auth_url:
            proc.kill()
            raise RuntimeError("Anbieter-Adresse nicht gefunden")
        sess.update(state="waiting", auth_url=auth_url)
        return {"id": sid, "url": auth_url}

    def _read_oauth(self, sess):
        for line in sess["proc"].stdout:
            sess["out"].append(line.rstrip())
            m = re.search(r"(http://127\.0\.0\.1:\d+/auth\?state=[^\s]+)", line)
            if m:
                sess["local_url"] = m.group(1)

    def finish_oauth(self, sid, redirect_url):
        sess = self.sessions.get(sid)
        if not sess or sess["provider"] not in OAUTH_PROVIDERS:
            raise ValueError("Unbekannte Anmeldung")
        parsed = urllib.parse.urlparse(redirect_url.strip())
        qs = urllib.parse.parse_qs(parsed.query)
        if "code" not in qs:
            raise ValueError("In der Adresse fehlt der Freigabe-Code (…?code=…)")
        local = urllib.parse.urlparse(sess["local_url"])
        _no_redirect_get(f"http://127.0.0.1:{local.port}/?{parsed.query}", timeout=30)
        try:
            sess["proc"].wait(timeout=60)
        except subprocess.TimeoutExpired:
            sess["proc"].kill()
            raise RuntimeError("rclone hat kein Token geliefert")
        text = "\n".join(sess["out"])
        m = re.search(r"--->\s*(.+?)\s*<---", text, re.S)
        if not m:
            raise RuntimeError("Token nicht erhalten: " + (sess["out"][-1] if sess["out"] else ""))
        token = _decode_token(m.group(1).strip())
        spec = OAUTH_PROVIDERS[sess["provider"]]
        name = self.remote_name(sess["provider"])
        self.create_remote(name, spec["backend"], {"token": token, **spec["extra"]})
        sess.update(state="done", remote=name, account=spec["label"])
        sess.pop("proc", None)
        return sess

    def create_form_remote(self, provider, fields):
        spec = FORM_PROVIDERS[provider]
        params = dict(spec["fixed"])
        for f in spec["fields"]:
            v = (fields.get(f) or "").strip()
            if not v:
                continue
            params[f] = self.obscure(v) if f in spec["secret"] else v
        name = self.remote_name(provider + "-" + (fields.get("host") or fields.get("user") or ""))
        self.create_remote(name, spec["backend"], params)
        return name

    def session_info(self, sid):
        s = self.sessions.get(sid)
        if not s:
            return None
        return {k: s.get(k) for k in ("id", "provider", "state", "error", "remote", "account")}


def _decode_token(blob):
    if blob.startswith("{"):
        data = json.loads(blob)
    else:
        try:
            data = json.loads(base64.b64decode(blob + "=" * (-len(blob) % 4)))
        except Exception:
            return blob
    if isinstance(data, dict) and "token" in data and isinstance(data["token"], (str, dict)):
        tok = data["token"]
        return tok if isinstance(tok, str) else json.dumps(tok)
    return json.dumps(data)


def _last_error(stderr):
    lines = [l for l in (stderr or "").splitlines() if l.strip()]
    for l in reversed(lines):
        if "ERROR" in l or "Failed" in l or "error" in l:
            return re.sub(r"^\S+ \S+ ", "", l).strip()[:300]
    return lines[-1][:300] if lines else ""
