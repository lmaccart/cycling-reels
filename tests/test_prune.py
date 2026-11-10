from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import TempProject
from reels import cli, prune, telemetry, transcribe
from reels.library import load_asset, save_asset
from reels.prune import apply_round, dry_run, mark, protections, unmark
from reels.render import build, load_plan
from reels.util import ReelsError, read_json, write_json

NOW = datetime(2026, 11, 9, 12, 0, 0)
OLD = "2026-10-01T08:00:00"
FRESH = "2026-11-07T08:00:00"
TAGS = {"type": "solo-ride", "shot": "pov", "quality": 3, "summary": "a ride", "moments": []}


class PruneTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = TempProject(self)
        self.project = self.temp.project

    def make(self, aid: str = "clip", *, created: str | None = OLD, moments: list | None = None, sounds: list | None = None,
             kind: str = "video", used: list | None = None, **extra) -> dict:  # fmt: skip
        asset = self.temp.asset(aid, kind=kind, used=used, tags={**TAGS, "moments": moments or [], "sounds": sounds or []})
        asset["created"] = created
        asset.update(extra)
        if created is None:
            del asset["created"]
        save_asset(self.project, asset)
        return asset

    def reasons(self, **kw) -> list[str]:
        return protections(self.project, self.make(**kw), NOW)


class ProtectionTests(PruneTestCase):
    def test_nothing_protects_an_old_plain_chapter(self):
        self.assertEqual(self.reasons(moments=[{"in": 1, "out": 3, "score": 2, "role": "texture", "speaker": "none"}]), [])

    def test_used(self):
        self.assertEqual(self.reasons(used=[{"slug": "a-reel", "in": 1, "out": 2}]), ["used"])

    def write_plan(self, name: str, text: str) -> None:
        self.project.plans_dir.mkdir(parents=True, exist_ok=True)
        (self.project.plans_dir / f"{name}.json").write_text(text)

    def test_named_in_an_unrendered_plan(self):
        self.write_plan("p", json.dumps({"segments": [{"asset": "clip", "in": 1, "out": 2}]}))
        self.assertEqual(self.reasons(), ["planned"])

    def test_named_in_a_plan_by_its_short_id(self):
        self.write_plan("p", json.dumps({"segments": [{"asset": "261107-gs080116", "in": 1, "out": 2}]}))
        self.assertEqual(self.reasons(aid="261107-gs080116-7dc8"), ["planned"])

    def test_named_only_as_a_cutaway(self):
        self.write_plan("p", json.dumps({"segments": [{"asset": "other", "cutaways": [{"asset": "clip", "in": 1, "out": 2}]}]}))
        self.assertEqual(self.reasons(), ["planned"])

    def test_a_longer_id_sharing_a_prefix_is_not_a_match(self):
        self.write_plan("p", json.dumps({"segments": [{"asset": "clip-two", "in": 1, "out": 2}]}))
        self.assertEqual(self.reasons(), [])

    def test_an_unreadable_plan_stops_the_check(self):
        self.write_plan("broken", "{not json")
        with self.assertRaises(ReelsError):
            self.reasons()

    def test_thread(self):
        self.assertEqual(self.reasons(moments=[{"in": 1, "out": 3, "thread": "road-to-5-ep1"}]), ["thread"])

    def test_sound(self):
        self.assertEqual(self.reasons(sounds=[{"at": 1, "for": 1, "kind": "freehub", "score": 3, "note": "x"}]), ["sound"])

    def test_voice_by_speaker_or_role(self):
        self.assertEqual(self.reasons(moments=[{"in": 1, "out": 3, "speaker": "me"}]), ["voice"])
        self.assertEqual(self.reasons(moments=[{"in": 1, "out": 3, "role": "voice", "speaker": "none"}]), ["voice"])

    def test_teammate_voice_does_not_protect(self):
        self.assertEqual(self.reasons(moments=[{"in": 1, "out": 3, "role": "crew", "speaker": "teammate"}]), [])

    def test_recent_window_edges(self):
        self.assertEqual(self.reasons(created=FRESH), ["recent"])
        self.assertEqual(self.reasons(created="2026-10-26T12:00:00"), ["recent"])  # exactly 14 days
        self.assertEqual(self.reasons(created="2026-10-26T11:59:59"), [])

    def test_missing_or_unreadable_created_counts_as_recent(self):
        self.assertEqual(self.reasons(created=None), ["recent"])
        self.assertEqual(self.reasons(created="around october"), ["recent"])

    def test_photo_is_not_a_video_chapter(self):
        self.assertIn("not-video-chapter", self.reasons(kind="photo"))

    def test_already_deleted(self):
        self.assertIn("deleted", self.reasons(deleted={"at": "2026-11-01T00:00:00+00:00", "reason": "space"}))

    def test_every_reason_is_reported(self):
        got = self.reasons(created=FRESH, used=[{"slug": "x"}], sounds=[{"at": 1}], moments=[{"in": 1, "out": 2, "thread": "t", "speaker": "me"}])
        self.assertEqual(got, ["used", "thread", "sound", "voice", "recent"])


class MarkTests(PruneTestCase):
    def test_mark_records_the_review(self):
        self.make()
        mark(self.project, "clip", "  lens is blocked throughout ", NOW)
        review = load_asset(self.project, "clip")["pruneReview"]
        self.assertEqual((review["verdict"], review["reason"]), ("candidate", "lens is blocked throughout"))
        self.assertTrue(review["at"])
        self.assertNotIn("waiting", review)

    def test_recent_only_waits(self):
        self.make(created=FRESH)
        mark(self.project, "clip", "empty road, no change", NOW)
        self.assertEqual(load_asset(self.project, "clip")["pruneReview"]["waiting"], "recent")

    def test_missing_created_waits_too(self):
        self.make(created=None)
        mark(self.project, "clip", "empty road, no change", NOW)
        self.assertEqual(load_asset(self.project, "clip")["pruneReview"]["waiting"], "recent")

    def test_refuses_any_other_protection_and_saves_nothing(self):
        cases = [{"used": [{"slug": "x"}]}, {"moments": [{"in": 1, "out": 2, "thread": "t"}]}, {"sounds": [{"at": 1}]},
                 {"moments": [{"in": 1, "out": 2, "speaker": "me"}]}, {"kind": "photo"}, {"deleted": {"at": "x"}}]  # fmt: skip
        for n, kw in enumerate(cases):
            aid = f"clip{n}"
            self.make(aid, **kw)
            with self.assertRaises(ReelsError) as caught:
                mark(self.project, aid, "empty", NOW)
            self.assertIn("protected by", str(caught.exception))
            self.assertNotIn("pruneReview", load_asset(self.project, aid))

    def test_refusal_prints_recent_alongside_the_blocker(self):
        self.make(created=FRESH, used=[{"slug": "x"}])
        with self.assertRaises(ReelsError) as caught:
            mark(self.project, "clip", "empty", NOW)
        self.assertIn("used, recent", str(caught.exception))

    def test_needs_a_reason(self):
        self.make()
        for reason in ("", "   ", None):
            with self.assertRaises(ReelsError):
                mark(self.project, "clip", reason, NOW)

    def test_unmark_removes_the_review(self):
        self.make()
        mark(self.project, "clip", "empty", NOW)
        self.assertTrue(unmark(self.project, "clip"))
        self.assertNotIn("pruneReview", load_asset(self.project, "clip"))
        self.assertFalse(unmark(self.project, "clip"))

    def test_mark_leaves_tags_and_status_alone(self):
        before = self.make(moments=[{"in": 1, "out": 2, "score": 2}])
        mark(self.project, "clip", "empty", NOW)
        after = load_asset(self.project, "clip")
        after.pop("pruneReview")
        self.assertEqual(after, before)

    def test_command_marks_and_unmarks(self):
        self.make()
        prune.cmd_prune(self.project, SimpleNamespace(mark="clip", unmark=None, reason="empty"))
        self.assertIn("pruneReview", load_asset(self.project, "clip"))
        prune.cmd_prune(self.project, SimpleNamespace(mark=None, unmark="clip", reason=None))
        self.assertNotIn("pruneReview", load_asset(self.project, "clip"))


class NoDeletionTests(PruneTestCase):
    def test_media_files_are_untouched(self):
        asset = self.make()
        media = self.temp.root / asset["media"]
        before = media.read_bytes()
        listing = sorted(p.name for p in self.project.media_dir.iterdir())
        mark(self.project, "clip", "empty", NOW)
        unmark(self.project, "clip")
        self.assertEqual(media.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.project.media_dir.iterdir()), listing)

    def test_the_only_removal_is_one_unlink_in_delete_one(self):
        tree = ast.parse(Path(prune.__file__).read_text(encoding="utf-8"))
        forbidden = {"remove", "rmtree", "rmdir", "rename", "replace", "move", "truncate", "system", "run", "Popen", "open", "write_bytes", "write_text"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, forbidden)
                if isinstance(node.value, ast.Name) and node.value.id == "shutil":
                    self.assertEqual(node.attr, "disk_usage")
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, forbidden)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
                for name in names:
                    self.assertNotIn(name.split(".")[0], {"os", "subprocess", "pathlib"})
        unlinks = [f.name for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) for n in ast.walk(f) if isinstance(n, ast.Attribute) and n.attr == "unlink"]
        self.assertEqual(unlinks, ["_delete_one"])


class RoundTestCase(PruneTestCase):
    def setUp(self):
        super().setUp()
        self.set_free(50.0)
        self.printed: list[str] = []
        log = mock.patch.object(prune, "log", side_effect=self.printed.append)
        log.start()
        self.addCleanup(log.stop)

    def set_free(self, gb: float) -> None:
        patcher = mock.patch.object(prune, "free_gb", return_value=gb)
        patcher.start()
        self.addCleanup(patcher.stop)

    def candidate(self, aid: str = "clip", created: str = OLD, **kw) -> dict:
        asset = self.make(aid, created=created, **kw)
        mark(self.project, aid, f"{aid} is empty", NOW)
        return asset

    def media(self, aid: str = "clip") -> Path:
        return self.project.media_dir / f"{aid}.mp4"

    def listed(self, *args, **kw) -> dict:
        return dry_run(self.project, *args, now=NOW, **kw)

    def apply(self, result: dict) -> list[dict]:
        return apply_round(self.project, result["round"], yes=True, now=NOW)

    def output(self) -> str:
        return "\n".join(self.printed)


class DryRunTests(RoundTestCase):
    def test_oldest_first_with_running_total(self):
        self.candidate("newer", created="2026-10-10T08:00:00")
        self.candidate("oldest", created="2026-09-01T08:00:00")
        self.candidate("middle", created="2026-10-01T08:00:00")
        result = self.listed()
        self.assertEqual([r["id"] for r in result["eligible"]], ["oldest", "middle", "newer"])
        self.assertLess(self.output().index("oldest"), self.output().index("newer"))
        self.assertIn("running total", self.output())
        self.assertIn("oldest is empty", self.output())

    def test_limit_keeps_the_oldest(self):
        self.candidate("b", created="2026-09-02T08:00:00")
        self.candidate("a", created="2026-09-01T08:00:00")
        result = self.listed(limit=1)
        self.assertEqual([r["id"] for r in result["eligible"]], ["a"])
        self.assertEqual([e["id"] for e in read_json(self.temp.root / result["round"])["assets"]], ["a"])
        with self.assertRaises(ReelsError):
            self.listed(limit=0)

    def test_protections_are_recomputed_and_shown(self):
        self.candidate("clip")
        asset = load_asset(self.project, "clip")
        asset["used"] = [{"plan": "a-reel", "in": 1, "out": 2}]
        save_asset(self.project, asset)
        self.candidate("waiting", created=FRESH)
        result = self.listed()
        self.assertEqual(result["eligible"], [])
        self.assertEqual({r["id"]: r["protections"] for r in result["protected"]}, {"clip": ["used"], "waiting": ["recent"]})
        self.assertIn("protected by: used", self.output())

    def test_an_aged_out_wait_is_eligible(self):
        self.candidate("clip", created="2026-10-26T11:59:59")
        asset = load_asset(self.project, "clip")
        asset["pruneReview"]["waiting"] = "recent"
        save_asset(self.project, asset)
        self.assertEqual([r["id"] for r in self.listed()["eligible"]], ["clip"])

    def test_unmarked_chapters_are_not_listed(self):
        self.make("plain")
        result = self.listed()
        self.assertEqual((result["eligible"], result["protected"]), ([], []))

    def test_says_when_triggered(self):
        self.candidate()
        self.assertTrue(self.listed()["triggered"])
        self.assertIn("50.0 GB", self.output())
        self.assertIn("100 GB", self.output())
        self.assertIn("IS triggered", self.output())

    def test_says_when_not_triggered_and_honours_the_config(self):
        self.set_free(120.0)
        self.assertFalse(self.listed()["triggered"])
        self.assertIn("NOT triggered", self.output())
        self.project.config["prune"]["minFreeGB"] = 200
        self.assertTrue(self.listed()["triggered"])

    def test_bad_threshold_is_refused(self):
        self.project.config["prune"]["minFreeGB"] = "lots"
        with self.assertRaises(ReelsError):
            self.listed()

    def snapshot(self) -> dict:
        return {str(p): p.read_bytes() for p in self.temp.root.rglob("*") if p.is_file() and "prune-rounds" not in p.parts}

    def test_removes_and_changes_nothing_but_the_round_file(self):
        self.candidate("a")
        self.candidate("b")
        before = self.snapshot()
        result = self.listed()
        self.assertEqual(self.snapshot(), before)
        doc = read_json(self.temp.root / result["round"])
        self.assertEqual([e["id"] for e in doc["assets"]], ["a", "b"])
        self.assertEqual([e["bytes"] for e in doc["assets"]], [self.media("a").stat().st_size, self.media("b").stat().st_size])
        self.assertTrue(result["round"].startswith("out/prune-rounds/"))

    def test_nothing_eligible_writes_no_round(self):
        self.assertIsNone(self.listed()["round"])
        self.assertFalse((self.project.out_dir / "prune-rounds").exists())

    def test_rounds_in_the_same_second_do_not_overwrite(self):
        self.candidate()
        self.assertNotEqual(self.listed()["round"], self.listed()["round"])


class ApplyTests(RoundTestCase):
    def test_only_round_listed_assets_are_deleted(self):
        self.candidate("a", created="2026-09-01T08:00:00")
        self.candidate("b", created="2026-09-02T08:00:00")
        self.candidate("c", created="2026-09-03T08:00:00")
        self.make("untouched")
        results = self.apply(self.listed(limit=2))
        self.assertEqual([(r["id"], r["status"]) for r in results], [("a", "deleted"), ("b", "deleted")])
        self.assertFalse(self.media("a").exists())
        self.assertFalse(self.media("b").exists())
        self.assertTrue(self.media("c").exists())
        self.assertTrue(self.media("untouched").exists())
        self.assertNotIn("deleted", load_asset(self.project, "c"))

    def test_an_edited_in_id_is_only_deleted_if_it_is_a_candidate(self):
        self.candidate("a")
        self.make("plain")
        result = self.listed()
        path = self.temp.root / result["round"]
        doc = read_json(path)
        doc["assets"] += [{"id": "plain"}, {"id": "../../nope"}, {"id": ["x"]}, "junk"]
        write_json(path, doc)
        statuses = [r["status"] for r in self.apply(result)]
        self.assertEqual(statuses, ["deleted", "skipped", "skipped", "skipped", "skipped"])
        self.assertTrue(self.media("plain").exists())

    def test_threshold_gates_the_whole_round(self):
        self.candidate()
        result = self.listed()
        for free in (100.0, 180.0):  # at the threshold counts as fine
            self.set_free(free)
            with self.assertRaises(ReelsError) as caught:
                self.apply(result)
            self.assertIn("refusing", str(caught.exception))
            self.assertTrue(self.media().exists())
            self.assertNotIn("deleted", load_asset(self.project, "clip"))
        self.assertNotIn("appliedAt", read_json(self.temp.root / result["round"]))

    def test_needs_yes(self):
        self.candidate()
        result = self.listed()
        with self.assertRaises(ReelsError):
            apply_round(self.project, result["round"], yes=False, now=NOW)
        self.assertTrue(self.media().exists())

    def test_needs_a_round_from_the_rounds_folder(self):
        self.candidate()
        self.listed()
        elsewhere = self.temp.root / "my-round.json"
        write_json(elsewhere, {"assets": [{"id": "clip"}]})
        for bad in (None, "my-round.json", "out/prune-rounds/missing.json", "out/prune-rounds/../../my-round.json"):
            with self.assertRaises(ReelsError):
                apply_round(self.project, bad, yes=True, now=NOW)
        self.assertTrue(self.media().exists())

    def test_live_recheck_skips_a_newly_protected_asset(self):
        self.candidate("a")
        self.candidate("b")
        result = self.listed()
        asset = load_asset(self.project, "a")
        asset["used"] = [{"plan": "new-reel", "in": 1, "out": 2}]
        save_asset(self.project, asset)
        results = self.apply(result)
        self.assertEqual([(r["id"], r["status"], r.get("why")) for r in results], [("a", "skipped", "used"), ("b", "deleted", None)])
        self.assertTrue(self.media("a").exists())
        self.assertNotIn("deleted", load_asset(self.project, "a"))
        self.assertIn("a: skipped (used)", self.output())

    def test_an_unmarked_asset_is_skipped(self):
        self.candidate()
        result = self.listed()
        unmark(self.project, "clip")
        self.assertEqual(self.apply(result)[0]["why"], "not-marked")
        self.assertTrue(self.media().exists())

    def retarget(self, media: str) -> None:
        asset = load_asset(self.project, "clip")
        asset["media"] = media
        save_asset(self.project, asset)

    def test_path_traversal_is_refused(self):
        self.candidate()
        result = self.listed()
        outside = self.temp.root / "library" / "precious.mov"
        outside.write_bytes(b"keep me")
        for media in ("library/media/../precious.mov", str(outside)):
            self.retarget(media)
            doc = read_json(self.temp.root / result["round"])
            doc.pop("appliedAt", None)
            write_json(self.temp.root / result["round"], doc)
            self.assertEqual(self.apply(result)[0]["why"], "media-outside-library")
            self.assertEqual(outside.read_bytes(), b"keep me")

    def test_the_media_folder_itself_is_refused(self):
        self.candidate()
        result = self.listed()
        self.retarget("library/media")
        self.assertEqual(self.apply(result)[0]["why"], "media-outside-library")
        self.assertTrue(self.project.media_dir.is_dir())

    def test_a_symlink_out_of_the_library_is_refused(self):
        self.candidate()
        result = self.listed()
        outside = self.temp.root / "precious.mov"
        outside.write_bytes(b"keep me")
        link = self.project.media_dir / "link.mov"
        os.symlink(outside, link)
        self.retarget("library/media/link.mov")
        self.assertEqual(self.apply(result)[0]["why"], "media-is-a-symlink")
        self.assertEqual(outside.read_bytes(), b"keep me")
        self.assertTrue(link.is_symlink())

    def test_tags_sheets_transcripts_and_sounds_are_kept(self):
        self.candidate()
        asset = load_asset(self.project, "clip")
        kept = [self.project.sheets_dir / "clip.jpg", self.project.transcripts_dir / "clip.json", self.project.library / "sounds" / "clip.jpg", self.project.library / "sounds" / "clip.json"]
        for path in kept:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"sidecar")
        self.apply(self.listed())
        for path in kept:
            self.assertEqual(path.read_bytes(), b"sidecar")
        after = load_asset(self.project, "clip")
        self.assertEqual(after.pop("deleted")["reason"], "clip is empty")
        self.assertEqual(after, asset)

    def test_deleted_marker_has_exactly_the_agreed_shape(self):
        self.candidate()
        self.apply(self.listed())
        deleted = load_asset(self.project, "clip")["deleted"]
        self.assertEqual(set(deleted), {"at", "reason"})
        self.assertEqual(deleted["reason"], "clip is empty")
        self.assertIsNotNone(datetime.fromisoformat(deleted["at"]).tzinfo)

    def test_a_failed_removal_leaves_no_marker(self):
        self.candidate()
        result = self.listed()
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("nope")):
            self.assertEqual(self.apply(result)[0]["status"], "failed")
        self.assertNotIn("deleted", load_asset(self.project, "clip"))

    def test_results_go_in_the_round_and_it_cannot_be_applied_twice(self):
        self.candidate()
        result = self.listed()
        self.apply(result)
        doc = read_json(self.temp.root / result["round"])
        self.assertEqual(doc["assets"][0]["result"]["status"], "deleted")
        self.assertTrue(doc["appliedAt"])
        with self.assertRaises(ReelsError) as caught:
            self.apply(result)
        self.assertIn("already applied", str(caught.exception))

    def test_deleted_assets_leave_the_next_dry_run(self):
        self.candidate()
        self.apply(self.listed())
        again = self.listed()
        self.assertEqual((again["eligible"], again["protected"]), ([], []))

    def test_command_runs_dry_run_and_apply(self):
        self.candidate()
        args = {"mark": None, "unmark": None, "reason": None}
        with mock.patch.object(prune, "protections", return_value=[]):  # the command uses today's date
            prune.cmd_prune(self.project, SimpleNamespace(**args, dry_run=True, limit=None, round=None, yes=False))
            self.assertTrue(self.media().exists())
            path = next((self.project.out_dir / "prune-rounds").glob("*.json"))
            with self.assertRaises(ReelsError):
                prune.cmd_prune(self.project, SimpleNamespace(**args, dry_run=False, limit=1, round=str(path), yes=True))
            self.assertTrue(self.media().exists())
            prune.cmd_prune(self.project, SimpleNamespace(**args, dry_run=False, limit=None, round=str(path), yes=True))
        self.assertFalse(self.media().exists())

    def test_parser_has_no_way_past_the_threshold_or_the_round(self):
        parsed = cli.build_parser().parse_args(["prune", "--apply", "--round", "x.json", "--yes"])
        self.assertEqual((parsed.apply, parsed.round, parsed.yes), (True, "x.json", True))
        self.assertLessEqual(set(vars(parsed)) - {"cmd", "fn"}, {"mark", "unmark", "dry_run", "apply", "reason", "limit", "round", "yes"})


class DeletedAssetTests(RoundTestCase):
    def setUp(self):
        super().setUp()
        self.gone = self.temp.asset("gone", tags=None)
        self.gone.update(status="needs_tags", audio={"speechSeconds": 9.0}, deleted={"at": "2026-11-09T00:00:00+00:00", "reason": "empty"})
        save_asset(self.project, self.gone)
        self.media("gone").unlink()
        self.kept = self.temp.asset("kept")
        self.kept.update(status="needs_tags", audio={"speechSeconds": 9.0})
        save_asset(self.project, self.kept)

    def test_pending_does_not_offer_it(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.cmd_pending(self.project, SimpleNamespace(json=True))
        self.assertEqual([a["id"] for a in json.loads(out.getvalue())], ["kept"])

    def test_sounds_pending_does_not_offer_it(self):
        with mock.patch("reels.sounds.analyse", return_value={}) as analyse, mock.patch("reels.sounds.summary", return_value=""), contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_sounds(self.project, SimpleNamespace(pending=True, force=True))
        self.assertEqual([c.args[1]["id"] for c in analyse.call_args_list], ["kept"])

    def test_transcribe_does_not_offer_it(self):
        with mock.patch.object(transcribe, "load_model", side_effect=AssertionError("nothing to do")), contextlib.redirect_stdout(io.StringIO()) as out:
            asset = load_asset(self.project, "kept")
            asset["transcript"] = "library/transcripts/kept.json"
            save_asset(self.project, asset)
            self.assertEqual(transcribe.transcribe(self.project), 0)
            self.assertEqual(transcribe.transcribe(self.project, ["gone"], force=True), 0)
        self.assertIn("nothing to transcribe", out.getvalue())

    def test_telemetry_does_not_read_it(self):
        with mock.patch.object(telemetry, "has_gpmd", return_value=False) as has_gpmd, contextlib.redirect_stderr(io.StringIO()):
            telemetry.run_telemetry(self.project)
            telemetry.run_telemetry(self.project, ["gone"])
        self.assertEqual([c.args[1]["id"] for c in has_gpmd.call_args_list], ["kept"])

    def assert_refused(self, plan: Path) -> None:
        with self.assertRaises(ReelsError) as caught:
            build(self.project, load_plan(self.project, plan))
        self.assertIn("deleted by ./reels prune", str(caught.exception))

    def test_check_refuses_a_plan_using_it_as_a_segment(self):
        self.assert_refused(self.temp.plan("uses-gone", [{"asset": "gone", "in": 1, "out": 3}]))

    def test_check_refuses_a_plan_using_it_as_a_cutaway(self):
        self.assert_refused(self.temp.plan("cuts-to-gone", [{"asset": "kept", "in": 0, "out": 8, "cutaways": [{"at": 1, "asset": "gone", "in": 1, "out": 3}]}]))


if __name__ == "__main__":
    unittest.main()
