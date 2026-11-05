from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from helpers import TempProject
from reels.analytics import DEFAULT_FIELDS, build_report, engagement, import_file, stats
from reels.review import audience, register_experiment
from reels.util import ReelsError, write_json


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.t = TempProject(self)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def post(self, slug: str) -> None:
        write_json(self.t.project.out_dir / slug / "post.json", {"slug": slug, "status": "posted", "caption": slug, "duration": 10,
                                                                  "reelId": slug, "file": "x", "cover": "y"})  # fmt: skip
        write_json(self.t.project.out_dir / slug / "plan.json", {"format": "pov-joke"})

    def pull(self, rows: list[list], pulled: datetime | None = None) -> str:
        pulled = pulled or self.now
        path = self.t.root / "pull.json"
        write_json(path, {"pulledAt": pulled.isoformat(), "fields": DEFAULT_FIELDS, "rows": rows})
        return str(path)

    def row(self, slug: str, *, age_days: float = 8, comments=2, likes=6, reach=200, saved=1, shares=3, views=260) -> list:
        posted = self.now - timedelta(days=age_days)
        return [posted.strftime("%Y%m%d%H%M%S"), slug, slug, "https://instagram.com/reel/x", comments, likes, reach, saved, shares,
                views, 6.0, 1200, None, 55.0, None]  # fmt: skip

    def test_future_pull_time_is_refused_before_anything_is_saved(self):
        self.post("r")
        with self.assertRaises(ReelsError) as cm:
            import_file(self.t.project, self.pull([self.row("r")], self.now + timedelta(hours=5)))
        self.assertIn("in the future", str(cm.exception))
        self.assertFalse((self.t.root / "analytics").exists() and any((self.t.root / "analytics").iterdir()))

    def test_interaction_and_replay_ratios(self):
        self.post("r")
        import_file(self.t.project, self.pull([self.row("r")]))
        reels, _ = build_report(self.t.project)
        r = reels[0]
        self.assertAlmostEqual(r["commentsReach"], 0.01)
        self.assertAlmostEqual(r["engagement"], 12 / 200)
        self.assertAlmostEqual(r["viewsReach"], 1.3)
        stats(self.t.project)
        md = (self.t.project.out_dir / "PERFORMANCE.md").read_text()
        self.assertIn("views/reach", md)
        self.assertIn("1.30×", md)
        self.assertIn("eng/reach", md)

    def test_missing_part_makes_engagement_unknown_not_zero(self):
        self.assertIsNone(engagement({"likes": 3, "comments": 0, "shares": 1, "saved": None, "reach": 100}))
        self.assertEqual(engagement({"likes": 0, "comments": 0, "shares": 0, "saved": 0, "reach": 100}), 0)
        self.assertIsNone(engagement({"likes": 1, "comments": 0, "shares": 0, "saved": 0, "reach": 0}))

    def test_review_samples_carry_the_new_metrics_and_experiments_can_target_them(self):
        self.post("r")
        import_file(self.t.project, self.pull([self.row("r")]))
        sample = audience(self.t.project)["sevenDayWindow"][0]
        self.assertAlmostEqual(sample["commentsReach"], 0.01)
        self.assertAlmostEqual(sample["viewsReach"], 1.3)
        register_experiment(self.t.project, {"id": "q-1", "hypothesis": "a question in the caption may draw comments",
                                             "change": "caption ends on a question", "primaryMetric": "commentsReach",
                                             "successCriterion": "repeatedly higher comments per reach", "testSlugs": ["t1"],
                                             "controlSlugs": ["c1"]})  # fmt: skip


if __name__ == "__main__":
    unittest.main()
