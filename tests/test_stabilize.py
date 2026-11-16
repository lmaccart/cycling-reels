from __future__ import annotations

import math
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from reels import stabilize as st
from reels.sphere import camera_filter, path_value, plan_camera


def axis_quat(axis: tuple[float, float, float], deg: float) -> st.Quat:
    h = math.radians(deg) / 2
    n = math.sqrt(sum(a * a for a in axis))
    return (math.cos(h), *(math.sin(h) * a / n for a in axis))  # type: ignore[return-value]


def close(a, b, tol=1e-6) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


class MathTests(unittest.TestCase):
    def test_v360_angles_invert_v360_matrix(self):
        for yaw, pitch, roll in [(0, 0, 0), (30, 20, 10), (-120, -40, 170), (160, 25, 90), (10, 70, -30), (179, -89, 0)]:
            m = st.v360_matrix(yaw, pitch, roll)
            back = st.v360_matrix(*st.v360_angles(m))
            for r1, r2 in zip(m, back):
                self.assertTrue(close(r1, r2, 1e-6), (yaw, pitch, roll))

    def test_straight_down_is_still_representable(self):
        m = st.v360_matrix(40, -90, 25)  # tiny-planet style: yaw and roll collapse into one
        back = st.v360_matrix(*st.v360_angles(m))
        for r1, r2 in zip(m, back):
            self.assertTrue(close(r1, r2, 1e-5))

    def test_slerp_ends_and_midpoint(self):
        a, b = axis_quat((0, 1, 0), 0), axis_quat((0, 1, 0), 60)
        self.assertTrue(close(st.qslerp(a, b, 0), a))
        self.assertTrue(close(st.qslerp(a, b, 1), b))
        self.assertTrue(close(st.qslerp(a, b, 0.5), axis_quat((0, 1, 0), 30)))


class SettingTests(unittest.TestCase):
    def test_settings(self):
        errors: list[str] = []
        self.assertIsNone(st.parse_setting(None, errors, "s"))
        self.assertIsNone(st.parse_setting(False, errors, "s"))
        self.assertEqual(st.parse_setting(True, errors, "s"), {"mode": "smooth", "seconds": st.DEFAULT_SMOOTH})
        self.assertEqual(st.parse_setting(1.5, errors, "s"), {"mode": "smooth", "seconds": 1.5})
        self.assertEqual(st.parse_setting("strong", errors, "s"), {"mode": "smooth", "seconds": 2.0})
        self.assertEqual(st.parse_setting("lock", errors, "s"), {"mode": "lock"})
        self.assertEqual(errors, [])
        for bad in (9, "wobbly", [1]):
            errs: list[str] = []
            self.assertIsNone(st.parse_setting(bad, errs, "s"))
            self.assertTrue(errs, bad)


class CorrectionTests(unittest.TestCase):
    def shaky(self) -> st.Orientation:
        # 60 Hz: a slow 30° turn about y plus 6° of 7 Hz shake about z
        ts = [i / 60 for i in range(600)]
        qs = [st.qmul(axis_quat((0, 1, 0), 3 * t), axis_quat((0, 0, 1), 6 * math.sin(2 * math.pi * 7 * t))) for t in ts]
        return st.Orientation(ts, qs)

    @staticmethod
    def angle(m: st.Mat) -> float:
        return math.degrees(math.acos(max(-1.0, min(1.0, (m[0][0] + m[1][1] + m[2][2] - 1) / 2))))

    def test_steady_camera_needs_no_correction(self):
        ori = st.Orientation([0, 1, 2, 3], [axis_quat((1, 0, 0), 20)] * 4)
        for m in st.corrections(ori, [0.5, 1.5, 2.5], {"mode": "smooth", "seconds": 0.8}):
            self.assertLess(self.angle(m), 1e-4)

    def test_smoothing_removes_the_shake_but_follows_the_turn(self):
        ori = self.shaky()
        times = [2 + i / 30 for i in range(120)]
        corr = st.corrections(ori, times, {"mode": "smooth", "seconds": 0.8})
        # stabilized orientation = Aᵀ·R should be the slow turn only
        for t, a in zip(times, corr):
            r = st.qmat(ori.at(t))
            stab = st.mmul(st.mT(a), r)
            turn = st.qmat(axis_quat((0, 1, 0), 3 * t))
            residual = self.angle(st.mmul(st.mT(turn), stab))
            self.assertLess(residual, 1.0, t)  # was up to 6°
        self.assertGreater(max(self.angle(a) for a in corr), 4.0)

    def test_lock_holds_the_starting_orientation(self):
        ori = self.shaky()
        times = [1 + i / 30 for i in range(60)]
        corr = st.corrections(ori, times, {"mode": "lock"})
        r0 = st.qmat(ori.at(times[0]))
        for t, a in zip(times, corr):
            stab = st.mmul(st.mT(a), st.qmat(ori.at(t)))
            self.assertLess(self.angle(st.mmul(st.mT(r0), stab)), 1e-3)

    def test_quaternion_and_matrix_forms_agree(self):
        ori = self.shaky()
        times = [1.0, 1.5, 2.0]
        for q, m in zip(st.correction_quats(ori, times, {"mode": "smooth", "seconds": 1}),
                        st.corrections(ori, times, {"mode": "smooth", "seconds": 1})):
            for r1, r2 in zip(st.qmat(q), m):
                self.assertTrue(close(r1, r2, 1e-9))


class GpmfTests(unittest.TestCase):
    @staticmethod
    def klv(key: str, typ: str, size: int, repeat: int, payload: bytes) -> bytes:
        data = key.encode() + bytes([ord(typ) if typ != "\0" else 0, size]) + struct.pack(">H", repeat) + payload
        return data + b"\0" * ((-len(payload)) % 4)

    def test_cori_samples_are_read_and_normalised(self):
        rows = [(32767, 0, 0, 0), (23170, 0, 23170, 0)]
        payload = b"".join(struct.pack(">4h", *r) for r in rows)
        strm = self.klv("SCAL", "s", 2, 1, struct.pack(">h", 32767)) + self.klv("CORI", "s", 8, 2, payload)
        devc_body = self.klv("STRM", "\0", 1, len(strm), strm)
        buf = self.klv("DEVC", "\0", 1, len(devc_body), devc_body)
        out = st._cori(buf)
        self.assertEqual(len(out), 1)
        self.assertTrue(close(out[0][0], (1, 0, 0, 0), 1e-4))
        self.assertTrue(close(out[0][1], axis_quat((0, 1, 0), 90), 1e-4))


class PathTests(unittest.TestCase):
    def keys(self):
        return [
            {"at": 0.0, "yaw": 0.0, "pitch": 0.0, "fov": 90.0, "roll": 0.0},
            {"at": 1.0, "yaw": 90.0, "pitch": 0.0, "fov": 90.0, "roll": 0.0, "cut": True},
            {"at": 2.0, "yaw": 0.0, "pitch": 10.0, "fov": 90.0, "roll": 0.0},
        ]

    def test_cut_keyframe_holds_then_jumps(self):
        keys = self.keys()
        self.assertEqual(path_value(keys, 0.99)["yaw"], 0.0)
        self.assertEqual(path_value(keys, 1.0)["yaw"], 90.0)
        mid = path_value(keys, 1.5)
        self.assertTrue(0 < mid["yaw"] < 90 and 0 < mid["pitch"] < 10)
        self.assertEqual(path_value(keys, 3)["yaw"], 0.0)

    def test_plan_camera_reads_cut_and_stabilize(self):
        errors: list[str] = []
        warnings: list[str] = []
        tags = {"angles": {"road": {"yaw": 90, "pitch": 25, "fov": 100, "roll": 0}, "side": {"yaw": 160, "pitch": 25, "fov": 100, "roll": 90}}}
        spec = {"stabilize": "lock", "path": [{"at": 0, "view": "road", "cut": True}, {"at": 1.5, "view": "side", "cut": True}]}
        out = plan_camera(spec, tags, None, 3.0, errors, warnings, "segment 1")
        self.assertEqual(errors, [])
        self.assertEqual(out["stabilize"], {"mode": "lock"})
        self.assertNotIn("cut", out["path"][0])  # the first keyframe has nothing to cut from
        self.assertTrue(out["path"][1]["cut"])
        bad = plan_camera({"stabilize": "maybe", "path": [{"at": 0}, {"at": 1, "cut": "yes"}]}, tags, None, 3.0, errors, warnings, "segment 2")
        self.assertIsNone(bad["stabilize"])
        self.assertEqual(len(errors), 2)


class CameraFilterTests(unittest.TestCase):
    def test_identity_corrections_keep_the_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = Path(tmp) / "c.cmd"
            sph = {"projection": "flat", "view": {"yaw": 160, "pitch": 25, "fov": 100, "roll": 90}, "path": None}
            plain = camera_filter(sph, "eac", 1080, 1920, 30, 30, cmd)
            same = camera_filter(sph, "eac", 1080, 1920, 30, 30, cmd, stab=[st.IDENTITY] * 30)
            self.assertEqual(plain, same)  # nothing moves: a fixed v360, no command file

    def test_corrections_become_per_frame_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = Path(tmp) / "c.cmd"
            sph = {"projection": "flat", "view": {"yaw": 0, "pitch": 0, "fov": 90, "roll": 0}, "path": None}
            stab = [st.qmat(axis_quat((0, 0, 1), 5 * math.sin(n))) for n in range(10)]
            graph = camera_filter(sph, "eac", 1080, 1920, 30, 10, cmd, stab=stab)
            self.assertTrue(graph.startswith("sendcmd=f=c.cmd,v360@cam="))
            lines = cmd.read_text().strip().splitlines()
            self.assertEqual(len(lines), 9)
            self.assertIn("roll", lines[0])


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class V360ConventionTests(unittest.TestCase):
    """The stabilizer composes rotations in v360's own convention: check it against ffmpeg."""

    def test_v360_matrix_matches_ffmpeg(self):
        w, h = 64, 32

        def ray(i: int, j: int) -> tuple[float, float, float]:
            phi = (2 * (i + 0.5) / w - 1) * math.pi
            th = (2 * (j + 0.5) / h - 1) * math.pi / 2
            return (math.sin(phi) * math.cos(th), math.sin(th), math.cos(phi) * math.cos(th))

        src = bytearray()
        for j in range(h):
            for i in range(w):
                for c in ray(i, j):
                    src += struct.pack(">H", int((c * 0.5 + 0.5) * 65535))
        yaw, pitch, roll = 40.0, -30.0, 70.0
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb48be", "-s", f"{w}x{h}", "-i", "-",
             "-vf", f"v360=input=e:output=e:yaw={yaw}:pitch={pitch}:roll={roll}:interp=near",
             "-f", "rawvideo", "-pix_fmt", "rgb48be", "-"],
            input=bytes(src), capture_output=True, check=True,
        ).stdout  # fmt: skip
        m = st.v360_matrix(yaw, pitch, roll)
        errs = []
        for j in range(4, h - 4):
            for i in range(w):
                k = (j * w + i) * 6
                got = [v / 65535 * 2 - 1 for v in struct.unpack(">3H", out[k : k + 6])]
                d = ray(i, j)
                want = [sum(m[r][c] * d[c] for c in range(3)) for r in range(3)]
                n = math.sqrt(sum(g * g for g in got)) or 1
                errs.append(math.degrees(math.acos(max(-1, min(1, sum(g * x for g, x in zip(got, want)) / n)))))
        errs.sort()
        self.assertLess(errs[len(errs) // 2], 4.0)  # one source pixel is ~5.6° at this size


if __name__ == "__main__":
    unittest.main()
