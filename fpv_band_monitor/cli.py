"""Command line entry point for the ESP32-C5 FPV band monitor."""

from __future__ import annotations

import argparse
import queue
import sys
import time
from pathlib import Path

from .analysis import (Smoother, SpectrumAverager, auto_search_halfwidth,
                       format_status, measure_all)
from .channels import DEFAULT_CHANNELS, format_table, parse_channels
from .esp_sdr import EspSettings, Stitcher, process_source
from .esp_sdr import probe as esp_probe
from .sweep import DeviceInfo, FrequencyGrid, SweepReader, SweepSettings

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "band_monitor.toml"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="band_monitor",
        description="ESP32-C5 (ESP-SDR ファーム) で FPV 5.8GHz の映像チャンネルを"
                    "監視する。スペクトラム・ウォーターフォール・実測中心周波数。",
        epilog="設定ファイル: 既定で band_monitor.toml (band_monitor.py と同じ"
               "フォルダ) を読む。キーはオプション名 (例 port = \"COM14\")。"
               "コマンドラインの指定が設定ファイルより優先。",
        add_help=False,
        # No prefix matching: a removed option like --band must fail loudly,
        # not turn into --bandwidth.
        allow_abbrev=False)
    p.add_argument("-h", "--help", action="help",
                   help="このヘルプを表示して終了")

    g = p.add_argument_group("基本")
    g.add_argument("--channels", default=DEFAULT_CHANNELS,
                   help=f"監視するチャンネル。カンマ区切り (既定 {DEFAULT_CHANNELS})。"
                        "MHz の数字や 名前@MHz も可。一覧は --list-channels")
    g.add_argument("--port", help="COM ポート (省略時は Espressif の USB を自動検出)")
    g.add_argument("--gain", type=int, default=40,
                   help="ゲイン (内部テーブル番号、dB ではない。既定 40)。"
                        "近くの VTX で山が横に広がるときは下げる (C5 実測: 55 以上で飽和)")
    g.add_argument("--config", metavar="PATH",
                   help="設定ファイル (既定 band_monitor.toml、無ければ使わない)")

    g = p.add_argument_group("動作モード")
    g.add_argument("--sim", action="store_true",
                   help="疑似 ESP32-C5 で動かす (機器不要)")
    g.add_argument("--headless", action="store_true",
                   help="画面なし。コンソールに実測中心を表示")
    g.add_argument("--info", action="store_true",
                   help="ESP32 の診断 (ファーム応答・ゲイン別 ADC レベル・ホップ速度・"
                        "ホップ内ノイズ形状) をして終了")
    g.add_argument("--list-channels", action="store_true",
                   help="5.8GHz のチャンネル表を表示して終了")
    g.add_argument("--csv", metavar="PATH", help="実測中心を CSV に記録")
    g.add_argument("--verbose", action="store_true",
                   help="画面ありでもコンソールに測定値を毎秒表示")

    g = p.add_argument_group("測定")
    g.add_argument("--min-snr", type=float, default=18.0,
                   help="信号ありと判定するフロアからの高さ dB (既定 18)。"
                        "画面の detect SNR でも変更可")
    g.add_argument("--avg-ms", type=float, default=3000.0,
                   help="測定に使うスペクトラムの平均時間 ms (既定 3000、0=平均なし)")
    g.add_argument("--smooth", type=float, default=0.3,
                   help="表示する中心の平滑化係数 (既定 0.3、1=平滑化なし)")
    g.add_argument("--dev-ok", type=float, default=0.5,
                   help="ずれ表示が青になる範囲 MHz (既定 0.5)")
    g.add_argument("--dev-warn", type=float, default=1.0,
                   help="ずれ表示が赤になる境界 MHz (既定 1.0)。間は黄色")
    g.add_argument("--search-mhz", type=float, default=8.0,
                   help="各チャンネルの山を探す片側幅 MHz (既定 8、隣と近いときは自動で狭める)")
    g.add_argument("--centroid-margin", type=float, default=3.0,
                   help="重心に含めるのはフロア + この dB を超えたビンだけ (既定 3)")
    g.add_argument("--centroid-half", type=float, default=7.5,
                   help="重心を取る片側幅 MHz (既定 7.5 = FM-ATV の 15MHz マスクの半分)")
    g.add_argument("--min-bw", type=float, default=0.0,
                   help="-3dB 幅がこれ未満なら信号なし扱い MHz (既定 0=無効)")

    g = p.add_argument_group("表示")
    g.add_argument("--bw-marks", default="9,15",
                   help="チャンネルの周りに描く帯域幅 MHz、カンマ区切り (既定 9,15)")
    g.add_argument("--y-range", metavar="LO:HI",
                   help="スペクトラム縦軸を固定 (例 -90:-20)。省略時は自動")
    g.add_argument("--waterfall-rows", type=int, default=400,
                   help="ウォーターフォールの行数 (既定 400)")
    g.add_argument("--wf-row-ms", type=float, default=500.0,
                   help="ウォーターフォール 1 行の時間 ms (既定 500。行数×これが表示する履歴)")
    g.add_argument("--no-hops", action="store_true",
                   help="ホップの LO 位置の点線を消す")

    g = p.add_argument_group("受信機 (上級)")
    g.add_argument("--margin-mhz", type=float, default=15.0,
                   help="掃引範囲を端のチャンネルの外へ広げる幅 MHz。Span ボタンの "
                        "1 チャンネル表示もこの ±幅 (既定 15)")
    g.add_argument("--samples", type=int, default=8192,
                   help="1 ホップの取り込みサンプル数 (256-16380、既定 8192)。"
                        "増やすと平均が効くが遅くなる")
    g.add_argument("--step", type=int, default=25,
                   help="ホップ間隔 MHz (既定 25)。--usable 以下にする "
                        "(どの周波数も 2 ホップで見るため)")
    g.add_argument("--usable", type=float, default=28.0,
                   help="1 ホップで使う LO からの片側幅 MHz (既定 28。"
                        "C5 実測でホップ内 ±30MHz が ±1dB 以内)")
    g.add_argument("--dc-khz", type=float, default=500.0,
                   help="LO の DC スパイクの周りで捨てる片側幅 kHz (既定 500)")
    g.add_argument("--bandwidth", type=int, default=0,
                   help="受信フィルタ MHz (11-48、既定 0=最大)")
    g.add_argument("--bin-khz", type=float, default=100.0,
                   help="スペクトラムのビン幅 kHz (既定 100)")
    return p


def load_config(path: Path, parser: argparse.ArgumentParser) -> dict:
    """Read a TOML settings file into argparse defaults.

    Keys are option names with - or _ (port, min-snr, min_snr ...); a list is
    accepted for channels / bw-marks. Unknown keys are an error, so a typo is
    not silently ignored.
    """
    import tomllib
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    dests = {a.dest: a for a in parser._actions}
    out = {}
    for key, value in raw.items():
        dest = key.replace("-", "_")
        if dest not in dests or dest in ("help", "config"):
            raise SystemExit(f"{path}: 不明な設定 '{key}'")
        action = dests[dest]
        if isinstance(value, list):
            value = ",".join(str(v) for v in value)
        if action.choices and value not in action.choices:
            raise SystemExit(f"{path}: {key} は {', '.join(map(str, action.choices))}"
                             f" のどれか (指定値 {value!r})")
        out[dest] = value
    return out


def _esp_settings(args) -> EspSettings:
    # 80 MS/s, IQ10, min-combine, no flip: the settings checked on the C5.
    return EspSettings(port=args.port, gain=args.gain, samples=args.samples,
                       bandwidth_mhz=args.bandwidth,
                       step_mhz=args.step, usable_mhz=args.usable,
                       dc_khz=args.dc_khz)


def _span(args, channels) -> tuple[float, float]:
    freqs = [c.freq_mhz for c in channels]
    return min(freqs) - args.margin_mhz, max(freqs) + args.margin_mhz


def _span_planner(args):
    """(start, stop) MHz -> (display grid, hop LOs) for that sweep span.

    The display grid is exactly the stitched sweep's bins. The GUI calls this
    again when the span buttons narrow the sweep to one channel.
    """
    es = _esp_settings(args)
    bin_hz = int(round(args.bin_khz * 1000))

    def plan(start: float, stop: float):
        st = Stitcher(start, stop, es, bin_hz)
        grid = FrequencyGrid(st.hz_low / 1e6,
                             st.hz_low / 1e6 + st.n * st.bin_hz / 1e6,
                             st.bin_hz)
        return grid, tuple(st.los)

    return plan


def _grid_and_settings(args):
    channels = parse_channels(args.channels)
    start, stop = _span(args, channels)
    bin_hz = int(round(args.bin_khz * 1000))
    settings = SweepSettings(start_mhz=start, stop_mhz=stop, bin_hz=bin_hz,
                             gain=args.gain, port=args.port)
    grid, los = _span_planner(args)(start, stop)
    return channels, settings, grid, los


def _source_factory(args, channels):
    # The device runs in its own process: sharing the GIL with the GUI made
    # the USB stack drop data (see esp_sdr.process_source).
    es = _esp_settings(args)
    centres = [c.freq_mhz for c in channels] if args.sim else None
    return lambda settings: process_source(es, centres)


def run_headless(args, channels, settings, grid, source_factory) -> int:
    search = auto_search_halfwidth(channels, args.search_mhz)
    q: "queue.Queue" = queue.Queue(maxsize=64)
    reader = SweepReader(settings, grid, source_factory(settings), q)
    reader.start()
    smoother = Smoother(args.smooth)
    averager = SpectrumAverager(args.avg_ms / 1000.0)
    print(f"span {grid.start_mhz:.0f}-{grid.stop_mhz:.0f} MHz, "
          f"bin {grid.bin_hz/1e3:.0f} kHz, channels: "
          + ", ".join(f"{c.name}@{c.freq_mhz:.0f}" for c in channels))
    last_print = 0.0
    try:
        while True:
            item = q.get()
            if isinstance(item, Exception):
                print(f"error: {item}", file=sys.stderr)
                return 1
            if isinstance(item, DeviceInfo):
                print(f"接続: {item.port} ({item.identity})", flush=True)
                continue
            averaged = averager.add(item.t, item.powers)
            now = time.monotonic()
            if now - last_print < 0.5:
                continue
            last_print = now
            ms = measure_all(averaged, grid, channels, search,
                             args.min_snr, args.centroid_margin,
                             args.min_bw, args.centroid_half)
            for m in ms:
                smoother.update(m)
            print(f"[{item.sweep_index:5d}] {format_status(ms, smoother)}",
                  flush=True)
    except KeyboardInterrupt:
        return 0
    finally:
        reader.stop()


def main(argv: list[str] | None = None) -> int:
    # Settings file first (its values become defaults), then the command line
    # on top, so an option typed at launch always wins.
    parser = build_parser()
    pre, _ = parser.parse_known_args(argv)
    cfg_path = Path(pre.config) if pre.config else DEFAULT_CONFIG
    if pre.config and not cfg_path.is_file():
        print(f"設定ファイルが見つかりません: {cfg_path}", file=sys.stderr)
        return 2
    if cfg_path.is_file():
        values = load_config(cfg_path, parser)
        if values:
            parser.set_defaults(**values)
            print(f"設定ファイル: {cfg_path} ({', '.join(values)})")
    args = parser.parse_args(argv)
    if args.list_channels:
        print(format_table())
        return 0
    if args.info:
        start, stop = _span(args, parse_channels(args.channels))
        return esp_probe(_esp_settings(args), start, stop,
                         int(round(args.bin_khz * 1000)))

    channels, settings, grid, los = _grid_and_settings(args)
    source_factory = _source_factory(args, channels)
    args.search_mhz = auto_search_halfwidth(channels, args.search_mhz)

    if args.headless:
        return run_headless(args, channels, settings, grid, source_factory)

    from .gui import ViewOptions, run_gui  # imported late: Qt is optional
    marks = tuple(float(x) for x in args.bw_marks.split(",") if x.strip())
    opts = ViewOptions(bw_marks_mhz=marks, search_mhz=args.search_mhz,
                       min_snr_db=args.min_snr,
                       noise_margin_db=args.centroid_margin,
                       centroid_half_mhz=args.centroid_half,
                       min_bw_mhz=args.min_bw,
                       smooth_alpha=args.smooth,
                       avg_s=args.avg_ms / 1000.0,
                       show_hops=not args.no_hops,
                       hop_los_mhz=los,
                       span_planner=_span_planner(args),
                       channel_span_half_mhz=args.margin_mhz,
                       dev_ok_mhz=args.dev_ok,
                       dev_warn_mhz=args.dev_warn,
                       waterfall_rows=args.waterfall_rows,
                       waterfall_row_ms=args.wf_row_ms,
                       csv_path=args.csv,
                       verbose=args.verbose)
    if args.y_range:
        try:
            lo, hi = (float(x) for x in args.y_range.split(":"))
        except ValueError:
            print("--y-range は LO:HI 形式で指定してください (例 -90:-20)",
                  file=sys.stderr)
            return 2
        opts.y_auto = False
        opts.y_min_dbm, opts.y_max_dbm = min(lo, hi), max(lo, hi)
    return run_gui(channels, settings, grid, source_factory, opts,
                   simulated=args.sim)


if __name__ == "__main__":
    sys.exit(main())
