"""Hop plan, stitching and the simulated ESP32-C5 (no real-device I/Q here).

The monitor now reads finished sweeps from the LCD firmware (fpv_link.py),
which hops, FFTs and stitches on the chip exactly as below. This module keeps
that reference: the hop plan and display grid (Stitcher), the Welch PSD and
min-combine stitch, and FakeEspSdr for --sim, which produces synthetic I/Q
and runs it through the same steps on the PC.

Stitching. Each hop's spectrum is trustworthy only near its centre, and every
hop carries receiver artefacts at fixed offsets from its own LO - the DC spike
and the I/Q-imbalance image (a strong carrier at LO+x leaves a ghost at LO-x).
Hops are therefore spaced so that every grid bin is seen by two hops, and the
default combine takes the *lower* of the two readings: a real carrier is at the
same absolute frequency in both, an artefact only in one, so the minimum keeps
the carrier and drops the ghost.
"""

from __future__ import annotations

import math
import multiprocessing as mp
import queue
import threading
import time
from dataclasses import dataclass
from typing import Iterator

import numpy as np

from .sweep import HZ, DeviceInfo, SpanLine, SweepSettings

ESPRESSIF_VID = 0x303A
# Rate index of CAP16/CAP20 -> nominal complex sample rate.
RATES_HZ = {0: 80e6, 1: 40e6, 2: 20e6, 3: 10e6, 4: 8e6, 5: 4e6}
MAX_SAMPLES = 16380


class EspSdrError(RuntimeError):
    pass


@dataclass
class EspSettings:
    port: str | None = None     # None = first Espressif USB device found
    gain: int = 40              # manual gain-table index; -1 = hardware AGC
    rate_index: int = 0         # 0 = 80 MS/s
    bits: int = 10              # 8 (CAP16) or 10 (CAP20)
    samples: int = 8192         # per hop, max 16380
    bandwidth_mhz: int = 0      # analog filter, 0 = widest (about 48 MHz)
    step_mhz: int = 15          # LO spacing between hops
    usable_mhz: float = 16.0    # half-width of each hop that is kept
    dc_khz: float = 500.0       # half-width around each LO that is dropped
    combine: str = "min"        # "min" (rejects artefacts) or "near"
    flip: bool = False          # mirror the spectrum (if carriers look swapped)
    channels: tuple = ()        # (name, MHz) pairs, sent to the LCD with the span

    @property
    def rate_hz(self) -> float:
        return RATES_HZ[self.rate_index]


def esp_fft_size(bin_hz: float, rate_hz: float) -> int:
    return max(16, int(round(rate_hz / float(bin_hz))))


def esp_bin_width(bin_hz: float, rate_hz: float = 80e6) -> float:
    """The bin width actually used for a requested one (whole-sample FFT)."""
    return rate_hz / esp_fft_size(bin_hz, rate_hz)


def hop_plan_mhz(start_mhz: float, stop_mhz: float, step_mhz: int) -> list[int]:
    """Whole-MHz LO positions. Consecutive LOs bracket every frequency in the
    span, so with usable >= step each bin is seen by two hops. The outer LOs
    sit half a step outside the span so their own DC holes are not in it."""
    los = [int(math.floor(start_mhz)) - int(step_mhz) // 2]
    while los[-1] < stop_mhz + step_mhz / 2:
        los.append(los[-1] + int(step_mhz))
    return los


# --------------------------------------------------------------------------- #
# Serial client
# --------------------------------------------------------------------------- #
def find_port() -> str | None:
    """First Espressif native-USB serial port (VID 0x303A)."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return None
    for p in list_ports.comports():
        if p.vid == ESPRESSIF_VID:
            return p.device
    return None


def list_port_lines() -> list[str]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return ["(pyserial is not installed)"]
    out = []
    for p in list_ports.comports():
        vid = f"{p.vid:04X}:{p.pid:04X}" if p.vid is not None else "----:----"
        out.append(f"{p.device:8s} {vid}  {p.description}")
    return out or ["(no serial ports)"]


def unpack_iq(raw: bytes, n: int, bits: int) -> np.ndarray:
    """Wire payload -> complex64 in +-1.0 full scale.

    Mirrors the reference client (esp-web-sdr radio.js): IQ8 is two signed
    bytes per sample, IQ10 packs two 20-bit words into five bytes, low field
    first; the second field is negated to get the spectrum the right way up.
    """
    b = np.frombuffer(raw, dtype=np.uint8)
    if bits == 8:
        v = b[:2 * n].view(np.int8).astype(np.float32) * (4.0 / 512.0)
        i, q = v[0::2], v[1::2]
    else:
        m = (n + 1) // 2
        pad = np.zeros(m * 5, dtype=np.uint8)
        pad[:b.size] = b[:m * 5]
        g = pad.reshape(m, 5).astype(np.uint64)
        pair = (g[:, 0] | (g[:, 1] << 8) | (g[:, 2] << 16) | (g[:, 3] << 24)
                | (g[:, 4] << 32))
        words = np.empty(m * 2, dtype=np.int64)
        words[0::2] = (pair & 0xFFFFF).astype(np.int64)
        words[1::2] = ((pair >> 20) & 0xFFFFF).astype(np.int64)
        words = words[:n]
        i = (words & 1023).astype(np.int32)
        q = (words >> 10).astype(np.int32)
        i = np.where(i >= 512, i - 1024, i).astype(np.float32) / 512.0
        q = np.where(q >= 512, q - 1024, q).astype(np.float32) / 512.0
    return (i - 1j * q).astype(np.complex64)


class FakeEspSdr:
    """Synthesises what a C5 would capture: FM-video carriers, thermal noise,
    a DC offset, an I/Q-imbalance image, the analog filter roll-off and 10-bit
    quantisation. Exercises the real hop/stitch path without hardware."""

    identity = "C5SDR 6 burst 16380 (simulated)"
    port = "SIM"

    def __init__(self, carriers_mhz: list[float], seed: int | None = None,
                 amp_dbfs: float = -25.0, noise_dbfs: float = -52.0,
                 dc: complex = 0.02 + 0.015j, image_db: float = -28.0,
                 lpf_mhz: float = 36.0):  # real C5: flat to +-30 MHz
        self.rng = np.random.default_rng(seed)
        self.carriers = [
            {"f": f, "drift": self.rng.uniform(0.15, 0.6),
             "rate": self.rng.uniform(0.05, 0.2),
             "phase": self.rng.uniform(0, 6.28),
             "amp": 10 ** ((amp_dbfs + self.rng.uniform(-10, 3)) / 20)}
            for f in carriers_mhz]
        self.noise = 10 ** (noise_dbfs / 20)
        self.dc = dc
        self.image = 10 ** (image_db / 20)
        self.lpf_mhz = lpf_mhz
        self.freq = 5800
        self.gain_max = 80
        self.dropped = 0
        self.t0 = time.monotonic()

    def set_freq(self, mhz: int) -> None:
        self.freq = int(mhz)

    def set_bandwidth(self, mhz: int) -> None:
        pass

    def set_gain(self, index: int) -> None:
        pass

    def capture(self, n: int, rate_index: int = 0, bits: int = 10) -> np.ndarray:
        fs = RATES_HZ[rate_index]
        n = max(256, min(int(n), MAX_SAMPLES))
        t = np.arange(n) / fs
        now = time.monotonic() - self.t0
        x = (self.rng.normal(size=n) + 1j * self.rng.normal(size=n)) \
            * self.noise / math.sqrt(2)
        for c in self.carriers:
            fc = c["f"] + c["drift"] * math.sin(c["rate"] * now + c["phase"])
            off = (fc - self.freq) * HZ
            if abs(off) > fs / 2 + 10e6:
                continue
            # FM video: a band-limited random message, +-4 MHz deviation.
            msg = np.cumsum(self.rng.normal(size=n))
            msg = np.convolve(msg, np.ones(40) / 40, mode="same")
            msg = (msg - msg.mean()) / (np.abs(msg).max() + 1e-9)
            phase = 2 * np.pi * np.cumsum(off + 4e6 * msg) / fs
            x += c["amp"] * np.exp(1j * phase)
        # Analog low-pass (smooth brick) applied in the frequency domain.
        spec = np.fft.fft(x)
        f = np.fft.fftfreq(n, 1 / fs)
        spec *= 1.0 / np.sqrt(1.0 + (np.abs(f) / (self.lpf_mhz * HZ)) ** 12)
        x = np.fft.ifft(spec)
        x = x + self.image * np.conj(x) + self.dc
        q = np.round(np.clip(x.real, -1, 511 / 512) * 512) / 512 \
            + 1j * np.round(np.clip(x.imag, -1, 511 / 512) * 512) / 512
        time.sleep(0.004)  # a little like the USB transfer
        return q.astype(np.complex64)

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# Hop + stitch
# --------------------------------------------------------------------------- #
def hop_spectrum_db(iq: np.ndarray, nfft: int) -> np.ndarray:
    """Welch PSD in dB full scale, 50 % overlap Hann, DC-centred (fftshift).

    Each segment has its mean removed, as the reference client does; that
    only touches the DC bin, which the stitcher drops anyway.
    """
    win = np.hanning(nfft).astype(np.float32)
    norm = float(win.sum()) ** 2
    hop = nfft // 2
    nseg = max(1, (iq.size - nfft) // hop + 1)
    acc = np.zeros(nfft, dtype=np.float64)
    for k in range(nseg):
        seg = iq[k * hop:k * hop + nfft]
        if seg.size < nfft:
            break
        seg = (seg - seg.mean()) * win
        acc += np.abs(np.fft.fft(seg)) ** 2
    acc /= nseg * norm
    return 10.0 * np.log10(np.maximum(np.fft.fftshift(acc), 1e-15))


class Stitcher:
    """Combine per-hop spectra onto the output bins of one sweep."""

    def __init__(self, start_mhz: float, stop_mhz: float, es: EspSettings,
                 bin_hz: float):
        self.es = es
        self.nfft = esp_fft_size(bin_hz, es.rate_hz)
        self.bin_hz = es.rate_hz / self.nfft
        self.los = hop_plan_mhz(start_mhz, stop_mhz, es.step_mhz)
        self.hz_low = math.floor(start_mhz) * HZ
        n = int(round((math.ceil(stop_mhz) - math.floor(start_mhz)) * HZ
                      / self.bin_hz))
        self.n = max(1, n)
        centres = self.hz_low + (np.arange(self.n) + 0.5) * self.bin_hz
        # Per hop: which output bins it serves, and from which FFT bin.
        rel = np.fft.fftshift(np.fft.fftfreq(self.nfft, 1 / es.rate_hz))
        self.maps = []
        for lo in self.los:
            off = centres - lo * HZ
            ok = (np.abs(off) <= es.usable_mhz * HZ) & \
                 (np.abs(off) >= es.dc_khz * 1e3)
            ks = np.nonzero(ok)[0]
            src = np.round(off[ks] / self.bin_hz).astype(np.int64) + self.nfft // 2
            src = np.clip(src, 0, self.nfft - 1)
            self.maps.append((ks, src, np.abs(off[ks])))

    def combine(self, spectra: list[np.ndarray]) -> np.ndarray:
        out = np.full(self.n, np.nan, dtype=np.float32)
        if self.es.combine == "near":
            dist = np.full(self.n, np.inf)
            for (ks, src, d), db in zip(self.maps, spectra):
                better = d < dist[ks]
                out[ks[better]] = db[src[better]]
                dist[ks[better]] = d[better]
        else:
            for (ks, src, _d), db in zip(self.maps, spectra):
                out[ks] = np.fmin(out[ks], db[src])
        if self.es.flip:
            out = out[::-1].copy()
        return out


def esp_source(es: EspSettings, device_factory=None):
    """Build a SweepReader source that hops an ESP32-C5 over the span.

    device_factory() returns a connected FakeEspSdr (the simulator). The
    first item yielded is a DeviceInfo naming the port that was opened.
    """

    def source(s: SweepSettings, stop: threading.Event) -> Iterator[SpanLine]:
        dev = device_factory()
        try:
            yield DeviceInfo(dev.port, dev.identity)
            st = Stitcher(s.start_mhz, s.stop_mhz, es, s.bin_hz)
            rng = getattr(dev, "range", None)
            if rng and not (rng[0] <= st.los[0] and st.los[-1] <= rng[1]):
                raise EspSdrError(f"firmware tuning range {rng} does not cover "
                                  f"{st.los[0]}-{st.los[-1]} MHz")
            # Always send it: until told otherwise the firmware keeps the PHY's
            # calibrated Wi-Fi 20 MHz filter, which leaves only about +-10 MHz
            # of each hop (measured on a C3). 0 = widest.
            dev.set_bandwidth(es.bandwidth_mhz)
            # The GUI's gain box edits s.gain, so that is the live value.
            dev.set_gain(s.gain)
            while not stop.is_set():
                spectra = []
                for lo in st.los:
                    if stop.is_set():
                        return
                    dev.set_freq(lo)
                    iq = dev.capture(es.samples, es.rate_index, es.bits)
                    spectra.append(hop_spectrum_db(iq, st.nfft))
                yield SpanLine(st.hz_low, st.bin_hz, st.combine(spectra))
        finally:
            dev.close()

    return source


def _receiver_process(es: EspSettings, s: SweepSettings,
                      sim_carriers: list | None, out, stop) -> None:
    """Child-process body: run the hop loop, ship finished sweeps back."""
    factory = ((lambda: FakeEspSdr(sim_carriers, seed=1))
               if sim_carriers is not None else None)
    if sim_carriers is None:                 # the device: finished sweeps
        from .fpv_link import fw_source
        src = fw_source(es)
    else:                                    # --sim: synthetic I/Q, stitched here
        src = esp_source(es, factory)
    try:
        for line in src(s, stop):
            if isinstance(line, DeviceInfo):
                out.put(("open", line.port, line.identity))
            else:
                out.put((line.hz_low, line.bin_width, line.powers))
    except Exception as e:
        out.put(("error", str(e)))


def process_source(es: EspSettings, sim_carriers: list | None = None):
    """Like esp_source, but the serial I/O runs in its own process.

    Measured on a C3 under the GUI: whenever the reader thread waited on the
    GIL while Qt was painting, the USB stack dropped exactly 128 bytes of a
    capture; the read then sat out its timeout and resynchronised - the
    display froze for 5 s roughly every 2 s of sweeping. Headless it never
    happened in 1879 captures, and a thread burning Python time reproduced
    it. A separate process has its own GIL, so painting cannot starve it.
    """

    def source(s: SweepSettings, stop: threading.Event) -> Iterator[SpanLine]:
        ctx = mp.get_context("spawn")
        out = ctx.Queue(maxsize=16)
        child_stop = ctx.Event()
        proc = ctx.Process(target=_receiver_process,
                           args=(es, s, sim_carriers, out, child_stop),
                           daemon=True)
        proc.start()
        try:
            while not stop.is_set():
                try:
                    item = out.get(timeout=0.2)
                except queue.Empty:
                    if not proc.is_alive():
                        raise EspSdrError("receiver process exited "
                                          f"(code {proc.exitcode})") from None
                    continue
                if item[0] == "error":
                    raise EspSdrError(item[1])
                if item[0] == "open":
                    yield DeviceInfo(item[1], item[2])
                    continue
                yield SpanLine(*item)
        finally:
            child_stop.set()
            # Unblock a child stuck on a full queue, then give it a moment to
            # release the port cleanly before forcing it.
            try:
                while True:
                    out.get_nowait()
            except Exception:
                pass
            proc.join(timeout=2.0)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=1.0)

    return source
