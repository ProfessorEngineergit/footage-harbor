"""End-to-end tests of the import engine with the folder backend.

Run: python3 -m unittest discover tests
"""
import json
import os
import shutil
import tempfile
import time
import unittest

from footage_harbor.cloud import Rclone
from footage_harbor.db import DB
from footage_harbor.devices import FolderBackend
from footage_harbor.engine import Station


def graph(*steps, cleanup=None, filter_cfg=None):
    nodes = [{"id": "t", "type": "trigger", "x": 0, "y": 0, "config": {"match": "any", "start": "auto"}}]
    edges, prev, x = [], "t", 300
    if filter_cfg is not None:
        nodes.append({"id": "f", "type": "filter", "x": x, "y": 0, "config": filter_cfg})
        edges.append({"id": "ef", "from": prev, "fromPort": "out", "to": "f"})
        prev, x = "f", x + 300
    for i, dest in enumerate(steps):
        nid = f"b{i}"
        nodes.append({"id": nid, "type": "backup", "x": x, "y": 0,
                      "config": {"destination_id": dest, "template": "{label}/{path}"}})
        edges.append({"id": f"e{i}", "from": prev, "fromPort": "out", "to": nid})
        prev, x = nid, x + 300
    if cleanup is not None:
        nodes.append({"id": "c", "type": "cleanup", "x": x, "y": 0, "config": cleanup})
        edges.append({"id": "ec", "from": prev, "fromPort": "out", "to": "c"})
    return {"nodes": nodes, "edges": edges}


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cards = os.path.join(self.tmp, "cards")
        self.arch = [os.path.join(self.tmp, "ssd"), os.path.join(self.tmp, "hdd")]
        for p in self.arch:
            os.makedirs(p)
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.station = Station(self.db, FolderBackend(self.cards), Rclone(self.tmp), self.tmp, self.cards)
        self.dest = [self.db.x("INSERT INTO destinations(name,kind,provider,config,created_at) VALUES(?,?,?,?,?)",
                               (f"D{i}", "local", "local", json.dumps({"path": p}), time.time()))
                     for i, p in enumerate(self.arch)]

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def flow(self, g):
        self.db.x("DELETE FROM flows")
        self.db.x("INSERT INTO flows(name,enabled,priority,graph,updated_at) VALUES(?,?,?,?,?)",
                  ("t", 1, 1, json.dumps(g), time.time()))

    def card(self, name, files):
        root = os.path.join(self.cards, name)
        for rel, data in files.items():
            p = os.path.join(root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as fh:
                fh.write(data)
        return root

    def run_until_idle(self, approve=None):
        self.station.poll()
        deadline = time.time() + 10
        while time.time() < deadline:
            states = [s.state for s in self.station.slots.values() if s.media]
            if "confirm" in states and approve is not None:
                for s in self.station.slots.values():
                    if s.state == "confirm":
                        self.station.decide_cleanup(s.key, approve)
            if states and all(st not in ("running", "mounting", "confirm") for st in states):
                return
            time.sleep(0.05)
        self.fail("import did not finish")

    def last_import(self):
        return self.db.one("SELECT * FROM imports ORDER BY id DESC LIMIT 1")

    def test_copy_and_verify(self):
        self.flow(graph(self.dest[0]))
        self.card("SONY_A", {"DCIM/A.ARW": b"raw" * 1000, "DCIM/A.JPG": b"jpg"})
        self.run_until_idle()
        imp = self.last_import()
        self.assertEqual(imp["state"], "done")
        self.assertEqual(imp["files_new"], 2)
        with open(os.path.join(self.arch[0], "SONY_A/DCIM/A.ARW"), "rb") as fh:
            self.assertEqual(fh.read(), b"raw" * 1000)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM receipts")["n"], 2)

    def test_reinsert_does_not_copy_again(self):
        self.flow(graph(self.dest[0]))
        root = self.card("SONY_A", {"DCIM/A.ARW": b"one"})
        self.run_until_idle()
        tmp = root + "_out"
        os.rename(root, os.path.join(self.cards, ".out"))
        self.station.poll()
        os.rename(os.path.join(self.cards, ".out"), root)
        self.run_until_idle()
        self.assertEqual(self.last_import()["files_new"], 0)
        self.assertFalse(os.path.exists(tmp))

    def test_same_name_other_content_is_versioned(self):
        self.flow(graph(self.dest[0]))
        root = self.card("SONY_A", {"DCIM/A.ARW": b"first"})
        self.run_until_idle()
        os.rename(root, os.path.join(self.cards, ".out"))
        self.station.poll()
        shutil.rmtree(os.path.join(self.cards, ".out"))
        self.card("SONY_A", {"DCIM/A.ARW": b"second, different"})
        self.run_until_idle()
        d = os.path.join(self.arch[0], "SONY_A/DCIM")
        self.assertEqual(sorted(os.listdir(d)), ["A (2).ARW", "A.ARW"])

    def test_cleanup_deletes_only_verified_and_filtered(self):
        self.flow(graph(self.dest[0], self.dest[1], cleanup={"require": "all", "confirm": True},
                        filter_cfg={"kinds": ["raw"]}))
        root = self.card("SONY_A", {"DCIM/A.ARW": b"raw", "DCIM/A.JPG": b"jpg", "notes.txt": b"keep me"})
        self.run_until_idle(approve=True)
        self.assertFalse(os.path.exists(os.path.join(root, "DCIM/A.ARW")))
        self.assertTrue(os.path.exists(os.path.join(root, "DCIM/A.JPG")))
        self.assertTrue(os.path.exists(os.path.join(root, "notes.txt")))
        self.assertTrue(os.path.isdir(os.path.join(root, "DCIM")))
        self.assertEqual(self.last_import()["files_deleted"], 1)

    def test_declined_cleanup_keeps_everything(self):
        self.flow(graph(self.dest[0], cleanup={"require": "all", "confirm": True}))
        root = self.card("SONY_A", {"DCIM/A.ARW": b"raw"})
        self.run_until_idle(approve=False)
        self.assertTrue(os.path.exists(os.path.join(root, "DCIM/A.ARW")))

    def test_file_changed_before_delete_is_kept(self):
        self.flow(graph(self.dest[0], cleanup={"require": "all", "confirm": True}))
        root = self.card("SONY_A", {"DCIM/A.ARW": b"original"})
        self.station.poll()
        deadline = time.time() + 10
        while time.time() < deadline and not any(s.state == "confirm" for s in self.station.slots.values()):
            time.sleep(0.05)
        with open(os.path.join(root, "DCIM/A.ARW"), "wb") as fh:
            fh.write(b"tampered")
        self.run_until_idle(approve=True)
        with open(os.path.join(root, "DCIM/A.ARW"), "rb") as fh:
            self.assertEqual(fh.read(), b"tampered")
        self.assertEqual(self.last_import()["files_deleted"], 0)

    def test_cleanup_without_backup_never_deletes(self):
        g = graph(cleanup={"require": "all", "confirm": False})
        self.flow(g)
        root = self.card("SONY_A", {"DCIM/A.ARW": b"raw"})
        self.run_until_idle()
        self.assertTrue(os.path.exists(os.path.join(root, "DCIM/A.ARW")))

    def test_condition_branches(self):
        g = {"nodes": [
            {"id": "t", "type": "trigger", "x": 0, "y": 0, "config": {"match": "any", "start": "auto"}},
            {"id": "c", "type": "condition", "x": 300, "y": 0, "config": {"field": "label", "op": "matches", "value": "DJI*"}},
            {"id": "y", "type": "backup", "x": 600, "y": 0, "config": {"destination_id": self.dest[0], "template": "{path}"}},
            {"id": "n", "type": "backup", "x": 600, "y": 200, "config": {"destination_id": self.dest[1], "template": "{path}"}},
        ], "edges": [
            {"id": "1", "from": "t", "fromPort": "out", "to": "c"},
            {"id": "2", "from": "c", "fromPort": "yes", "to": "y"},
            {"id": "3", "from": "c", "fromPort": "no", "to": "n"},
        ]}
        self.flow(g)
        self.card("SONY_A", {"A.ARW": b"x"})
        self.run_until_idle()
        self.assertFalse(os.path.exists(os.path.join(self.arch[0], "A.ARW")))
        self.assertTrue(os.path.exists(os.path.join(self.arch[1], "A.ARW")))


if __name__ == "__main__":
    unittest.main()
