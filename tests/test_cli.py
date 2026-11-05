import unittest

from aicare.cli import main, parse_frame, format_services
from aicare.decoders import service_label


class TestParseFrame(unittest.TestCase):
    def test_0x_prefixed(self):
        self.assertEqual(parse_frame("0x0200620048"), b"\x02\x00\x62\x00\x48")

    def test_colon_separated(self):
        self.assertEqual(parse_frame("02:00:62:00:48"), b"\x02\x00\x62\x00\x48")

    def test_space_separated(self):
        self.assertEqual(parse_frame("02 00 62 00 48"), b"\x02\x00\x62\x00\x48")

    def test_compact(self):
        self.assertEqual(parse_frame("02006200 48"), b"\x02\x00\x62\x00\x48")

    def test_invalid(self):
        with self.assertRaises(ValueError):
            parse_frame("zz")

    def test_odd_length_compact(self):
        with self.assertRaises(ValueError):
            parse_frame("02006")


class TestDecodeCommand(unittest.TestCase):
    def test_plx_frame(self):
        code = main(["decode", "00:00:62:00:48:00"])
        self.assertEqual(code, 0)

    def test_heuristic_frame(self):
        code = main(["decode", "01:62:48"])
        self.assertEqual(code, 0)

    def test_no_hits(self):
        code = main(["decode", "00:01:02"])
        self.assertEqual(code, 1)

    def test_fixed_offsets(self):
        code = main(
            ["decode", "01:62:48:15", "--spo2-offset", "1", "--pulse-offset", "2"]
        )
        self.assertEqual(code, 0)


class TestGuessCommand(unittest.TestCase):
    def test_match(self):
        code = main(
            ["guess", "--spo2", "98", "--pulse", "72", "01:62:48"]
        )
        self.assertEqual(code, 0)

    def test_no_match(self):
        code = main(
            ["guess", "--spo2", "55", "--pulse", "44", "01:62:48"]
        )
        self.assertEqual(code, 1)


class TestServiceLabel(unittest.TestCase):
    def test_known_service(self):
        self.assertIn(
            "Body Composition",
            service_label("0000181b-0000-1000-8000-00805f9b34fb"),
        )

    def test_plx_service(self):
        self.assertIn(
            "Pulse Oximeter",
            service_label("00001822-0000-1000-8000-00805f9b34fb"),
        )

    def test_unknown_service_unchanged(self):
        uuid = "0000ffff-0000-1000-8000-00805f9b34fb"
        self.assertEqual(service_label(uuid), uuid)


class TestFormatServices(unittest.TestCase):
    def test_annotation_in_scan_line(self):
        line = format_services(["0000181b-0000-1000-8000-00805f9b34fb"])
        self.assertIn("[Body Composition (smart scale)]", line)


if __name__ == "__main__":
    unittest.main()
