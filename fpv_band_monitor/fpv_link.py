"""Finished sweeps from the LCD band-monitor firmware ("FPV" protocol).

The XIAO ESP32-C5 LCD firmware (ESP-SDR + main/fpv) hops, FFTs and stitches
on the chip and streams every finished sweep over USB: about 4 kB per sweep
instead of 20 kB of raw I/Q per hop, so the PC is not limited by the serial
link. This is the monitor's only receiver.

Wire format (see main/fpv/link.h in the firmware):

    FPV?                    -> FPV 1 bin_hz=78125 maxbins=2304 ...
    FPV SPAN <a> <b> [chs]  -> OK        sweep a..b MHz, show chs on the LCD
    FPV GAIN <n|AGC>        -> OK
    FPV STREAM ON|OFF       -> OK
    SWEEP <seq> <start_mhz> <nbins> <bin_hz> <t_ms> <crc32>\\n + nbins x uint16
          (dB = code / 100 - 200, 0xFFFF = no data)
"""

from __future__ import annotations

import time
import zlib
from typing import Iterator

import numpy as np

from .sweep import HZ, DeviceInfo, SpanLine, SweepSettings

FPV_NONE = 0xFFFF
FW_BIN_HZ = 78125.0          # 80 MS/s / 1024-point FFT


class FpvLinkError(RuntimeError):
    pass


def decode_codes(payload: bytes) -> np.ndarray:
    """uint16 codes -> dB (float32), NaN where the firmware had no data."""
    codes = np.frombuffer(payload, dtype="<u2")
    db = codes.astype(np.float32) / 100.0 - 200.0
    db[codes == FPV_NONE] = np.nan
    return db


def channel_token(name: str, freq_mhz: float) -> str:
    """What the firmware accepts: a channel name like F3, or whole MHz."""
    key = name.strip().upper()
    if len(key) == 2 and key[0] in "ABEFRL" and key[1] in "12345678":
        return key
    return str(int(round(freq_mhz)))


class FpvLink:
    """Synchronous client: text commands, interleaved SWEEP frames."""

    def __init__(self, port: str | None = None, timeout_s: float = 2.0):
        try:
            import serial
        except ImportError:
            raise FpvLinkError("pyserial is required: pip install pyserial") from None
        if port is None:
            from .esp_sdr import find_port
            port = find_port()
            if port is None:
                raise FpvLinkError("no Espressif USB device found (VID 303A)")
        self.port = port
        try:
            self.ser = serial.Serial()
            self.ser.port = port
            self.ser.baudrate = 2_000_000
            self.ser.timeout = timeout_s
            self.ser.dtr = False
            self.ser.rts = False
            self.ser.open()
        except Exception as e:
            raise FpvLinkError(f"could not open {port}: {e}") from None
        self.timeout_s = timeout_s
        self.bad_frames = 0
        self._pending: list[tuple] = []
        try:
            self.ser.reset_input_buffer()
            self.hello = self.ask("FPV?", expect="FPV ")
        except Exception:
            self.close()
            raise
        self.info = dict(kv.split("=", 1) for kv in self.hello.split()[2:] if "=" in kv)
        self.bin_hz = float(self.info.get("bin_hz", FW_BIN_HZ))

    # -- framing -------------------------------------------------------------
    def _send(self, text: str) -> None:
        self.ser.write(("\n" + text + "\n").encode("ascii"))

    def _read_item(self):
        """('line', text) or ('sweep', seq, start_mhz, bin_hz, db) or None on timeout."""
        raw = self.ser.readline()
        if not raw:
            return None
        if raw.startswith(b"SWEEP "):
            try:
                _, seq, start, nbins, bin_hz, _t, crc = raw.decode("ascii").split()
                n = int(nbins)
                payload = self.ser.read(2 * n)
                if len(payload) != 2 * n or zlib.crc32(payload) != int(crc, 16):
                    self.bad_frames += 1
                    return ("bad",)
                return ("sweep", int(seq), float(start), float(bin_hz), decode_codes(payload))
            except (ValueError, UnicodeDecodeError):
                self.bad_frames += 1
                return ("bad",)
        text = raw.decode("ascii", "replace").strip()
        return ("line", text) if text else ("bad",)

    def ask(self, cmd: str, expect: str | None = None) -> str:
        """Send a command; return its reply line (sweeps meanwhile are kept)."""
        self._send(cmd)
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            item = self._read_item()
            if item is None:
                continue
            if item[0] == "sweep":
                self._pending.append(item)
                continue
            if item[0] != "line":
                continue
            text = item[1]
            if text.startswith("ERR"):
                raise FpvLinkError(f"{cmd!r}: {text}")
            if expect is None and text == "OK":
                return text
            if expect is not None and text.startswith(expect):
                return text
        raise FpvLinkError(f"no answer to {cmd!r} from {self.port}")

    def configure(self, start_mhz: float, stop_mhz: float, channels, gain: int) -> None:
        # the LCD shows at most 4 channels; the sweep is the same either way
        tokens = ",".join(channel_token(n, f) for n, f in list(channels)[:4])
        self.ask(f"FPV SPAN {start_mhz:.3f} {stop_mhz:.3f} {tokens}".rstrip())
        self.ask("FPV GAIN AGC" if gain is None or gain < 0 else f"FPV GAIN {int(gain)}")

    def stream(self, on: bool) -> None:
        self.ask("FPV STREAM ON" if on else "FPV STREAM OFF")

    def next_sweep(self):
        """The next frame as (seq, start_mhz, bin_hz, db), or None on a timeout."""
        if self._pending:
            return self._pending.pop(0)[1:]
        while True:
            item = self._read_item()
            if item is None:
                return None
            if item[0] == "sweep":
                return item[1:]

    def close(self) -> None:
        try:
            if self.ser.is_open:
                try:
                    self._send("FPV STREAM OFF")
                except Exception:
                    pass
                self.ser.close()
        except Exception:
            pass


def detect_firmware(port: str | None, timeout_s: float = 1.0) -> bool:
    """True if the device on port runs the LCD firmware with FPV streaming."""
    try:
        link = FpvLink(port, timeout_s=timeout_s)
    except Exception:
        return False
    link.close()
    return True


def expected_bins(start_mhz: float, stop_mhz: float, bin_hz: float = FW_BIN_HZ) -> int:
    """Bins of the firmware's sweep over start..stop (whole MHz, like Stitcher)."""
    return max(1, int(round((np.ceil(stop_mhz) - np.floor(start_mhz)) * HZ / bin_hz)))


def fw_source(es, link_factory=None):
    """SweepReader source over the FPV stream (same shape as esp_source)."""

    def source(s: SweepSettings, stop) -> Iterator[SpanLine]:
        link = (link_factory or (lambda: FpvLink(s.port)))()
        try:
            yield DeviceInfo(link.port, link.hello)
            link.configure(s.start_mhz, s.stop_mhz, es.channels, s.gain)
            link.stream(True)
            want = int(np.floor(s.start_mhz))
            foreign = 0
            while not stop.is_set():
                fr = link.next_sweep()
                if fr is None:
                    continue
                _seq, start, bin_hz, db = fr
                if int(start) != want or db.size != expected_bins(s.start_mhz, s.stop_mhz, bin_hz):
                    # a sweep of another span: still the previous one, or the
                    # LCD's own mode was chosen on the device; take it back
                    foreign += 1
                    if foreign >= 5:
                        link.configure(s.start_mhz, s.stop_mhz, es.channels, s.gain)
                        foreign = 0
                    continue
                foreign = 0
                yield SpanLine(start * HZ, bin_hz, db)
        finally:
            link.close()

    return source


def probe(port: str | None, start_mhz: float, stop_mhz: float, channels, gain: int,
          seconds: float = 5.0) -> int:
    """--info: who answers, its state, and 5 s of the stream."""
    from .esp_sdr import list_port_lines
    print("serial ports:")
    for line in list_port_lines():
        print("  " + line)
    try:
        link = FpvLink(port)
    except FpvLinkError as e:
        print(f"\n{e}\n(the LCD firmware answers 'FPV?'; is it flashed and is the port free?)")
        return 1
    try:
        print(f"\nport {link.port}: {link.hello}")
        print(link.ask("FPV STATE?", expect="STATE"))
        link.configure(start_mhz, stop_mhz, channels, gain)
        link.stream(True)
        t0 = time.monotonic()
        n, floors, last_t = 0, [], None
        while time.monotonic() - t0 < seconds:
            fr = link.next_sweep()
            if fr is None:
                continue
            n += 1
            db = fr[3]
            floors.append(float(np.nanmedian(db)))
        dt = time.monotonic() - t0
        print(f"\n{n} sweeps in {dt:.1f} s -> {n / dt:.2f} sweeps/s, "
              f"{link.bad_frames} bad frames")
        if floors:
            print(f"floor (median of a sweep): {np.median(floors):.1f} dB")
        print(link.ask("FPV STATE?", expect="STATE"))
    finally:
        link.close()
    return 0
