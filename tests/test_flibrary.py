from __future__ import annotations

import json
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import TempProject
from reels import flibrary as fl
from reels.util import ReelsError

PROBE360 = {"width": 2944, "height": 1472, "duration": 100.0, "fps": 59.94, "hasAudio": True,
            "sphere": {"projection": "gopro-eac", "tracks": [0, 5], "face": 736, "overlap": 64, "width": 2944, "height": 1472}}
ANGLES = {"forward": {"yaw": 180.0, "pitch": 40.0, "fov": 90.0, "roll": 180.0}}


def make_360(fx: TempProject, aid="a-360") -> dict:
    asset = fx.asset(aid, duration=100.0, width=2944, height=1472, tags={"type": "x", "angles": ANGLES})
    asset["probe"] = dict(PROBE360)
    (fx.project.assets_dir / f"{aid}.json").write_text(json.dumps(asset))
    return asset


class ListTests(unittest.TestCase):
    def test_shape(self):
        fx = TempProject(self)
        make_360(fx)
        fx.asset("b-flat", duration=5.0)
        (fx.project.thumbs_dir / "a-360.jpg").write_bytes(b"x")
        out = fl.cmd_list(fx.project)
        self.assertEqual({a["id"] for a in out["assets"]}, {"a-360", "b-flat"})
        self.assertEqual(set(out), {"root", "exportsDir", "freeBytes", "assets"})
        a = next(x for x in out["assets"] if x["id"] == "a-360")
        for k in ("id", "kind", "media", "originalName", "created", "duration", "width", "height", "fps", "is360",
                  "projection", "thumb", "sheets", "sheetGrid", "tags", "angles", "exports"):
            self.assertIn(k, a)
        self.assertTrue(a["is360"])
        self.assertEqual(a["projection"], "gopro-eac")
        self.assertTrue(a["thumb"].endswith("a-360.jpg"))
        self.assertEqual(a["angles"]["forward"]["roll"], 180.0)
        self.assertNotIn("angles", a["tags"])
        b = next(x for x in out["assets"] if x["id"] == "b-flat")
        self.assertFalse(b["is360"])
        self.assertIsNone(b["projection"])
        self.assertIsNone(b["thumb"])
        json.dumps(out)

    def test_exports_listed_and_temp_files_skipped(self):
        fx = TempProject(self)
        make_360(fx)
        d = fl.exports_dir(fx.project)
        d.mkdir()
        (d / "a-360-equirect-stab.mp4").write_bytes(b"12345")
        (d / "a-360-flat-1080x1920-0123abcd.mp4").write_bytes(b"1")
        (d / ".a-360-equirect.mp4.partial").write_bytes(b"1")
        ex = fl.existing_exports(fx.project, "a-360")
        self.assertEqual({e["kind"] for e in ex}, {"equirect", "flat"})
        self.assertEqual(len(ex), 2)
        self.assertEqual(next(e for e in ex if e["kind"] == "flat")["label"], "flat 1080x1920")

    def test_sheet_grid(self):
        from PIL import Image

        fx = TempProject(self)
        a = make_360(fx)
        a["probe"]["duration"] = 40.0  # 10 tiles: one sheet, 4 rows of 3
        Image.new("RGB", (1600, 72 + 4 * 270 + 10)).save(fx.project.sheets_dir / "a-360.jpg")
        grid = fl.sheet_grid(a, fl.sheet_files(fx.project, "a-360"))
        self.assertEqual((grid["cols"], grid["rows"], grid["tileH"], len(grid["times"])), (3, 4, 260, 10))
        a["probe"]["duration"] = 400.0  # sheet count no longer matches: can't infer
        self.assertIsNone(fl.sheet_grid(a, fl.sheet_files(fx.project, "a-360")))


class JobTests(unittest.TestCase):
    def setUp(self):
        self.fx = TempProject(self)
        make_360(self.fx)
        self.keys = [{"at": 0, "yaw": 180, "pitch": 0, "roll": 180, "fov": 90}, {"at": 2, "yaw": 150, "pitch": 5, "roll": 180, "fov": 70}]

    def job(self, **kw):
        return {"id": "a-360", "kind": "flat", "in": 10, "out": 20, "keys": self.keys, **kw}

    def test_equirect_defaults(self):
        job, _ = fl.validate_job(self.fx.project, {"id": "a-360", "kind": "equirect"})
        self.assertEqual((job["in"], job["out"], job["width"], job["height"]), (0.0, 100.0, 2944, 1472))
        self.assertAlmostEqual(job["fps"], 60000 / 1001)
        self.assertEqual(fl.output_name(job), "a-360-equirect.mp4")
        job, _ = fl.validate_job(self.fx.project, {"id": "a-360", "kind": "equirect", "stabilize": True})
        self.assertEqual(fl.output_name(job), "a-360-equirect-stab.mp4")

    def test_flat_name_and_hash(self):
        j1, _ = fl.validate_job(self.fx.project, self.job())
        j2, _ = fl.validate_job(self.fx.project, self.job())
        j3, _ = fl.validate_job(self.fx.project, self.job(out=21))
        self.assertEqual(fl.output_name(j1), fl.output_name(j2))
        self.assertNotEqual(fl.output_name(j1), fl.output_name(j3))
        self.assertRegex(fl.output_name(j1), r"^a-360-flat-1080x1920-[0-9a-f]{8}\.mp4$")

    def test_invalid_jobs(self):
        bad = [
            {"kind": "flat"}, {"id": "nope", "kind": "equirect"}, {"id": "a-360", "kind": "gif"},
            self.job(**{"in": 20, "out": 20}), self.job(stabilize="yes"), self.job(width=1081), self.job(fps=0),
            self.job(keys=[]), self.job(keys=[{"at": 1, "yaw": 0}, {"at": 1, "yaw": 5}]),
            self.job(keys=[{"at": 0, "pitch": 95}]), self.job(keys=[{"at": 0, "fov": 5}]),
            self.job(keys=[{"at": 0, "roll": 200}]), self.job(keys=[{"at": 0, "yaw": float("nan")}]),
            self.job(keys=[{"at": 99}]),
        ]
        for j in bad:
            with self.assertRaises(ReelsError, msg=str(j)):
                fl.validate_job(self.fx.project, j)
        with self.assertRaises(ReelsError):
            fl.validate_job(self.fx.project, [])

    def test_photo_rejected(self):
        self.fx.asset("pic", kind="photo")
        with self.assertRaises(ReelsError):
            fl.validate_job(self.fx.project, {"id": "pic", "kind": "equirect"})

    def test_estimate_and_disk_guard(self):
        job, _ = fl.validate_job(self.fx.project, {"id": "a-360", "kind": "equirect"})
        size = fl.estimate_bytes(job)
        self.assertGreater(size, 100e6)
        self.assertTrue(fl.disk_check(self.fx.project, job, free=size + 11 * fl.GIB)["ok"])
        res = fl.disk_check(self.fx.project, job, free=size + 9 * fl.GIB)
        self.assertFalse(res["ok"])
        self.assertIn("not enough disk space", res["reason"])
        flat, _ = fl.validate_job(self.fx.project, self.job())
        peak = fl.estimate_bytes(flat) * 2
        self.assertFalse(fl.disk_check(self.fx.project, flat, free=peak + 9 * fl.GIB)["ok"])

    def test_export_refuses_without_writing(self):
        job = Path(self.fx.root) / "job.json"
        job.write_text(json.dumps({"id": "a-360", "kind": "equirect"}))
        (self.fx.project.media_dir / "a-360.mp4").write_bytes(b"x")
        orig = fl.free_bytes
        fl.free_bytes = lambda p: 1 * fl.GIB
        self.addCleanup(setattr, fl, "free_bytes", orig)
        with self.assertRaises(ReelsError):
            fl.cmd_export(self.fx.project, str(job))
        self.assertFalse(fl.exports_dir(self.fx.project).exists() and any(fl.exports_dir(self.fx.project).iterdir()))

    def test_existing_output_reused(self):
        job = Path(self.fx.root) / "job.json"
        job.write_text(json.dumps({"id": "a-360", "kind": "equirect"}))
        (self.fx.project.media_dir / "a-360.mp4").write_bytes(b"x")
        fl.exports_dir(self.fx.project).mkdir()
        (fl.exports_dir(self.fx.project) / "a-360-equirect.mp4").write_bytes(b"done")
        self.assertEqual(fl.cmd_export(self.fx.project, str(job)), 0)


class OrientationTests(unittest.TestCase):
    def test_tag_fallback_uses_full_forward_angle(self):
        self.assertEqual(fl._tag_level(ANGLES), {"yaw": -180.0, "pitch": 40.0, "roll": 180.0, "source": "tag"})
        self.assertEqual(fl._tag_level(None), {"yaw": 0.0, "pitch": 0.0, "roll": 0.0, "source": "none"})

    def test_level_rotation_without_media_falls_back_to_tag(self):
        fx = TempProject(self)
        a = make_360(fx)
        got = fl.level_rotation(fx.project, a)  # media file doesn't exist
        self.assertEqual(got["source"], "tag")

    def test_cmd_level_prints_the_rotation(self):
        fx = TempProject(self)
        make_360(fx)
        got = fl.cmd_level(fx.project, "a-360")  # media offline: tag fallback
        self.assertEqual({k: got[k] for k in ("yaw", "pitch", "roll")}, {"yaw": -180.0, "pitch": 40.0, "roll": 180.0})
        fx.asset("b-flat", duration=5.0)
        with self.assertRaises(ReelsError):
            fl.cmd_level(fx.project, "b-flat")

    def test_keys_compose_with_the_levelling_rotation(self):
        from reels.stabilize import mmul, v360_matrix

        level = {"yaw": -179.6, "pitch": 28.4, "roll": 178.6}
        straight = fl.in_source_frame(level, {"at": 0, "yaw": 0, "pitch": 0, "roll": 0, "fov": 90})
        self.assertEqual((straight["yaw"], straight["pitch"], straight["roll"], straight["fov"]), (-179.6, 28.4, 178.6, 90))
        key = {"at": 1, "yaw": 30, "pitch": -10, "roll": 5, "fov": 70, "cut": True}
        got = fl.in_source_frame(level, key)
        want = mmul(v360_matrix(**level), v360_matrix(30, -10, 5))
        have = v360_matrix(got["yaw"], got["pitch"], got["roll"])
        for i in range(3):
            for j in range(3):
                self.assertAlmostEqual(want[i][j], have[i][j], places=4)
        self.assertEqual((got["at"], got["fov"], got["cut"]), (1, 70, True))
        ident = fl.in_source_frame({"yaw": 0, "pitch": 0, "roll": 0}, key)
        self.assertEqual((ident["yaw"], ident["pitch"], ident["roll"]), (30, -10, 5))

    def test_level_frame_looks_forward_and_up_is_up(self):
        """From gravity + forward (stabilize._level_view): the levelled frame's z is forward, y is gravity."""
        from reels.stabilize import _level_view, v360_matrix

        down = (0.03, -0.88, 0.476)  # a bar-mounted, upside-down, tilted camera
        n = sum(c * c for c in down) ** 0.5
        down = tuple(c / n for c in down)
        fwd = (0.0, 0.476, 0.88)
        k = sum(a * b for a, b in zip(fwd, down))
        fwd = tuple(a - k * b for a, b in zip(fwd, down))
        n = sum(c * c for c in fwd) ** 0.5
        fwd = tuple(c / n for c in fwd)
        v = _level_view(fwd, down, 100)
        m = v360_matrix(v["yaw"], v["pitch"], v["roll"])
        for i in range(3):
            self.assertAlmostEqual(m[i][1], down[i], places=3)  # levelled down = gravity
            self.assertAlmostEqual(m[i][2], fwd[i], places=3)  # levelled forward = bike forward

    def test_filter_static_and_stabilized(self):
        base = fl._tag_level(ANGLES)
        f = fl.equirect_filter("eac", 2944, 1472, 30, 10, base, None, Path("/nonexistent"))
        self.assertIn("output=e", f)
        self.assertIn("yaw=-180.000", f)
        self.assertIn("roll=180.000", f)
        self.assertNotIn("sendcmd", f)
        ident = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
        self.assertNotIn("sendcmd", fl.equirect_filter("eac", 64, 32, 30, 5, base, [ident] * 5, Path("/nonexistent")))  # no motion
        from reels.stabilize import qmat

        tilt = qmat((0.9990, 0.04, 0.0, 0.0))
        with tempfile.TemporaryDirectory() as td:
            cmd = Path(td) / "c.cmd"
            f = fl.equirect_filter("eac", 64, 32, 30, 5, base, [ident, tilt, tilt, ident, tilt], cmd)
            self.assertIn("sendcmd=f=", f)
            self.assertIn("v360@cam=", f)
            self.assertIn("v360@cam", cmd.read_text())


def box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


def synthetic_mp4(moov_first: bool) -> bytes:
    """A hand-made MP4 whose two traks (video then audio) have stco entries pointing into mdat."""
    mdat_payload = bytes(range(200))

    def trak(handler: bytes, offsets: list[int]) -> bytes:
        stco = box(b"stco", struct.pack(">II", 0, len(offsets)) + b"".join(struct.pack(">I", o) for o in offsets))
        hdlr = box(b"hdlr", struct.pack(">I", 0) + b"\0\0\0\0" + handler + b"\0" * 12 + b"x\0")
        return box(b"trak", box(b"tkhd", b"\0" * 84) + box(b"mdia", hdlr + box(b"minf", box(b"stbl", stco))))

    def build(mdat_start: int) -> bytes:
        return box(b"moov", box(b"mvhd", b"\0" * 100) + trak(b"vide", [mdat_start + 8, mdat_start + 108]) + trak(b"soun", [mdat_start + 58]))

    ftyp = box(b"ftyp", b"isom\0\0\2\0isom")
    mdat = box(b"mdat", mdat_payload)
    if moov_first:
        moov_len = len(build(0))
        return ftyp + build(len(ftyp) + moov_len) + mdat
    return ftyp + mdat + build(len(ftyp))


def chunk_data(data: bytes) -> list[bytes]:
    """The 8 bytes each stco entry points at, in file order (video, video, audio); must stay the same."""
    out = []
    pos = 0
    while pos < len(data):
        size, typ = struct.unpack_from(">I4s", data, pos)
        if typ == b"stco":
            n = struct.unpack_from(">I", data, pos + 12)[0]
            out += [data[o : o + 4] for o in struct.unpack_from(f">{n}I", data, pos + 16)]
        if typ in (b"moov", b"trak", b"mdia", b"minf", b"stbl"):
            pos += 8
        else:
            pos += size
    return out


class MetalRequestTests(unittest.TestCase):
    def test_equirect_request_is_full_res_ntsc_with_one_rotation_key(self):
        fx = TempProject(self)
        a = make_360(fx)
        job, _ = fl.validate_job(fx.project, {"id": "a-360", "kind": "equirect", "in": 1, "out": 4})
        streams = [{"index": 0, "id": "1", "codec_type": "video", "color_range": "pc", "pix_fmt": "yuvj420p"},
                   {"index": 5, "id": "6", "codec_type": "video", "color_range": "pc", "pix_fmt": "yuvj420p"}]
        level = {"yaw": -179.6, "pitch": 28.4, "roll": 178.6}
        req = fl.metal_request(a, job, Path("/x/a.360"), Path("/x/o.mp4"), streams, 179, None, fl._equirect_keys(level))
        self.assertEqual((req["projection"], req["width"], req["height"], req["fps"], req["fpsDivisor"]), ("equirect", 2944, 1472, 60, 1001))
        self.assertEqual(req["keys"], [{"at": 0, "yaw": -179.6, "pitch": 28.4, "roll": 178.6, "fov": 90}])
        self.assertEqual(req["trackIDs"], [1, 6])
        self.assertIsNone(req["corrections"])
        quats = [[1.0, 0.0, 0.0, 0.0]] * 179
        req = fl.metal_request(a, job, Path("/x/a.360"), Path("/x/o.mp4"), streams, 179, quats, fl._equirect_keys(level))
        self.assertAlmostEqual(req["corrections"]["sourceStep"], 1001 / 60000)

    def test_flat_request_unchanged_shape(self):
        fx = TempProject(self)
        a = make_360(fx)
        keys = [{"at": 0, "yaw": 0, "pitch": 0, "roll": 0, "fov": 90}]
        job, _ = fl.validate_job(fx.project, {"id": "a-360", "kind": "flat", "in": 1, "out": 4, "keys": keys})
        streams = [{"index": 0, "id": "1", "codec_type": "video", "pix_fmt": "yuv420p"}, {"index": 5, "id": "6", "codec_type": "video", "pix_fmt": "yuv420p"}]
        req = fl.metal_request(a, job, Path("/x/a.360"), Path("/x/o.mp4"), streams, 90, None, keys)
        self.assertEqual((req["projection"], req["fps"]), ("flat", 30))
        self.assertNotIn("fpsDivisor", req)


class SphericalTests(unittest.TestCase):
    def check(self, data: bytes, moov_first: bool):
        before = chunk_data(data)
        out = fl.inject_spherical_v1(data)
        self.assertEqual(len(out), len(data) + len(fl.spherical_uuid_box()))
        boxes = fl.parse_boxes(out)  # raises if any top-level size is wrong
        self.assertEqual([b[0] for b in boxes], [b"ftyp", b"moov", b"mdat"] if moov_first else [b"ftyp", b"mdat", b"moov"])
        self.assertEqual(sum(b[2] for b in boxes), len(out))
        moov = next(b for b in boxes if b[0] == b"moov")
        traks = [b for b in fl.parse_boxes(out, moov[1] + 8, moov[1] + moov[2]) if b[0] == b"trak"]
        self.assertEqual(len(traks), 2)
        kids = [b[0] for b in fl.parse_boxes(out, traks[0][1] + 8, traks[0][1] + traks[0][2])]
        self.assertEqual(kids[-1], b"uuid")  # in the video trak
        self.assertNotIn(b"uuid", [b[0] for b in fl.parse_boxes(out, traks[1][1] + 8, traks[1][1] + traks[1][2])])
        u = fl.parse_boxes(out, traks[0][1] + 8, traks[0][1] + traks[0][2])[-1]
        self.assertEqual(out[u[1] + 8 : u[1] + 24], fl.SPHERICAL_UUID)
        self.assertIn(b"<GSpherical:Spherical>true</GSpherical:Spherical>", out[u[1] : u[1] + u[2]])
        self.assertIn(b"equirectangular", out[u[1] : u[1] + u[2]])
        self.assertEqual(chunk_data(out), before)  # every chunk offset still lands on the same media bytes
        self.assertEqual(fl.inject_spherical_v1(out), out)  # idempotent: re-injection replaces the box

    def test_moov_last(self):
        self.check(synthetic_mp4(False), False)

    def test_moov_first_shifts_offsets(self):
        self.check(synthetic_mp4(True), True)

    def test_file_versions_match_pure_function(self):
        for first in (False, True):
            data = synthetic_mp4(first)
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "x.mp4"
                p.write_bytes(data)
                fl.inject_spherical_file(p)
                self.assertEqual(p.read_bytes(), fl.inject_spherical_v1(data))

    def test_no_video_track(self):
        data = box(b"ftyp", b"isom") + box(b"moov", box(b"mvhd", b"\0" * 100))
        with self.assertRaises(ReelsError):
            fl.inject_spherical_v1(data)

    def test_real_ffmpeg_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.mp4"
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=128x64:rate=10:duration=1",
                                "-f", "lavfi", "-i", "sine=duration=1", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(p)],
                               capture_output=True)
            if r.returncode:
                self.skipTest("ffmpeg unavailable")
            for faststart in (False, True):
                if faststart:
                    q = Path(td) / "f.mp4"
                    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(p), "-c", "copy", "-movflags", "+faststart", str(q)], check=True)
                    p = q
                fl.inject_spherical_file(p)
                info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-count_frames", "-of", "json", str(p)],
                                                 capture_output=True, text=True, check=True).stdout)
                v = next(s for s in info["streams"] if s["codec_type"] == "video")
                self.assertEqual(int(v["nb_read_frames"]), 10)  # chunk offsets still decode
                self.assertIn(b"GSpherical:ProjectionType", p.read_bytes())
                dec = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-f", "null", "-"], capture_output=True, text=True)
                self.assertEqual(dec.stderr.strip(), "")


if __name__ == "__main__":
    unittest.main()
