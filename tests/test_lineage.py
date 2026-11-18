from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from helpers import TempProject, words
from reels.analytics import DEFAULT_FIELDS, build_report, stats
from reels.footage_report import length_band, rows, section
from reels.lineage import backfill, best_moment, from_built, from_plan, record
from reels.library import load_asset, save_asset
from reels.render import build
from reels.review import review
from reels.util import ReelsError, read_json, write_json


def moment(mid: str, start: float, end: float, **tags) -> dict:
    return {"id": mid, "in": start, "out": end, "label": mid, "score": 4, "focusX": 0.5, "focusY": 0.5, "role": "effort", "speaker": "none",
            "face": False, "mood": "calm", "opener": False, **tags}  # fmt: skip


def tags(*moments: dict) -> dict:
    return {"type": "solo-ride", "shot": "pov", "quality": 4, "summary": "x", "moments": list(moments)}


class LineageTests(unittest.TestCase):
    def setUp(self):
        self.t = TempProject(self)
        self.project = self.t.project

    def test_best_moment_prefers_most_overlap_then_closest_fit(self):
        asset = {"tags": tags(moment("wide", 0, 10), moment("tight", 2, 4), moment("off", 20, 22))}
        self.assertEqual(best_moment(asset, 2, 4)[0]["id"], "tight")
        self.assertEqual(best_moment(asset, 3, 8)[0]["id"], "wide")
        self.assertEqual(best_moment(asset, 12, 14), (None, 0.0))

    def test_rendered_lineage_covers_segments_cutaways_and_shares(self):
        self.t.asset("voice-aaaa", tags=tags(moment("m-v", 0, 8, role="voice", speaker="me", face=True, thread="road-to-5", beat="setup", line="plan")))
        self.t.asset("broll-bbbb", tags=tags(moment("m-b", 0, 8, score=3, role="texture")))
        path = self.t.plan("r", [{"asset": "voice-aaaa", "in": 0, "out": 6, "cutaways": [{"at": 2, "asset": "broll-bbbb", "in": 1, "out": 3}]},
                                 {"asset": "broll-bbbb", "in": 4, "out": 6}])  # fmt: skip
        doc = from_built("r", build(self.project, read_json(path)))
        self.assertFalse(doc["backfilled"])
        seg, cut, end = doc["segments"]
        self.assertEqual((seg["asset"], seg["moment"], seg["position"], seg["cutaway"]), ("voice-aaaa", "m-v", "first", False))
        self.assertEqual((cut["moment"], cut["position"], cut["cutaway"]), ("m-b", "middle", True))
        self.assertEqual(end["position"], "last")
        self.assertEqual(seg["tags"]["thread"], "road-to-5")
        self.assertAlmostEqual(seg["share"], 4 / 8)
        self.assertAlmostEqual(cut["share"], 2 / 8)
        self.assertAlmostEqual(end["share"], 2 / 8)
        self.assertAlmostEqual(doc["voiceShare"], 6 / 8)  # the voice carries on under the cutaway
        self.assertAlmostEqual(doc["faceShare"], 4 / 8)  # the face doesn't
        self.assertAlmostEqual(doc["avgShotSeconds"], 8 / 4)
        self.assertEqual(doc["thread"], "road-to-5")

    def test_two_track_voice_under_footage_counts_the_whole_voice(self):
        # The sprint-zone layout: voice pieces back to back on V1, one long clip on V2 over all of them.
        self.t.asset("voice-aaaa", duration=20, tags=tags(moment("m-v", 0, 20, role="voice", speaker="me", face=True)))
        self.t.asset("sprint-bbbb", duration=20, tags=tags(moment("m-s", 0, 20, role="effort")))
        timeline = lambda track, start, frames: {"track": track, "startFrame": start, "durationFrames": frames, "transitionInFrames": 0}
        segments = [{"asset": "voice-aaaa", "in": 0, "out": 3, "timeline": timeline("V1", 0, 90)},
                    {"asset": "voice-aaaa", "in": 5, "out": 10, "timeline": timeline("V1", 90, 150)},
                    {"asset": "sprint-bbbb", "in": 0, "out": 8, "timeline": timeline("V2", 0, 240)}]  # fmt: skip
        path = self.t.plan("r", segments, timeline={"version": 1, "fps": 30, "durationFrames": 240})
        plan = read_json(path)
        for doc in (from_built("r", build(self.project, plan)), from_plan(self.project, plan, {"slug": "r"})):
            with self.subTest(backfilled=doc["backfilled"]):
                self.assertAlmostEqual(doc["runtimeSeconds"], 8)
                self.assertAlmostEqual(doc["voiceShare"], 1.0)
                first, second, sprint = doc["segments"]
                self.assertEqual((first["seconds"], second["seconds"]), (0, 0))  # V2 covers the picture
                self.assertAlmostEqual(sprint["share"], 1.0)
                self.assertEqual(sprint["position"], "first")
                self.assertAlmostEqual(doc["faceShare"], 0.0)

    def test_upper_track_only_covers_where_it_sits(self):
        self.t.asset("voice-aaaa", duration=20, tags=tags(moment("m-v", 0, 20, role="voice", speaker="me")))
        self.t.asset("broll-bbbb", duration=20, tags=tags(moment("m-b", 0, 20)))
        timeline = lambda track, start, frames: {"track": track, "startFrame": start, "durationFrames": frames, "transitionInFrames": 0}
        path = self.t.plan("r", [{"asset": "voice-aaaa", "in": 0, "out": 6, "timeline": timeline("V1", 0, 180)},
                                 {"asset": "broll-bbbb", "in": 0, "out": 2, "timeline": timeline("V2", 60, 60)}],
                           timeline={"version": 1, "fps": 30, "durationFrames": 180})  # fmt: skip
        doc = from_built("r", build(self.project, read_json(path)))
        voice, broll = doc["segments"]
        self.assertAlmostEqual(voice["share"], 4 / 6, places=3)
        self.assertAlmostEqual(broll["share"], 2 / 6, places=3)
        self.assertEqual((voice["position"], broll["position"]), ("first", "middle"))
        self.assertAlmostEqual(doc["voiceShare"], 1.0)
        self.assertAlmostEqual(doc["avgShotSeconds"], 6 / 3)

    def test_record_writes_the_file_and_never_fails_a_render(self):
        self.t.asset("clip-aaaa", tags=tags(moment("m-a", 0, 8)))
        path = self.t.plan("r", [{"asset": "clip-aaaa", "in": 0, "out": 4}])
        record(self.project, "r", build(self.project, read_json(path)))
        self.assertEqual(read_json(self.project.out_dir / "r" / "lineage.json")["segments"][0]["moment"], "m-a")
        record(self.project, "r", {})  # malformed: warns, doesn't raise

    def test_teammate_captions_do_not_count_as_the_riders_voice(self):
        self.t.asset("talk-aaaa", tags=tags(moment("m-t", 0, 4, role="crew", speaker="teammate")), words=words(("hello", 0.5, 1.0)))
        path = self.t.plan("r", [{"asset": "talk-aaaa", "in": 0, "out": 4, "captions": "transcript"}])
        built = build(self.project, {**read_json(path), "spine": "voice"})
        self.assertEqual(from_built("r", built)["voiceShare"], 0.0)
        self.assertTrue(any("spine: marked voice" in w for w in built["warnings"]))

    def test_muted_missing_and_speed_disabled_audio_do_not_count_as_voice(self):
        self.t.asset("talk", tags=tags(moment("m-aaaaaa", 0, 9, role="voice", speaker="me")))
        for spec in ({"volume": 0}, {"speed": 4}):
            with self.subTest(spec=spec):
                plan = {"slug": "r", "spine": "voice", "autoSfx": False,
                        "segments": [{"asset": "talk", "in": 0, "out": 8, **spec}]}
                built = build(self.project, plan)
                self.assertEqual(from_built("r", built)["voiceShare"], 0)
                self.assertEqual(from_plan(self.project, plan, {})["voiceShare"], 0)
                self.assertTrue(any("spine: marked voice" in w for w in built["warnings"]))
        asset = load_asset(self.project, "talk")
        asset["probe"]["hasAudio"] = False
        save_asset(self.project, asset)
        plan["segments"][0] = {"asset": "talk", "in": 0, "out": 8}
        self.assertEqual(from_built("r", build(self.project, plan))["voiceShare"], 0)
        self.assertEqual(from_plan(self.project, plan, {})["voiceShare"], 0)

    def test_nat_volume_curve_counts_only_unmuted_time_in_render_and_backfill(self):
        self.t.asset("talk", tags=tags(moment("m-aaaaaa", 0, 9, role="voice", speaker="me")))
        plan = {"slug": "r", "autoSfx": False, "segments": [{"asset": "talk", "in": 0, "out": 8, "volume": 0,
                "nat": {"at": 2, "for": 2, "volume": 1}}]}
        self.assertAlmostEqual(from_built("r", build(self.project, plan))["voiceShare"], 0.3)  # 2 s + two .2 s ramps
        self.assertAlmostEqual(from_plan(self.project, plan, {})["voiceShare"], 0.3)
        plan["segments"][0].update(volume=1, nat={"volume": 0})
        self.assertEqual(from_built("r", build(self.project, plan))["voiceShare"], 0)
        self.assertEqual(from_plan(self.project, plan, {})["voiceShare"], 0)

    def test_config_and_instagram_muting_are_respected(self):
        self.t.asset("talk", tags=tags(moment("m-aaaaaa", 0, 9, role="voice", speaker="me")), words=words(("hello", 1, 2)))
        plan = {"slug": "r", "autoSfx": False, "segments": [{"asset": "talk", "in": 0, "out": 8, "captions": "transcript"}]}
        self.project.config["audio"]["speechVolume"] = 0
        self.assertEqual(from_built("r", build(self.project, plan))["voiceShare"], 0)
        self.assertEqual(from_plan(self.project, plan, {})["voiceShare"], 0)
        self.project.config["audio"]["speechVolume"] = 1
        built = build(self.project, plan)
        self.assertEqual(from_built("r", built)["voiceShare"], 1)
        built["music"] = {"instagram": {"videoVolume": 0}}
        self.assertEqual(from_built("r", built)["voiceShare"], 0)
        self.assertEqual(from_plan(self.project, plan, {"instagramAudio": {"videoVolume": 0}})["voiceShare"], 0)

    def test_cutaway_at_zero_is_the_opening_shot(self):
        self.t.asset("a-aaaa", tags=tags(moment("m-a", 0, 8)))
        self.t.asset("b-bbbb", tags=tags(moment("m-b", 0, 8, opener=True)))
        path = self.t.plan("r", [{"asset": "a-aaaa", "in": 0, "out": 5, "cutaways": [{"at": 0, "asset": "b-bbbb", "in": 0, "out": 2}]}])
        seg, cut = from_built("r", build(self.project, read_json(path)))["segments"]
        self.assertEqual((cut["position"], seg["position"]), ("first", "last"))

    def test_photo_uses_its_own_score_and_has_no_moment(self):
        self.t.asset("clip-aaaa", tags=tags(moment("m-a", 0, 8)))
        self.t.asset("pic-bbbb", kind="photo", tags={"type": "scenery", "shot": "pov", "quality": 4, "summary": "x", "score": 5})
        path = self.t.plan("r", [{"asset": "clip-aaaa", "in": 0, "out": 4}, {"asset": "pic-bbbb", "duration": 2}])
        photo = from_built("r", build(self.project, read_json(path)))["segments"][1]
        self.assertEqual((photo["moment"], photo["match"], photo["tags"]["score"]), (None, "photo", 5))

    def rendered(self, slug: str, plan_segments: list[dict], post_segments: list[dict] | None = None, length: float = 10) -> None:
        write_json(self.project.out_dir / slug / "plan.json", {"slug": slug, "format": "pov-joke", "segments": plan_segments})
        write_json(self.project.out_dir / slug / "post.json", {"slug": slug, "status": "posted", "caption": slug, "duration": length, "reelId": slug,
                                                               "file": "x", "cover": "y", "segments": post_segments or []})  # fmt: skip

    def test_backfill_resolves_short_ids_and_moment_indexes(self):
        self.t.asset("261029-img-3536-db04", tags=tags(moment("m-x", 0, 4), moment("m-y", 5, 9, score=5)))
        self.rendered("r", [{"asset": "261029-img-3536", "moment": 1}], [{"asset": "261029-img-3536-db04", "in": 5.5, "out": 8.5}])
        result = backfill(self.project)
        doc = read_json(self.project.out_dir / "r" / "lineage.json")
        self.assertEqual((result["momentMatched"], result["unmatched"]), (1, []))
        self.assertTrue(doc["backfilled"])
        self.assertEqual((doc["segments"][0]["asset"], doc["segments"][0]["moment"]), ("261029-img-3536-db04", "m-y"))
        self.assertAlmostEqual(doc["runtimeSeconds"], 3.0)

    def test_backfill_uses_the_moment_index_when_post_has_no_timing(self):
        self.t.asset("clip-aaaa", tags=tags(moment("m-x", 0, 4), moment("m-y", 5, 9)))
        self.rendered("r", [{"asset": "clip-aaaa", "moment": 1, "speed": 2}])
        backfill(self.project)
        doc = read_json(self.project.out_dir / "r" / "lineage.json")
        self.assertEqual(doc["segments"][0]["moment"], "m-y")
        self.assertAlmostEqual(doc["runtimeSeconds"], 2.0)

    def test_backfill_resolves_stable_moment_ids(self):
        self.t.asset("clip", tags=tags(moment("m-aaaaaa", 0, 4), moment("m-bbbbbb", 5, 9)))
        self.rendered("r", [{"asset": "clip", "moment": "m-bbbbbb"}])
        backfill(self.project)
        entry = read_json(self.project.out_dir / "r" / "lineage.json")["segments"][0]
        self.assertEqual((entry["moment"], entry["in"], entry["out"]), ("m-bbbbbb", 5, 9))

    def test_backfill_upgrades_legacy_voice_without_replacing_snapshots(self):
        self.t.asset("talk", tags=tags(moment("m-aaaaaa", 0, 9, role="crew", speaker="teammate", score=2)), words=words(("hello", 1, 2)))
        segments = [{"asset": "talk", "in": 0, "out": 8, "captions": "transcript"}]
        self.rendered("r", segments, length=8)
        plan = read_json(self.project.out_dir / "r" / "plan.json")
        old = from_plan(self.project, plan, {})
        old.update(version=1, voiceShare=1, backfilled=False)
        del old["segments"][0]["audioSeconds"]
        write_json(self.project.out_dir / "r" / "lineage.json", old)
        asset = load_asset(self.project, "talk")
        asset["tags"]["moments"][0].update(role="voice", speaker="me", score=5)
        save_asset(self.project, asset)
        self.assertEqual(backfill(self.project)["upgraded"], ["r"])
        upgraded = read_json(self.project.out_dir / "r" / "lineage.json")
        self.assertEqual(upgraded["voiceShare"], 0)
        self.assertEqual(upgraded["segments"][0]["tags"], old["segments"][0]["tags"])
        self.assertEqual(upgraded["createdAt"], old["createdAt"])
        self.assertFalse(upgraded["backfilled"])
        before = (self.project.out_dir / "r" / "lineage.json").read_bytes()
        self.assertEqual(backfill(self.project)["alreadyHad"], 1)
        self.assertEqual((self.project.out_dir / "r" / "lineage.json").read_bytes(), before)

    def test_legacy_upgrade_refuses_a_changed_timeline_without_overwriting(self):
        self.t.asset("talk", tags=tags(moment("m-aaaaaa", 0, 9, role="voice", speaker="me")))
        self.rendered("r", [{"asset": "talk", "in": 0, "out": 8}], length=8)
        backfill(self.project)
        path = self.project.out_dir / "r" / "lineage.json"
        doc = read_json(path)
        doc["version"] = 1
        write_json(path, doc)
        before = path.read_bytes()
        write_json(self.project.out_dir / "r" / "plan.json", {"slug": "r", "segments": [{"asset": "talk", "in": 3, "out": 8}]})
        with self.assertRaisesRegex(ReelsError, "no longer matches"):
            backfill(self.project)
        self.assertEqual(path.read_bytes(), before)

    def test_backfill_reports_a_segment_that_matches_nothing(self):
        self.t.asset("clip-aaaa", tags=tags(moment("m-x", 0, 2)))
        self.rendered("r", [{"asset": "clip-aaaa", "in": 5, "out": 7}])
        self.assertEqual([u["asset"] for u in backfill(self.project)["unmatched"]], ["clip-aaaa"])

    def test_backfill_is_idempotent(self):
        self.t.asset("clip-aaaa", tags=tags(moment("m-x", 0, 4)))
        self.rendered("r", [{"asset": "clip-aaaa", "in": 0, "out": 4}])
        first = backfill(self.project)
        path = self.project.out_dir / "r" / "lineage.json"
        before = path.read_text()
        second = backfill(self.project)
        self.assertEqual((first["written"], second["written"], second["alreadyHad"]), (["r"], [], 1))
        self.assertEqual(path.read_text(), before)

    def test_snapshot_keeps_tags_from_the_time_it_was_written(self):
        asset = self.t.asset("clip-aaaa", tags=tags(moment("m-x", 0, 4, score=2)))
        self.rendered("r", [{"asset": "clip-aaaa", "in": 0, "out": 4}])
        backfill(self.project)
        asset["tags"]["moments"][0]["score"] = 5
        write_json(self.project.assets_dir / "clip-aaaa.json", asset)
        backfill(self.project)
        self.assertEqual(read_json(self.project.out_dir / "r" / "lineage.json")["segments"][0]["tags"]["score"], 2)

    def test_from_plan_matches_a_fade_and_a_cutaway_like_the_renderer(self):
        self.t.asset("a-aaaa", tags=tags(moment("m-a", 0, 9)))
        self.t.asset("b-bbbb", tags=tags(moment("m-b", 0, 9)))
        segments = [{"asset": "a-aaaa", "in": 0, "out": 5, "cutaways": [{"at": 1, "asset": "b-bbbb", "in": 1, "out": 2}]},
                    {"asset": "b-bbbb", "in": 3, "out": 6, "transition": "fade"}]  # fmt: skip
        plan = read_json(self.t.plan("r", segments))
        rendered = from_built("r", build(self.project, plan))
        rebuilt = from_plan(self.project, plan, {"slug": "r", "segments": []})
        self.assertAlmostEqual(rendered["runtimeSeconds"], rebuilt["runtimeSeconds"])
        for a, b in zip(rendered["segments"], rebuilt["segments"]):
            self.assertEqual((a["asset"], a["cutaway"], a["moment"], a["position"]), (b["asset"], b["cutaway"], b["moment"], b["position"]))
            self.assertAlmostEqual(a["share"], b["share"])


class FootageReportTests(unittest.TestCase):
    def setUp(self):
        self.t = TempProject(self)
        self.project = self.t.project
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def reel(self, slug: str, *, score=4, role="effort", opener=False, face=False, speaker="none", length=10.0, voice=0.0, thread=None) -> dict:
        entry = {"asset": "a", "moment": "m", "cutaway": False, "position": "first", "share": 1.0,
                 "tags": {"score": score, "role": role, "speaker": speaker, "face": face, "mood": "calm", "opener": opener, "line": None, "thread": thread, "beat": None}}  # fmt: skip
        write_json(self.project.out_dir / slug / "lineage.json", {"version": 2, "slug": slug, "backfilled": False, "voiceShare": voice, "faceShare": 0.0,
                                                                  "avgShotSeconds": 4.0, "thread": thread, "segments": [entry]})  # fmt: skip
        return {"slug": slug, "early": False, "length": length, "hold": 50.0, "watchThrough": 0.5, "sharesReach": 0.01, "commentsReach": 0.0, "format": "pov-joke"}

    def text(self, reels: list[dict]) -> str:
        return "\n".join(section(self.project, reels))

    def test_length_bands(self):
        self.assertEqual([length_band(x) for x in (10.99, 11, 15, 15.01, None)], ["under 11 s", "11–15 s", "11–15 s", "over 15 s", "unknown length"])

    def test_every_comparison_is_split_by_length(self):
        text = self.text([self.reel("a", length=8), self.reel("b", length=12), self.reel("c", length=20)])
        for band in ("| under 11 s |", "| 11–15 s |", "| over 15 s |"):
            self.assertIn(band, text)
        for line in text.splitlines():
            if line.startswith("| length"):
                self.assertIn("reels", line)

    def test_groups_under_five_are_flagged_and_five_is_not(self):
        reels = [self.reel(f"small{i}", score=3) for i in range(4)] + [self.reel(f"big{i}", score=5) for i in range(5)]
        lines = section(self.project, reels)
        small = next(l for l in lines if l.startswith("| under 11 s | 3 | 4 |"))
        big = next(l for l in lines if l.startswith("| under 11 s | 5 | 5 |"))
        self.assertIn("too small to act on", small)
        self.assertNotIn("too small", big)

    def test_early_reels_are_left_out_and_counted(self):
        early = {**self.reel("e", score=1), "early": True}
        data, counts = rows(self.project, [early, self.reel("k", score=4)])
        self.assertEqual(([r["slug"] for r in data], counts["early"]), (["k"], 1))
        self.assertNotIn("| under 11 s | 1 |", self.text([early, self.reel("k", score=4)]))

    def test_shares_are_only_grouped_by_format_and_thread(self):
        text = self.text([self.reel("a", thread="road-to-5")])
        tables = text.split("### Shares and comments, by format and thread only")[1].split("### Score calibration")[0]
        heads = [l.split("|")[2].strip() for l in tables.splitlines() if l.startswith("| length")]
        self.assertEqual(heads, ["format", "thread"])

    def test_hold_uses_the_opening_shot_not_later_ones(self):
        self.reel("a", score=2)
        path = self.project.out_dir / "a" / "lineage.json"
        doc = read_json(path)
        doc["segments"][0]["share"] = 0.5
        doc["segments"].append({**doc["segments"][0], "position": "last", "tags": {**doc["segments"][0]["tags"], "score": 5}})
        write_json(path, doc)
        data, _ = rows(self.project, [{"slug": "a", "early": False, "length": 10.0, "hold": 50.0, "watchThrough": 0.5}])
        self.assertEqual(data[0]["opening"]["score"], 2)
        self.assertAlmostEqual(data[0]["meanScore"], 3.5)

    def test_reports_reels_without_lineage(self):
        text = self.text([{"slug": "no-lineage", "early": False, "length": 10, "hold": 50.0, "watchThrough": 0.5}])
        self.assertIn("1 without lineage (no-lineage)", text)

    def test_legacy_voice_metrics_are_not_mixed_with_corrected_metrics(self):
        reel = self.reel("old")
        path = self.project.out_dir / "old" / "lineage.json"
        doc = read_json(path)
        doc["version"] = 1
        write_json(path, doc)
        data, counts = rows(self.project, [reel])
        self.assertEqual(data, [])
        self.assertEqual(counts["noLineage"], ["old"])

    def _posted(self, slug: str, segments: list[dict]) -> None:
        write_json(self.project.out_dir / slug / "post.json", {"slug": slug, "status": "posted", "caption": slug, "duration": 10, "reelId": slug,
                                                               "file": "x", "cover": "y", "segments": segments})  # fmt: skip
        write_json(self.project.out_dir / slug / "plan.json", {"slug": slug, "format": "pov-joke", "segments": segments})

    def _pull(self, rows_: list[list]) -> None:
        write_json(self.project.root / "analytics" / "2026-11-01T000000.json", {"pulledAt": self.now.isoformat(), "fields": DEFAULT_FIELDS, "rows": rows_})

    def _row(self, slug: str, days: float) -> list:
        posted = self.now - timedelta(days=days)
        return [posted.strftime("%Y%m%d%H%M%S"), slug, slug, "u", 1, 6, 200, 1, 3, 260, 6.0, 1200, None, 55.0, None]

    def test_stats_writes_the_footage_section(self):
        self._posted("r", [])
        self._posted("young", [])
        self.reel("r", score=4)
        self.reel("young", score=1)
        self._pull([self._row("r", 8), self._row("young", 1)])
        stats(self.project)
        md = (self.project.out_dir / "PERFORMANCE.md").read_text()
        self.assertIn("## Footage (reels older than 48 h)", md)
        self.assertIn("1 under 48 h old", md)
        self.assertIn("| under 11 s | 4 | 1 | 55.0% | 55.0% to 55.0% | too small to act on (under 5) |", md)
        self.assertNotIn("| under 11 s | 1 | 1 |", md)

    def test_sphere_flag_resolves_short_asset_ids(self):
        self.t.asset("261029-img-3536-db04", tags=tags(moment("m-x", 0, 4)))
        asset = read_json(self.project.assets_dir / "261029-img-3536-db04.json")
        asset["probe"]["sphere"] = True
        write_json(self.project.assets_dir / "261029-img-3536-db04.json", asset)
        write_json(self.project.out_dir / "s" / "post.json", {"slug": "s", "status": "posted", "caption": "s", "duration": 10, "reelId": "s",
                                                              "file": "x", "cover": "y"})  # fmt: skip
        write_json(self.project.out_dir / "s" / "plan.json", {"segments": [{"asset": "261029-img-3536"}, {"asset": "gone-for-good"}]})
        self._pull([self._row("s", 8)])
        self.assertTrue(build_report(self.project)[0][0]["sphere"])

    def test_review_points_at_the_footage_section_without_changing_its_record(self):
        self._posted("r", [])
        self.reel("r")
        self._pull([self._row("r", 8)])
        checkpoint = review(self.project)
        week = sorted((self.project.root / "learning/reviews").glob("*.md"))[0].read_text()
        self.assertIn("- Footage: 1 of 1 matched reels have lineage", week)
        self.assertNotIn("footage", checkpoint)


if __name__ == "__main__":
    unittest.main()
