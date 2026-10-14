"""FpvLink framing without a device: resync after joining mid-frame."""
import io
import time
import unittest
import zlib

import numpy as np

from fpv_band_monitor import fpv_link


class FakeSerial:
    def __init__(self, data: bytes, timeout: float = 0.3):
        self.buf = io.BytesIO(data)
        self.timeout = timeout

    def readline(self):
        return self.buf.readline()

    def read(self, n):
        return self.buf.read(n)


def frame(seq: int, codes: np.ndarray) -> bytes:
    payload = codes.astype("<u2").tobytes()
    head = f"SWEEP {seq} 5670 {codes.size} 78125 0 {zlib.crc32(payload):08x}\n".encode()
    return head + payload


def link_over(data: bytes) -> fpv_link.FpvLink:
    link = object.__new__(fpv_link.FpvLink)      # no port: just the framing
    link.ser = FakeSerial(data)
    link.port = "fake"
    link.timeout_s = 0.3
    link.bad_frames = 0
    link._pending = []
    return link


class TestFraming(unittest.TestCase):
    def test_resync_after_a_payload_newline(self):
        # payload bytes containing 0x0A, and the stream joined mid-frame
        # one newline inside frame 1's payload: the line after it runs into
        # frame 2's header
        codes = np.full(64, 0x4141, dtype=np.uint16)
        codes[32] = 0x000A
        f1 = frame(1, codes)
        whole = f1 + frame(2, np.arange(64, dtype=np.uint16) + 20000)
        link = link_over(whole[len(f1) - 100:])  # starts inside frame 1's payload
        got = link.next_sweep()
        self.assertIsNotNone(got)
        self.assertEqual(got[0], 2)
        self.assertGreaterEqual(link.bad_frames, 1)

    def test_next_sweep_returns_while_junk_keeps_coming(self):
        link = link_over(b"junk line\n" * 100000)
        t0 = time.monotonic()
        self.assertIsNone(link.next_sweep())
        self.assertLess(time.monotonic() - t0, 3.0)


if __name__ == "__main__":
    unittest.main()
