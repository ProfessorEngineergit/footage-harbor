"""Tests for the auto-updater against a fake GitHub and a fake station.

Run: python3 -m unittest discover tests
"""
import http.server
import io
import json
import os
import shutil
import tarfile
import tempfile
import threading
import unittest

from footage_harbor.updater import Config, Updater, parse_refs

OLD = "a" * 40
NEW = "b" * 40
REPO = "me/harbor"


def pkt(text):
    return b"%04x" % (len(text) + 4) + text


def advertisement(branch_sha):
    return (pkt(b"# service=git-upload-pack\n") + b"0000"
            + pkt(b"c" * 40 + b" HEAD\0multi_ack agent=git/test\n")
            + pkt(b"c" * 40 + b" refs/heads/main\n")
            + (pkt(branch_sha.encode() + b" refs/heads/stable\n") if branch_sha else b"")
            + b"0000")


def make_tree(marker, complete=True, evil=False):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        def add(name, data):
            info = tarfile.TarInfo(f"harbor-top/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        add("footage_harbor/__init__.py", b"")
        add("footage_harbor/__main__.py", b"")
        add("footage_harbor/server.py", f"MARK = {marker!r}\n".encode())
        add("footage_harbor/engine.py", b"")
        if complete:
            add("web/index.html", b"<html></html>")
        add("systemd/footage-harbor.service", b"[Service]\nEnvironment=FH_PORT=@PORT@\n")
        add("systemd/footage-harbor-updater.service", b"[Service]\nExecStart=@APP@/x\n")
        add("install.sh", b"#!/bin/sh\n")
        if evil:
            info = tarfile.TarInfo("harbor-top/../escaped.txt")
            info.size = 4
            tf.addfile(info, io.BytesIO(b"evil"))
    return buf.getvalue()


class Fake:
    """One local server playing GitHub (git + codeload) and the station API."""

    def __init__(self):
        self.branch_sha = NEW
        self.tarballs = {NEW: make_tree("new")}
        self.state = {"enabled": True, "busy": False}
        self.healthy = True
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, body=b"", ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith(f"/{REPO}.git/info/refs"):
                    return self.reply(200, advertisement(outer.branch_sha))
                if self.path.startswith(f"/{REPO}/tar.gz/"):
                    sha = self.path.rsplit("/", 1)[1]
                    return self.reply(200, outer.tarballs[sha]) if sha in outer.tarballs else self.reply(404)
                if self.path == "/api/internal/update-state":
                    return self.reply(200, json.dumps(outer.state).encode())
                if self.path == "/api/status":
                    return self.reply(200 if outer.healthy else 500, b"{}")
                self.reply(404)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.httpd.shutdown()


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = os.path.join(self.tmp, "app")
        self.data = os.path.join(self.tmp, "data")
        self.units = os.path.join(self.tmp, "units")
        for d in (self.app, self.data, self.units):
            os.makedirs(d)
        os.makedirs(os.path.join(self.app, "footage_harbor"))
        os.makedirs(os.path.join(self.app, "web"))
        for name, data in (("footage_harbor/__init__.py", ""), ("footage_harbor/server.py", "MARK = 'old'\n"),
                           ("web/index.html", "old"), ("VERSION", OLD + "\n")):
            with open(os.path.join(self.app, name), "w") as fh:
                fh.write(data)
        self.fake = Fake()
        env = {"FH_REPO": REPO, "FH_APP": self.app, "FH_DATA": self.data, "FH_PORT": "8080",
               "FH_GIT_BASE": self.fake.base, "FH_CODE_BASE": self.fake.base, "FH_LOCAL": self.fake.base,
               "FH_SYSTEMCTL": shutil.which("true"), "FH_UNIT_DIR": self.units, "FH_HEALTH_WAIT": "2"}
        self.up = Updater(Config(env))

    def tearDown(self):
        self.fake.close()
        shutil.rmtree(self.tmp)

    def mark(self):
        with open(os.path.join(self.app, "footage_harbor/server.py")) as fh:
            return fh.read().strip()

    def test_parse_refs(self):
        self.assertEqual(parse_refs(advertisement(NEW), "stable"), NEW)
        self.assertIsNone(parse_refs(advertisement(None), "stable"))

    def test_updates_to_new_commit(self):
        self.assertEqual(self.up.check_once(), "updated")
        self.assertEqual(self.up.installed(), NEW)
        self.assertEqual(self.mark(), "MARK = 'new'")
        self.assertEqual(self.up.check_once(), "current")

    def test_unit_files_are_rendered(self):
        self.up.check_once()
        with open(os.path.join(self.units, "footage-harbor.service")) as fh:
            self.assertIn("FH_PORT=8080", fh.read())

    def test_nothing_to_do_when_current(self):
        self.fake.branch_sha = OLD
        self.assertEqual(self.up.check_once(), "current")
        self.assertEqual(self.mark(), "MARK = 'old'")

    def test_waits_while_importing(self):
        self.fake.state = {"enabled": True, "busy": True}
        self.assertEqual(self.up.check_once(), "busy")
        self.assertEqual(self.mark(), "MARK = 'old'")
        self.fake.state = {"enabled": True, "busy": False}
        self.assertEqual(self.up.check_once(), "updated")

    def test_respects_switch_off(self):
        self.fake.state = {"enabled": False, "busy": False}
        self.assertEqual(self.up.check_once(), "disabled")
        self.assertEqual(self.up.installed(), OLD)

    def test_rolls_back_if_station_does_not_start(self):
        self.fake.healthy = False
        self.assertEqual(self.up.check_once(), "failed")
        self.assertEqual(self.up.installed(), OLD)
        self.assertEqual(self.mark(), "MARK = 'old'")
        self.fake.healthy = True
        self.assertEqual(self.up.check_once(), "skipped-failed")

    def test_incomplete_download_changes_nothing(self):
        self.fake.tarballs[NEW] = make_tree("new", complete=False)
        self.assertEqual(self.up.check_once(), "failed")
        self.assertEqual(self.mark(), "MARK = 'old'")

    def test_tar_path_traversal_is_ignored(self):
        self.fake.tarballs[NEW] = make_tree("new", evil=True)
        self.up.check_once()
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escaped.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.app, "..", "escaped.txt")))

    def test_offline_is_reported_not_raised(self):
        self.fake.close()
        self.assertEqual(self.up.check_once(), "offline")
        with open(os.path.join(self.data, "update.json")) as fh:
            self.assertIn("nicht erreichbar", json.load(fh)["error"])

    def test_missing_branch(self):
        self.fake.branch_sha = None
        self.assertEqual(self.up.check_once(), "no-branch")


if __name__ == "__main__":
    unittest.main()
