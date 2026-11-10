from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from helpers import TempProject
from reels import cli
from reels.account import days, growth, import_file, is_account_pull, parse_days, section
from reels.review import review
from reels.util import ReelsError, write_json

FIELDS = ["IGEV01", "IGEV43", "IGEV44", "IGEV06", "IGEV42"]


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.t = TempProject(self)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def pull(self, rows: list[list], *, ago_hours: float = 0, name: str = "account.json", fields: list[str] | None = None) -> str:
        path = self.t.root / name
        write_json(path, {"pulledAt": (self.now - timedelta(hours=ago_hours)).isoformat(), "fields": fields or FIELDS, "rows": rows})
        return str(path)

    def test_rows_are_metrics_then_the_day_and_come_back_sorted(self):
        _, got = parse_days({"fields": FIELDS, "rows": [["14.0", None, None, "402.0", "11.0", "20261108"], [None, None, None, "0.0", "0.0", "20261020"]]})
        self.assertEqual([d["date"] for d in got], ["2026-10-20", "2026-11-08"])
        self.assertEqual(got[1]["followers"], 14)
        self.assertEqual(got[1]["reach"], 402)
        self.assertIsNone(got[0]["followers"])  # null is unknown, not zero
        self.assertIsNone(got[1]["gained"])

    def test_bad_shapes_are_refused(self):
        for data in ({"rows": [["1", "20261108"]]}, {"fields": ["IGEV99"], "rows": []}, {"fields": ["IGEV01"], "rows": [["1", "Nov 8"]]},
                     {"fields": ["IGEV01"], "rows": []} | {"pulledAt": "yesterday"}, []):  # fmt: skip
            with self.subTest(data=data), self.assertRaises(ReelsError):
                parse_days(data)

    def test_a_future_pull_is_refused_and_nothing_is_saved(self):
        with self.assertRaises(ReelsError):
            import_file(self.t.project, self.pull([["1", None, None, None, None, "20261108"]], ago_hours=-5))
        self.assertEqual(days(self.t.project), [])

    def test_later_pulls_win_but_unknown_never_erases_a_known_value(self):
        import_file(self.t.project, self.pull([["14", None, None, "402", "11", "20261108"]], ago_hours=48, name="a.json"))
        import_file(self.t.project, self.pull([[None, None, None, "410", "11", "20261108"], ["20", "6", "0", "100", "2", "20261109"]], name="b.json"))
        got = days(self.t.project)
        self.assertEqual((got[0]["followers"], got[0]["reach"]), (14, 410))
        self.assertEqual(growth(got), {"followers": 20, "asOf": "2026-11-09", "from": "2026-11-08", "change": 6, "knownDays": 2})

    def test_growth_with_no_or_one_known_count_claims_no_change(self):
        self.assertEqual(growth([{"date": "2026-11-08", "followers": None}])["followers"], None)
        one = growth([{"date": "2026-11-07", "followers": None}, {"date": "2026-11-08", "followers": 14.0}])
        self.assertEqual((one["followers"], one["change"]), (14, None))
        self.assertIsNone(growth([{"date": "2026-11-01", "followers": 9.0}], since="2026-11-02")["followers"])

    def test_report_puts_a_days_change_beside_that_days_reel_without_inventing_zeros(self):
        self.assertIn("No account numbers imported yet", "\n".join(section(self.t.project, [])))
        import_file(self.t.project, self.pull([["1", None, None, "154", "0", "20261106"], [None, None, None, "327", "2", "20261107"],
                                               ["14", None, None, "402", "11", "20261108"], [None, None, None, "0", "0", "20261020"]]))  # fmt: skip
        md = "\n".join(section(self.t.project, [{"date": "2026-11-08", "slug": "club-ride"}, {"date": "2026-11-07", "slug": "regroup"}]))
        self.assertIn("as Metricool reports them: 14 on 2026-11-08, +13 since 2026-11-06 (known on 2 of 4 days).", md)
        self.assertIn("| 2026-11-08 | club-ride | 402 | 11 | 14 | +13 | — | — |", md)
        self.assertIn("| 2026-11-07 | regroup | 327 | 2 | — | — | — | — |", md)
        self.assertNotIn("2026-10-20", md)  # an empty day with no reel is left out
        self.assertIn("not proof", md)

    def test_scheduled_and_posted_reels_are_listed_by_day_even_before_analytics_has_them(self):
        for slug, status in (("live", "posted"), ("soon", "scheduled"), ("draft", "rendered")):
            write_json(self.t.project.out_dir / slug / "post.json", {"slug": slug, "status": status, "postDate": "2026-11-08"})
        import_file(self.t.project, self.pull([["14", None, None, "402", "11", "20261108"], ["14", None, None, "90", "1", "20261109"]]))
        md = "\n".join(section(self.t.project))
        self.assertIn("| 2026-11-08 | live, soon | 402 | 11 | 14 | — |", md)
        self.assertIn("| 2026-11-09 | — | 90 | 1 | 14 | — |", md)  # an unchanged count is not a change

    def test_stats_import_routes_account_pulls_by_their_fields(self):
        self.assertTrue(is_account_pull({"fields": FIELDS}))
        self.assertFalse(is_account_pull({"fields": ["IGRE02", "IGEV01"]}))
        self.assertFalse(is_account_pull({"rows": []}))
        path = self.pull([["14", None, None, "402", "11", "20261108"]])
        self.assertEqual(cli.cmd_stats(self.t.project, SimpleNamespace(words=["import", path], since=None)), 0)
        self.assertEqual(len(list((self.t.root / "analytics" / "account").glob("*.json"))), 1)
        self.assertEqual(list((self.t.root / "analytics").glob("*.json")), [])  # not mistaken for reel analytics

    def test_weekly_review_records_followers_or_says_unknown(self):
        first = review(self.t.project)
        self.assertIsNone(first["account"]["followers"])
        md = next((self.t.root / "learning/reviews").glob("*.md")).read_text()
        self.assertIn("Followers: unknown", md)
        day = self.now.strftime("%Y%m%d")
        import_file(self.t.project, self.pull([["14", None, None, "402", "11", day]]))
        self.assertEqual(review(self.t.project)["account"]["followers"], 14)
        self.assertIn("Followers: 14 on", next((self.t.root / "learning/reviews").glob("*.md")).read_text())


if __name__ == "__main__":
    unittest.main()
