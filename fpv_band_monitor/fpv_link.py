"""Finished sweeps from the LCD band-monitor firmware ("FPV" protocol).

The XIAO ESP32-C5 LCD firmware (ESP-SDR + main/fpv) hops, FFTs and stitches
on the chip and can stream every finished sweep over USB: about 4 kB per
sweep instead of 20 kB of raw I/Q per hop, so the PC is no longer limited by
the serial link. The same firmware still answers the stock ESP-SDR I/Q
protocol, so ``--receiver iq`` keeps working with it; ``--receiver auto``
(the default) takes the stream when the device answers ``FPV?`` and falls
back to I/Q otherwise.

Wire format (see main/fpv/link.h in the firmware):

    FPV?                    -> FPV 1 bin_hz=78125 maxbins=2304 ...
    FPV SPAN <a> <b> [chs]  -> OK        sweep a..b MHz, show chs on the LCD
    FPV GAIN <n|AGC>        -> OK
    FPV STREAM ON|OFF       -> OK
    FPV NOLCD | FPV LCD     -> OK        LCD off: the chip only sweeps (faster);
                                         ends with STREAM OFF or a tap
    FPV STATE?              -> STATE mode=... clip=<per mille> ...
    FPV WIFI <ch> [ms]      -> WIFI ch=.. frames=.. rssi_max=.., AP lines, END
                                         (receive only: the chip never transmits)
    SWEEP <seq> <start_mhz> <nbins> <bin_hz> <t_ms> <crc32>\\n + nbins x uint16
          (dB = code / 100 - 200, 0xFFFF = no data)
"""

from __future__ import annotations

import time
import zlib
from typing import Iterator

import numpy as np

from .sweep import HZ, DeviceInfo, DeviceStatus, SpanLine, SweepSettings

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
            for attempt in range(3):
                try:
                    self.hello = self.ask("FPV?", expect="FPV ")
                    break
                except FpvLinkError:
                    # a stale ERR from the previous session can arrive first
                    # (Windows USB CDC), or the device is still sending a
                    # stream nobody stopped: ask again
                    if attempt == 2:
                        raise
                    self.ser.reset_input_buffer()
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
        # Out of step (joined mid-frame, or a payload byte was a newline):
        # the next header then sits at the end of a line of payload bytes.
        k = raw.find(b"SWEEP ")
        if k > 0:
            self.bad_frames += 1
            raw = raw[k:]
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

    def lcd(self, on: bool) -> None:
        self.ask("FPV LCD" if on else "FPV NOLCD")

    def state(self) -> dict:
        line = self.ask("FPV STATE?", expect="STATE")
        return dict(w.split("=", 1) for w in line.split()[1:] if "=" in w)

    def wifi(self, channel: int, ms: int):
        """Listen (receive only) on a Wi-Fi channel: the WIFI line and the AP
        lines. Sweeps arriving meanwhile are dropped."""
        self._send(f"FPV WIFI {int(channel)} {int(ms)}")
        deadline = time.monotonic() + ms / 1000.0 + self.timeout_s + 1.0
        head, aps = None, []
        while time.monotonic() < deadline:
            item = self._read_item()
            if item is None or item[0] != "line":
                continue
            text = item[1]
            if text.startswith("ERR"):
                raise FpvLinkError(f"FPV WIFI {channel}: {text}")
            if text.startswith("WIFI "):
                head = text
            elif text.startswith("AP ") and head:
                aps.append(text)
            elif text == "END" and head:
                return head, aps
        raise FpvLinkError(f"no answer to FPV WIFI {channel} from {self.port}")

    def next_sweep(self):
        """The next frame as (seq, start_mhz, bin_hz, db), or None on a timeout."""
        if self._pending:
            return self._pending.pop(0)[1:]
        # bounded even while bytes keep coming, so a caller can check "stop"
        deadline = time.monotonic() + max(0.5, 2 * (self.ser.timeout or 0.5))
        while time.monotonic() < deadline:
            item = self._read_item()
            if item is None:
                return None
            if item[0] == "sweep":
                return item[1:]
        return None

    def close(self) -> None:
        try:
            if self.ser.is_open:
                try:
                    self._send("FPV STREAM OFF")
                    self.ser.flush()    # closing mid-write leaves a broken line on Windows
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
            # short reads: ask() still waits its full timeout for a reply,
            # but the loop below notices "stop" within ~0.3 s
            link.ser.timeout = 0.3
            yield DeviceInfo(link.port, link.hello)
            link.configure(s.start_mhz, s.stop_mhz, es.channels, s.gain)
            link.stream(True)
            if getattr(es, "nolcd", False):
                link.lcd(False)     # the firmware turns it back on at STREAM OFF
            want = int(np.floor(s.start_mhz))
            foreign = 0
            # the ADC's saturation, for the GUI's SAT mark (newer firmware only)
            poll_state, t_state = True, time.monotonic()
            while not stop.is_set():
                if poll_state and time.monotonic() - t_state >= 1.0:
                    t_state = time.monotonic()
                    try:
                        clip = link.state().get("clip")
                        if clip is None:
                            poll_state = False
                        else:
                            yield DeviceStatus(int(clip))
                    except (FpvLinkError, ValueError):
                        poll_state = False
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
                        if getattr(es, "nolcd", False):
                            link.lcd(False)
                        foreign = 0
                    continue
                foreign = 0
                yield SpanLine(start * HZ, bin_hz, db)
        finally:
            link.close()

    return source


def probe(port: str | None, start_mhz: float, stop_mhz: float, channels, gain: int,
          seconds: float = 5.0, nolcd: bool = False) -> int:
    """--info for the fft receiver: who answers, its state, 5 s of the stream."""
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
        if nolcd:
            link.lcd(False)
        t0 = time.monotonic()
        n, floors = 0, []
        while time.monotonic() - t0 < seconds:
            fr = link.next_sweep()
            if fr is None:
                continue
            n += 1
            floors.append(float(np.nanmedian(fr[3])))
        dt = time.monotonic() - t0
        print(f"\n{n} sweeps in {dt:.1f} s -> {n / dt:.2f} sweeps/s"
              f"{' (LCD off)' if nolcd else ''}, {link.bad_frames} bad frames")
        if floors:
            print(f"floor (median of a sweep): {np.median(floors):.1f} dB")
        print(link.ask("FPV STATE?", expect="STATE"))
    finally:
        link.close()
    return 0
