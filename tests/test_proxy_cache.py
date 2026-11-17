from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import TempProject
from reels.editor.service import remove_partial_proxies
from reels.editor.worker import _stat_identity, prepare_source_proxy, prepare_sphere_proxy
from reels.proxy import proxy_reservation
from reels.proxy_cache import MIN_FREE_BYTES, ProxyDiskFull, budget_root, reserve_disk
from reels.stabilize import Orientation, cached_correction_quats


class ProxyConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = TempProject(self)
        self.source = self.fixture.project.media_dir / "pano.mp4"
        self.source.write_bytes(b"original fixture")
        self.root = self.fixture.root / "workspace"
        self.root.mkdir()
        self.job = {"sourcePath": str(self.source), "sourceIdentity": _stat_identity(self.source),
                    "assetId": "pano", "sourceKind": "video", "mediaRoot": str(self.source.parent),
                    "workspaceRoot": str(self.root), "probe": {"duration": 2, "sphere": {"projection": "equirect", "width": 512, "height": 256}},
                    "start": 0, "end": 2, "stabilize": "medium", "view": {"yaw": 0}}

    def child(self, code, *args):
        process = subprocess.Popen([sys.executable, "-u", "-c", code, *map(str, args)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def finish():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
        self.addCleanup(finish)
        return process

    def line(self, process):
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(10), "child failed to reach synchronization point")
        result = process.stdout.readline().strip()
        self.assertTrue(result, process.stderr.read() if process.poll() is not None else "child closed stdout")
        return result

    def test_two_processes_share_one_complete_cache_entry_and_live_reader(self):
        code = '''
import json, sys
from pathlib import Path
from unittest.mock import patch
from reels.editor.worker import prepare_source_proxy, prepare_sphere_proxy
job = json.loads(sys.argv[1])
worker = prepare_source_proxy if sys.argv[2] == 'source' else prepare_sphere_proxy
def encode(source, output, *args, **kwargs):
    output.write_bytes(b'partial')
    print('encoding', flush=True)
    sys.stdin.readline()
    output.write_bytes(b'complete proxy')
with patch('reels.editor.worker.encode_proxy', side_effect=encode), patch('reels.stabilize.read_orientation', return_value=None):
    print(json.dumps(worker(job)), flush=True)
'''
        for kind in ("source", "sphere"):
            with self.subTest(kind=kind):
                directory = self.root / "preview" / ("sources" if kind == "source" else "spheres") / kind
                job = {**self.job, "revisionDir": str(directory)}
                first = self.child(code, json.dumps(job), kind)
                self.assertEqual(self.line(first), "encoding")
                # Even an old-looking partial owned by a CLI process is not an orphan.
                partial = next(directory.rglob(f".{kind}-*.mp4"))
                os.utime(partial, (time.time() - 3600,) * 2)
                self.assertEqual(remove_partial_proxies(self.root), [])
                self.assertTrue(partial.exists())
                second = self.child(code, json.dumps(job), kind)
                first.stdin.write("publish\n")
                first.stdin.flush()
                a = json.loads(self.line(first))
                b = json.loads(self.line(second))  # 'encoding' here would mean a duplicate/partial hit
                self.assertFalse(a["reused"])
                self.assertTrue(b["reused"])
                self.assertEqual(a["path"], b["path"])
                self.assertEqual(Path(a["path"]).read_bytes(), b"complete proxy")
                self.assertEqual(first.wait(10), 0)
                self.assertEqual(second.wait(10), 0)
                # An editor reader opened before a CLI cache hit sees the same immutable inode.
                with open(a["path"], "rb") as reader:
                    before = os.fstat(reader.fileno())
                    with patch("reels.editor.worker.encode_proxy", side_effect=AssertionError("re-encoded live media")), patch("reels.stabilize.read_orientation", return_value=None):
                        again = (prepare_source_proxy if kind == "source" else prepare_sphere_proxy)(job)
                    self.assertTrue(again["reused"])
                    self.assertEqual(Path(again["path"]).stat().st_ino, before.st_ino)
                    self.assertEqual(reader.read(), b"complete proxy")

    def test_disk_reservations_are_shared_and_abandoned_leases_reclaimed(self):
        root = budget_root(self.root)
        amount = proxy_reservation(180)
        code = '''
import sys
from pathlib import Path
from reels.proxy_cache import reserve_disk, MIN_FREE_BYTES
with reserve_disk(Path(sys.argv[1]), int(sys.argv[2]), lambda _: MIN_FREE_BYTES + int(sys.argv[2])):
    print('reserved', flush=True)
    sys.stdin.readline()
'''
        child = self.child(code, root, amount)
        self.assertEqual(self.line(child), "reserved")
        with reserve_disk(root, amount, lambda _: MIN_FREE_BYTES + 2 * amount):
            self.assertEqual(len(list(root.glob("*.reserve"))), 2)
        with self.assertRaises(ProxyDiskFull):
            with reserve_disk(root, amount, lambda _: MIN_FREE_BYTES + amount):
                self.fail("double-reserved the same free disk")
        child.kill()
        child.wait(10)
        with reserve_disk(root, amount, lambda _: MIN_FREE_BYTES + amount):
            self.assertEqual(len(list(root.glob("*.reserve"))), 1)
        self.assertEqual(list(root.glob("*.reserve")), [])

    def test_cached_media_plays_on_full_disk_but_new_encodes_are_refused(self):
        def encode(source, output, *args, **kwargs):
            output.write_bytes(b"finished")

        for worker, kind in ((prepare_source_proxy, "source"), (prepare_sphere_proxy, "sphere")):
            with self.subTest(kind=kind):
                job = {**self.job, "revisionDir": str(self.root / kind),
                       "freeDisk": lambda _: MIN_FREE_BYTES + proxy_reservation(2)}
                with patch("reels.editor.worker.encode_proxy", side_effect=encode), patch("reels.stabilize.read_orientation", return_value=None):
                    first = worker(job)
                full = {**job, "freeDisk": lambda _: MIN_FREE_BYTES - 1}
                with patch("reels.editor.worker.encode_proxy", side_effect=AssertionError("unexpected encode")):
                    self.assertTrue(worker(full)["reused"])
                    self.assertEqual(Path(first["path"]).read_bytes(), b"finished")
                    with self.assertRaises(ProxyDiskFull):
                        worker({**full, "revisionDir": str(self.root / f"{kind}-missing")})

    def test_cleanup_removes_abandoned_nested_aim_partial_but_keeps_finished_media(self):
        directory = self.root / "preview/spheres/key/0.000-180.000"
        directory.mkdir(parents=True)
        partial = directory / ".sphere-abandoned.mp4"
        finished = directory / "sphere.mp4"
        partial.write_bytes(b"partial")
        finished.write_bytes(b"finished")
        os.utime(partial, (time.time() - 3600,) * 2)
        self.assertEqual(remove_partial_proxies(self.root), [partial])
        self.assertEqual(finished.read_bytes(), b"finished")

    def test_unreadable_orientation_can_recover_without_a_permanent_negative_cache(self):
        root = self.root / "cori"
        args = (self.source, root, self.job["sourceIdentity"], 0, 2, 30, {"mode": "lock"})
        with patch("reels.stabilize.read_orientation", side_effect=OSError("temporary read failure")):
            self.assertFalse(cached_correction_quats(*args)["hasOrientation"])
        ori = Orientation([0, 2], [(1, 0, 0, 0), (1, 0, 0, 0)])
        with patch("reels.stabilize.read_orientation", return_value=ori):
            recovered = cached_correction_quats(*args)
        self.assertTrue(recovered["hasOrientation"])
        self.assertEqual(recovered["stabilization"]["quats"], [[1, 0, 0, 0]] * 60)


if __name__ == "__main__":
    unittest.main()
