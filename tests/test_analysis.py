"""No hardware needed: synthetic spectra with known centres and widths."""

from __future__ import annotations

import math
import os
import sys
import threading
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fpv_band_monitor.analysis import (SpectrumAverager, auto_search_halfwidth,
                                       measure_all, measure_channel)
from fpv_band_monitor.channels import Channel, parse_channels
from fpv_band_monitor.sweep import FrequencyGrid, SpanLine, SweepAccumulator

CHANNELS = parse_channels("E2,E1,F3,F5")
GRID = FrequencyGrid(5670.0, 5836.0, 100_000)


def synth(centres_mhz, amp_dbm=-40.0, floor_dbm=-95.0, hw_3db=4.5):
    """Super-Gaussian carriers: -3 dB at +/-hw_3db MHz."""
    lin = np.full(GRID.n, 10.0 ** (floor_dbm / 10.0))
    for fc in centres_mhz:
        d = np.clip((GRID.freqs_mhz - fc) / hw_3db, -3.5, 3.5)
        lin += 10.0 ** (amp_dbm / 10.0) * np.exp(-math.log(2.0) * d ** 4)
    return (10.0 * np.log10(lin)).astype(np.float32)


class TestMeasure(unittest.TestCase):
    def test_centre_within_50_khz(self):
        offsets = [0.0, +0.37, -0.62, +1.25]
        centres = [c.freq_mhz + o for c, o in zip(CHANNELS, offsets)]
        ms = measure_all(synth(centres), GRID, CHANNELS)
        self.assertTrue(all(m.present for m in ms))
        for m, want in zip(ms, centres):
            self.assertAlmostEqual(m.centre_mhz, want, delta=0.05,
                                   msg=f"{m.name}: {m.centre_mhz} vs {want}")

    def test_bandwidth_matches_the_9_and_15_mhz_marks(self):
        ms = measure_all(synth([c.freq_mhz for c in CHANNELS]), GRID, CHANNELS)
        for m in ms:
            self.assertAlmostEqual(m.bw_db3_mhz, 9.0, delta=0.4)
            self.assertAlmostEqual(m.bw_db20_mhz, 14.7, delta=0.7)

    def test_absent_channel_reports_no_signal(self):
        powers = synth([CHANNELS[0].freq_mhz, CHANNELS[2].freq_mhz])
        ms = {m.name: m for m in measure_all(powers, GRID, CHANNELS)}
        self.assertTrue(ms["E2"].present)
        self.assertTrue(ms["F3"].present)
        self.assertFalse(ms["E1"].present)
        self.assertFalse(ms["F5"].present)
        self.assertIsNone(ms["E1"].centre_mhz)

    def test_narrow_carrier_is_still_detected(self):
        """A VTX on a static picture is only a few bins wide at -3 dB.

        Regression: a 1 MHz minimum -3 dB width used to reject exactly this,
        so a transmitting VTX showed up as "no signal".
        """
        fc = CHANNELS[1].freq_mhz + 0.4
        lin = np.full(GRID.n, 10.0 ** (-95.0 / 10.0))
        sigma = 0.13  # MHz -> about 0.3 MHz wide at -3 dB
        lin += 10.0 ** (-60.0 / 10.0) * np.exp(
            -0.5 * ((GRID.freqs_mhz - fc) / sigma) ** 2)
        powers = (10.0 * np.log10(lin)).astype(np.float32)
        m = {x.name: x for x in measure_all(powers, GRID, CHANNELS)}[CHANNELS[1].name]
        self.assertTrue(m.present, m.reason)
        self.assertLess(m.bw_db3_mhz, 1.0)
        self.assertAlmostEqual(m.centre_mhz, fc, delta=0.05)

    def test_off_frequency_carrier_is_not_dragged_towards_nominal(self):
        """A VTX a few MHz off must read where it really is.

        One-pass centroid over the search window clips the far tail and reads
        450 kHz low at a 4 MHz offset; re-centring once fixes it.
        """
        for off in (0.0, 2.0, 4.0):
            with self.subTest(off=off):
                fc = CHANNELS[1].freq_mhz + off
                m = measure_channel(synth([fc]), GRID, CHANNELS[1])
                self.assertTrue(m.present, m.reason)
                self.assertAlmostEqual(m.centre_mhz, fc, delta=0.05)

    def test_measurement_is_independent_of_absolute_gain(self):
        """LNA/VGA shift every bin together, so nothing may change.

        Everything is referenced to the measured floor, and the centroid
        weights scale by a common factor that cancels in the ratio. What is
        left is float32 rounding: under 1 kHz, against ~94 kHz of measurement
        noise.
        """
        powers = synth([CHANNELS[1].freq_mhz + 0.3])
        base = measure_channel(powers, GRID, CHANNELS[1])
        for gain in (-20.0, -6.0, +6.0, +20.0):
            with self.subTest(gain=gain):
                m = measure_channel(powers + gain, GRID, CHANNELS[1])
                self.assertAlmostEqual(m.centre_mhz, base.centre_mhz,
                                       delta=0.002)
                self.assertAlmostEqual(m.snr_db, base.snr_db, delta=0.01)
                self.assertAlmostEqual(m.bw_db3_mhz, base.bw_db3_mhz,
                                       delta=0.002)

    def test_raised_noise_floor_barely_moves_the_centre(self):
        """A floor that climbs towards the signal costs SNR, not accuracy.

        Measured on a real capture: +12 dB of floor (SNR 31 -> 21 dB) moved the
        centre by 17 kHz.
        """
        fc = CHANNELS[1].freq_mhz + 0.3
        quiet = measure_channel(synth([fc], floor_dbm=-95.0), GRID, CHANNELS[1])
        noisy = measure_channel(synth([fc], floor_dbm=-83.0), GRID, CHANNELS[1])
        self.assertTrue(noisy.present, noisy.reason)
        self.assertLess(noisy.snr_db, quiet.snr_db - 10.0)
        self.assertAlmostEqual(noisy.centre_mhz, quiet.centre_mhz, delta=0.05)

    def test_default_snr_threshold_clears_measured_noise_peaks(self):
        """Real HackRF noise peaks reached floor + 13.7 dB over 300 sweeps."""
        import inspect

        from fpv_band_monitor.analysis import measure_channel
        default = inspect.signature(measure_channel).parameters["min_snr_db"].default
        self.assertGreaterEqual(default, 15.0)

    def test_noise_only_centre_is_not_invented(self):
        rng = np.random.default_rng(3)
        powers = (-95.0 + rng.normal(0.0, 1.5, GRID.n)).astype(np.float32)
        for m in measure_all(powers, GRID, CHANNELS):
            self.assertFalse(m.present)

    def test_search_window_never_reaches_a_neighbour(self):
        # E2 5685 and E1 5705 are 20 MHz apart -> half-width must stay <= 10.
        self.assertLessEqual(auto_search_halfwidth(CHANNELS, 20.0), 10.0)

    def test_strong_neighbour_does_not_pull_the_centre(self):
        chans = [Channel("E2", 5685.0), Channel("E1", 5705.0)]
        powers = synth([5685.0, 5705.0])
        powers[GRID.window(5705.0, 5.0)[0]:GRID.window(5705.0, 5.0)[1]] += 20.0
        hw = auto_search_halfwidth(chans, 8.0)
        ms = measure_all(powers, GRID, chans, search_mhz=hw)
        self.assertAlmostEqual(ms[0].centre_mhz, 5685.0, delta=0.1)


def synth_tilted(fc, rng, amp_dbm=-40.0, floor_dbm=-95.0):
    """One sweep of an FM video carrier: lopsided, as a snapshot really is."""
    lin = np.full(GRID.n, 10.0 ** (floor_dbm / 10.0))
    d = np.clip((GRID.freqs_mhz - fc) / 4.5, -3.5, 3.5)
    tilt = rng.normal(0.0, 0.6)
    lin += 10.0 ** (amp_dbm / 10.0) * np.exp(-math.log(2.0) * d ** 4 + tilt * d)
    return (10.0 * np.log10(lin)).astype(np.float32)


class TestAveraging(unittest.TestCase):
    def test_averaging_beats_a_single_sweep_on_a_live_carrier(self):
        """A single 20 ms sweep puts the centroid hundreds of kHz off.

        Regression for "5705 の中心がこんなにずれるのか": the carrier is steady,
        the snapshot is not, so measurements run on a time average.
        """
        rng = np.random.default_rng(11)
        fc = CHANNELS[1].freq_mhz + 0.1
        av = SpectrumAverager(1.0)
        inst_err, avg_err = [], []
        for k in range(90):
            t = k * 0.022                       # about 45 sweeps/s
            powers = synth_tilted(fc, rng)
            averaged = av.add(t, powers)
            mi = measure_channel(powers, GRID, CHANNELS[1])
            ma = measure_channel(averaged, GRID, CHANNELS[1])
            if k >= 45:                          # once the window is full
                inst_err.append(abs(mi.centre_mhz - fc))
                avg_err.append(abs(ma.centre_mhz - fc))
        # Averaging N sweeps only buys sqrt(N): 45 sweeps turn a ~0.5 MHz
        # per-sweep error into ~0.1 MHz, which is why --avg-ms is adjustable.
        self.assertGreater(np.mean(inst_err), 0.15, "test signal is not jittery")
        self.assertLess(np.mean(avg_err), 0.2)
        self.assertLess(np.mean(avg_err), np.mean(inst_err) / 3.0)

    def test_peak_hold_mode_keeps_a_single_burst(self):
        """Averaging buries a 1-in-50 burst; hold keeps it.

        Measured on real Wi-Fi: 1 s averaging detected the channel in 18% of
        frames, 1 s peak hold in 95%.
        """
        quiet = np.full(GRID.n, -95.0, dtype=np.float32)
        # 23 dB over the floor: averaging one burst into 50 sweeps costs
        # 10*log10(50) = 17 dB and drops it under the detection threshold.
        burst = synth([CHANNELS[1].freq_mhz], amp_dbm=-72.0)
        avg = SpectrumAverager(1.0, mode="mean")
        hold = SpectrumAverager(1.0, mode="max")
        for k in range(50):
            frame = burst if k == 25 else quiet
            t = k * 0.02
            a, h = avg.add(t, frame), hold.add(t, frame)
        self.assertFalse(measure_channel(a, GRID, CHANNELS[1]).present)
        m = measure_channel(h, GRID, CHANNELS[1])
        self.assertTrue(m.present, m.reason)
        self.assertAlmostEqual(m.centre_mhz, CHANNELS[1].freq_mhz, delta=0.05)

    def test_zero_window_passes_the_sweep_through(self):
        av = SpectrumAverager(0.0)
        p = np.linspace(-90, -40, GRID.n).astype(np.float32)
        self.assertIs(av.add(0.0, p), p)


class TestDeviationDisplay(unittest.TestCase):
    """Colour bands for the deviation readout (no Qt needed for the logic)."""

    def setUp(self):
        from fpv_band_monitor.gui import (DEV_BAD_COLOR, DEV_IDLE_COLOR,
                                          DEV_OK_COLOR, DEV_WARN_COLOR,
                                          deviation_color, format_deviation)
        self.colour = lambda v: deviation_color(v, 0.5, 1.0)
        self.fmt = format_deviation
        self.ok, self.warn = DEV_OK_COLOR, DEV_WARN_COLOR
        self.bad, self.idle = DEV_BAD_COLOR, DEV_IDLE_COLOR

    def test_colour_bands(self):
        # blue up to 0.5 MHz inclusive, yellow between, red from 1.0 MHz on.
        for v in (0.0, 0.2, -0.49, 0.5, -0.5):
            self.assertEqual(self.colour(v), self.ok, v)
        for v in (0.5001, -0.7, 0.99):
            self.assertEqual(self.colour(v), self.warn, v)
        for v in (1.0, -1.0, 2.3, 7.0):
            self.assertEqual(self.colour(v), self.bad, v)
        self.assertEqual(self.colour(None), self.idle)

    def test_always_mhz(self):
        self.assertEqual(self.fmt(0.05), "+0.05 MHz")
        self.assertEqual(self.fmt(-0.52), "-0.52 MHz")
        self.assertEqual(self.fmt(1.42), "+1.42 MHz")


class TestSweepPath(unittest.TestCase):
    def test_coarser_spectrum_does_not_leave_holes_in_a_finer_grid(self):
        """A hole next to a carrier truncates every contiguous-run width."""
        grid = FrequencyGrid(5670.0, 5690.0, 98_039.22)
        acc = SweepAccumulator(grid)
        acc.paint(5670e6, 100_000.0, np.full(200, -80.0, dtype=np.float32))
        inner = acc.powers[2:-2]
        self.assertFalse(np.any(np.isnan(inner)),
                         f"{int(np.sum(np.isnan(inner)))} holes in the grid")

    def test_flush_emits_numbered_frames(self):
        acc = SweepAccumulator(GRID)
        line = SpanLine(GRID.start_hz, GRID.bin_hz,
                        np.full(GRID.n, -70.0, dtype=np.float32))
        acc.paint(line.hz_low, line.bin_width, line.powers)
        f1 = acc.flush()
        f2 = acc.flush()
        self.assertEqual((f1.sweep_index, f2.sweep_index), (1, 2))
        self.assertTrue(np.allclose(f1.powers, -70.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
