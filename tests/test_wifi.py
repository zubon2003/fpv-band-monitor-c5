import unittest

from fpv_band_monitor import wifi as wf
from fpv_band_monitor.channels import GROUPS, group_of, parse_channels


def freqs(group):
    return [c.freq_mhz for c in parse_channels(GROUPS[group])]


class TestGroups(unittest.TestCase):
    def test_group_of(self):
        self.assertEqual(group_of(parse_channels("F5,E2,F3,E1")), "4CH")
        self.assertEqual(group_of(parse_channels("E1,A8,A5,F5")), "4CH2")
        self.assertIsNone(group_of(parse_channels("R1,R8")))


class TestRelations(unittest.TestCase):
    def test_lists_match_the_lcd_firmware(self):
        # the channels the LCD's Wi-Fi page listens to (main/fpv/monitor.cpp scan_prepare)
        want = {
            "4CH": [108, 112, 116, 120, 124, 128, 132, 136, 140, 144, 149, 153, 157, 161, 165, 169, 173, 177],
            "4CH2": [116, 120, 124, 128, 132, 136, 140, 144, 149, 153, 157, 161, 165, 169, 173],
            "4CH3": [116, 120, 124, 128, 132, 136, 140, 144, 149, 153, 157, 161, 165, 169, 173, 177],
            "3CH": [120, 124, 132, 136, 140, 144, 149, 153, 157, 161, 169, 173, 177],
        }
        for g, chs in want.items():
            self.assertEqual([r.ch for r in wf.channels_to_check(freqs(g), 8.5)], chs, g)

    def test_direct_and_imd(self):
        f = freqs("4CH")                      # E2 E1 F3 F5
        r = wf.relation(f, 157, 8.5)          # 5785: on F3
        self.assertEqual(r.direct, {2})
        r = wf.relation(f, 108, 8.5)          # 5540: 2 x E2 - 5540 = 5830 -> F5
        self.assertEqual(r.direct, set())
        self.assertEqual(r.imd, {3: (0, "2V-W")})


class TestVerdicts(unittest.TestCase):
    def test_strongest_direct_wins(self):
        names = ["E2", "E1", "F3", "F5"]
        checks = wf.channels_to_check(freqs("4CH"), 8.5)
        heard = {r.ch: wf.Heard(r.ch, 0, -127, []) for r in checks}
        heard[108] = wf.Heard(108, 20, -55, [])
        heard[157] = wf.Heard(157, 5, -80, [])
        aps = [wf.Ap("aa", 157, 5775, 5795, -80, "Venue")]
        v = wf.verdicts(names, checks, heard, aps)
        self.assertEqual([x.kind for x in v[:2]], ["OK", "OK"])
        self.assertEqual((v[2].kind, v[2].ch, v[2].ssid), ("Direct", 157, "Venue"))
        self.assertEqual((v[3].kind, v[3].ch, v[3].how), ("IMD", 108, "2xE2-WiFi"))

    def test_not_done_yet(self):
        checks = wf.channels_to_check(freqs("3CH"), 8.5)
        v = wf.verdicts(["E1", "F1", "F4"], checks, {}, [])
        self.assertEqual({x.kind for x in v}, {"..."})


class TestParse(unittest.TestCase):
    def test_listen_lines(self):
        h = wf.parse_listen(
            "WIFI ch=100 ms=1200 frames=68 mgmt=55 ctrl=0 data=13 bytes=21190 rssi_max=-50 aps=2",
            ["AP 28:de:a8:cd:7c:cb ch=100 mhz=5490-5650 rssi=-75 beacons=10 ssid=Chat Noir 58"])
        self.assertEqual((h.ch, h.frames, h.rssi_max), (100, 68, -50))
        a = h.aps[0]
        self.assertEqual((a.ch, a.lo_mhz, a.hi_mhz, a.rssi, a.ssid), (100, 5490, 5650, -75, "Chat Noir 58"))
        self.assertIs(wf.ap_on(h.aps, 112), a)
        self.assertIsNone(wf.ap_on(h.aps, 149))

    def test_strength(self):
        self.assertEqual(wf.strength(-40, 0.25), 1.0)
        self.assertEqual(wf.strength(-90, 0.25), 0.25)
        self.assertAlmostEqual(wf.strength(-67.5, 0.0), 0.5)


if __name__ == "__main__":
    unittest.main()
