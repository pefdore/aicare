import math
import unittest

from aicare.decoders import (
    FixedOffsets,
    decode_plx,
    decode_sfloat,
    guess_offsets,
    heuristic_decode,
)


class TestSFloat(unittest.TestCase):
    def test_positive_mantissa_zero_exponent(self):
        # mantissa 0x0062 = 98, exponent 0
        self.assertEqual(decode_sfloat(0x0062), 98.0)

    def test_negative_mantissa(self):
        # mantissa -2 (0x0FFE), exponent 0
        self.assertEqual(decode_sfloat(0x0FFE), -2.0)

    def test_negative_exponent(self):
        # mantissa 21, exponent -1 -> 2.1
        self.assertAlmostEqual(decode_sfloat(0xF015), 2.1)

    def test_nan_reserved(self):
        self.assertTrue(math.isnan(decode_sfloat(0x07FF)))


def plx_frame(spo2: int, pulse: int, flags: int = 0x0000) -> bytes:
    return bytes(
        [
            flags & 0xFF,
            (flags >> 8) & 0xFF,
            spo2 & 0xFF,
            (spo2 >> 8) & 0xFF,
            pulse & 0xFF,
            (pulse >> 8) & 0xFF,
        ]
    )


class TestPlx(unittest.TestCase):
    def test_basic_frame(self):
        out = decode_plx(plx_frame(98, 72))
        self.assertEqual(out["spo2"], 98)
        self.assertEqual(out["pulse"], 72)

    def test_too_short(self):
        self.assertIsNone(decode_plx(b"\x00\x00\x62"))

    def test_implausible_values_rejected(self):
        self.assertIsNone(decode_plx(plx_frame(200, 72)))

    def test_perfusion_index_flag(self):
        frame = plx_frame(98, 72, flags=0x0200) + b"\x15\x00"  # PI = 2.1
        out = decode_plx(frame)
        self.assertEqual(out["spo2"], 98)
        self.assertAlmostEqual(out["pi"], 21.0)

    def test_realistic_sfloat_pulse(self):
        # spo2=98 (sfloat), pulse=72.5 encoded with exponent -1 is not
        # representable as int pulse; ensure integer path stays dominant
        frame = bytes([0x00, 0x00, 0x62, 0x00, 0x85, 0xF0])  # pulse 8.5
        self.assertIsNone(decode_plx(frame))


class TestHeuristic(unittest.TestCase):
    def test_unambiguous_positions(self):
        # 98 is only spo2-plausible, 180 is only pulse-plausible
        hits = heuristic_decode(bytes([0x00, 98, 180]))
        by_type = {h["type"]: h for h in hits}
        self.assertEqual(by_type["spo2?"]["value"], 98)
        self.assertEqual(by_type["spo2?"]["offset"], 1)
        self.assertEqual(by_type["pulse?"]["value"], 180)
        self.assertEqual(by_type["pulse?"]["offset"], 2)

    def test_ambiguous_byte_reported_twice(self):
        # 80 is both a plausible spo2 and pulse: appears once per type
        hits = heuristic_decode(bytes([80]))
        self.assertEqual(len(hits), 2)

    def test_no_hits(self):
        self.assertEqual(heuristic_decode(bytes([0x00, 0x01, 0x02])), [])


class TestGuessOffsets(unittest.TestCase):
    def test_constant_offsets(self):
        frames = [bytes([0x01, 98, 72]), bytes([0x01, 97, 80])]
        spo2 = [98, 97]
        pulse = [72, 80]
        offsets = guess_offsets(frames, spo2, pulse)
        self.assertEqual((offsets.spo2, offsets.pulse), (1, 2))

    def test_same_value_everywhere_prefers_first(self):
        frames = [bytes([98, 72])]
        offsets = guess_offsets(frames, [98], [72])
        self.assertEqual((offsets.spo2, offsets.pulse), (0, 1))

    def test_no_consistent_offset(self):
        frames = [bytes([98, 72]), bytes([97, 80])]
        # spo2 values that never match the frames
        offsets = guess_offsets(frames, [99, 99], [72, 80])
        self.assertIsNone(offsets)

    def test_misaligned_input_raises(self):
        with self.assertRaises(ValueError):
            guess_offsets([b"\x00"], [1, 2], [3])


class TestFixedOffsets(unittest.TestCase):
    def test_decode_with_pi(self):
        off = FixedOffsets(spo2=1, pulse=2, pi=3, scale_pi=0.1)
        out = off.decode(bytes([0x01, 98, 72, 21]))
        self.assertEqual(out["spo2"], 98)
        self.assertEqual(out["pulse"], 72)
        self.assertAlmostEqual(out["pi"], 2.1)

    def test_decode_pi_out_of_range_is_skipped(self):
        off = FixedOffsets(spo2=1, pulse=2, pi=5)
        out = off.decode(bytes([0x01, 98, 72]))
        self.assertNotIn("pi", out)


if __name__ == "__main__":
    unittest.main()
