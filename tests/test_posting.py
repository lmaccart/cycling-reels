from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from helpers import TempProject
from reels import queue as q
from reels.render import build, load_plan
from reels.util import ReelsError, read_json, write_json

CHICAGO = ZoneInfo("America/Chicago")


class FakeNow(datetime):
    fixed: datetime = datetime(2026, 11, 5, 12, 0, tzinfo=CHICAGO)

    @classmethod
    def now(cls, tz=None):
        return cls.fixed.astimezone(tz) if tz else cls.fixed


class HandleTests(unittest.TestCase):
    def test_valid_handles_are_normalized_and_deduplicated(self):
        errors: list[str] = []
        self.assertEqual(q.handles(["@Rice.Cycling", "rice.cycling", "team_mate1"], "collaborators", errors), ["rice.cycling", "team_mate1"])
        self.assertEqual(q.handles("@solo", "peopleTags", errors), ["solo"])
        self.assertEqual(errors, [])

    def test_bad_handles_own_account_and_limit_are_errors(self):
        errors: list[str] = []
        q.handles(["no spaces", "trailing.", "dou..ble", "x" * 31, 7], "collaborators", errors)
        self.assertEqual(len(errors), 5)
        errors = []
        q.handles(["@Me"], "collaborators", errors, own="me")
        self.assertIn("is this account", errors[0])
        errors = []
        q.handles([f"c{i}" for i in range(6)], "collaborators", errors, limit=q.MAX_COLLABORATORS)
        self.assertIn("at most 5", errors[0])
        errors = []
        q.handles({"a": 1}, "peopleTags", errors)
        self.assertIn("must be a list", errors[0])


class PlanPostTests(unittest.TestCase):
    def setUp(self):
        self.t = TempProject(self)
        self.t.asset("clip")
        self.t.project.config["brand"]["handle"] = "@myhandle"

    def build(self, post: dict) -> dict:
        plan = self.t.plan("collab", [{"asset": "clip", "in": 0, "out": 4}], post={"caption": "x", **post})
        return build(self.t.project, load_plan(self.t.project, plan))

    def test_collaborators_and_tags_reach_the_built_post(self):
        got = self.build({"collaborators": ["@Club.Account"], "peopleTags": ["@friend_1"]})["post"]
        self.assertEqual(got, {"collaborators": ["club.account"], "peopleTags": ["friend_1"]})

    def test_invalid_handles_fail_the_check(self):
        with self.assertRaises(ReelsError) as cm:
            self.build({"collaborators": ["@myhandle", "bad handle"]})
        self.assertIn("is this account", str(cm.exception))
        self.assertIn("isn't an Instagram handle", str(cm.exception))


class CarryOverTests(unittest.TestCase):
    def test_rerender_keeps_history_and_ids_but_not_the_old_upload(self):
        prev = {"status": "scheduled", "metricoolId": "413117128", "reelId": "1789", "instagramUrl": "https://instagram.com/reel/x",
                "mediaUrl": "https://pub.example/old.mp4", "history": [{"status": "approved", "at": "2026-11-01T17:45:04+00:00"}]}  # fmt: skip
        post = {"status": "rendered", "renderedAt": "2026-11-05T10:00:00+00:00", "mediaUrl": None, "metricoolId": None}
        q.carry_over(prev, post)
        self.assertEqual((post["metricoolId"], post["reelId"], post["instagramUrl"]), ("413117128", "1789", "https://instagram.com/reel/x"))
        self.assertIsNone(post["mediaUrl"])  # the file changed: it needs uploading again
        self.assertEqual(post["status"], "rendered")  # and approving again
        self.assertEqual([h["status"] for h in post["history"]], ["approved", "rendered"])

    def test_first_render_starts_the_history(self):
        post = q.carry_over({}, {"renderedAt": "2026-11-05T10:00:00+00:00"})
        self.assertEqual(post["history"], [{"status": "rendered", "at": "2026-11-05T10:00:00+00:00"}])


class QueueSlotTests(unittest.TestCase):
    def setUp(self):
        self.t = TempProject(self)
        self.t.project.config["schedule"] = {"timezone": "America/Chicago", "times": ["06:00", "18:30"]}

    def post(self, slug: str, **extra) -> None:
        write_json(self.t.project.out_dir / slug / "post.json", {"slug": slug, "status": "approved", "caption": "c", "file": f"out/{slug}/reel.mp4",
                                                                  "cover": f"out/{slug}/cover.jpg", **extra})  # fmt: skip

    def queued(self, now: datetime, **kw) -> dict[str, str]:
        with patch.object(q, "datetime", FakeNow), patch.object(FakeNow, "fixed", now):
            q.queue(self.t.project, **kw)
        return {p["slug"]: p["scheduledFor"] for _, p in q.posts(self.t.project)}

    def test_a_post_date_of_today_skips_slots_that_already_passed(self):
        self.post("today", postDate="2026-11-05")
        self.assertEqual(self.queued(datetime(2026, 11, 5, 12, 0, tzinfo=CHICAGO))["today"], "2026-11-05T18:30:00")

    def test_late_in_the_day_it_moves_to_tomorrow(self):
        self.post("late", postDate="2026-11-05")
        self.assertEqual(self.queued(datetime(2026, 11, 5, 18, 20, tzinfo=CHICAGO))["late"], "2026-11-06T06:00:00")  # 18:30 is < 15 min away

    def test_start_today_never_schedules_in_the_past(self):
        self.post("a")
        self.post("b")
        got = self.queued(datetime(2026, 11, 5, 7, 0, tzinfo=CHICAGO), start="2026-11-05")
        self.assertEqual(sorted(got.values()), ["2026-11-05T18:30:00", "2026-11-06T06:00:00"])

    def test_queue_md_lists_collabs_tags_and_cover_time(self):
        self.post("collab", postDate="2026-11-06", collaborators=["club"], peopleTags=["friend"], coverMs=2467)
        self.queued(datetime(2026, 11, 5, 12, 0, tzinfo=CHICAGO))
        md = (self.t.project.out_dir / "QUEUE.md").read_text()
        self.assertIn("- Collab with: @club (each accepts the invite in the Instagram app)", md)
        self.assertIn("- Tag: @friend", md)
        self.assertIn("(frame at 2.5 s)", md)
        self.assertNotIn("first comment", md.lower())
        self.assertEqual(read_json(self.t.project.out_dir / "collab/post.json")["status"], "queued")


if __name__ == "__main__":
    unittest.main()
