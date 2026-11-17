import math
import unittest

from reels.editor.bench_proxy_metal import _quality_gate, _selected_frames


class ProxyQualityGateTests(unittest.TestCase):
    def samples(self):
        return [{"frame": frame, "preEncodePSNR": {"average": 35.0},
                 "commonReferencePSNR": {"metal": {"Y": 39.9, "average": 39.9},
                                         "videotoolbox": {"Y": 40.0, "average": 40.0}}}
                for frame in (0, 899)]

    def test_any_raw_sample_below_35_fails_without_averaging(self):
        samples = self.samples()
        self.assertTrue(_quality_gate(samples)["pass"])
        samples[0]["preEncodePSNR"]["average"] = 100.0
        samples[1]["preEncodePSNR"]["average"] = 34.999999
        result = _quality_gate(samples)
        self.assertFalse(result["pass"])
        self.assertEqual(result["preEncodeFailures"],
                         [{"frame": 899, "plane": "average", "native": 34.999999}])

    def test_either_common_reference_plane_deficit_above_point_one_fails(self):
        for plane in ("Y", "average"):
            with self.subTest(plane=plane):
                samples = self.samples()
                samples[0]["commonReferencePSNR"]["metal"][plane] = 100.0
                samples[1]["commonReferencePSNR"]["metal"][plane] = 39.899999
                result = _quality_gate(samples)
                self.assertFalse(result["pass"])
                self.assertEqual(result["commonReferenceFailures"],
                                 [{"frame": 899, "plane": plane, "native": 39.899999, "W5b": 40.0}])

    def test_exact_equality_infinity_and_nan_are_explicit(self):
        for native, baseline, expected in [(40, 40, True), (math.inf, math.inf, True),
                                            (math.inf, 40, True), (40, math.inf, False),
                                            (math.nan, 40, False), (40, math.nan, False),
                                            (-math.inf, -math.inf, False)]:
            for plane in ("Y", "average"):
                with self.subTest(native=native, baseline=baseline, plane=plane):
                    samples = self.samples()
                    samples[1]["commonReferencePSNR"]["metal"][plane] = native
                    samples[1]["commonReferencePSNR"]["videotoolbox"][plane] = baseline
                    self.assertEqual(_quality_gate(samples)["pass"], expected)
        for raw, expected in [("inf", True), (math.nan, False), (-math.inf, False)]:
            samples = self.samples()
            samples[1]["preEncodePSNR"]["average"] = raw
            self.assertEqual(_quality_gate(samples)["pass"], expected)
        self.assertFalse(_quality_gate([])["pass"])

    def test_original_first_last_and_interior_samples_are_retained(self):
        self.assertEqual(_selected_frames(900), [0, 30, 150, 300, 450, 600, 750, 899])
