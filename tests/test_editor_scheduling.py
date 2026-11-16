from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from helpers import TempProject
from reels.editor import scheduling as sch
from reels.editor.jobs import JobContext
from reels.editor.protocol import ProtocolError
from reels.util import read_json, write_json

TZ = ZoneInfo("America/Chicago")
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=TZ)


def post(**kw):
    base = {"slug": "r1", "status": "rendered", "caption": "dry caption. second line #x", "hashtags": ["#cycling", "#roadcycling"],
            "mediaUrl": "https://pub.r2.dev/reels/r1.mp4", "coverUrl": "https://pub.r2.dev/reels/r1.jpg", "coverMs": 1200,
            "altText": "alt", "file": "out/r1/reel.mp4", "cover": "out/r1/cover.jpg"}
    base.update(kw)
    return base


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.project = TempProject(self).project
        self.when = datetime(2026, 11, 18, 18, 30, tzinfo=TZ)

    def test_networks_and_fields(self):
        payload = sch.build_payload(self.project, post(), self.when, "dry caption. second line #x")
        info = payload["info"]
        self.assertEqual(payload["blogId"], "7655493")
        self.assertEqual(payload["date"], "2026-11-18T18:30:00-06:00")
        self.assertNotIn("id", payload)
        self.assertEqual([p["network"] for p in info["providers"]], ["instagram", "tiktok", "youtube"])
        self.assertEqual(info["instagramData"]["type"], "REEL")
        self.assertIs(info["autoPublish"], True)
        self.assertEqual(info["firstCommentText"], "")
        self.assertEqual(info["tiktokData"]["title"], "dry caption. second line")
        self.assertEqual(info["youtubeData"]["title"], "dry caption. second line")
        self.assertEqual(info["youtubeData"]["tags"], ["cycling", "roadcycling"])
        self.assertEqual(info["youtubeData"]["type"], "short")
        self.assertEqual(info["videoThumbnailUrl"], "https://pub.r2.dev/reels/r1.jpg")
        self.assertEqual(info["publicationDate"], {"dateTime": "2026-11-18T18:30:00", "timezone": "America/Chicago"})
        self.assertIn("#cycling #roadcycling", info["text"])

    def test_title_is_first_line_without_hashtags(self):
        self.assertEqual(sch.title_for("#cycling\nhello there #x\nmore"), "hello there")
        self.assertEqual(len(sch.title_for("a" * 300)), 100)

    def test_collabs_only_from_post(self):
        none = sch.build_payload(self.project, post(), self.when, "c")["info"]["instagramData"]
        self.assertNotIn("collaborators", none)
        self.assertNotIn("carouselTags", none)
        ig = sch.build_payload(self.project, post(collaborators=["pal"], peopleTags=["bud"]), self.when, "c")["info"]["instagramData"]
        self.assertEqual(ig["collaborators"], [{"username": "pal", "deleted": False}])
        self.assertEqual(ig["carouselTags"], {"0": [{"username": "bud"}]})

    def test_cover_ms_fallback_and_update_id(self):
        payload = sch.build_payload(self.project, post(coverUrl=None), self.when, "c", existing_id="42")
        self.assertEqual(payload["id"], "42")
        self.assertEqual(payload["info"]["videoCoverMilliseconds"], 1200)
        self.assertNotIn("videoThumbnailUrl", payload["info"])

    def test_requires_caption_and_media(self):
        with self.assertRaises(ProtocolError):
            sch.build_payload(self.project, post(), self.when, "#only #tags")
        with self.assertRaises(ProtocolError):
            sch.build_payload(self.project, post(mediaUrl=None), self.when, "x")

    def test_claude_args(self):
        payload = sch.build_payload(self.project, post(), self.when, "c")
        create = sch.claude_args(payload, update=False, claude_bin="/bin/claude")
        self.assertEqual(create[:2], ["/bin/claude", "-p"])
        self.assertEqual(create[create.index("--allowedTools") + 1], sch.CREATE_TOOL)
        self.assertIn("--output-format", create)
        update = sch.claude_args({**payload, "id": "9"}, update=True, claude_bin="/bin/claude")
        self.assertEqual(update[update.index("--allowedTools") + 1], f"{sch.GET_TOOL},{sch.UPDATE_TOOL}")
        self.assertIn("updateScheduledPost", update[2])

    def test_parse_output(self):
        self.assertEqual(sch.parse_claude_output(json.dumps({"result": '{"id": 123}'})), "123")
        self.assertEqual(sch.parse_claude_output(json.dumps({"result": 'ok {"id": "abc-1"}'})), "abc-1")
        with self.assertRaises(ProtocolError) as e:
            sch.parse_claude_output(json.dumps({"result": "TikTok title is required"}))
        self.assertIn("TikTok title is required", str(e.exception))
        with self.assertRaises(ProtocolError):
            sch.parse_claude_output(json.dumps({"is_error": True, "result": "not logged in"}))
        with self.assertRaises(ProtocolError):
            sch.parse_claude_output("garbage")


class SlotTests(unittest.TestCase):
    def setUp(self):
        self.project = TempProject(self).project
        self.project.config["schedule"] = {"timezone": "America/Chicago", "times": ["10:00", "18:30"]}

    def test_default_is_next_open_slot(self):
        self.assertEqual(sch.default_slot(self.project, now=NOW), datetime(2026, 11, 16, 18, 30, tzinfo=TZ))
        d = self.project.out_dir / "other"
        d.mkdir(parents=True)
        write_json(d / "post.json", {"slug": "other", "status": "scheduled", "scheduledFor": "2026-11-16T18:30:00"})
        self.assertEqual(sch.default_slot(self.project, now=NOW), datetime(2026, 11, 17, 10, 0, tzinfo=TZ))
        self.assertEqual(sch.default_slot(self.project, now=NOW, exclude_slug="other"), datetime(2026, 11, 16, 18, 30, tzinfo=TZ))

    def test_lead_time(self):
        late = datetime(2026, 11, 16, 18, 20, tzinfo=TZ)
        self.assertEqual(sch.default_slot(self.project, now=late).day, 17)
        with self.assertRaises(ProtocolError):
            sch.parse_when(self.project, "2026-11-16T18:30:00", now=late)
        self.assertEqual(sch.parse_when(self.project, "2026-11-16T18:30:00", now=NOW).hour, 18)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.fx = TempProject(self)
        self.project = self.fx.project
        self.project.config["schedule"] = {"timezone": "America/Chicago", "times": ["18:30"]}
        out = self.project.out_dir / "r1"
        out.mkdir(parents=True)
        (out / "reel.mp4").write_bytes(b"x")
        self.plan = {"slug": "r1", "segments": [{"asset": "a"}], "post": {"caption": "dry caption", "hashtags": ["#cycling"]}}
        write_json(out / "post.json", post(**{sch.HASH_KEY: sch.plan_hash(self.plan)}))
        self.jobs = []
        service = SimpleNamespace(project=self.project, document_path="plans/r1.json",
                                  snapshot=lambda: {"plan": self.plan, "revisions": {"document": "d"}},
                                  jobs=SimpleNamespace(submit=lambda kind, rev, work, publish=None, independent=False: work(JobContext())))
        self.sched = sch.Scheduler(service)
        self.logs = []
        self.ctx = JobContext()
        self.ctx.on_log = self.logs.append

    def run_job(self, when="2026-11-18T18:30:00"):
        snapshot = {"plan": self.plan, "revisions": {}}
        return self.sched._run(self.ctx, snapshot, sch.parse_when(self.project, when), "dry caption")

    def test_no_render_needed_then_create(self):
        sent = {}
        def fake_send(ctx, payload, update):
            sent.update(payload=payload, update=update)
            return "777"
        with patch.object(self.sched, "_render") as render, patch.object(self.sched, "_upload") as upload, \
                patch.object(self.sched, "_send", side_effect=fake_send):
            result = self.run_job()
        render.assert_not_called()
        upload.assert_not_called()  # urls already present and newer than the reel
        self.assertFalse(sent["update"])
        self.assertEqual(result["metricoolId"], "777")
        saved = read_json(self.project.out_dir / "r1" / "post.json")
        self.assertEqual(saved["status"], "scheduled")
        self.assertEqual(saved["metricoolId"], "777")
        self.assertEqual(saved["scheduledFor"], "2026-11-18T18:30:00")
        self.assertEqual([x for x in self.logs if x.startswith("STEP send")], ["STEP send running", "STEP send done"])

    def test_changed_plan_renders_and_uploads_then_updates(self):
        self.plan["segments"].append({"asset": "b"})
        self.assertTrue(self.sched.needs_render(self.plan))
        path = self.project.out_dir / "r1" / "post.json"

        def fake_render(ctx, snapshot):  # a real render rewrites post.json: status rendered, no urls, id carried over
            write_json(path, post(mediaUrl=None, coverUrl=None, metricoolId="55", **{sch.HASH_KEY: sch.plan_hash(self.plan)}))

        def fake_upload(ctx, slug):
            write_json(path, {**read_json(path), "mediaUrl": "https://u/r1.mp4", "coverUrl": "https://u/r1.jpg"})
        with patch.object(self.sched, "_render", side_effect=fake_render) as render, \
                patch.object(self.sched, "_upload", side_effect=fake_upload) as upload, \
                patch.object(self.sched, "_send", return_value="56") as send:
            result = self.run_job()
        render.assert_called_once()
        upload.assert_called_once()
        self.assertTrue(send.call_args.kwargs["update"])
        self.assertEqual(send.call_args.args[1]["id"], "55")
        self.assertEqual(result["metricoolId"], "56")
        self.assertEqual(read_json(path)["status"], "scheduled")

    def test_send_failure_keeps_error_and_does_not_mark_scheduled(self):
        with patch.object(self.sched, "_send", side_effect=ProtocolError("claude_error", "TikTok title is required")):
            with self.assertRaises(ProtocolError):
                self.run_job()
        self.assertEqual(read_json(self.project.out_dir / "r1" / "post.json")["status"], "approved")
        self.assertTrue(any("TikTok title is required" in x for x in self.logs))

    def test_caption_only_change_does_not_rerender(self):
        self.plan["post"]["caption"] = "new caption"
        self.assertFalse(self.sched.needs_render(self.plan))
        self.sched._refresh_metadata(self.plan)
        self.assertEqual(read_json(self.project.out_dir / "r1" / "post.json")["caption"], "new caption")
        self.assertIn("new caption", (self.project.out_dir / "r1" / "caption.txt").read_text())

    def test_fake_claude_binary_end_to_end(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        script = Path(tmp) / "claude"
        script.write_text('#!/bin/sh\necho "$@" > "$0.args"\necho \'{"is_error": false, "result": "{\\"id\\": \\"9001\\"}"}\'\n')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        with patch.dict(os.environ, {"REELS_CLAUDE_BIN": str(script)}):
            result = self.run_job()
        self.assertEqual(result["metricoolId"], "9001")
        args = (Path(tmp) / "claude.args").read_text()
        self.assertIn("createScheduledPost", args)
        self.assertNotIn("updateScheduledPost", args)

    def test_fake_claude_failure_is_verbatim(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        script = Path(tmp) / "claude"
        script.write_text('#!/bin/sh\necho "Error: Metricool says no" >&2\nexit 1\n')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        with patch.dict(os.environ, {"REELS_CLAUDE_BIN": str(script)}):
            with self.assertRaises(ProtocolError) as e:
                self.run_job()
        self.assertIn("Metricool says no", e.exception.message)


if __name__ == "__main__":
    unittest.main()
