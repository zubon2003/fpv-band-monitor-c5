"""Per-channel measurements taken from one swept spectrum frame.

The centre frequency is the power-weighted centroid of a fixed band around the
channel, taken on a time-averaged spectrum and then re-centred once on its own
result. Measured against a live VTX, that holds to about 0.1 MHz while any
peak-relative estimator (peak bin, centroid of the -6 dB run) wanders by a
megahertz: one sweep is a 20 ms snapshot of an FM video carrier, whose
instantaneous spectrum is lopsided even though the carrier is steady.

Occupied bandwidth is measured the same way, as the width of the contiguous run
above peak - X dB, with linear interpolation at both edges so the answer is not
quantised to the bin width.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from .channels import Channel
from .sweep import FrequencyGrid


@dataclass
class Measurement:
    name: str
    nominal_mhz: float
    present: bool
    centre_mhz: float | None      # measured, power-weighted
    offset_mhz: float | None      # centre - nominal
    peak_mhz: float | None
    peak_dbm: float
    floor_dbm: float
    snr_db: float
    bw_db3_mhz: float | None
    bw_db20_mhz: float | None
    reason: str = ""            # why `present` is False, for the readout


def noise_floor(powers: np.ndarray) -> float:
    """Median of the frame: robust while carriers cover a minority of bins."""
    finite = powers[np.isfinite(powers)]
    if finite.size == 0:
        return float("nan")
    return float(np.median(finite))


def _contiguous_run(powers: np.ndarray, peak_i: int, threshold: float) -> tuple[int, int]:
    """Inclusive [lo, hi] run around peak_i whose bins stay >= threshold."""
    lo = peak_i
    while lo - 1 >= 0 and np.isfinite(powers[lo - 1]) and powers[lo - 1] >= threshold:
        lo -= 1
    hi = peak_i
    n = powers.size
    while hi + 1 < n and np.isfinite(powers[hi + 1]) and powers[hi + 1] >= threshold:
        hi += 1
    return lo, hi


def _edge_interp(powers: np.ndarray, inside: int, outside: int,
                 threshold: float) -> float:
    """Fractional bin offset from `inside` towards `outside` where p crosses."""
    if outside < 0 or outside >= powers.size or not np.isfinite(powers[outside]):
        return 0.0
    p_in, p_out = float(powers[inside]), float(powers[outside])
    if p_in <= p_out:
        return 0.0
    frac = (p_in - threshold) / (p_in - p_out)
    return float(np.clip(frac, 0.0, 1.0))


def _bandwidth(powers: np.ndarray, freqs: np.ndarray, peak_i: int,
               peak: float, drop_db: float) -> float | None:
    threshold = peak - drop_db
    lo, hi = _contiguous_run(powers, peak_i, threshold)
    bin_mhz = float(freqs[1] - freqs[0]) if freqs.size > 1 else 0.0
    if bin_mhz <= 0:
        return None
    left = _edge_interp(powers, lo, lo - 1, threshold)
    right = _edge_interp(powers, hi, hi + 1, threshold)
    return (hi - lo + left + right) * bin_mhz


def _window_centroid(powers: np.ndarray, freqs: np.ndarray, centre_mhz: float,
                     half_mhz: float, floor_dbm: float,
                     noise_margin_db: float) -> float | None:
    """Power-weighted centroid of everything in a fixed band around centre.

    Weighted by power above the noise floor, with bins within noise_margin_db
    of the floor dropped entirely. Measured on a live VTX this beat every
    peak-relative estimator: sd 93 kHz over 25 s against 869 kHz for the
    centroid of the -6 dB run, which follows whichever bin is momentarily
    loudest.
    """
    m = (freqs > centre_mhz - half_mhz) & (freqs < centre_mhz + half_mhz)
    if not np.any(m):
        return None
    seg, fw = powers[m], freqs[m]
    ok = np.isfinite(seg) & (seg > floor_dbm + noise_margin_db)
    if not np.any(ok):
        return None
    w = 10.0 ** (seg[ok] / 10.0) - 10.0 ** (floor_dbm / 10.0)
    w = np.maximum(w, 0.0)
    total = float(np.sum(w))
    if total <= 0:
        return None
    return float(np.sum(fw[ok] * w) / total)


def measure_channel(powers: np.ndarray, grid: FrequencyGrid, ch: Channel,
                    search_mhz: float = 8.0, min_snr_db: float = 18.0,
                    noise_margin_db: float = 3.0,
                    floor_dbm: float | None = None,
                    min_bw_mhz: float = 0.0,
                    centroid_half_mhz: float = 7.5) -> Measurement:
    lo, hi = grid.window(ch.freq_mhz, search_mhz)
    seg = powers[lo:hi]
    freqs = grid.freqs_mhz[lo:hi]
    floor = noise_floor(powers) if floor_dbm is None else floor_dbm

    if seg.size == 0 or not np.any(np.isfinite(seg)):
        return Measurement(ch.name, ch.freq_mhz, False, None, None, None,
                           float("nan"), floor, float("nan"), None, None,
                           "no data in this window")

    peak_i = int(np.nanargmax(seg))
    peak = float(seg[peak_i])
    snr = peak - floor
    if not np.isfinite(snr) or snr < min_snr_db:
        return Measurement(ch.name, ch.freq_mhz, False, None, None,
                           float(freqs[peak_i]), peak, floor, snr, None, None,
                           f"SNR {snr:.1f} < {min_snr_db:.0f} dB (--min-snr)")

    bw3 = _bandwidth(seg, freqs, peak_i, peak, 3.0)
    if min_bw_mhz > 0.0 and (bw3 is None or bw3 < min_bw_mhz):
        # Optional extra filter. Off by default: a real VTX carrying a static
        # picture (or a burst caught mid-sweep) is only a few bins wide at
        # -3 dB, so width alone does not separate signal from a noise spike -
        # the SNR threshold does that.
        width = "n/a" if bw3 is None else f"{bw3:.1f}"
        return Measurement(ch.name, ch.freq_mhz, False, None, None,
                           float(freqs[peak_i]), peak, floor, snr, bw3, None,
                           f"-3 dB width {width} < {min_bw_mhz:.1f} MHz "
                           "(--min-bw)")

    # Pass 1 over the search window, then re-centre on that estimate so a VTX
    # that sits a few MHz off nominal is not dragged back by a clipped tail.
    half = min(centroid_half_mhz, search_mhz)
    centre = _window_centroid(powers, grid.freqs_mhz, ch.freq_mhz, search_mhz,
                              floor, noise_margin_db)
    if centre is not None:
        centre = min(max(centre, ch.freq_mhz - search_mhz),
                     ch.freq_mhz + search_mhz)
        refined = _window_centroid(powers, grid.freqs_mhz, centre, half, floor,
                                   noise_margin_db)
        if refined is not None:
            centre = refined
    if centre is None:
        centre = float(freqs[peak_i])

    return Measurement(
        name=ch.name,
        nominal_mhz=ch.freq_mhz,
        present=True,
        centre_mhz=centre,
        offset_mhz=centre - ch.freq_mhz,
        peak_mhz=float(freqs[peak_i]),
        peak_dbm=peak,
        floor_dbm=floor,
        snr_db=snr,
        bw_db3_mhz=bw3,
        bw_db20_mhz=_bandwidth(seg, freqs, peak_i, peak, 20.0),
    )


def measure_all(powers: np.ndarray, grid: FrequencyGrid,
                channels: list[Channel], search_mhz: float = 8.0,
                min_snr_db: float = 18.0,
                noise_margin_db: float = 3.0,
                min_bw_mhz: float = 0.0,
                centroid_half_mhz: float = 7.5) -> list[Measurement]:
    floor = noise_floor(powers)
    return [
        measure_channel(powers, grid, ch, search_mhz, min_snr_db,
                        noise_margin_db, floor_dbm=floor,
                        min_bw_mhz=min_bw_mhz,
                        centroid_half_mhz=centroid_half_mhz)
        for ch in channels
    ]


def auto_search_halfwidth(channels: list[Channel], requested: float) -> float:
    """Shrink the search window so neighbouring channels cannot steal a peak."""
    freqs = sorted(c.freq_mhz for c in channels)
    gaps = [b - a for a, b in zip(freqs, freqs[1:])]
    if not gaps:
        return requested
    return min(requested, max(1.0, min(gaps) / 2.0))


class SpectrumAverager:
    """Linear-power average of the last `window_s` seconds of sweeps.

    One sweep is a ~20 ms snapshot: an FM video carrier is asymmetric at any
    instant, so a single-sweep centroid wanders by several hundred kHz even
    though the carrier itself is steady. Averaging first removes that.
    """

    def __init__(self, window_s: float = 1.0, max_frames: int = 400,
                 mode: str = "mean"):
        self.window_s = float(window_s)
        self.mode = mode          # "mean" for continuous signals, "max" for bursts
        self._buf: deque = deque(maxlen=max_frames)

    def add(self, t: float, powers_db: np.ndarray) -> np.ndarray:
        if self.window_s <= 0.0:
            return powers_db
        self._buf.append((t, 10.0 ** (powers_db / 10.0)))
        while len(self._buf) > 1 and t - self._buf[0][0] > self.window_s:
            self._buf.popleft()
        if len(self._buf) == 1:
            return powers_db
        stack = np.stack([b for _, b in self._buf])
        if self.mode == "max":
            # Peak hold: a burst that only lands in one sweep survives, where
            # averaging would dilute it into the noise.
            with np.errstate(invalid="ignore", divide="ignore"):
                lin = np.nanmax(np.where(np.isfinite(stack), stack, -np.inf),
                                axis=0)
                lin = np.where(np.isfinite(lin), lin, np.nan)
                return (10.0 * np.log10(np.maximum(lin, 1e-20))).astype(np.float32)
        valid = np.isfinite(stack)
        count = valid.sum(axis=0)
        # Bins nothing has covered yet stay NaN instead of warning about an
        # empty mean.
        with np.errstate(invalid="ignore", divide="ignore"):
            lin = np.where(count > 0,
                           np.where(valid, stack, 0.0).sum(axis=0)
                           / np.maximum(count, 1),
                           np.nan)
            return (10.0 * np.log10(np.maximum(lin, 1e-20))).astype(np.float32)

    def reset(self) -> None:
        self._buf.clear()

    @property
    def n_frames(self) -> int:
        return len(self._buf)


def format_status(ms: list[Measurement], smoother: "Smoother | None" = None) -> str:
    """One console line: measured centre, or why a channel was rejected."""
    parts = []
    for m in ms:
        v = smoother.value(m.name) if smoother is not None else m.centre_mhz
        if m.present and v is not None:
            bw = f"{m.bw_db3_mhz:4.1f}" if m.bw_db3_mhz is not None else "  --"
            parts.append(f"{m.name} {v:8.2f} ({v - m.nominal_mhz:+5.2f}) "
                         f"{m.peak_dbm:6.1f}dBm bw{bw}")
        else:
            parts.append(f"{m.name}     --.--  [{m.reason}]")
    floor = ms[0].floor_dbm if ms else float("nan")
    return f"floor {floor:6.1f} dBm | " + " | ".join(parts)


class Smoother:
    """EMA over measured centres, with hold-last while a channel is absent."""

    def __init__(self, alpha: float = 0.3):
        self.alpha = alpha
        self._value: dict[str, float] = {}

    def update(self, m: Measurement) -> float | None:
        if m.present and m.centre_mhz is not None:
            prev = self._value.get(m.name)
            v = m.centre_mhz if prev is None else prev + self.alpha * (m.centre_mhz - prev)
            self._value[m.name] = v
            return v
        return self._value.get(m.name)

    def value(self, name: str) -> float | None:
        return self._value.get(name)
