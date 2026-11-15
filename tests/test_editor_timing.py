from __future__ import annotations

import unittest

from reels.editor.timing import (
    frames_to_seconds,
    seconds_to_frames,
    split_segment,
    timeline_positions,
    trim_segment,
    trim_local_source,
)


class EditorTimingTests(unittest.TestCase):
    def test_frame_conversion_uses_renderer_rounding(self):
        self.assertEqual(seconds_to_frames(1.5 / 30, 30), 2)
        self.assertEqual(frames_to_seconds(45, 30), 1.5)

    def test_timeline_positions_use_fade_overlap_not_duration_sum(self):
        segments = [
            {"in": 0, "out": 4, "speed": 2},
            {"in": 2, "out": 8, "speed": 1, "transition": "fade"},
        ]
        positions = timeline_positions(segments, fps=30, fade_frames=12)
        self.assertEqual(positions[0]["frames"], 60)
        self.assertEqual(positions[1]["frames"], 180)
        self.assertEqual(positions[1]["transitionInFrames"], 12)
        self.assertEqual(positions[1]["startFrame"], 48)
        self.assertEqual(positions[-1]["endFrame"], 228)

    def test_trim_ripples_global_events_by_speed_adjusted_frame_delta(self):
        plan = {
            "text": [{"text": "after", "at": 6, "for": 1}],
            "musicDrops": [{"at": 4, "for": 4}],
            "segments": [{"asset": "a", "in": 0, "out": 10, "speed": 2,
                          "text": [{"text": "local", "at": 1, "for": 1}]}],
        }
        new_frames = trim_segment(plan, 0, 2, 8, fps=30, start_frame=0, old_frames=150)
        self.assertEqual(new_frames, 90)
        self.assertEqual(plan["text"][0]["at"], 4)
        self.assertEqual(plan["musicDrops"][0], {"at": 4, "for": 2})
        self.assertEqual(plan["segments"][0]["text"], [{"text": "local", "at": 0, "for": 1}])

    def test_speed_aware_split_rebases_bleeps_and_crossing_cutaway_source_range(self):
        source = {
            "asset": "voice", "in": 10, "out": 20, "speed": 2,
            "transition": "fade",
            "bleep": [{"at": 1, "for": 1.5}, {"at": 4, "for": 0.8}],
            "cutaways": [{"at": 1, "asset": "ride", "in": 2, "out": 6, "speed": 2}],
            "text": [{"text": "line", "at": 2, "for": 1}],
        }
        left, right = split_segment(source, local_frame=45, fps=30, total_frames=150)
        self.assertEqual(left["out"], 13)
        self.assertEqual(right["in"], 13)
        self.assertEqual(left["transition"], "fade")
        self.assertEqual(right["transition"], "cut")
        self.assertEqual(left["bleep"], [{"at": 1, "for": 0.5}])
        self.assertEqual(right["bleep"], [{"at": 0, "for": 1.0}, {"at": 2.5, "for": 0.8}])
        self.assertEqual(left["cutaways"], [{"at": 1, "asset": "ride", "in": 2, "out": 3, "speed": 2}])
        self.assertEqual(right["cutaways"], [{"at": 0, "asset": "ride", "in": 3, "out": 6, "speed": 2}])
        self.assertEqual(left["text"], [])
        self.assertEqual(right["text"], [{"text": "line", "at": 0.5, "for": 1}])

    def test_local_trim_rebases_nested_windows_and_keys_preserving_unknown_fields(self):
        clip = {"in": 2, "out": 10, "speed": 2, "at": 3, "extension": {"keep": True},
                "bleep": {"windows": [{"at": 1, "for": 1, "custom": "keep"}], "sound": "mute"},
                "nat": {"at": 1, "for": 2, "volume": 1},
                "exhibits": [{"at": 1, "for": 2, "custom": 7}],
                "sphere": {"path": [{"at": 0, "yaw": 0}, {"at": 4, "yaw": 40}]}}
        trim_local_source(clip, 4, 8)
        self.assertEqual(clip["at"], 3)
        self.assertEqual(clip["extension"], {"keep": True})
        self.assertEqual(clip["bleep"]["windows"], [{"at": 0, "for": 1, "custom": "keep"}])
        self.assertEqual(clip["nat"], {"at": 0, "for": 2, "volume": 1})
        self.assertEqual(clip["exhibits"], [{"at": 0, "for": 2, "custom": 7}])
        self.assertEqual(clip["sphere"]["path"], [{"at": 0, "yaw": 10}, {"at": 2, "yaw": 30}])


if __name__ == "__main__":
    unittest.main()
