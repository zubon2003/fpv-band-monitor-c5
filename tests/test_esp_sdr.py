"""ESP32-C5 backend: wire format, hop plan and artefact-rejecting stitch."""

from __future__ import annotations

import os
import sys
import threading
import unittest
import zlib

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fpv_band_monitor.esp_sdr import (EspSettings, FakeEspSdr, Stitcher,
                                      esp_source, hop_plan_mhz, unpack_iq)
from fpv_band_monitor.sweep import DeviceInfo, SpanLine, SweepSettings


def reference_unpack(raw: bytes, n: int, bits: int):
    """Line-for-line port of esp-web-sdr radio.js capture decoding."""
    out = []
    acc = nbits = k = 0
    for j in range(n):
        if bits == 8:
            i, q = raw[2 * j], raw[2 * j + 1]
            i = i - 256 if i >= 128 else i
            q = q - 256 if q >= 128 else q
            i *= 4
            q *= 4
        else:
            while nbits < 20:
                acc |= raw[k] << nbits
                k += 1
                nbits += 8
            w = acc & 0xFFFFF
            acc >>= 20
            nbits -= 20
            i, q = w & 1023, w >> 10
            i = i - 1024 if i >= 512 else i
            q = q - 1024 if q >= 512 else q
        out.append(complex(i / 512, -q / 512))
    return np.array(out)


def pack_iq10(i: np.ndarray, q: np.ndarray) -> bytes:
    """Firmware pack_iq(): 20-bit words (I low, Q high), two per 5 bytes."""
    words = [(int(a) & 1023) | ((int(b) & 1023) << 10) for a, b in zip(i, q)]
    out = bytearray()
    for j in range(0, len(words), 2):
        a = words[j]
        b = words[j + 1] if j + 1 < len(words) else 0
        out += bytes([a & 255, (a >> 8) & 255, ((a >> 16) | (b << 4)) & 255])
        if j + 1 < len(words):
            out += bytes([(b >> 4) & 255, (b >> 12) & 255])
    return bytes(out)


class WireFormat(unittest.TestCase):
    def test_iq10_matches_reference_client(self):
        rng = np.random.default_rng(3)
        for n in (256, 257):  # even, and an odd tail of three bytes
            i = rng.integers(-512, 512, n)
            q = rng.integers(-512, 512, n)
            raw = pack_iq10(i, q)
            self.assertEqual(len(raw), (n * 20 + 7) // 8)
            np.testing.assert_allclose(unpack_iq(raw, n, 10),
                                       reference_unpack(raw, n, 10), atol=1e-7)

    def test_iq8_matches_reference_client(self):
        raw = np.random.default_rng(4).integers(0, 256, 600).astype(np.uint8)
        np.testing.assert_allclose(unpack_iq(raw.tobytes(), 300, 8),
                                   reference_unpack(raw.tobytes(), 300, 8),
                                   atol=1e-7)

    def test_crc_is_plain_crc32(self):
        # radio.js: reflected 0xEDB88320, init/xorout 0xFFFFFFFF == zlib.
        self.assertEqual(zlib.crc32(b"123456789"), 0xCBF43926)


class HopPlan(unittest.TestCase):
    def test_every_bin_seen_by_two_hops(self):
        es = EspSettings(step_mhz=15, usable_mhz=16.0, dc_khz=500.0)
        st = Stitcher(5670.0, 5835.0, es, 100_000)
        count = np.zeros(st.n, dtype=int)
        for ks, _src, _d in st.maps:
            count[ks] += 1
        self.assertGreaterEqual(count.min(), 2)

    def test_los_cover_span(self):
        los = hop_plan_mhz(5670.0, 5835.0, 15)
        self.assertLess(los[0], 5670)
        self.assertGreater(los[-1], 5835)
        self.assertEqual(len(los), 13)


def one_sweep(fake, es, start=5670.0, stop=5835.0) -> tuple[np.ndarray, Stitcher]:
    s = SweepSettings(start_mhz=start, stop_mhz=stop, bin_hz=100_000,
                      gain=40)
    gen = esp_source(es, lambda: fake)(s, threading.Event())
    info = next(gen)
    assert isinstance(info, DeviceInfo) and info.port == "SIM"
    line = next(gen)
    gen.close()
    assert isinstance(line, SpanLine)
    return line.powers, Stitcher(start, stop, es, 100_000)


class Stitch(unittest.TestCase):
    def test_dc_spikes_are_removed(self):
        # Big DC offset, no carriers: the stitched floor must stay flat.
        fake = FakeEspSdr([], seed=5, dc=0.2 + 0.1j)
        p, st = one_sweep(fake, EspSettings(samples=16380))
        floor = np.median(p)
        self.assertLess(p.max() - floor, 6.0)

    def test_iq_image_rejected_by_min_but_not_by_near(self):
        # Strong carrier 6 MHz above a LO; its image lands 6 MHz below it.
        lo = 5753  # one of the hop LOs for 5670-5835 at 15 MHz steps
        self.assertIn(lo, hop_plan_mhz(5670.0, 5835.0, 15))
        fake = FakeEspSdr([lo + 6.0], seed=6, amp_dbfs=-12.0, image_db=-20.0)
        fake.carriers[0]["drift"] = 0.0
        fake.carriers[0]["amp"] = 10 ** (-12 / 20)
        freqs = None
        res = {}
        for mode in ("min", "near"):
            es = EspSettings(samples=16380, combine=mode)
            p, st = one_sweep(fake, es)
            freqs = (st.hz_low + (np.arange(st.n) + 0.5) * st.bin_hz) / 1e6
            floor = np.median(p)
            ghost = (freqs > lo - 8.5) & (freqs < lo - 3.5)
            res[mode] = p[ghost].max() - floor
            real = (freqs > lo + 3.5) & (freqs < lo + 8.5)
            self.assertGreater(p[real].max() - floor, 30.0, mode)
        self.assertLess(res["min"], 10.0)
        self.assertGreater(res["near"], res["min"] + 10.0)


if __name__ == "__main__":
    unittest.main()
