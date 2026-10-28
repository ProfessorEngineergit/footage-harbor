"""Auto-updater: python3 -m footage_harbor.updater

Runs as its own service so it can restart the main one. It watches the `stable`
branch, which CI only moves forward once the tests pass, downloads a new commit,
checks it, swaps it in and rolls back if the station does not come up again.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

MANAGED = ["footage_harbor", "web", "systemd", "install.sh", "uninstall.sh", "LICENSE", "README.md", "VERSION"]
UNITS = {"footage-harbor.service": "/etc/systemd/system/footage-harbor.service",
         "footage-harbor-updater.service": "/etc/systemd/system/footage-harbor-updater.service"}


class Config:
    def __init__(self, env=os.environ):
        self.repo = env.get("FH_REPO", "ProfessorEngineergit/footage-harbor")
        self.branch = env.get("FH_CHANNEL", "stable")
        self.app = env.get("FH_APP", "/opt/footage-harbor")
        self.data = env.get("FH_DATA", "/var/lib/footage-harbor")
        self.port = env.get("FH_PORT", "80")
        self.interval = int(env.get("FH_UPDATE_INTERVAL", "15"))
        self.git_base = env.get("FH_GIT_BASE", "https://github.com")
        self.code_base = env.get("FH_CODE_BASE", "https://codeload.github.com")
        self.local = env.get("FH_LOCAL", f"http://127.0.0.1:{self.port}")
        self.systemctl = env.get("FH_SYSTEMCTL", "systemctl")
        self.unit_dir = env.get("FH_UNIT_DIR", "/etc/systemd/system")
        self.health_wait = int(env.get("FH_HEALTH_WAIT", "40"))


def log(*a):
    print("[updater]", *a, flush=True)


def parse_refs(data, branch):
    """Pull the commit of refs/heads/<branch> out of a git-upload-pack advertisement."""
    want = f"refs/heads/{branch}"
    i = 0
    while i + 4 <= len(data):
        try:
            n = int(data[i:i + 4], 16)
        except ValueError:
            return None
        if n == 0:
            i += 4
            continue
        line = data[i + 4:i + n].split(b"\0")[0].strip().decode("ascii", "replace")
        sha, _, ref = line.partition(" ")
        if ref == want and len(sha) == 40:
            return sha
        i += n
    return None


class Updater:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state_path = os.path.join(cfg.data, "update.json")
        self.state = self._load()

    # ------------------------------------------------------------- state
    def _load(self):
        try:
            with open(self.state_path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save(self, **kw):
        self.state.update(kw)
        self.state["installed"] = self.installed()
        try:
            os.makedirs(self.cfg.data, exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self.state, fh)
            os.replace(tmp, self.state_path)
        except OSError as e:
            log("could not write state:", e)

    def installed(self):
        try:
            with open(os.path.join(self.cfg.app, "VERSION")) as fh:
                return fh.read().strip()
        except OSError:
            return ""

    # ----------------------------------------------------------- network
    def remote_sha(self):
        url = f"{self.cfg.git_base}/{self.cfg.repo}.git/info/refs?service=git-upload-pack"
        req = urllib.request.Request(url, headers={"User-Agent": "git/2.43.0", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return parse_refs(r.read(), self.cfg.branch)

    def local_state(self):
        """Ask the running station if updating is allowed right now."""
        try:
            with urllib.request.urlopen(f"{self.cfg.local}/api/internal/update-state", timeout=5) as r:
                return json.loads(r.read())
        except (OSError, ValueError):
            return {"enabled": True, "busy": False, "down": True}

    def download(self, sha):
        url = f"{self.cfg.code_base}/{self.cfg.repo}/tar.gz/{sha}"
        req = urllib.request.Request(url, headers={"User-Agent": "footage-harbor-updater"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()

    # ------------------------------------------------------------- steps
    def unpack(self, blob, dest):
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        os.makedirs(dest)
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            members = []
            for m in tf.getmembers():
                parts = m.name.split("/", 1)
                if len(parts) < 2 or not parts[1]:
                    continue
                name = parts[1]
                if name.startswith("/") or ".." in name.split("/") or not (m.isfile() or m.isdir()):
                    continue
                m.name = name
                members.append(m)
            tf.extractall(dest, members=members)
        for need in ("footage_harbor/__main__.py", "footage_harbor/server.py", "web/index.html", "systemd"):
            if not os.path.exists(os.path.join(dest, need)):
                raise RuntimeError(f"Update unvollständig: {need} fehlt")

    def verify(self, new):
        r = subprocess.run([sys.executable, "-m", "compileall", "-q", os.path.join(new, "footage_harbor")],
                           capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("Syntaxfehler im Update: " + (r.stdout + r.stderr)[-300:])
        env = dict(os.environ, PYTHONPATH=new, PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, "-c", "import footage_harbor.server, footage_harbor.engine"],
                           capture_output=True, text=True, env=env, cwd=new)
        if r.returncode:
            raise RuntimeError("Update lässt sich nicht laden: " + r.stderr[-300:])

    def swap(self, new, prev):
        if os.path.isdir(prev):
            shutil.rmtree(prev)
        os.makedirs(prev)
        for name in MANAGED:
            cur, nxt = os.path.join(self.cfg.app, name), os.path.join(new, name)
            if os.path.exists(cur):
                os.replace(cur, os.path.join(prev, name))
            if os.path.exists(nxt):
                os.replace(nxt, cur)

    def restore(self, prev):
        for name in MANAGED:
            old, cur = os.path.join(prev, name), os.path.join(self.cfg.app, name)
            if os.path.exists(old):
                if os.path.isdir(cur):
                    shutil.rmtree(cur)
                elif os.path.exists(cur):
                    os.remove(cur)
                os.replace(old, cur)

    def render_units(self):
        """Install changed unit files from the new tree. Returns True if anything changed."""
        changed = False
        subs = {"@PORT@": self.cfg.port, "@APP@": self.cfg.app, "@DATA@": self.cfg.data}
        for name in UNITS:
            src = os.path.join(self.cfg.app, "systemd", name)
            if not os.path.isfile(src):
                continue
            with open(src) as fh:
                text = fh.read()
            for k, v in subs.items():
                text = text.replace(k, v)
            dst = os.path.join(self.cfg.unit_dir, name)
            try:
                with open(dst) as fh:
                    if fh.read() == text:
                        continue
            except OSError:
                pass
            with open(dst, "w") as fh:
                fh.write(text)
            changed = True
        return changed

    def ctl(self, *args):
        return subprocess.run([self.cfg.systemctl, *args], capture_output=True, text=True)

    def healthy(self):
        deadline = time.time() + self.cfg.health_wait
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{self.cfg.local}/api/status", timeout=3) as r:
                    if r.status == 200:
                        return True
            except OSError:
                pass
            time.sleep(1)
        return False

    # ------------------------------------------------------------- update
    def apply(self, sha):
        app = self.cfg.app
        new, prev = os.path.join(app, ".update-new"), os.path.join(app, ".update-prev")
        log("updating to", sha[:7])
        blob = self.download(sha)
        self.unpack(blob, new)
        with open(os.path.join(new, "VERSION"), "w") as fh:
            fh.write(sha + "\n")
        self.verify(new)
        self.swap(new, prev)
        self.render_units()
        self.ctl("daemon-reload")
        self.ctl("restart", "footage-harbor")
        if not self.healthy():
            log("new version did not come up, rolling back")
            self.restore(prev)
            self.render_units()
            self.ctl("daemon-reload")
            self.ctl("restart", "footage-harbor")
            raise RuntimeError("Neue Version startete nicht, zurückgerollt")
        shutil.rmtree(new, ignore_errors=True)
        log("now running", sha[:7])

    def check_once(self):
        """One poll. Returns a short status word, mostly useful for tests."""
        now = time.time()
        try:
            sha = self.remote_sha()
        except (OSError, urllib.error.URLError) as e:
            self._save(last_check=now, error=f"GitHub nicht erreichbar: {e}"[:200])
            return "offline"
        if not sha:
            self._save(last_check=now, error=f"Zweig {self.cfg.branch} nicht gefunden")
            return "no-branch"
        self._save(last_check=now, latest=sha, error="")
        if sha == self.installed():
            return "current"
        if sha == self.state.get("failed"):
            return "skipped-failed"
        st = self.local_state()
        if not st.get("enabled", True):
            self._save(pending=sha)
            return "disabled"
        if st.get("busy"):
            self._save(pending=sha)
            return "busy"
        try:
            self.apply(sha)
        except Exception as e:
            log("update failed:", e)
            self._save(failed=sha, error=str(e)[:200], pending="")
            return "failed"
        self._save(pending="", failed="", updated_at=time.time())
        return "updated"

    def run(self):
        log(f"watching {self.cfg.repo}@{self.cfg.branch}, installed {self.installed()[:7] or 'unknown'}")
        while True:
            result = self.check_once()
            if result == "updated":
                # the new code may include a new updater: let systemd start it fresh
                sys.exit(0)
            time.sleep(self.cfg.interval)


def main():
    Updater(Config()).run()


if __name__ == "__main__":
    main()
