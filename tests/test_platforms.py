from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from helpers import TempProject
from reels import cli
from reels.analytics import DEFAULT_FIELDS, import_file as import_ig, stats
from reels.platforms import NETWORKS, import_file, network_of, parse_rows, report
from reels.util import ReelsError, read_json, write_json

TK = NETWORKS["tiktok"]["default"]
YT = NETWORKS["youtube"]["default"]


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.t = TempProject(self)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.posted = self.now - timedelta(days=8)

    def post(self, slug: str, caption: str) -> None:
        write_json(self.t.project.out_dir / slug / "post.json", {
            "slug": slug, "status": "posted", "caption": caption, "duration": 12, "file": "x", "cover": "y",
            "scheduledFor": self.posted.astimezone(timezone(timedelta(hours=-6))).replace(tzinfo=None).isoformat(),
            "timezone": "America/Chicago"})  # fmt: skip
        write_json(self.t.project.out_dir / slug / "plan.json", {"format": "pov-joke"})

    def file(self, data: dict, name: str = "pull.json") -> str:
        path = self.t.root / name
        write_json(path, {"pulledAt": self.now.isoformat(), **data})
        return str(path)

    def tk_row(self, caption: str, vid: str = "7708189167049575694", views="948", likes="18") -> list:
        return [self.posted.strftime("%Y%m%d%H%M%S"), caption, f"https://www.tiktok.com/@x/video/{vid}?utm_campaign=tt4d", "12", views,
                likes, "0", "0"] + [None] * 9  # fmt: skip

    def yt_row(self, title: str) -> list:
        return [self.posted.strftime("%Y%m%d%H%M%S"), title, "abcDEF123", "https://youtube.com/shorts/abcDEF123", "300", "30", None, "9", "1", "2"]

    def test_network_is_told_from_the_field_ids(self):
        self.assertEqual(network_of({"fields": TK, "rows": []}), "tiktok")
        self.assertEqual(network_of({"fields": ["TKEV07"], "rows": []}), "tiktok")
        self.assertEqual(network_of({"fields": YT, "rows": []}), "youtube")
        self.assertEqual(network_of({"fields": ["YTVP02", "YTVP17", "YTVP05"], "rows": []}), "youtube")
        self.assertIsNone(network_of({"fields": DEFAULT_FIELDS, "rows": []}))
        self.assertIsNone(network_of({"fields": ["TKPO02", "IGRE02"], "rows": []}))

    def test_tiktok_rows_parse_with_nulls_unknown_and_tracking_stripped(self):
        _, rows, days = parse_rows("tiktok", {"fields": TK, "rows": [self.tk_row("hi there friends")],
                                              "evolution": {"fields": ["TKEV07"], "rows": [["3.0", "20261115"], [None, "20261114"]]}})  # fmt: skip
        r = rows[0]
        self.assertEqual(r["id"], "7708189167049575694")
        self.assertNotIn("utm", r["url"])
        self.assertEqual((r["views"], r["likes"], r["comments"]), (948, 18, 0))
        self.assertIsNone(r["reach"])  # unknown, not zero
        self.assertEqual([d["date"] for d in days], ["2026-11-14", "2026-11-15"])
        self.assertIsNone(days[0]["followers"])

    def test_bad_shapes_are_refused(self):
        for net, data in (("tiktok", {"fields": ["TKPO99"], "rows": []}), ("tiktok", {"fields": TK, "rows": [["1"]]}),
                          ("tiktok", {"fields": ["TKPO07"], "rows": []}), ("youtube", {"fields": ["YTEV01"], "rows": [["1", "Nov"]]}),
                          ("tiktok", {"pulledAt": "yesterday", "fields": TK, "rows": []})):  # fmt: skip
            with self.subTest(data=data), self.assertRaises(ReelsError):
                parse_rows(net, data)

    def test_tiktok_import_matches_by_caption_without_line_breaks_and_saves_ids(self):
        cap = "houston group ride rules: every bridge up is a sprint.\n\n#cycling #sprint"
        self.post("bridge", cap)
        self.post("other", "something else entirely here")
        import_file(self.t.project, self.file({"fields": TK, "rows": [self.tk_row("houston group ride rules: every bridge up is a sprint. #cycling #sprint")]}))
        post = read_json(self.t.project.out_dir / "bridge" / "post.json")
        self.assertEqual(post["tiktokId"], "7708189167049575694")
        self.assertTrue(post["tiktokUrl"].startswith("https://www.tiktok.com/"))
        self.assertNotIn("tiktokId", read_json(self.t.project.out_dir / "other" / "post.json"))
        self.assertTrue(any((self.t.root / "analytics" / "tiktok").glob("*.json")))
        self.assertFalse(list((self.t.root / "analytics").glob("*.json")))  # not mixed into the Instagram snapshots

    def test_youtube_title_is_the_first_line_of_the_caption(self):
        self.post("bridge", "every bridge up is a sprint\n\nwho calls it?\n\n#cycling")
        import_file(self.t.project, self.file({"fields": YT, "rows": [self.yt_row("every bridge up is a sprint")]}))
        self.assertEqual(read_json(self.t.project.out_dir / "bridge" / "post.json")["youtubeId"], "abcDEF123")
        reels, unmatched, _ = report(self.t.project, "youtube")
        self.assertEqual(unmatched, [])
        self.assertAlmostEqual(reels[0]["likesView"], 0.03)
        self.assertIsNone(reels[0]["fullWatch"])

    def test_a_far_away_time_does_not_match(self):
        self.post("bridge", "every bridge up is a sprint")
        row = self.tk_row("every bridge up is a sprint")
        row[0] = (self.posted - timedelta(days=2)).strftime("%Y%m%d%H%M%S")
        import_file(self.t.project, self.file({"fields": TK, "rows": [row]}))
        self.assertNotIn("tiktokId", read_json(self.t.project.out_dir / "bridge" / "post.json"))

    def test_future_pull_is_refused(self):
        path = self.t.root / "f.json"
        write_json(path, {"pulledAt": (self.now + timedelta(hours=5)).isoformat(), "fields": TK, "rows": []})
        with self.assertRaises(ReelsError):
            import_file(self.t.project, str(path))

    def test_cli_routes_and_report_has_network_sections(self):
        self.post("bridge", "every bridge up is a sprint")
        ig = [self.posted.strftime("%Y%m%d%H%M%S"), "every bridge up is a sprint", "r1", "https://instagram.com/reel/x", 0, 10, 1000, 0, 1,
              2000, 9.0, 18000, None, 76.0, None]  # fmt: skip
        import_ig(self.t.project, self.file({"fields": DEFAULT_FIELDS, "rows": [ig]}, "ig.json"))
        cli.cmd_stats(self.t.project, SimpleNamespace(words=["import", self.file({"fields": TK, "rows": [self.tk_row("every bridge up is a sprint")],
                                                                                   "evolution": {"fields": ["TKEV07"], "rows": [["4.0", "20261115"]]}}, "tk.json")], since=None))  # fmt: skip
        stats(self.t.project)
        md = (self.t.project.out_dir / "PERFORMANCE.md").read_text()
        self.assertIn("## TikTok", md)
        self.assertIn("| bridge | 12.0 | 948 | 2000 |", md)
        self.assertIn("Followers: 4 on 2026-11-15", md)
        self.assertIn("## YouTube Shorts", md)
        self.assertIn("No YouTube Shorts numbers imported yet", md)


if __name__ == "__main__":
    unittest.main()
