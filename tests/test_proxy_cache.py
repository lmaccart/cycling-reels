from __future__ import annotations

import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import TempProject
from reels.editor.service import remove_partial_proxies
from reels.editor.worker import _stat_identity, prepare_source_proxy, prepare_sphere_proxy
from reels.editor.proxy import proxy_reservation
from reels.editor.proxy_cache import MIN_FREE_BYTES, ProxyDiskFull, budget_root, cache_lock, reserve_disk
from reels.stabilize import Orientation
from reels.editor.proxy_stabilize import cached_correction_quats


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
from reels.editor.proxy_cache import reserve_disk, MIN_FREE_BYTES
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

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_surviving_encoder_keeps_key_and_reservation_after_parent_signals(self):
        # Stop the parent just after Popen launches ffmpeg, before its caller can
        # enter subprocess.run's cleanup scope. SIGINT unwinds Python contexts;
        # SIGKILL cannot. In both cases the real encoder survives and writes.
        code = '''
import json, subprocess, sys
from pathlib import Path
from unittest.mock import patch
from reels.editor.proxy import encode_proxy
from reels.editor.proxy_cache import MIN_FREE_BYTES, proxy_job
job = json.loads(sys.argv[1])
job['freeDisk'] = lambda _: MIN_FREE_BYTES + int(sys.argv[2])
popen = subprocess.Popen
def launch(*args, **kwargs):
    process = popen(*args, **kwargs)
    print(process.pid, flush=True)
    sys.stdin.readline()
    return process
@proxy_job
def work(job):
    job['reserveOutput']()
    encode_proxy(Path('testsrc2=size=512x256:rate=30:duration=60'),
                 Path(job['revisionDir']) / '.source-interrupted.mp4', [],
                 inputs=['-re', '-f', 'lavfi'], seconds=60)
with patch('subprocess.Popen', side_effect=launch), patch('reels.editor.proxy.sys.platform', 'linux'):
    work(job)
'''
        amount = proxy_reservation(60)
        for sig in (signal.SIGINT, signal.SIGKILL):
            with self.subTest(signal=sig):
                directory = self.root / "preview/sources" / str(sig)
                job = {**self.job, "revisionDir": str(directory), "end": 60}
                parent = self.child(code, json.dumps(job), amount)
                encoder = int(self.line(parent))
                partial = directory / ".source-interrupted.mp4"
                try:
                    # Ensure ffmpeg owns an open output before interrupting Python.
                    deadline = time.monotonic() + 10
                    while not partial.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(partial.exists())
                    parent.send_signal(sig)
                    self.assertNotEqual(parent.wait(10), 0)
                    before = partial.stat().st_size
                    deadline = time.monotonic() + 10
                    while partial.stat().st_size <= before and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertGreater(partial.stat().st_size, before, "orphan encoder did not keep writing")
                    with cache_lock(directory, blocking=False) as acquired:
                        self.assertFalse(acquired, "released the key while the encoder still writes")
                    with self.assertRaises(ProxyDiskFull):
                        with reserve_disk(budget_root(self.root), amount, lambda _: MIN_FREE_BYTES + amount):
                            self.fail("replaced a surviving encoder's reservation")
                    os.utime(partial, (time.time() - 3600,) * 2)
                    self.assertEqual(remove_partial_proxies(self.root), [])
                    self.assertTrue(partial.exists())
                finally:
                    os.kill(encoder, signal.SIGTERM)
                    # The child is orphaned, so lock release is the exit barrier.
                    deadline = time.monotonic() + 10
                    while True:
                        with cache_lock(directory, blocking=False) as acquired:
                            if acquired:
                                break
                        self.assertLess(time.monotonic(), deadline, "encoder did not exit")
                        time.sleep(0.02)
                with reserve_disk(budget_root(self.root), amount, lambda _: MIN_FREE_BYTES + amount):
                    self.assertEqual(len(list(budget_root(self.root).glob("*.reserve"))), 1)
                os.utime(partial, (time.time() - 3600,) * 2)
                self.assertEqual(remove_partial_proxies(self.root), [partial])

    def test_covered_split_plays_without_persisting_missing_corrections_below_floor(self):
        directory = self.root / "sphere"
        job = {**self.job, "revisionDir": str(directory),
               "freeDisk": lambda _: MIN_FREE_BYTES + proxy_reservation(2)}
        ori = Orientation([0, 1, 2], [(1, 0, 0, 0), (0.98, 0, 0.2, 0), (1, 0, 0, 0)], (0, 1, 0))
        with patch("reels.editor.worker.encode_proxy", side_effect=lambda src, out, *a, **kw: out.write_bytes(b"finished")), patch("reels.stabilize.read_orientation", return_value=ori):
            full = prepare_sphere_proxy(job)
        saved = {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()}
        for setting in (False, "lock", "medium"):
            with self.subTest(stabilize=setting):
                request = {**job, "start": 0.5, "end": 1.5, "stabilize": setting,
                           "freeDisk": lambda _: MIN_FREE_BYTES - 1}
                with patch("reels.editor.worker.encode_proxy", side_effect=AssertionError("split re-encoded")), patch("reels.stabilize.read_orientation", return_value=ori):
                    split = prepare_sphere_proxy(request)
                self.assertEqual(split["status"], "ready")
                self.assertTrue(split["reused"])
                self.assertEqual(split["path"], full["path"])
                self.assertEqual((split["start"], split["end"]), (0, 2))
                self.assertEqual(split["gravity"], [0, 1, 0])
                if setting:
                    self.assertEqual(split["stabilization"]["start"], 0.5)
                    self.assertEqual(len(split["stabilization"]["quats"]), 30)
                    if setting == "lock":
                        self.assertEqual(split["stabilization"]["quats"][0], [1, 0, 0, 0])
                        self.assertNotEqual(split["stabilization"]["quats"][-1], [1, 0, 0, 0])
                else:
                    self.assertIsNone(split["stabilization"])
                self.assertEqual({p: p.read_bytes() for p in directory.rglob("*") if p.is_file()}, saved)
                # Once space is available, the same request may persist its sidecar.
                with patch("reels.editor.worker.encode_proxy", side_effect=AssertionError("split re-encoded")), patch("reels.stabilize.read_orientation", return_value=ori):
                    persisted = prepare_sphere_proxy({**request, "freeDisk": job["freeDisk"]})
                self.assertEqual(persisted, split)
                saved = {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()}

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
