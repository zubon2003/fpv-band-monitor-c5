"""Sweep plumbing: frequency grid, frames and the reader thread.

The ESP32-C5 source (fpv_link.fw_source; esp_sdr.esp_source for --sim) yields
one SpanLine per finished sweep. SweepAccumulator paints it onto the fixed
display grid and SweepReader turns it into a Frame on a queue for the GUI/CLI.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator

import numpy as np

HZ = 1e6  # MHz -> Hz


@dataclass
class Frame:
    """One completed sweep over the configured span."""

    t: float             # time.monotonic() when the sweep finished
    powers: np.ndarray   # dBFS per grid bin; NaN where nothing was seen yet
    sweep_index: int


@dataclass
class SpanLine:
    """A whole stitched sweep: bin i is centred at hz_low + (i + 0.5) * bin_width."""

    hz_low: float
    bin_width: float
    powers: np.ndarray


@dataclass
class DeviceInfo:
    """The receiver opened a device: which port, and what it answered."""

    port: str
    identity: str


# --------------------------------------------------------------------------- #
# Frequency grid
# --------------------------------------------------------------------------- #
class FrequencyGrid:
    """Fixed bin grid; bin k is centred at start_hz + (k + 0.5) * bin_hz."""

    def __init__(self, start_mhz: float, stop_mhz: float, bin_hz: float):
        self.start_hz = start_mhz * HZ
        self.bin_hz = float(bin_hz)
        self.n = max(1, int(round((stop_mhz - start_mhz) * HZ / self.bin_hz)))
        self.freqs_mhz = (
            self.start_hz + (np.arange(self.n) + 0.5) * self.bin_hz
        ) / HZ

    @property
    def start_mhz(self) -> float:
        return self.start_hz / HZ

    @property
    def stop_mhz(self) -> float:
        return (self.start_hz + self.n * self.bin_hz) / HZ

    def index_of(self, freq_mhz: float) -> int:
        return int(round((freq_mhz * HZ - self.start_hz) / self.bin_hz - 0.5))

    def window(self, centre_mhz: float, halfwidth_mhz: float) -> tuple[int, int]:
        lo = max(0, self.index_of(centre_mhz - halfwidth_mhz))
        hi = min(self.n, self.index_of(centre_mhz + halfwidth_mhz) + 1)
        return lo, hi


class SweepAccumulator:
    """Paint spectra into a persistent grid; flush() emits it as a Frame."""

    def __init__(self, grid: FrequencyGrid):
        self.grid = grid
        self.powers = np.full(grid.n, np.nan, dtype=np.float32)
        self._sweeps = 0

    def flush(self) -> Frame:
        """Emit what is on the grid now as a finished sweep."""
        self._sweeps += 1
        return Frame(time.monotonic(), self.powers.copy(), self._sweeps)

    def paint(self, hz_low: float, bin_width: float, powers: np.ndarray) -> None:
        # Map by walking the *grid* bins this spectrum covers and taking the
        # nearest source bin. Mapping the other way leaves holes whenever the
        # grid is finer than the source, and a hole next to a carrier truncates
        # every contiguous-run measurement.
        start, gbin = self.grid.start_hz, self.grid.bin_hz
        hz_high = hz_low + powers.size * bin_width
        k0 = int(np.ceil((hz_low - start) / gbin - 0.5))
        k1 = int(np.floor((hz_high - start) / gbin - 0.5))
        k0, k1 = max(k0, 0), min(k1, self.grid.n - 1)
        if k1 >= k0:
            ks = np.arange(k0, k1 + 1)
            centres = start + (ks + 0.5) * gbin
            src = np.round((centres - hz_low) / bin_width - 0.5).astype(np.int64)
            np.clip(src, 0, powers.size - 1, out=src)
            self.powers[ks] = powers[src]


@dataclass
class SweepSettings:
    start_mhz: float
    stop_mhz: float
    bin_hz: int = 100_000
    gain: int = 40              # ESP32 gain-table index; -1 = hardware AGC
    port: str | None = None     # COM port; None = first Espressif USB device


class SweepReader:
    """Runs a sweep source in a thread and pushes Frames onto a queue."""

    def __init__(self, settings: SweepSettings, grid: FrequencyGrid,
                 source: Callable[[SweepSettings, threading.Event], Iterator[SpanLine]],
                 out: "queue.Queue"):
        self.settings = settings
        self.grid = grid
        self._source = source
        self.out = out
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        acc = SweepAccumulator(self.grid)
        try:
            for line in self._source(self.settings, self._stop):
                if self._stop.is_set():
                    break
                if isinstance(line, DeviceInfo):
                    self.out.put(line)  # connection report for the GUI
                    continue
                acc.paint(line.hz_low, line.bin_width, line.powers)
                self.out.put(acc.flush())
        except Exception as e:  # surfaced by the GUI / CLI
            self.out.put(e)
