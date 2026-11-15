from __future__ import annotations

import json
import unittest
from concurrent.futures import Future

from helpers import TempProject, words
from reels.editor.search import MODEL_ID, SearchCoordinator, SearchIndex


def moment(mid: str, start: float, end: float, label: str, **extra) -> dict:
    return {"id": mid, "in": start, "out": end, "label": label, "score": 4, **extra}


def tags(*moments, **extra) -> dict:
    return {
        "type": "solo-ride",
        "summary": "A steady ride",
        "moments": list(moments),
        **extra,
    }


class ManualClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class ManualTimer:
    def __init__(self, due: float, callback):
        self.due = due
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class ManualScheduler:
    def __init__(self, clock: ManualClock):
        self.clock = clock
        self.timers: list[ManualTimer] = []

    def call_later(self, delay: float, callback):
        timer = ManualTimer(self.clock() + delay, callback)
        self.timers.append(timer)
        return timer

    def advance(self, seconds: float):
        self.clock.now += seconds
        due = [timer for timer in self.timers if not timer.cancelled and timer.due <= self.clock()]
        self.timers = [timer for timer in self.timers if timer not in due]
        for timer in due:
            timer.callback()


class ManualExecutor:
    """Deterministic executor: work only runs when a test explicitly drains it."""

    def __init__(self):
        self.queue = []
        self.closed = False

    def submit(self, function, *args):
        future = Future()
        self.queue.append((future, function, args))
        return future

    def run_next(self):
        future, function, args = self.queue.pop(0)
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(function(*args))
            except BaseException as error:
                future.set_exception(error)

    def run_all(self):
        while self.queue:
            self.run_next()

    def shutdown(self, **_kwargs):
        self.closed = True


class EditorSearchIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = TempProject(self)

    def test_corrected_word_is_authoritative_with_exact_unicode_highlights(self):
        asset = self.temp.asset(
            "talking",
            tags=tags(moment("m-voice", 1, 4, "warm-up")),
            words=words(("Zwift!", 2.125, 2.5)),
        )
        transcript_path = self.temp.project.abs(asset["transcript"])
        transcript = json.loads(transcript_path.read_text())
        transcript["segments"] = [{"start": 2.0, "end": 3.0, "text": "swift"}]
        transcript_path.write_text(json.dumps(transcript))

        index = SearchIndex(self.temp.project, "lib-1")
        result = index.regex("zwift", {})

        row, = [hit for hit in result["matches"] if hit["where"] == "transcript"]
        self.assertEqual(row["text"], "Zwift!")
        self.assertEqual(row["highlights"], [{"start": 0, "end": 5}])
        self.assertEqual(row["highlightSourceRanges"], [[{"in": 2.125, "out": 2.5}]])
        self.assertFalse(any(hit["where"] == "transcript" for hit in index.regex("swift")["matches"]))

    def test_indexes_metadata_fields_and_moment_filters(self):
        self.temp.asset(
            "one",
            tags=tags(
                moment("m-voice", 1, 3, "bridge effort", role="voice", face=True),
                moment("m-effort", 4, 6, "climbing", role="effort", face=False),
                hooks=["Hold the line"],
                notes="windy overlook",
                landmarks=[{"at": 5.0, "label": "stone bridge"}],
            ),
        )
        index = SearchIndex(self.temp.project, "rev")

        self.assertEqual(
            {r["momentId"] for r in index.regex("bridge")["matches"]},
            {"m-voice", "m-effort"},
        )
        voice = index.regex("bridge|climbing", {"role": "voice"})
        self.assertEqual({r["momentId"] for r in voice["matches"]}, {"m-voice"})
        self.assertTrue(any(row["where"] == "hook" for row in index.regex("line")["matches"]))
        self.assertTrue(any(row["where"] == "notes" for row in index.regex("overlook")["matches"]))

    def test_paginated_catalog_and_tools_are_json_safe_and_never_expose_paths(self):
        self.temp.asset("a", tags=tags(moment("m-a", 1, 2, "first")))
        self.temp.asset("b", tags=tags(moment("m-b", 1, 2, "second")))
        index = SearchIndex(self.temp.project, "rev")

        page1 = index.catalog(offset=0, limit=1)
        page2 = index.catalog(offset=1, limit=1)
        self.assertEqual((page1["total"], page2["total"]), (2, 2))
        self.assertNotEqual(page1["items"][0]["assetId"], page2["items"][0]["assetId"])
        passage = index.search_passages("second")["items"][0]
        got, = index.get_passages(ids=[passage["passageId"]])
        self.assertEqual(got["passageId"], passage["passageId"])
        encoded = json.dumps({"catalog": page1, "passage": got})
        self.assertNotIn("media/", encoded)
        self.assertNotIn("originalPath", encoded)

    def test_empty_query_returns_filtered_catalog_and_no_regex_hits(self):
        self.temp.asset(
            "voice",
            tags=tags(moment("m-v", 1, 2, "voice clip", role="voice")),
        )
        self.temp.asset(
            "effort",
            tags=tags(moment("m-e", 1, 2, "effort clip", role="effort")),
        )
        result = SearchIndex(self.temp.project, "r").regex("", {"role": "voice"})
        self.assertEqual(result["matches"], [])
        self.assertEqual([row["assetId"] for row in result["catalog"]["items"]], ["voice"])

    def test_low_confidence_is_based_on_the_matched_words(self):
        asset = self.temp.asset(
            "confidence",
            tags=tags(moment("m", 1, 3, "speech", role="voice")),
            words=[
                {"w": "clear", "s": 1.2, "e": 1.5, "p": 0.99},
                {"w": "guess", "s": 1.6, "e": 1.9, "p": 0.01},
            ],
        )
        index = SearchIndex(self.temp.project, "r")
        clear, = [row for row in index.regex("clear")["matches"] if row["where"] == "transcript"]
        guess, = [row for row in index.regex("guess")["matches"] if row["where"] == "transcript"]
        self.assertFalse(clear["lowConfidence"])
        self.assertTrue(guess["lowConfidence"])

    def test_three_word_transcript_uses_precomputed_keyterms(self):
        self.temp.project.config["transcription"]["keyterms"] = ["climb", "the", "hill"]
        self.temp.asset(
            "keyterms",
            tags=tags(moment("m", 1, 4, "speech", role="voice")),
            words=[
                {"w": "climb", "s": 1.2, "e": 1.4},
                {"w": "the", "s": 1.45, "e": 1.55},
                {"w": "hill", "s": 1.6, "e": 1.9},
            ],
        )

        index = SearchIndex(self.temp.project, "r")
        row, = [row for row in index.regex("hill")["matches"] if row["where"] == "transcript"]
        self.assertTrue(row["lowConfidence"])

    def test_invalid_and_pathological_patterns_are_inline_errors(self):
        self.temp.asset("haystack", tags=tags(summary=("a" * 20_000) + "!"))
        index = SearchIndex(
            self.temp.project, "r", regex_deadline=0.015, match_deadline=0.005,
        )

        invalid = index.regex("(")
        self.assertEqual(invalid["error"]["code"], "invalid_regex")
        slow = index.regex(r"(a+)+$")
        self.assertEqual(slow["error"]["code"], "regex_timeout")
        self.assertEqual(index.regex("a" * 4097)["error"]["code"], "invalid_regex")
        self.assertEqual(index.regex("(" * 1500)["error"]["code"], "invalid_regex")


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = TempProject(self)
        self.temp.asset(
            "clip",
            tags=tags(moment("m", 1, 3, "red bridge", role="voice")),
            words=words(("bridge", 1.5, 1.8)),
        )
        self.index = SearchIndex(self.temp.project, "revision-7")
        self.clock = ManualClock()
        self.scheduler = ManualScheduler(self.clock)
        self.executor = ManualExecutor()
        self.events = []

    def coordinator(self, runner, **kwargs):
        return SearchCoordinator(
            self.index,
            runner,
            self.events.append,
            clock=self.clock,
            scheduler=self.scheduler,
            executor=self.executor,
            **kwargs,
        )

    def test_semantic_starts_at_600ms_even_after_regex_match_and_merges_grounded_result(self):
        calls = []

        def runner(query, filters, index, cancel_event):
            calls.append((query, filters, index, cancel_event))
            passage = next(
                row for row in index.search_passages("bridge", filters)["items"]
                if row["where"] == "transcript"
            )
            return {"matches": [{
                "passageId": passage["passageId"],
                "reason": "The rider mentions a bridge.",
                "evidence": "bridge",
                "sourceIn": -100,
                "path": "/private/original.mp4",
            }]}

        coordinator = self.coordinator(runner)
        generation = coordinator.query("bridge", {"role": "voice"})
        self.executor.run_next()  # regex runs in background
        regex_event, = self.events
        self.assertEqual(regex_event["type"], "regex")
        self.assertTrue(regex_event["matches"])
        self.scheduler.advance(0.599)
        self.assertEqual(calls, [])
        self.scheduler.advance(0.001)
        self.assertEqual(len(self.executor.queue), 1)
        self.executor.run_next()

        semantic, = [event for event in self.events if event["type"] == "semantic"]
        self.assertEqual(calls[0][:3], ("bridge", {"role": "voice"}, self.index))
        self.assertEqual((semantic["generation"], semantic["libraryRevision"]), (generation, "revision-7"))
        self.assertEqual(semantic["model"], MODEL_ID)
        self.assertEqual(semantic["relatedRows"], [])
        self.assertEqual(semantic["enrichedRows"][0]["related"]["sourceIn"], 1.5)
        self.assertNotIn("path", semantic["enrichedRows"][0]["related"])
        coordinator.close()

    def test_empty_query_does_not_schedule_semantic_work(self):
        calls = []
        coordinator = self.coordinator(lambda *args: calls.append(args) or {"matches": []})
        coordinator.query("")
        self.executor.run_next()
        self.scheduler.advance(1.0)
        self.executor.run_all()
        self.assertEqual(calls, [])
        self.assertEqual(self.events[0]["catalog"]["total"], 1)
        coordinator.close()

    def test_semantic_different_range_enriches_existing_file_without_duplicate(self):
        def runner(query, filters, index, cancel_event):
            passage = next(row for row in index.search_passages("bridge")["items"] if row["where"] == "transcript")
            return {"matches": [{"passageId": passage["passageId"], "reason": "Same file, spoken detail.", "evidence": "bridge"}]}
        coordinator = self.coordinator(runner)
        coordinator.query("red")  # tagged moment at 1–3; spoken evidence at 1.5–1.8
        self.executor.run_next()
        self.scheduler.advance(0.6)
        self.executor.run_next()
        semantic, = [event for event in self.events if event["type"] == "semantic"]
        self.assertEqual(semantic["relatedRows"], [])
        enriched, = semantic["enrichedRows"]
        self.assertEqual(enriched["sourceRanges"][0]["sourceIn"], 1.5)
        self.assertEqual(enriched["sourceRanges"][0]["sourceOut"], 1.8)
        coordinator.close()

    def test_invalid_regex_still_triggers_semantic_search_after_idle(self):
        calls = []
        coordinator = self.coordinator(lambda *args: calls.append(args) or {"matches": []})
        coordinator.query("(")
        self.executor.run_next()
        self.assertEqual(self.events[0]["error"]["code"], "invalid_regex")
        self.scheduler.advance(0.6)
        self.executor.run_next()
        self.assertEqual(len(calls), 1)
        coordinator.close()

    def test_index_loading_does_not_add_another_600ms_debounce(self):
        calls = []
        coordinator = self.coordinator(lambda *args: calls.append(args) or {"matches": []})
        self.clock.now = 0.2
        coordinator.query("bridge", submitted_at=0.0)
        self.executor.run_next()
        self.scheduler.advance(0.399)
        self.assertEqual(calls, [])
        self.scheduler.advance(0.001)
        self.executor.run_next()
        self.assertEqual(len(calls), 1)
        coordinator.close()

    def test_change_cancels_old_generation_and_discards_late_result(self):
        calls = []

        class Runner:
            def __call__(self, query, filters, index, cancel_event):
                calls.append((query, cancel_event))
                return {"matches": []}

            def cancel(self, generation):
                calls.append(("cancel", generation))

        runner = Runner()
        coordinator = self.coordinator(runner)
        old = coordinator.query("old")
        self.executor.run_next()
        self.scheduler.advance(0.6)  # queues old semantic request
        old_cancel = calls[-1] if calls else None
        new = coordinator.query("new", {"face": True})
        self.assertGreater(new, old)
        self.executor.run_all()
        self.assertFalse(any(event["type"] == "semantic" for event in self.events))
        self.assertIn(("cancel", old), calls)
        self.assertNotEqual(old_cancel, ("old", None))
        coordinator.close()

    def test_structured_runner_error_is_preserved_and_regex_remains_available(self):
        class ProtocolError(Exception):
            code = "authentication"
            message = "Claude login is required"

        coordinator = self.coordinator(lambda *_args: (_ for _ in ()).throw(ProtocolError()))
        coordinator.query("bridge")
        self.executor.run_next()
        self.scheduler.advance(0.6)
        self.executor.run_next()
        error, = [event for event in self.events if event["type"] == "semanticError"]
        self.assertEqual(error["error"], {
            "code": "authentication", "message": "Claude login is required",
        })
        self.assertTrue(any(event["type"] == "regex" for event in self.events))
        coordinator.close()

    def test_semantic_cache_reuses_exact_query_filter_revision_model_prompt_key(self):
        calls = []

        def runner(query, filters, index, cancel_event):
            calls.append((query, dict(filters), index.library_revision))
            return {"matches": []}

        coordinator = self.coordinator(runner)
        coordinator.query("bridge", {"role": "voice"})
        self.executor.run_next()
        self.scheduler.advance(0.6)
        self.executor.run_next()
        self.assertEqual(len(calls), 1)

        coordinator.query("bridge", {"role": "voice"})
        self.executor.run_next()
        self.scheduler.advance(0.6)  # exact cache key; no new runner call
        self.assertEqual(len(calls), 1)
        cached, = [event for event in self.events if event["type"] == "semantic" and event["cached"]]
        self.assertEqual(cached["model"], MODEL_ID)
        coordinator.close()

    def test_semantic_evidence_is_verified_and_model_timestamps_are_ignored(self):
        def runner(query, filters, index, cancel_event):
            passage = next(
                row for row in index.search_passages("bridge", filters)["items"]
                if row["where"] == "transcript"
            )
            return {"matches": [
                {"passageId": "missing", "evidence": "bridge"},
                {"passageId": passage["passageId"], "evidence": "not a quote", "sourceIn": -500},
                {
                    "passageId": passage["passageId"],
                    "reason": "Grounded.",
                    "evidence": "bridge",
                    "sourceIn": -500,
                    "confidence": 1.0,
                },
            ]}

        coordinator = self.coordinator(runner)
        coordinator.query("bridge")
        self.executor.run_next()
        self.scheduler.advance(0.6)
        self.executor.run_next()
        semantic, = [event for event in self.events if event["type"] == "semantic"]
        related = semantic["relatedRows"][0] if semantic["relatedRows"] else semantic["enrichedRows"][0]["related"]
        self.assertEqual(related["sourceIn"], 1.5)
        self.assertEqual(related["confidence"], 0.95)
        coordinator.close()


if __name__ == "__main__":
    unittest.main()
