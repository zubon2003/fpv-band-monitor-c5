"""LCD-firmware receiver (fpv_link): frame decoding and the command/stream
exchange, against a fake firmware on a pseudo-terminal."""

from __future__ import annotations

import os
import sys
import threading
import time
import tty
import unittest
import zlib

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fpv_band_monitor import fpv_link
from fpv_band_monitor.esp_sdr import EspSettings
from fpv_band_monitor.sweep import DeviceInfo, SweepSettings


def encode(db: np.ndarray) -> bytes:
    """Same rounding as the firmware's fpv_code()."""
    codes = np.where(np.isfinite(db), np.clip(np.floor((db + 200.0) * 100.0 + 0.5), 0, 65534), 0xFFFF)
    return codes.astype("<u2").tobytes()


class Fake(threading.Thread):
    def __init__(self, fd, db, start=5670):
        super().__init__(daemon=True)
        self.fd, self.payload, self.start_mhz, self.n = fd, encode(db), start, db.size
        self.lines, self.streaming, self.halt = [], False, threading.Event()

    def run(self):
        buf, seq, nxt = b"", 0, 0.0
        os.set_blocking(self.fd, False)
        while not self.halt.is_set():
            try:
                buf += os.read(self.fd, 4096)
            except BlockingIOError:
                pass
            except OSError:
                return
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                t = raw.decode().strip()
                if not t:
                    continue
                self.lines.append(t)
                if t == "FPV?":
                    os.write(self.fd, b"FPV 1 bin_hz=78125 maxbins=2304\n")
                elif t.startswith("FPV STREAM"):
                    self.streaming = t.endswith("ON")
                    os.write(self.fd, b"OK\n")
                elif t.startswith("FPV GAIN 99"):
                    os.write(self.fd, b"ERR GAIN 0..60|AGC\n")
                else:
                    os.write(self.fd, b"OK\n")
            if self.streaming and time.monotonic() > nxt:
                seq += 1
                hdr = f"SWEEP {seq} {self.start_mhz} {self.n} 78125 0 {zlib.crc32(self.payload):08x}\n"
                os.write(self.fd, hdr.encode() + self.payload)
                nxt = time.monotonic() + 0.05
            time.sleep(0.002)


def pty():
    m, s = os.openpty()
    tty.setraw(s)
    return m, s, os.ttyname(s)


class TestFpvLink(unittest.TestCase):
    def test_decode_and_tokens(self):
        db = np.array([-80.004, -20.0, np.nan, -200.0], np.float32)
        out = fpv_link.decode_codes(encode(db))
        self.assertTrue(np.isnan(out[2]))
        np.testing.assert_allclose(out[[0, 1, 3]], [-80.0, -20.0, -200.0], atol=0.0051)
        self.assertEqual(fpv_link.channel_token("f3", 5780), "F3")
        self.assertEqual(fpv_link.channel_token("TX@5795.5", 5795.5), "5796")

    def test_stream(self):
        rng = np.random.default_rng(1)
        db = rng.uniform(-90, -20, 2112).astype(np.float32)
        db[:3] = np.nan
        m, s, path = pty()
        dev = Fake(m, db)
        dev.start()
        es = EspSettings(backend="fft", nolcd=True, channels=(("E2", 5685), ("E1", 5705), ("F3", 5780), ("F5", 5820), ("F1", 5740)))
        st = SweepSettings(start_mhz=5670.0, stop_mhz=5835.0, bin_hz=78125, gain=40, port=path)
        stop = threading.Event()
        lines = []
        for item in fpv_link.fw_source(es)(st, stop):
            if isinstance(item, DeviceInfo):
                continue
            lines.append(item)
            if len(lines) == 3:
                stop.set()
                break
        dev.halt.set()
        os.close(s)
        self.assertIn("FPV SPAN 5670.000 5835.000 E2,E1,F3,F5", dev.lines)   # 4 channels at most
        self.assertIn("FPV GAIN 40", dev.lines)
        self.assertIn("FPV STREAM ON", dev.lines)
        on = dev.lines.index("FPV STREAM ON")
        self.assertIn("FPV NOLCD", dev.lines[on:])                          # LCD off after STREAM ON
        self.assertEqual(lines[0].hz_low, 5670e6)
        self.assertEqual(lines[0].bin_width, 78125)
        np.testing.assert_allclose(lines[0].powers[3:], db[3:], atol=0.0051)
        self.assertTrue(np.all(np.isnan(lines[0].powers[:3])))

    def test_error_reply(self):
        m, s, path = pty()
        dev = Fake(m, np.zeros(10, np.float32))
        dev.start()
        link = fpv_link.FpvLink(path, timeout_s=0.5)
        with self.assertRaises(fpv_link.FpvLinkError):
            link.ask("FPV GAIN 99")
        link.close()
        dev.halt.set()
        os.close(s)


class TestReceiverChoice(unittest.TestCase):
    def args(self, *argv):
        from fpv_band_monitor.cli import build_parser
        return build_parser().parse_args(["--channels", "E2,E1,F3,F5", *argv])

    def test_auto_fft_iq(self):
        from fpv_band_monitor.cli import ReceiverControl, _span_planner
        a = self.args()
        rc = ReceiverControl(a)
        self.assertEqual(rc.choice, "auto")
        self.assertEqual(rc.resolve(detect=lambda port: True), "fft")
        self.assertEqual((a.bin_khz, a.step), (78.125, 25))
        grid, _ = _span_planner(a)(5670.0, 5835.0)
        self.assertEqual((grid.n, grid.bin_hz), (fpv_link.expected_bins(5670, 5835), 78125))
        self.assertEqual(rc.resolve(detect=lambda port: False), "iq")     # no FPV? answer
        self.assertEqual(a.bin_khz, 100.0)
        rc.choice = "fft"
        self.assertEqual(rc.resolve(detect=lambda port: False), "fft")    # forced
        rc.choice = "iq"
        self.assertEqual(rc.resolve(detect=lambda port: True), "iq")
        self.assertEqual(ReceiverControl(self.args("--receiver", "fw")).choice, "fft")

    def test_nolcd_reaches_settings(self):
        from fpv_band_monitor.cli import ReceiverControl, _esp_settings
        a = self.args("--nolcd")
        rc = ReceiverControl(a)
        rc.resolve(detect=lambda port: True)
        es = _esp_settings(a)
        self.assertEqual((es.backend, es.nolcd), ("fft", True))
        rc.nolcd = False
        self.assertFalse(_esp_settings(a).nolcd)
        self.assertEqual(rc.label(), "fft")


class TestGuiReceiverButtons(unittest.TestCase):
    def test_switch_regrids(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from fpv_band_monitor import gui
        except Exception as e:                      # Qt / pyqtgraph missing
            self.skipTest(str(e))
        from fpv_band_monitor import cli
        rng = np.random.default_rng(2)
        m, s, path = pty()
        dev = Fake(m, rng.uniform(-90, -20, fpv_link.expected_bins(5670, 5835)).astype(np.float32))
        dev.start()
        a = cli.build_parser().parse_args(["--channels", "E2,E1,F3,F5", "--port", path])
        rc = cli.ReceiverControl(a)
        rc.resolve()                                 # auto: the fake answers FPV? -> fft
        self.assertEqual(rc.backend, "fft")
        channels, settings, grid, los = cli._grid_and_settings(a)
        app = gui.QtWidgets.QApplication.instance() or gui.QtWidgets.QApplication([])
        opts = gui.ViewOptions(span_planner=rc.span_planner(), receiver=rc, hop_los_mhz=los)
        win = gui.BandMonitorWindow(channels, settings, grid, rc.source_factory(channels), opts)
        try:
            self.assertEqual(win.grid.bin_hz, 78125)
            self.assertEqual(win.rx_label.text(), "-> fft")
            self.assertTrue(win.chk_nolcd.isEnabled())
            win.rx_buttons["iq"].click()             # iq: PC FFT, 100 kHz grid
            self.assertEqual((rc.backend, win.grid.bin_hz), ("iq", 100000))
            self.assertFalse(win.chk_nolcd.isEnabled())
            win.rx_buttons["auto"].click()           # back to the firmware's sweeps
            self.assertEqual((rc.backend, win.grid.bin_hz), ("fft", 78125))
            self.assertEqual(win.wf.shape[1], win.grid.n)
            win.chk_nolcd.setChecked(True)
            t0 = time.monotonic()
            while "FPV NOLCD" not in dev.lines and time.monotonic() - t0 < 5:
                app.processEvents()
                time.sleep(0.02)
            self.assertIn("FPV NOLCD", dev.lines)
        finally:
            win.close()
            dev.halt.set()
            os.close(s)


if __name__ == "__main__":
    unittest.main()
