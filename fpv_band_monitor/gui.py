"""PyQt6 + pyqtgraph band monitor: spectrum, waterfall, measured centres."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui, QtWidgets

from . import wifi as wf
from .analysis import (Measurement, Smoother, SpectrumAverager,
                       auto_search_halfwidth, format_status, measure_all)
from .channels import GROUPS, Channel, group_of, parse_channels
from .sweep import (DeviceInfo, DeviceStatus, Frame, FrequencyGrid, SweepReader,
                    SweepSettings)

ESPRESSIF_VID = 0x303A

# Channel colours by frequency, lowest first: red, green, blue, yellow (as on
# the LCD); a 5th/6th channel gets purple/cyan.
CH_COLORS = ["#ff5f56", "#45d97a", "#4aa8ff", "#ffbd2e", "#c678dd", "#00d0c0"]
PANEL_BG = "#16161d"
WIFI_DIRECT = "#ff3b30"
WIFI_IMD = "#ffd23f"


def rank_colors(channels: list[Channel]) -> list[str]:
    """Each channel's colour: its rank by frequency."""
    order = sorted(range(len(channels)), key=lambda i: (channels[i].freq_mhz, i))
    out = [""] * len(channels)
    for rank, i in enumerate(order):
        out[i] = CH_COLORS[rank % len(CH_COLORS)]
    return out


def blend(fg: str, bg: str, a: float) -> str:
    """fg over bg at opacity a, as #rrggbb."""
    f, b = QtGui.QColor(fg), QtGui.QColor(bg)
    mix = [round(bb + a * (ff - bb)) for ff, bb in
           ((f.red(), b.red()), (f.green(), b.green()), (f.blue(), b.blue()))]
    return "#%02x%02x%02x" % tuple(mix)

pg.setConfigOptions(imageAxisOrder="row-major", antialias=False,
                    background="#0e0e12", foreground="#c8c8d4")


def _lut() -> np.ndarray:
    for name in ("viridis", "CET-L8", "inferno", "CET-L9"):
        try:
            cmap = pg.colormap.get(name)
            if cmap is not None:
                return cmap.getLookupTable(0.0, 1.0, 256)
        except Exception:
            continue
    cmap = pg.ColorMap(
        [0.0, 0.35, 0.65, 0.85, 1.0],
        [(6, 8, 30), (20, 60, 130), (30, 160, 140), (230, 200, 60), (255, 255, 230)],
    )
    return cmap.getLookupTable(0.0, 1.0, 256)


@dataclass
class ViewOptions:
    bw_marks_mhz: tuple[float, ...] = (9.0, 17.0)
    search_mhz: float = 8.0
    search_request_mhz: float = 8.0   # before narrowing to the channel spacing
    min_snr_db: float = 18.0
    noise_margin_db: float = 3.0
    min_bw_mhz: float = 0.0
    centroid_half_mhz: float = 7.5
    smooth_alpha: float = 0.3
    waterfall_rows: int = 400
    waterfall_row_ms: float = 100.0
    csv_path: str | None = None
    csv_interval_s: float = 0.25
    avg_s: float = 1.0          # measurement averaging window
    dev_ok_mhz: float = 0.5     # blue up to here, yellow to dev_warn, red from
    dev_warn_mhz: float = 1.0
    show_hops: bool = True      # dotted line at each hop's LO
    hop_los_mhz: tuple = ()
    # Span buttons: (start, stop) MHz -> (grid, hop LOs); None hides them.
    span_planner: Callable | None = None
    receiver: object | None = None      # cli.ReceiverControl: auto / iq / fft
    channel_span_half_mhz: float = 15.0  # one-channel span = centre +- this
    y_auto: bool = True         # spectrum dBm axis follows the noise floor
    y_min_dbm: float = -100.0
    y_max_dbm: float = -20.0
    verbose: bool = False


class FlowLayout(QtWidgets.QLayout):
    """Left-to-right layout that wraps onto a new row when it runs out of width.

    The toolbar has a dozen controls; in a plain QHBoxLayout they set a minimum
    window width wider than the screen.
    """

    def __init__(self, parent=None, spacing: int = 6):
        super().__init__(parent)
        self._items: list[QtWidgets.QLayoutItem] = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return QtCore.Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._layout(QtCore.QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QtCore.QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QtCore.QSize(m.left() + m.right(), m.top() + m.bottom())

    def _layout(self, rect, apply: bool) -> int:
        m = self.contentsMargins()
        x, y = rect.x() + m.left(), rect.y() + m.top()
        right = rect.right() - m.right()
        row_height = 0
        for item in self._items:
            hint = item.sizeHint()
            if row_height and x + hint.width() > right:
                x = rect.x() + m.left()
                y += row_height + self.spacing()
                row_height = 0
            if apply:
                item.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), hint))
            x += hint.width() + self.spacing()
            row_height = max(row_height, hint.height())
        return y + row_height - rect.y() + m.bottom()


DEV_OK_COLOR = "#4aa8ff"      # within dev_ok_mhz of nominal
DEV_WARN_COLOR = "#ffd23f"    # within dev_warn_mhz
DEV_BAD_COLOR = "#ff5f56"     # further out
DEV_IDLE_COLOR = "#5a5a68"


def deviation_color(offset_mhz: float | None, ok_mhz: float,
                    warn_mhz: float) -> str:
    """Blue up to and including ok_mhz, yellow between, red from warn_mhz on."""
    if offset_mhz is None:
        return DEV_IDLE_COLOR
    d = abs(offset_mhz)
    if d <= ok_mhz:
        return DEV_OK_COLOR
    if d < warn_mhz:
        return DEV_WARN_COLOR
    return DEV_BAD_COLOR


def format_deviation(offset_mhz: float) -> str:
    """Always MHz, at 10 kHz resolution - the measurement is good to ~0.1 MHz."""
    return f"{offset_mhz:+.2f} MHz"


class ChannelReadout(QtWidgets.QFrame):
    """Live readout of one channel. The deviation is the point: large, in its
    colour, on a panel tinted the same way; the measured centre below it.
    A click zooms the sweep into this channel (and back)."""

    def __init__(self, ch: Channel, color: str, ok_mhz: float = 0.5,
                 warn_mhz: float = 1.0, on_click: Callable[[str], None] | None = None,
                 parent=None):
        super().__init__(parent)
        self.ch = ch
        self.color = color
        self.ok_mhz = ok_mhz
        self.warn_mhz = warn_mhz
        self._on_click = on_click
        self._tint = None
        self.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        self._set_tint(None)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setToolTip("click: sweep only this channel (click again: ALL)")
        mono = QtGui.QFont("Consolas")
        mono.setStyleHint(QtGui.QFont.StyleHint.Monospace)

        self._mono = mono

        def font(size: int, bold: bool = False) -> QtGui.QFont:
            f = QtGui.QFont(mono)
            f.setPointSize(size)
            f.setBold(bold)
            return f

        self.title = QtWidgets.QLabel(f"{ch.name}  <span style='color:#8a8a9a'>"
                                      f"{ch.freq_mhz:.0f} MHz</span>")
        self.title.setFont(font(13, True))
        self.title.setStyleSheet(f"color: {color}; border: none; background: transparent;")
        title = self.title

        self.dev = QtWidgets.QLabel("NO SIGNAL")
        self.dev.setFont(font(22, True))
        self.dev.setStyleSheet(f"color: {DEV_IDLE_COLOR}; border: none; background: transparent;")
        self.dev.setToolTip(
            f"deviation from nominal: blue <= {ok_mhz:.2f} MHz, "
            f"yellow below {warn_mhz:.2f} MHz, red from there on")

        self.centre = QtWidgets.QLabel("----.-- MHz")
        self.centre.setFont(font(13, True))
        self.centre.setStyleSheet("color: #a0a0b0; border: none; background: transparent;")

        self.level = QtWidgets.QLabel("peak  --   SNR  --")
        self.bw = QtWidgets.QLabel("-3 dB  --   -20 dB  --")
        self.info = QtWidgets.QLabel("")
        for lab in (self.level, self.bw, self.info):
            lab.setFont(font(9))
            lab.setStyleSheet("color: #a0a0b0; border: none; background: transparent;")

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(1)
        for w in (title, self.dev, self.centre, self.level, self.bw, self.info):
            # Ignored width policy: the panel may be squeezed below the text
            # width, and resizeEvent shrinks the font to match.
            w.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                            QtWidgets.QSizePolicy.Policy.Preferred)
            lay.addWidget(w)
        self.setMinimumWidth(130)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred,
                           QtWidgets.QSizePolicy.Policy.Maximum)

    def _set_tint(self, dev_color: str | None) -> None:
        """Panel background: the deviation's colour, faint; plain without a signal."""
        if dev_color == self._tint and self.styleSheet():
            return
        self._tint = dev_color
        bg = blend(dev_color, PANEL_BG, 0.22) if dev_color else PANEL_BG
        self.setStyleSheet(
            f"ChannelReadout {{ background: {bg}; border: 2px solid {self.color}; "
            f"border-radius: 6px; }}")

    def mousePressEvent(self, event) -> None:
        if self._on_click is not None and event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._on_click(self.ch.name)
        super().mousePressEvent(event)

    def resizeEvent(self, event) -> None:
        """Scale the text with the panel, so four of them fit any width."""
        super().resizeEvent(event)
        w = max(1, self.width() - 20)

        def sized(ratio: float, lo: int, hi: int, bold: bool) -> QtGui.QFont:
            f = QtGui.QFont(self._mono)
            f.setPointSize(int(max(lo, min(hi, w / ratio))))
            f.setBold(bold)
            return f

        self.info.setVisible(w >= 150)
        self.bw.setVisible(w >= 110)
        self.title.setFont(sized(13.0, 7, 13, True))
        self.dev.setFont(sized(8.0, 10, 26, True))
        self.centre.setFont(sized(14.0, 8, 14, True))
        # 30 monospace characters have to fit: "peak  -48.1 dBm   SNR 46.2 dB"
        for lab in (self.level, self.bw, self.info):
            lab.setFont(sized(26.0, 6, 9, False))

    def update_values(self, m: Measurement, smoothed: float | None) -> None:
        floor = f"{m.floor_dbm:6.1f}" if np.isfinite(m.floor_dbm) else "   --"
        if m.present and smoothed is not None:
            offset = smoothed - m.nominal_mhz
            col = deviation_color(offset, self.ok_mhz, self.warn_mhz)
            self._set_tint(col)
            self.dev.setText(format_deviation(offset))
            self.dev.setStyleSheet(f"color: {col}; border: none; background: transparent;")
            self.centre.setText(f"{smoothed:8.2f} MHz")
            self.level.setText(
                f"peak {m.peak_dbm:7.1f} dBm   SNR {m.snr_db:5.1f} dB")
            b3 = f"{m.bw_db3_mhz:5.1f}" if m.bw_db3_mhz is not None else "  --"
            b20 = f"{m.bw_db20_mhz:5.1f}" if m.bw_db20_mhz is not None else "  --"
            self.bw.setText(f"-3 dB {b3}   -20 dB {b20} MHz")
            self.info.setText(f"floor {floor} dBm")
        else:
            self._set_tint(None)
            weak = m.reason.startswith("SNR") and np.isfinite(m.snr_db) and m.snr_db >= 6.0
            self.dev.setText("WEAK" if weak else "NO SIGNAL")
            self.dev.setStyleSheet(f"color: {DEV_IDLE_COLOR}; border: none; background: transparent;")
            self.centre.setText(f"{smoothed:8.2f} MHz" if smoothed is not None
                                else "----.-- MHz")
            # Say what the strongest thing in the window was and why it was
            # rejected, so a real carrier that misses a threshold is visible.
            if m.peak_mhz is not None and np.isfinite(m.peak_dbm):
                self.level.setText(f"peak  {m.peak_mhz:8.2f} MHz "
                                   f"{m.peak_dbm:6.1f} dBm")
            else:
                self.level.setText("peak        --")
            self.bw.setText(f"no signal: {m.reason}")
            self.info.setText(f"floor {floor} dBm")


class WifiMap(QtWidgets.QWidget):
    """The group's channels (17 MHz each) and the Wi-Fi channels checked:
    grey not yet, dark nothing heard, red Direct / yellow IMD, fainter the
    weaker the Wi-Fi signal."""

    LO, HI = 5490.0, 5900.0

    def __init__(self, names, freqs, colors, checks, half_mhz, parent=None):
        super().__init__(parent)
        self.names, self.freqs, self.colors = names, freqs, colors
        self.checks, self.half = checks, half_mhz
        self.heard: dict = {}
        self.current: int | None = None
        self.setMinimumHeight(86)

    def _x(self, mhz: float) -> float:
        return 8 + (mhz - self.LO) / (self.HI - self.LO) * (self.width() - 16)

    def paintEvent(self, event) -> None:
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor("#0e0e12"))
        small = QtGui.QFont("Consolas", 8)
        p.setFont(small)
        y_grp, h_grp = 4, 34
        for name, f, col in zip(self.names, self.freqs, self.colors):
            x0, x1 = self._x(f - self.half), self._x(f + self.half)
            c = QtGui.QColor(blend(col, "#0e0e12", 0.35))
            p.fillRect(QtCore.QRectF(x0, y_grp + 14, x1 - x0, h_grp - 14), c)
            p.setPen(QtGui.QColor(col))
            p.drawLine(QtCore.QPointF(self._x(f), y_grp + 14), QtCore.QPointF(self._x(f), y_grp + h_grp))
            p.drawText(QtCore.QRectF(self._x(f) - 20, y_grp, 40, 14),
                       QtCore.Qt.AlignmentFlag.AlignCenter, name)
        y_w, h_w = y_grp + h_grp + 3, 18
        for r in self.checks:
            f = wf.wifi_mhz(r.ch)
            x0, x1 = self._x(f - 10) + 1, self._x(f + 10) - 1
            h = self.heard.get(r.ch)
            if h is None:
                fill, text = QtGui.QColor("#0e0e12"), QtGui.QColor("#8a8a9a")
            elif not h.frames:
                fill, text = QtGui.QColor("#2a2a36"), QtGui.QColor("#8a8a9a")
            else:
                a = wf.strength(h.rssi_max, 0.25)
                fill = QtGui.QColor(blend(WIFI_DIRECT if r.direct else WIFI_IMD, "#0e0e12", a))
                text = QtGui.QColor("#0e0e12" if a > 0.6 else "#f0f0f6")
            rect = QtCore.QRectF(x0, y_w, x1 - x0, h_w)
            p.fillRect(rect, fill)
            p.setPen(QtGui.QColor("#f0f0f6" if r.ch == self.current else "#262632"))
            p.drawRect(rect)
            p.setPen(text)
            p.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter, str(r.ch))
        p.setPen(QtGui.QColor("#8a8a9a"))
        y_a = y_w + h_w + 2
        for f in range(5500, 5901, 50):
            x = self._x(f)
            p.drawLine(QtCore.QPointF(x, y_a), QtCore.QPointF(x, y_a + 3))
            p.drawText(QtCore.QRectF(x - 20, y_a + 3, 40, 14), QtCore.Qt.AlignmentFlag.AlignCenter, str(f))
        p.end()


class WifiDialog(QtWidgets.QDialog):
    """Wi-Fi check of the group shown: listen (receive only, through the LCD
    firmware's FPV WIFI) on every Wi-Fi channel that can hurt it, then say
    per FPV channel: OK, Direct (a Wi-Fi signal on it) or IMD."""

    def __init__(self, win: "BandMonitorWindow"):
        super().__init__(win)
        self.win = win
        self.names = [c.name for c in win.channels]
        self.freqs = [c.freq_mhz for c in win.channels]
        self.colors = list(win.colors)
        half = max(win.opts.bw_marks_mhz) / 2.0 if win.opts.bw_marks_mhz else 8.5
        self.checks = wf.channels_to_check(self.freqs, half)
        self.heard: dict[int, wf.Heard] = {}
        self.aps: list[wf.Ap] = []
        self._q: "queue.Queue" = queue.Queue()
        self._thread = None
        self._halt = threading.Event()
        self.setWindowTitle(f"WiFi check - {win.group_name or 'custom'}")
        self.resize(760, 360)

        lay = QtWidgets.QVBoxLayout(self)
        self.map = WifiMap(self.names, self.freqs, self.colors, self.checks, half)
        lay.addWidget(self.map)
        grid = QtWidgets.QGridLayout()
        mono = QtGui.QFont("Consolas", 11)
        self.rows = []
        for k, (name, f, col) in enumerate(zip(self.names, self.freqs, self.colors)):
            lab = QtWidgets.QLabel(f"{name}  <span style='color:#8a8a9a'>{f:.0f}</span>")
            lab.setFont(QtGui.QFont("Consolas", 14, QtGui.QFont.Weight.Bold))
            lab.setStyleSheet(f"color: {col};")
            kind = QtWidgets.QLabel("...")
            kind.setFont(QtGui.QFont("Consolas", 14, QtGui.QFont.Weight.Bold))
            kind.setMinimumWidth(90)
            detail = QtWidgets.QLabel("")
            detail.setFont(mono)
            grid.addWidget(lab, k, 0)
            grid.addWidget(kind, k, 1)
            grid.addWidget(detail, k, 2)
            self.rows.append((kind, detail))
        grid.setColumnStretch(2, 1)
        lay.addLayout(grid)
        bottom = QtWidgets.QHBoxLayout()
        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet("color: #8a8a9a;")
        self.btn_scan = QtWidgets.QPushButton("Scan")
        self.btn_scan.clicked.connect(self._scan_or_stop)
        btn_close = QtWidgets.QPushButton("Close")
        btn_close.clicked.connect(self.close)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.btn_scan)
        bottom.addWidget(btn_close)
        lay.addLayout(bottom)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(100)
        self._show()
        self._start()

    # -- scanning -----------------------------------------------------------
    def _start(self) -> None:
        if self._thread is not None or not self.checks:
            return
        self.heard.clear()
        self.aps.clear()
        self._halt.clear()
        self.win.pause_receiver()            # the port is needed for FPV WIFI
        port = self.win.settings.port
        checks = [r.ch for r in self.checks]
        self._thread = threading.Thread(target=self._run, args=(port, checks), daemon=True)
        self._thread.start()
        self.btn_scan.setText("Stop")

    def _run(self, port, checks) -> None:
        from .fpv_link import FpvLink, FpvLinkError
        link = None
        try:
            for attempt in range(15):           # the receiver may still hold the port
                try:
                    link = FpvLink(port)
                    break
                except FpvLinkError as e:
                    if attempt == 14:
                        raise
                    if "FPV" in str(e) and "no answer" in str(e):
                        raise FpvLinkError("the device did not answer FPV? - "
                                           "the Wi-Fi check needs the LCD firmware") from None
                    time.sleep(0.2)
            for ch in checks:
                if self._halt.is_set():
                    break
                self._q.put(("current", ch))
                head, aps = link.wifi(ch, wf.DWELL_MS)
                self._q.put(("heard", wf.parse_listen(head, aps)))
            self._q.put(("done", None))
        except Exception as e:
            self._q.put(("done", str(e)))
        finally:
            if link is not None:
                link.close()

    def _poll(self) -> None:
        changed = False
        while True:
            try:
                kind, val = self._q.get_nowait()
            except queue.Empty:
                break
            changed = True
            if kind == "current":
                self.map.current = val
            elif kind == "heard":
                self.heard[val.ch] = val
                for a in val.aps:
                    if all(b.bssid != a.bssid for b in self.aps):
                        self.aps.append(a)
            elif kind == "done":
                self._finish(val)
        if changed:
            self._show()

    def _finish(self, error) -> None:
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._thread = None
        self.map.current = None
        self.btn_scan.setText("Scan")
        self.win.resume_receiver()
        if error:
            self.status.setText("error: " + error)
            self.status.setStyleSheet("color: #ff5f56;")

    def _scan_or_stop(self) -> None:
        if self._thread is not None:
            self._halt.set()
        else:
            self.status.setStyleSheet("color: #8a8a9a;")
            self._start()

    def _show(self) -> None:
        self.map.heard = self.heard
        self.map.update()
        done = len(self.heard)
        if self._thread is not None:
            self.status.setText(f"listening (receive only) ch {self.map.current or ''}  "
                                f"{done}/{len(self.checks)} - sweeping is paused")
        elif not self.status.text().startswith("error"):
            self.status.setText(f"{len(self.checks)} Wi-Fi channels can reach this group; "
                                f"{done} checked")
        for (kind, detail), v in zip(self.rows, wf.verdicts(self.names, self.checks,
                                                             self.heard, self.aps)):
            kind.setText(v.kind)
            if v.kind == "OK":
                kind.setStyleSheet("color: #45d97a;")
                detail.setText("")
                continue
            if v.kind == "...":
                kind.setStyleSheet("color: #5a5a68;")
                detail.setText("")
                continue
            base = WIFI_DIRECT if v.kind == "Direct" else WIFI_IMD
            kind.setStyleSheet(f"color: {blend(base, '#0e0e12', wf.strength(v.rssi, 0.4))};")
            text = f"ch{v.ch} {wf.wifi_mhz(v.ch)}"
            if v.how:
                text += f"  {v.how}"
            text += f"  {v.rssi} dBm  {v.ssid or '(no beacon heard)'}"
            if v.more:
                text += f"  +{v.more} more"
            detail.setText(text)

    def closeEvent(self, event) -> None:
        self.timer.stop()
        if self._thread is not None:
            self._halt.set()
            self._thread.join(timeout=wf.DWELL_MS / 1000.0 + 4.0)
            self._thread = None
            self.win.resume_receiver()
        super().closeEvent(event)


class PortCombo(QtWidgets.QComboBox):
    """Serial-port picker: re-reads the port list every time it is opened,
    so a board plugged in after start-up shows up without a refresh button."""

    def __init__(self, on_open: Callable[[], None]):
        super().__init__()
        self._on_open = on_open

    def showPopup(self) -> None:
        self._on_open()
        super().showPopup()


def serial_ports() -> list:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return sorted(list_ports.comports(), key=lambda p: p.device)


class BandMonitorWindow(QtWidgets.QMainWindow):
    def __init__(self, channels: list[Channel], settings: SweepSettings,
                 grid: FrequencyGrid,
                 source_factory: Callable[[SweepSettings], Callable],
                 opts: ViewOptions, simulated: bool = False,
                 title: str = "ESP32-C5"):
        super().__init__()
        self.device_title = title
        self.channels = channels
        self.colors = rank_colors(channels)
        self.group_name = group_of(channels)     # None: custom --channels
        self._clip = 0                            # firmware: saturated samples per mille
        self._paused_for_wifi = False
        self._marker_items: list[tuple] = []
        self.settings = settings
        self.grid = grid
        self.source_factory = source_factory
        self.opts = opts
        self.simulated = simulated

        self.queue: "queue.Queue" = queue.Queue(maxsize=256)
        self.reader: SweepReader | None = None
        self.smoother = Smoother(opts.smooth_alpha)
        self.averager = SpectrumAverager(opts.avg_s)
        self.paused = False
        self.peak_hold: np.ndarray | None = None
        self.last_powers: np.ndarray | None = None
        self._wf_top: float | None = None
        self._row_accum: np.ndarray | None = None
        self._image_placed = False
        self._row_t: float | None = None
        self.sweep_period = 0.05
        self._period_fresh = True       # take the first interval as is
        self._placed_period = 0.0
        self._last_frame_t: float | None = None
        self._frames = 0
        self._csv = None
        self._csv_last = 0.0
        self._csv_t0: float | None = None
        self._verbose_last = 0.0
        self._x_refitted = False
        # The span the program was started with is what ALL returns to.
        self._full_span = (settings.start_mhz, settings.stop_mhz)
        self._span_name = "ALL"
        # Connection: the user's intent (connect / stay disconnected) and what
        # the receiver last reported.
        self._want_connected = True
        self._conn_state = "connecting"

        self.setWindowTitle("FPV band monitor - " + title
                            + ("  [SIM]" if simulated else ""))
        self.resize(1280, 860)
        self.setMinimumSize(560, 420)
        self._build_ui()
        self._start_reader()

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(40)

        if opts.csv_path:
            self._csv = open(opts.csv_path, "w", encoding="utf-8", newline="")
            self._csv.write("elapsed_s,channel,nominal_mhz,centre_mhz,offset_mhz,"
                            "peak_dbm,snr_db,bw3_mhz,bw20_mhz\n")

    # ----------------------------------------------------------------- UI --
    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(8, 6, 8, 8)
        root.setSpacing(6)
        root.addLayout(self._build_toolbar())

        self.glw = pg.GraphicsLayoutWidget()
        self.glw.setMinimumHeight(160)
        root.addWidget(self.glw, stretch=1)

        self.p_spec = self.glw.addPlot(row=0, col=0)
        self.p_spec.setLabel("left", "power (dBFS, uncalibrated)")
        self.p_spec.showGrid(x=True, y=True, alpha=0.25)
        self.p_spec.setMouseEnabled(x=True, y=True)
        self.p_spec.setXRange(self.grid.start_mhz, self.grid.stop_mhz, padding=0)
        self.p_spec.setYRange(self.opts.y_min_dbm, self.opts.y_max_dbm,
                              padding=0)

        self.p_wf = self.glw.addPlot(row=1, col=0)
        self.p_wf.setLabel("left", "age", units="s")
        self.p_wf.setLabel("bottom", "frequency", units="MHz")
        self.p_wf.getAxis("bottom").enableAutoSIPrefix(False)
        self.p_wf.setMouseEnabled(x=True, y=False)
        # Give the waterfall its own x range *before* linking: a setYRange on a
        # linked child whose x is still at the default pushes that default back
        # onto the parent, which collapses both plots to a 1 MHz window.
        self.p_wf.setXRange(self.grid.start_mhz, self.grid.stop_mhz, padding=0)
        self.p_wf.setYRange(-self._wf_seconds(), 0, padding=0)
        self.p_wf.setXLink(self.p_spec)
        self.glw.ci.layout.setRowStretchFactor(0, 4)
        self.glw.ci.layout.setRowStretchFactor(1, 5)

        self.img = pg.ImageItem()
        self.img.setLookupTable(_lut())
        self.p_wf.addItem(self.img)
        self.wf = np.full((self.opts.waterfall_rows, self.grid.n), -120.0,
                          dtype=np.float32)

        self.curve = self.p_spec.plot(pen=pg.mkPen("#6e6e86", width=1),
                                      connect="finite")
        self.curve_avg = self.p_spec.plot(pen=pg.mkPen("#f2f2f8", width=2),
                                          connect="finite")
        self.curve_hold = self.p_spec.plot(pen=pg.mkPen("#7a7a92", width=1,
                                                        style=QtCore.Qt.PenStyle.DashLine),
                                           connect="finite")
        self.curve_hold.setVisible(False)

        self._build_markers()

        # Keep the view inside the swept range. Without this the x range can
        # creep outwards as items are added and the layout settles, leaving the
        # data in a shrinking island; zooming and panning within the band still
        # work.
        self.p_spec.setXRange(self.grid.start_mhz, self.grid.stop_mhz, padding=0)
        for plot in (self.p_spec, self.p_wf):
            plot.vb.disableAutoRange(pg.ViewBox.XAxis)
            plot.vb.setLimits(xMin=self.grid.start_mhz, xMax=self.grid.stop_mhz)

        self.readout_row = QtWidgets.QHBoxLayout()
        self.readout_row.setSpacing(6)
        self.readouts: dict[str, ChannelReadout] = {}
        self._build_readouts()
        root.addLayout(self.readout_row)

    def _build_readouts(self) -> None:
        for ro in self.readouts.values():
            self.readout_row.removeWidget(ro)
            ro.deleteLater()
        self.readouts = {}
        for ch, color in zip(self.channels, self.colors):
            ro = ChannelReadout(ch, color, self.opts.dev_ok_mhz, self.opts.dev_warn_mhz,
                                on_click=self._readout_clicked)
            self.readouts[ch.name] = ro
            self.readout_row.addWidget(ro)

    def _build_toolbar(self) -> QtWidgets.QLayout:
        bar = FlowLayout(spacing=6)

        def label(text):
            lab = QtWidgets.QLabel(text)
            lab.setStyleSheet("color: #9a9aac;")
            return lab

        # Port picker: device name next to each COM port; Espressif USB ports
        # are tagged. "auto" = first Espressif port, as at start-up.
        self.cmb_port = PortCombo(self._refresh_ports)
        self.cmb_port.setMinimumWidth(260)
        self.cmb_port.setToolTip("COM port of the ESP32-C5 (list is re-read "
                                 "each time it is opened)")
        self.btn_connect = QtWidgets.QPushButton("Disconnect")
        self.btn_connect.setMinimumWidth(90)
        self.btn_connect.clicked.connect(self._toggle_connect)
        self.conn_label = QtWidgets.QLabel("connecting...")
        self.conn_label.setMinimumWidth(150)
        self._refresh_ports()

        # Receiver: auto (FFT'd sweeps from the LCD firmware if it answers,
        # else I/Q), iq (FFT here), fft (the firmware's sweeps). No LCD: in
        # fft, the chip stops drawing and only sweeps.
        rc = self.opts.receiver
        self.rx_group = QtWidgets.QButtonGroup(self)
        self.rx_group.setExclusive(True)
        self.rx_buttons = {}
        for name, tip in (("auto", "LCD firmware -> fft, otherwise iq"),
                          ("iq", "raw I/Q, FFT on the PC (any ESP-SDR firmware)"),
                          ("fft", "finished sweeps from the LCD firmware "
                                  "(78.125 kHz bins, less USB traffic)")):
            b = QtWidgets.QPushButton(name)
            b.setCheckable(True)
            b.setMinimumWidth(40)
            b.setToolTip(tip)
            b.setStyleSheet("QPushButton:checked { background: #d8d8e4;"
                            " color: #101018; font-weight: bold; }")
            b.setChecked(rc is not None and rc.choice == name)
            b.clicked.connect(lambda _=False, n=name: self._select_receiver(n))
            self.rx_group.addButton(b)
            self.rx_buttons[name] = b
        self.rx_label = QtWidgets.QLabel("")
        self.rx_label.setMinimumWidth(36)
        self.chk_nolcd = QtWidgets.QCheckBox("No LCD")
        self.chk_nolcd.setToolTip(
            "fft only: the LCD goes dark and the chip spends every cycle "
            "sweeping (a tap on the screen brings it back)")
        self.chk_nolcd.setChecked(rc is not None and rc.nolcd)
        self.chk_nolcd.toggled.connect(self._set_nolcd)
        self._show_receiver()

        # One PHY gain-table index; -1 hands gain to the chip's AGC.
        self.spin_gain = QtWidgets.QSpinBox()
        self.spin_gain.setRange(-1, 127)
        self.spin_gain.setSingleStep(4)
        self.spin_gain.setValue(self.settings.gain)
        self.spin_gain.setSpecialValueText("AGC")
        self.spin_gain.setToolTip("ESP32 gain-table index (not dB); "
                                  "the device clamps it to its maximum")
        btn_apply = QtWidgets.QPushButton("Apply gain")
        btn_apply.clicked.connect(self._apply_gains)

        self.btn_pause = QtWidgets.QPushButton("Pause")
        self.btn_pause.setCheckable(True)
        self.btn_pause.toggled.connect(self._toggle_pause)

        self.chk_hold = QtWidgets.QCheckBox("Peak hold")
        self.chk_hold.toggled.connect(self._toggle_hold)

        btn_clear = QtWidgets.QPushButton("Clear")
        btn_clear.clicked.connect(self._clear)

        self.chk_hops = QtWidgets.QCheckBox("Hops")
        self.chk_hops.setChecked(self.opts.show_hops)
        self.chk_hops.setToolTip(
            "Dotted lines at each hop's LO. Its DC spike is cut out and "
            "filled from the neighbouring hops.")
        self.chk_hops.toggled.connect(self._set_show_hops)

        # Spectrum dBm axis: auto-follows the floor, or is pinned to the two
        # spin boxes so the trace can be compared between runs.
        self.chk_y_auto = QtWidgets.QCheckBox("Auto Y")
        self.chk_y_auto.setChecked(self.opts.y_auto)
        self.chk_y_auto.setToolTip(
            "Off = fix the dBm axis to the values on the right")
        self.spin_y_min = QtWidgets.QSpinBox()
        self.spin_y_min.setRange(-140, 30)
        self.spin_y_min.setValue(int(self.opts.y_min_dbm))
        self.spin_y_max = QtWidgets.QSpinBox()
        self.spin_y_max.setRange(-140, 30)
        self.spin_y_max.setValue(int(self.opts.y_max_dbm))
        for sp in (self.spin_y_min, self.spin_y_max):
            sp.setSuffix(" dBm")
            sp.setSingleStep(5)
            sp.valueChanged.connect(self._apply_y_range)
        self.chk_y_auto.toggled.connect(self._set_y_auto)

        self.chk_auto = QtWidgets.QCheckBox("Auto level")
        self.chk_auto.setChecked(True)
        self.spin_wf_min = QtWidgets.QSpinBox()
        self.spin_wf_min.setRange(-130, 0)
        self.spin_wf_min.setValue(-95)
        self.spin_wf_max = QtWidgets.QSpinBox()
        self.spin_wf_max.setRange(-130, 20)
        self.spin_wf_max.setValue(-35)

        # Detection threshold, live: a weak or a saturating VTX is rescued
        # here instead of by restarting with --min-snr.
        self.spin_snr = QtWidgets.QDoubleSpinBox()
        self.spin_snr.setRange(1.0, 60.0)
        self.spin_snr.setSingleStep(1.0)
        self.spin_snr.setDecimals(0)
        self.spin_snr.setSuffix(" dB")
        self.spin_snr.setValue(self.opts.min_snr_db)
        self.spin_snr.setToolTip(
            "Peak must be this far above the noise floor to count as a signal")
        self.spin_snr.valueChanged.connect(self._set_min_snr)

        self.status = QtWidgets.QLabel("starting...")
        self.status.setStyleSheet("color: #8a8a9a;")

        widths = sorted(self.opts.bw_marks_mhz)
        names = ["inner", "outer"] + [""] * max(0, len(widths) - 2)
        shades = "   ".join(f"{w:g} MHz = {n} shade"
                           for w, n in zip(widths, names))
        self.hint = label(f"marks:  {shades}   dotted = hop LOs")
        hint = self.hint

        # Groups (as on the LCD): the channels shown; each has its Wi-Fi check.
        group_widgets = []
        self.group_buttons: dict[str, QtWidgets.QPushButton] = {}
        if self.opts.span_planner is not None:
            self.grp_group = QtWidgets.QButtonGroup(self)
            self.grp_group.setExclusive(True)
            group_widgets.append(label("Group"))
            for name, spec in GROUPS.items():
                b = QtWidgets.QPushButton(name)
                b.setCheckable(True)
                b.setChecked(name == self.group_name)
                b.setMinimumWidth(44)
                b.setToolTip(spec.replace(",", " "))
                b.setStyleSheet("QPushButton:checked { background: #d8d8e4;"
                                " color: #101018; font-weight: bold; }")
                b.clicked.connect(lambda _=False, n=name: self._select_group(n))
                self.grp_group.addButton(b)
                self.group_buttons[name] = b
                group_widgets.append(b)
            self.btn_wifi = QtWidgets.QPushButton("WiFi check")
            self.btn_wifi.setToolTip(
                "Listen (receive only) on the Wi-Fi channels that can hurt this "
                "group: Direct = on a channel, IMD = a 3rd-order product with a "
                "VTX lands on one. Needs the LCD firmware; sweeping pauses meanwhile.")
            self.btn_wifi.clicked.connect(self._open_wifi)
            group_widgets += [self.btn_wifi, label("|")]

        # Sweep span: one button per channel of the group (its centre +-
        # margin, a few hops, so several times faster) plus ALL.
        span_widgets = []
        if self.opts.span_planner is not None:
            self.span_box = QtWidgets.QWidget()
            self.span_box_lay = QtWidgets.QHBoxLayout(self.span_box)
            self.span_box_lay.setContentsMargins(0, 0, 0, 0)
            self.span_box_lay.setSpacing(6)
            self.span_buttons: dict[str, QtWidgets.QPushButton] = {}
            self._build_span_buttons()
            span_widgets += [label("Span"), self.span_box, label("|")]

        # The ADC saturates (too much gain, or a VTX very close): LCD firmware only.
        self.sat_label = QtWidgets.QLabel("")
        self.sat_label.setStyleSheet("background: #ff3030; color: #f0f0f6;"
                                     " font-weight: bold; padding: 1px 6px;")
        self.sat_label.setToolTip("the ADC clips: lower the gain")
        self.sat_label.setVisible(False)

        rx_widgets = []
        if rc is not None:
            rx_widgets = [label("Rx"), *self.rx_buttons.values(), self.rx_label,
                          self.chk_nolcd, label("|")]
        for w in (label("Port"), self.cmb_port, self.btn_connect,
                  self.conn_label, label("|"), *rx_widgets, *group_widgets,
                  *span_widgets, label("Gain idx"), self.spin_gain, btn_apply,
                  self.sat_label,
                  self.btn_pause, self.chk_hold, btn_clear, self.chk_hops,
                  label("| detect SNR >="), self.spin_snr,
                  label("| Y"), self.chk_y_auto, self.spin_y_min,
                  self.spin_y_max,
                  label("| WF"), self.chk_auto,
                  self.spin_wf_min, self.spin_wf_max):
            bar.addWidget(w)
        bar.addWidget(hint)
        bar.addWidget(self.status)
        if self.simulated:
            for w in (self.spin_gain, btn_apply, self.cmb_port, self.chk_nolcd,
                      *self.rx_buttons.values()):
                w.setEnabled(False)
            if self.opts.span_planner is not None:
                self.btn_wifi.setEnabled(False)
        return bar

    # --------------------------------------------------------------- port --
    def _refresh_ports(self) -> None:
        """Rebuild the list, keeping the current choice selected."""
        if self.simulated:
            self.cmb_port.clear()
            self.cmb_port.addItem("SIM (simulated ESP32-C5)", None)
            return
        current = (self.cmb_port.currentData() if self.cmb_port.count()
                   else self.settings.port)
        self.cmb_port.blockSignals(True)
        self.cmb_port.clear()
        self.cmb_port.addItem("auto (first Espressif USB)", None)
        found = False
        for p in serial_ports():
            esp = p.vid == ESPRESSIF_VID
            text = f"{p.device}   {p.description}" + ("   [ESP32]" if esp else "")
            self.cmb_port.addItem(text, p.device)
            vidpid = (f"{p.vid:04X}:{p.pid:04X}" if p.vid is not None
                      else "no USB ID")
            self.cmb_port.setItemData(
                self.cmb_port.count() - 1,
                f"{p.device}\n{p.description}\nUSB {vidpid}"
                f"\nmanufacturer: {p.manufacturer or '-'}"
                f"\nserial: {p.serial_number or '-'}",
                QtCore.Qt.ItemDataRole.ToolTipRole)
            found = found or p.device == current
        if current and not found:
            # Keep a chosen port that is unplugged right now, marked as such.
            self.cmb_port.addItem(f"{current}   (not present)", current)
        idx = self.cmb_port.findData(current)
        self.cmb_port.setCurrentIndex(max(idx, 0))
        self.cmb_port.blockSignals(False)

    def _set_conn_state(self, state: str, text: str, tip: str = "") -> None:
        colors = {"connected": "#45d97a", "connecting": "#ffbd2e",
                  "disconnected": "#8a8a9a", "error": "#ff5f56"}
        self._conn_state = state
        self.conn_label.setText(text)
        self.conn_label.setToolTip(tip or text)
        self.conn_label.setStyleSheet(f"color: {colors[state]};")
        live = state in ("connected", "connecting")
        self.btn_connect.setText("Disconnect" if live else "Connect")

    def _toggle_connect(self) -> None:
        if self._conn_state in ("connected", "connecting"):
            self._disconnect("disconnected")
        else:
            self._want_connected = True
            self.settings.port = self.cmb_port.currentData()
            self._restart_reader(resolve=True)

    def _disconnect(self, text: str, state: str = "disconnected",
                    tip: str = "") -> None:
        self._want_connected = False
        if self.reader is not None:
            self.reader.stop()
            self.reader = None
        self._set_conn_state(state, text, tip)
        self.status.setText("not connected - choose a port and press Connect")

    def _build_hop_lines(self) -> None:
        """Mark each hop's LO: its DC spike is cut out there, so a peak that
        lands on one of these deserves a second look."""
        for plot, line in getattr(self, "hop_line_items", []):
            plot.removeItem(line)  # rebuilt when the span changes
        self.hop_lines: list[pg.InfiniteLine] = []
        self.hop_line_items: list[tuple] = []
        pen = pg.mkPen(QtGui.QColor(165, 172, 200, 170), width=1,
                       style=QtCore.Qt.PenStyle.DotLine)
        for f in self.opts.hop_los_mhz:
            if not (self.grid.start_mhz <= f <= self.grid.stop_mhz):
                continue
            for plot in (self.p_spec, self.p_wf):
                line = pg.InfiniteLine(pos=f, angle=90, pen=pen)
                # Above the channel shading, below the channel markers.
                line.setZValue(-15)
                line.setVisible(self.opts.show_hops)
                plot.addItem(line)
                self.hop_lines.append(line)
                self.hop_line_items.append((plot, line))

    def _set_show_hops(self, on: bool) -> None:
        self.opts.show_hops = on
        for line in self.hop_lines:
            line.setVisible(on)

    def _build_markers(self) -> None:
        """Band-width shading and centre lines, on both plots."""
        self._build_hop_lines()
        for plot, item in self._marker_items:
            plot.removeItem(item)
        self._marker_items = []

        def add(plot, item):
            plot.addItem(item)
            self._marker_items.append((plot, item))

        self.line_measured: dict[str, list[pg.InfiniteLine]] = {}
        marks = sorted(self.opts.bw_marks_mhz, reverse=True)  # widest first
        for ch, ch_color in zip(self.channels, self.colors):
            color = QtGui.QColor(ch_color)
            for depth, bw in enumerate(marks):
                alpha = 26 + depth * 26
                brush = QtGui.QColor(color)
                brush.setAlpha(alpha)
                pen = QtGui.QColor(color)
                pen.setAlpha(150)
                half = bw / 2.0
                for plot in (self.p_spec, self.p_wf):
                    region = pg.LinearRegionItem(
                        values=(ch.freq_mhz - half, ch.freq_mhz + half),
                        movable=False, brush=brush,
                        pen=pg.mkPen(pen, width=1,
                                     style=QtCore.Qt.PenStyle.DashLine))
                    region.setZValue(-20 - depth)
                    add(plot, region)
            # Nominal centre.
            for plot in (self.p_spec, self.p_wf):
                nominal = pg.InfiniteLine(
                    pos=ch.freq_mhz, angle=90,
                    pen=pg.mkPen(color, width=1,
                                 style=QtCore.Qt.PenStyle.DotLine))
                nominal.setZValue(-10)
                add(plot, nominal)
            # Measured centre (moves).
            lines = []
            label_opts = {"position": 0.95, "color": color.name(),
                          "fill": (10, 10, 16, 190), "movable": False}
            for plot, with_label in ((self.p_spec, True), (self.p_wf, False)):
                line = pg.InfiniteLine(
                    pos=ch.freq_mhz, angle=90,
                    pen=pg.mkPen(color, width=2),
                    label=(f"{ch.name} ----.--" if with_label else None),
                    labelOpts=label_opts if with_label else None)
                line.setZValue(10)
                add(plot, line)
                lines.append(line)
            self.line_measured[ch.name] = lines

    def showEvent(self, event) -> None:
        """Re-fit x once the layout has settled.

        The axis furniture (tick labels, the dBm label) is not measured until
        the window is laid out, and the plot area shrinking afterwards takes a
        few MHz off the view. Re-applying the range once fixes the start-up
        width; the user's own pan and zoom are untouched after that.
        """
        super().showEvent(event)
        if not self._x_refitted:
            self._x_refitted = True
            QtCore.QTimer.singleShot(300, self._fit_x)

    def _fit_x(self) -> None:
        self.p_spec.setXRange(self.grid.start_mhz, self.grid.stop_mhz, padding=0)

    def resizeEvent(self, event) -> None:
        """Drop the marker legend on a narrow window; the toolbar itself wraps."""
        super().resizeEvent(event)
        if hasattr(self, "hint"):
            self.hint.setVisible(self.width() >= 900)

    def _row_per_sweep(self) -> bool:
        """fft (the LCD firmware's own sweeps, a few per second): one
        waterfall row per sweep instead of one per waterfall_row_ms."""
        rc = self.opts.receiver
        return rc is not None and not self.simulated and rc.backend == "fft"

    def _row_period(self) -> float:
        if self._row_per_sweep():
            return self.sweep_period
        return self.opts.waterfall_row_ms / 1000.0

    def _wf_seconds(self) -> float:
        return self.opts.waterfall_rows * self._row_period()

    def _place_image(self) -> None:
        """Map the image onto MHz / seconds. Only works once it has data:
        ImageItem.setRect scales by the current image size."""
        span = self.grid.stop_mhz - self.grid.start_mhz
        total = self._wf_seconds()
        self.img.setRect(QtCore.QRectF(self.grid.start_mhz, -total, span, total))
        self.p_wf.setYRange(-total, 0, padding=0)
        self._placed_period = self._row_period()
        self._image_placed = True

    # ------------------------------------------------------------- reader --
    def _start_reader(self) -> None:
        if not self._want_connected:
            return  # stay disconnected; settings apply on the next Connect
        self._set_conn_state("connecting",
                             f"connecting {self.settings.port or 'auto'}...")
        self.reader = SweepReader(self.settings, self.grid,
                                  self.source_factory(self.settings), self.queue)
        self.reader.start()

    def _stop_reader(self) -> None:
        if self.reader is not None:
            self.reader.stop()
            self.reader = None
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break

    def _restart_reader(self, resolve: bool = False) -> None:
        self._stop_reader()
        if resolve:
            self._resolve_receiver()
        # the sweep rate changes with the receiver (iq / fft, LCD on / off)
        self._last_frame_t = None
        self._period_fresh = True
        self._image_placed = False
        self._start_reader()

    # ----------------------------------------------------------- receiver --
    def _show_receiver(self) -> None:
        rc = self.opts.receiver
        if rc is None:
            return
        self.rx_label.setText("-> " + rc.backend if rc.choice == "auto" else "")
        self.rx_label.setToolTip(f"receiving: {rc.label()}")
        self.chk_nolcd.setEnabled(not self.simulated and rc.backend == "fft")

    def _resolve_receiver(self) -> None:
        """Settle auto/iq/fft on the port (the reader is stopped: the port is
        free for the FPV? probe) and re-plan the grid if the bins changed."""
        rc = self.opts.receiver
        if rc is None or self.simulated or not self._want_connected:
            return
        old_bin = self.grid.bin_hz
        rc.resolve(port=self.settings.port)
        self.source_factory = rc.source_factory(self.channels)
        self.opts.span_planner = rc.span_planner()
        self.settings.bin_hz = rc.bin_hz()
        if abs(rc.bin_hz() - old_bin) > 1e-6:
            self._replan(self.settings.start_mhz, self.settings.stop_mhz)
        self._show_receiver()

    def _select_receiver(self, name: str) -> None:
        rc = self.opts.receiver
        if rc is None:
            return
        rc.choice = name
        if self._want_connected:
            self._set_conn_state("connecting", f"{name}: probing...")
            QtWidgets.QApplication.processEvents()
            self._restart_reader(resolve=True)
            self.status.setText(f"receiver {rc.label()}")
        else:
            self._show_receiver()

    def _set_nolcd(self, on: bool) -> None:
        rc = self.opts.receiver
        if rc is None or rc.nolcd == on:
            return
        rc.nolcd = on
        self.source_factory = rc.source_factory(self.channels)
        if rc.backend == "fft" and self._want_connected:
            self._restart_reader()
        self._show_receiver()

    # --------------------------------------------------------------- span --
    def _replan(self, start: float, stop: float) -> None:
        """New sweep span or bin width: new grid, everything on it restarts."""
        self.settings.start_mhz, self.settings.stop_mhz = start, stop
        self.grid, self.opts.hop_los_mhz = self.opts.span_planner(start, stop)

        # Everything sized or averaged on the old grid starts over.
        self.wf = np.full((self.opts.waterfall_rows, self.grid.n), -120.0,
                          dtype=np.float32)
        self.img.clear()
        self._image_placed = False
        self._row_accum = None
        self._row_t = None
        self._wf_top = None
        self._last_frame_t = None
        self._period_fresh = True
        self.averager.reset()
        self.smoother = Smoother(self.opts.smooth_alpha)
        self.peak_hold = None
        self.last_powers = None
        for c in (self.curve, self.curve_avg, self.curve_hold):
            c.setData([], [])

        for plot in (self.p_spec, self.p_wf):
            plot.vb.setLimits(xMin=self.grid.start_mhz, xMax=self.grid.stop_mhz)
        self.p_spec.setXRange(self.grid.start_mhz, self.grid.stop_mhz, padding=0)
        self._build_hop_lines()

    def _build_span_buttons(self) -> None:
        for b in self.span_buttons.values():
            self.span_box_lay.removeWidget(b)
            b.deleteLater()
        self.span_buttons = {}
        self.span_group = QtWidgets.QButtonGroup(self)
        self.span_group.setExclusive(True)
        for name, color in [(c.name, col) for c, col in zip(self.channels, self.colors)] + [("ALL", "#d8d8e4")]:
            b = QtWidgets.QPushButton(name)
            b.setCheckable(True)
            b.setChecked(name == self._span_name)
            b.setMinimumWidth(44)
            b.setStyleSheet(f"QPushButton:checked {{ background: {color};"
                            " color: #101018; font-weight: bold; }")
            b.clicked.connect(lambda _=False, n=name: self._select_span(n))
            self.span_group.addButton(b)
            self.span_box_lay.addWidget(b)
            self.span_buttons[name] = b
        self.span_box.adjustSize()

    def _readout_clicked(self, name: str) -> None:
        """A card zooms into its channel; the same card again goes back to ALL."""
        if self.opts.span_planner is None:
            return
        self._select_span("ALL" if self._span_name == name else name)

    def _select_group(self, name: str) -> None:
        """Show another group: its channels, colours, cards and span."""
        if name == self.group_name and self.channels:
            return
        self.group_name = name
        self.channels = parse_channels(GROUPS[name])
        self.colors = rank_colors(self.channels)
        self.opts.search_mhz = auto_search_halfwidth(self.channels,
                                                     self.opts.search_request_mhz)
        half = self.opts.channel_span_half_mhz
        freqs = [c.freq_mhz for c in self.channels]
        self._full_span = (min(freqs) - half, max(freqs) + half)
        self._span_name = "ALL"
        rc = self.opts.receiver
        if rc is not None:
            self.source_factory = rc.source_factory(self.channels)
        self._stop_reader()
        self._replan(*self._full_span)
        self._build_markers()
        self._build_readouts()
        self._build_span_buttons()
        self._start_reader()
        self.status.setText(f"group {name}: " + " ".join(c.name for c in self.channels))

    def pause_receiver(self) -> None:
        """Free the port (the Wi-Fi check talks to the device itself)."""
        self._paused_for_wifi = self.reader is not None
        self._stop_reader()
        if self._paused_for_wifi:
            self._set_conn_state("connecting", "paused: WiFi check")

    def resume_receiver(self) -> None:
        if self._paused_for_wifi and self._want_connected:
            self._restart_reader()
        self._paused_for_wifi = False

    def _open_wifi(self) -> None:
        dlg = WifiDialog(self)
        dlg.show()

    def _select_span(self, name: str) -> None:
        """Sweep one channel (centre +- margin) or ALL (the starting span)."""
        if name == self._span_name:
            return
        if name in getattr(self, "span_buttons", {}):
            self.span_buttons[name].setChecked(True)
        if name == "ALL":
            start, stop = self._full_span
        else:
            ch = next(c for c in self.channels if c.name == name)
            half = self.opts.channel_span_half_mhz
            start, stop = ch.freq_mhz - half, ch.freq_mhz + half
        self._span_name = name
        self._stop_reader()
        self._replan(start, stop)
        self._start_reader()
        self.status.setText(f"span {name}: {self.grid.start_mhz:.0f}-"
                            f"{self.grid.stop_mhz:.0f} MHz, "
                            f"{len(self.opts.hop_los_mhz)} hops - restarting")

    def _apply_gains(self) -> None:
        self.settings.gain = self.spin_gain.value()
        self._restart_reader()
        self.status.setText("restarted with new gain" if self._want_connected
                            else "gain set - applies on the next Connect")

    def _set_min_snr(self, value: float) -> None:
        self.opts.min_snr_db = float(value)

    def _set_spec_y(self, lo: float, hi: float) -> None:
        """Set the dBm axis and keep the spin boxes showing what is on screen."""
        self.p_spec.setYRange(lo, hi, padding=0)
        self._sync_y_spins(lo, hi)

    def _sync_y_spins(self, lo: float, hi: float) -> None:
        for sp, v in ((self.spin_y_min, lo), (self.spin_y_max, hi)):
            sp.blockSignals(True)
            sp.setValue(int(round(v)))
            sp.blockSignals(False)
        self.opts.y_min_dbm = float(self.spin_y_min.value())
        self.opts.y_max_dbm = float(self.spin_y_max.value())

    def _set_y_auto(self, on: bool) -> None:
        """Freeze the axis where it is now, or hand it back to auto."""
        self.opts.y_auto = on
        if not on:
            lo, hi = self.p_spec.viewRange()[1]
            self._sync_y_spins(lo, hi)

    def _apply_y_range(self) -> None:
        lo = float(self.spin_y_min.value())
        hi = float(self.spin_y_max.value())
        if hi <= lo:
            hi = lo + 5.0
            self.spin_y_max.blockSignals(True)
            self.spin_y_max.setValue(int(hi))
            self.spin_y_max.blockSignals(False)
        self.opts.y_min_dbm, self.opts.y_max_dbm = lo, hi
        if self.chk_y_auto.isChecked():
            # Leave auto mode without letting _set_y_auto write the old view
            # range back over the value that was just typed.
            self.chk_y_auto.blockSignals(True)
            self.chk_y_auto.setChecked(False)
            self.chk_y_auto.blockSignals(False)
            self.opts.y_auto = False
        self.p_spec.setYRange(lo, hi, padding=0)

    def _toggle_pause(self, on: bool) -> None:
        self.paused = on
        self.btn_pause.setText("Resume" if on else "Pause")

    def _toggle_hold(self, on: bool) -> None:
        self.peak_hold = None
        self.curve_hold.setVisible(on)

    def _clear(self) -> None:
        self.averager.reset()
        self.wf[:] = -120.0
        self.peak_hold = None
        self._wf_top = None
        self._row_accum = None
        self.img.setImage(self.wf, autoLevels=False,
                          levels=self._wf_levels())

    def _wf_levels(self) -> tuple[float, float]:
        if self.chk_auto.isChecked() and self.last_powers is not None:
            finite = self.last_powers[np.isfinite(self.last_powers)]
            if finite.size:
                floor = float(np.median(finite))
                # Track the loudest carrier so strong signals keep their shape
                # instead of saturating the top of the colour map.
                top = float(np.max(finite))
                self._wf_top = (top if self._wf_top is None
                                else self._wf_top + 0.2 * (top - self._wf_top))
                return floor - 3.0, max(self._wf_top + 3.0, floor + 25.0)
        lo = float(self.spin_wf_min.value())
        hi = float(self.spin_wf_max.value())
        return (lo, hi) if hi > lo else (lo, lo + 10.0)

    # --------------------------------------------------------------- tick --
    def _tick(self) -> None:
        frames: list[Frame] = []
        while True:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, DeviceInfo):
                self._set_conn_state("connected", f"connected {item.port}",
                                     f"{item.port}: {item.identity}")
                continue
            if isinstance(item, DeviceStatus):
                self._clip = item.clip_permille
                sat = self._clip >= 10
                self.sat_label.setVisible(sat)
                if sat:
                    self.sat_label.setText(f"SAT {(self._clip + 5) // 10}%")
                continue
            if isinstance(item, Exception):
                # Not reachable at start-up, or unplugged: drop to the
                # disconnected state and let the user pick a port.
                msg = str(item)
                self._disconnect("not connected: " + msg.splitlines()[0][:60],
                                 state="error", tip=msg)
                return
            frames.append(item)

        if not frames or self.paused:
            return

        per_sweep = self._row_per_sweep()
        for fr in frames:
            if self._last_frame_t is not None:
                dt = fr.t - self._last_frame_t
                if 0.0005 < dt < 5.0:
                    if self._period_fresh:
                        self.sweep_period = dt
                        self._period_fresh = False
                    else:
                        self.sweep_period += 0.1 * (dt - self.sweep_period)
            self._last_frame_t = fr.t
            row = np.nan_to_num(fr.powers, nan=-120.0, posinf=-120.0,
                                neginf=-120.0)
            if per_sweep:
                self.wf[:-1] = self.wf[1:]
                self.wf[-1] = row
                continue
            # One waterfall row per waterfall_row_ms, holding the peak of the
            # sweeps that fell in it: a short burst cannot slip between rows.
            self._row_accum = (row if self._row_accum is None
                               else np.fmax(self._row_accum, row))
            period = self.opts.waterfall_row_ms / 1000.0
            if self._row_t is None:
                self._row_t = fr.t
            elif fr.t - self._row_t >= period:
                repeats = min(int((fr.t - self._row_t) / period), 8)
                for _ in range(repeats):
                    self.wf[:-1] = self.wf[1:]
                    self.wf[-1] = self._row_accum
                self._row_t = fr.t
                self._row_accum = None
        self._frames += len(frames)
        # the time axis follows the measured sweep rate
        if (per_sweep and self._image_placed
                and abs(self.sweep_period - self._placed_period) > 0.1 * self._placed_period):
            self._place_image()

        latest = frames[-1]
        self.last_powers = latest.powers
        self._draw(latest)

    def _draw(self, frame: Frame) -> None:
        powers = frame.powers
        # The live sweep is drawn dim; measurements run on the time average,
        # which is the bright trace.
        averaged = self.averager.add(frame.t, powers)
        self.curve.setData(self.grid.freqs_mhz, powers)
        self.curve_avg.setData(self.grid.freqs_mhz, averaged)
        if self.chk_hold.isChecked():
            self.peak_hold = (powers.copy() if self.peak_hold is None
                              else np.fmax(self.peak_hold, powers))
            self.curve_hold.setData(self.grid.freqs_mhz, self.peak_hold)

        self.img.setImage(self.wf, autoLevels=False, levels=self._wf_levels())
        if not self._image_placed:
            self._place_image()

        ms = measure_all(averaged, self.grid, self.channels,
                         self.opts.search_mhz, self.opts.min_snr_db,
                         self.opts.noise_margin_db, self.opts.min_bw_mhz,
                         self.opts.centroid_half_mhz)
        for m in ms:
            smoothed = self.smoother.update(m)
            self.readouts[m.name].update_values(m, smoothed)
            lines = self.line_measured[m.name]
            if smoothed is not None:
                for line in lines:
                    line.setPos(smoothed)
                lab = lines[0].label
                if lab is not None:
                    lab.setFormat(f"{m.name} {smoothed:.2f}"
                                  + ("" if m.present else " (hold)"))
        self._log_csv(frame, ms)
        if self.opts.verbose and frame.t - self._verbose_last >= 1.0:
            self._verbose_last = frame.t
            print(f"[{frame.sweep_index:5d}] {format_status(ms, self.smoother)}",
                  flush=True)

        floor = ms[0].floor_dbm if ms else float("nan")
        if np.isfinite(floor) and self.chk_y_auto.isChecked():
            top = max([m.peak_dbm for m in ms if np.isfinite(m.peak_dbm)] or [floor])
            self._set_spec_y(floor - 8, max(top + 8, floor + 30))
        rate = 1.0 / self.sweep_period if self.sweep_period > 0 else 0.0
        self.status.setText(
            f"span {self._span_name}   {rate:5.1f} sweeps/s   bin {self.grid.bin_hz/1e3:.1f} kHz   "
            f"avg "
            f"{self.opts.avg_s:.1f}s ({self.averager.n_frames} sweeps)   "
            f"floor {floor:6.1f} dBm   sweeps {frame.sweep_index}")

    def _log_csv(self, frame: Frame, ms: list[Measurement]) -> None:
        if self._csv is None:
            return
        now = time.monotonic()
        if now - self._csv_last < self.opts.csv_interval_s:
            return
        self._csv_last = now
        if self._csv_t0 is None:
            self._csv_t0 = frame.t
        elapsed = frame.t - self._csv_t0
        for m in ms:
            if not m.present:
                continue
            b3 = f"{m.bw_db3_mhz:.3f}" if m.bw_db3_mhz is not None else ""
            b20 = f"{m.bw_db20_mhz:.3f}" if m.bw_db20_mhz is not None else ""
            self._csv.write(
                f"{elapsed:.3f},{m.name},{m.nominal_mhz:.3f},"
                f"{m.centre_mhz:.4f},{m.offset_mhz:+.4f},{m.peak_dbm:.2f},"
                f"{m.snr_db:.2f},{b3},{b20}\n")
        self._csv.flush()

    def closeEvent(self, event) -> None:
        self.timer.stop()
        if self.reader is not None:
            self.reader.stop()
        if self._csv is not None:
            self._csv.close()
        super().closeEvent(event)


def run_gui(channels: list[Channel], settings: SweepSettings,
            grid: FrequencyGrid, source_factory, opts: ViewOptions,
            simulated: bool = False, title: str = "ESP32-C5") -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = BandMonitorWindow(channels, settings, grid, source_factory, opts,
                            simulated, title)
    win.show()
    return app.exec()
