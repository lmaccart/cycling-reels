from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from reels.analytics import DEFAULT_FIELDS
from reels.cli import build_parser, cmd_render, main
from reels.config import Project
from reels.queue import mark
from reels.review import (
    audience, auto_event, experiment_results, record_feedback, register_experiment,
    review, week_start,
)
from reels.util import ReelsError, read_json, write_json


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Project(Path(self.temp.name))
        self.project.ensure_dirs()
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def post(self, slug="ride", status="rendered", **extra):
        data = {"slug": slug, "status": status, "caption": slug, "duration": 10,
                "reelId": slug, "file": f"out/{slug}/reel.mp4", "cover": f"out/{slug}/cover.jpg", **extra}
        write_json(self.project.out_dir / slug / "post.json", data)
        write_json(self.project.out_dir / slug / "plan.json", {"format": "pov-joke", "hook": "chill ride"})
        return data

    def snapshot(self, slug="ride", age=8, ago=0, reach=100, shares=3, saved=None, hold=50):
        pulled = self.now - timedelta(days=ago)
        posted = pulled - timedelta(days=age)
        row = [posted.strftime("%Y%m%d%H%M%S"), slug, slug, "https://instagram.com/reel/test",
               0, 5, reach, saved, shares, 120, 6, 720, None, hold, None]
        path = self.project.root / "analytics" / f"{pulled:%Y-%m-%dT%H%M%S}.json"
        old = read_json(path) if path.exists() else {"pulledAt": pulled.isoformat(), "fields": DEFAULT_FIELDS, "rows": []}
        old["rows"].append(row)
        write_json(path, old)

    def design(self):
        return {"id": "face-first", "hypothesis": "face-first may improve hold", "change": "opening shot only",
                "primaryMetric": "hold", "successCriterion": "repeated material improvement",
                "testSlugs": ["test1", "test2", "test3"], "controlSlugs": ["control1", "control2", "control3"]}

    def lesson(self, state="tentative"):
        return {"id": "audio-01", "category": "production", "observation": "wind masked the freehub",
                "evidence": ["feedback: actual test fixture"], "confidence": "low", "state": state,
                "nextAction": "listen before rendering", "reviewDate": "2026-11-09"}

    def test_empty_project_creates_learning_without_posting(self):
        result = review(self.project)
        self.assertTrue(result["audience"]["stale"])
        self.assertEqual(result["audience"]["latest"], [])
        self.assertFalse(result["schedulingComplete"])
        self.assertIn("No active lessons", (self.project.root / "learning/LESSONS.md").read_text())
        self.assertEqual(list(self.project.out_dir.glob("*/post.json")), [])

    def test_old_pull_does_not_mature_with_wall_clock(self):
        self.post()
        self.snapshot(age=1, ago=10)
        result = audience(self.project)
        self.assertTrue(result["stale"])
        self.assertEqual(result["under48Hours"], 1)
        self.assertEqual(result["sevenDayWindow"], [])

    def test_window_uses_earliest_eligible_snapshot_not_lifetime(self):
        self.post()
        self.snapshot(age=1, ago=19, shares=1)
        self.snapshot(age=8, ago=12, shares=3)
        self.snapshot(age=11, ago=9, shares=8)
        self.snapshot(age=20, shares=20)
        result = audience(self.project)
        self.assertEqual(result["latest"][0]["sharesReach"], .2)
        self.assertEqual(result["sevenDayWindow"][0]["sharesReach"], .03)
        self.assertEqual(result["sevenDayWindow"][0]["ageDays"], 8)
        self.assertEqual(result["latest"][0]["format"], "pov-joke")

    def test_no_window_substitution_for_old_totals(self):
        self.post()
        self.snapshot(age=20)
        self.assertEqual(audience(self.project)["sevenDayWindow"], [])

    def test_missing_zero_and_nonfinite_metrics(self):
        self.post()
        self.snapshot(shares=0, saved=None, hold=float("nan"))
        sample = audience(self.project)["latest"][0]
        self.assertEqual(sample["sharesReach"], 0)
        self.assertIsNone(sample["savesReach"])
        self.assertIsNone(sample["hold"])
        self.assertEqual(sample["watchThrough"], .6)

    def test_zero_reach_is_unknown_ratio(self):
        self.post()
        self.snapshot(reach=0, shares=0)
        self.assertIsNone(audience(self.project)["latest"][0]["sharesReach"])

    def test_unmatched_and_malformed_snapshots_reported(self):
        self.snapshot(slug="unknown")
        bad = self.project.root / "analytics/bad.json"
        bad.write_text("not JSON")
        result = audience(self.project)
        self.assertEqual(result["unmatchedReelIds"], ["unknown"])
        self.assertEqual(result["skippedSnapshots"], ["bad.json"])

    def test_future_snapshot_is_not_evidence(self):
        self.post()
        self.snapshot(ago=-2)
        self.assertEqual(audience(self.project)["latest"], [])

    def test_feedback_categories_and_week_boundaries(self):
        record_feedback(self.project, "preference", "revision", "less polished", slug="ride", at="2026-10-26T06:00:00Z")
        record_feedback(self.project, "production", "audio", "wind", slug="ride", at="2026-10-26T04:00:00Z")
        result = review(self.project, week="2026-10-28")
        self.assertEqual(len(result["feedback"]), 1)  # Chicago Monday starts at 05:00 UTC.
        self.assertEqual(result["feedback"][0]["category"], "preference")
        with self.assertRaises(ReelsError):
            record_feedback(self.project, "audience", "guess", "not actual feedback")

    def test_malformed_feedback_does_not_erase_valid_entries(self):
        record_feedback(self.project, "production", "audio", "wind")
        with (self.project.root / "learning/feedback.jsonl").open("a") as f:
            f.write("not json\n")
        self.assertEqual(len(review(self.project)["feedback"]), 1)

    def test_history_and_lessons_preserved_on_rerun(self):
        review(self.project, week="2026-10-28", notes={"lessons": [self.lesson()], "decisions": ["listen first"]})
        review(self.project, week="2026-10-28")
        record = read_json(self.project.root / "learning/reviews/2026-10-26.json")
        self.assertEqual(len(record["checkpoints"]), 2)
        self.assertEqual(record["checkpoints"][0]["notes"]["decisions"], ["listen first"])
        self.assertIn("audio-01", (self.project.root / "learning/LESSONS.md").read_text())
        review(self.project, week="2026-10-28", notes={"lessons": [self.lesson("retired")]})
        self.assertNotIn("## audio-01", (self.project.root / "learning/LESSONS.md").read_text())

    def test_invalid_notes_fail_before_history_write(self):
        for notes in ([], {"unrecognized": []}, {"strengths": "guess"}, {"lessons": [{"id": "missing-evidence"}]}):
            with self.subTest(notes=notes), self.assertRaises(ReelsError):
                review(self.project, notes=notes)
        self.assertFalse((self.project.root / "learning").exists())

    def test_late_amendment_to_old_week_updates_lessons(self):
        review(self.project, week="2026-10-19", notes={"lessons": [self.lesson()]})
        review(self.project, week="2026-10-26", notes={"lessons": [self.lesson("active")]})
        review(self.project, week="2026-10-19", notes={"lessons": [self.lesson("retired")]})
        self.assertNotIn("## audio-01", (self.project.root / "learning/LESSONS.md").read_text())

    def test_experiment_registration_is_immutable_and_idempotent(self):
        design = self.design()
        register_experiment(self.project, design)
        register_experiment(self.project, design)
        registry = read_json(self.project.root / "learning/experiments.json")
        self.assertEqual(len(registry["experiments"]), 1)
        design["hypothesis"] = "different"
        with self.assertRaises(ReelsError):
            register_experiment(self.project, design)

    def test_experiment_needs_preregistration_and_distinct_groups(self):
        self.post("test1")
        with self.assertRaises(ReelsError):
            register_experiment(self.project, self.design())
        design = self.design()
        design["testSlugs"] = ["control1"]
        with self.assertRaises(ReelsError):
            register_experiment(self.project, design)

    def test_experiment_cannot_use_path_slugs_or_unknown_metric(self):
        for key, value in (("testSlugs", ["../ride"]), ("primaryMetric", "views"), ("testSlugs", ["a", "a"])):
            design = self.design()
            design[key] = value
            with self.subTest(key=key), self.assertRaises(ReelsError):
                register_experiment(self.project, design)

    def test_experiments_need_both_groups_and_no_automatic_winner(self):
        register_experiment(self.project, self.design())
        for slug in self.design()["testSlugs"] + self.design()["controlSlugs"]:
            self.post(slug)
            self.snapshot(slug, hold=60 if slug.startswith("test") else 30)
        result = experiment_results(self.project, audience(self.project))[0]
        self.assertEqual(result["status"], "ready-for-review")
        self.assertEqual(result["test"]["median"], 60)
        self.assertEqual(result["control"]["n"], 3)
        evidence = audience(self.project)
        evidence["sevenDayWindow"] = evidence["sevenDayWindow"][:2]
        self.assertEqual(experiment_results(self.project, evidence)[0]["status"], "awaiting-data")

    def test_closing_is_explicit_batch_scoped_and_does_not_change_status(self):
        first = self.post("one", status="scheduled", scheduledFor="2026-11-02T18:30:00", metricoolId="123")
        second = self.post("two", status="queued", scheduledFor="2026-11-03T18:30:00")
        self.post("unrelated", status="scheduled", scheduledFor="2026-11-04T18:30:00")
        with self.assertRaises(ReelsError):
            review(self.project, close=True)
        result = review(self.project, close=True, slugs=["one", "two"])
        self.assertFalse(result["schedulingComplete"])
        self.assertEqual([p["slug"] for p in result["batch"]], ["one", "two"])
        self.assertEqual(result["batch"][0]["metricoolId"], "123")
        self.assertEqual(read_json(self.project.out_dir / "one/post.json"), first)
        self.assertEqual(read_json(self.project.out_dir / "two/post.json"), second)
        self.assertTrue(review(self.project, close=True, slugs=["one"])["schedulingComplete"])

    def test_unknown_batch_fails_without_record(self):
        with self.assertRaises(ReelsError):
            review(self.project, close=True, slugs=["absent"])
        self.assertFalse((self.project.root / "learning").exists())

    def test_close_retains_experiment_assignments(self):
        register_experiment(self.project, self.design())
        self.post("test1", status="queued")
        result = review(self.project, close=True, slugs=["test1"])
        self.assertEqual(result["batch"][0]["experiments"], [{"id": "face-first", "group": "test"}])

    def test_mark_records_fact_not_preference(self):
        self.post()
        mark(self.project, "ride", "approved")
        events = review(self.project)["feedback"]
        self.assertEqual(events[0]["kind"], "status-change")
        self.assertEqual(events[0]["source"], "cli")
        self.assertEqual(events[0]["category"], "production")

    def test_render_attempts_and_failures_logged_without_raw_errors(self):
        args = build_parser().parse_args(["render", "plans/one.json", "plans/two.json"])
        with patch("reels.render.render_plan", side_effect=[{"slug": "one"}, ReelsError("secret-token")]):
            self.assertEqual(cmd_render(self.project, args), 1)
        text = (self.project.root / "learning/feedback.jsonl").read_text()
        self.assertNotIn("secret-token", text)
        kinds = [json.loads(line)["kind"] for line in text.splitlines()]
        self.assertEqual(kinds, ["render-attempt", "render-success", "render-attempt", "render-failed"])

    def test_learning_write_failure_does_not_fail_status_or_render(self):
        with patch("reels.review.record_feedback", side_effect=OSError("read-only")):
            auto_event(self.project, "render-success", "done")

    def test_cli_commands_parse_and_dispatch(self):
        parser = build_parser()
        for words in (["review"], ["review", "--week", "2026-10-28"],
                      ["review", "close", "--slugs", "a", "b"],
                      ["review", "feedback", "--category", "preference", "--kind", "revision", "--note", "shorter"],
                      ["review", "experiment", "--file", "experiment.json"]):
            self.assertEqual(parser.parse_args(words).fn.__name__, "cmd_review")
        self.assertEqual(parser.parse_args(["review", "--week", "2026-10-28", "close", "--slugs", "a"]).week, "2026-10-28")
        with patch("reels.cli.Project", return_value=self.project):
            self.assertEqual(main(["review"]), 0)
            self.assertEqual(main(["review", "--file", "nonexistent.json"]), 1)

    def test_cli_failure_logs_only_command_name_not_sensitive_payload(self):
        with patch("reels.cli.Project", return_value=self.project), patch("reels.cli.cmd_ingest", side_effect=ReelsError("secret response")):
            self.assertEqual(main(["ingest"]), 1)
        text = (self.project.root / "learning/feedback.jsonl").read_text()
        self.assertIn("command-failed", text)
        self.assertNotIn("secret response", text)

    def test_week_normalizes_and_rejects_bad_dates(self):
        self.assertEqual(week_start(self.project, "2026-10-28"), "2026-10-26")
        with self.assertRaises(ReelsError):
            week_start(self.project, "2026-13-40")


if __name__ == "__main__":
    unittest.main()
