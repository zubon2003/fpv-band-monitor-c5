# CLAUDE.md — fpv-band-monitor-c5（PC 版）

クラウドの Claude セッションから引き継いだ作業。ユーザーとは日本語で話す（簡潔に）。

## 何か

- ESP32-C5 で FPV 5.8 GHz（FM-ATV）の映像チャンネルを監視する PC アプリ（Python、PyQt + pyqtgraph）。スペクトラム・ウォーターフォール・重心による実測中心周波数・SNR 判定。
- 液晶版ファーム（別リポジトリ `fpv-lcd`、ESP-SDR のフォーク）に対応している（ブランチ `lcd-firmware-stream` で作り、`main` に取り込んだ）。

## 受信方式（このブランチで追加）

- `fpv_band_monitor/fpv_link.py`：液晶版ファームの `FPV` プロトコル（`FPV?`, `FPV SPAN`, `FPV GAIN`, `FPV STREAM ON|OFF`, `FPV NOLCD|LCD`, `SWEEP` バイナリフレーム = uint16 の dB×100+200、CRC32）。`fw_source()` が SweepReader 用のソース。`probe()` は `--info`。
- `cli.py` の `ReceiverControl`：`auto`（`FPV?` に答えれば `fft`、だめなら `iq`）/ `iq`（従来の I/Q を PC で FFT）/ `fft`（`fw` は旧名）。fft はビン 78.125 kHz・ステップ 25 MHz、iq は `--bin-khz`（既定 100）。`--nolcd` で fft 時に `FPV NOLCD` を送る。
- `gui.py`：ツールバーに **Rx: auto / iq / fft** ボタンと **No LCD**。切り替えると受信を止め、方式を決め直し、ビン幅が変われば `_replan()` で格子を作り直す。
- ユーザーの指定：「PC 側はどちらでも使えるように。基本は auto で fft を受け取る。だめなら iq。ボタンで auto / iq / fft を選べる。nolcd を送ったら LCD 表示をせず全力で FFT」。（一度「新ファームのみ」にして、すぐ取り消した経緯がある：コミット 056b2d1 → revert 4585539。）

## テスト

```
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests
```
31 件。`tests/test_fpv_link.py` は疑似端末（pty、Linux/macOS のみ）で偽ファームと通信する。ファーム側の結合テストは `fpv-lcd/tests/fpv_host/check_link.py <このリポジトリ>`。

## 実機での確認（2026-10-09〜10）

- Windows 11 と Mac で動作確認済み。液晶版ファームの XIAO ESP32-C5 で fft / iq / No LCD を確認。
- 掃引速度（4CH 相当 9 ホップ、ファームの DC キャッシュ入り）：液晶あり 2.96、No LCD 6.3 sweeps/s。iq は約 1.4。
- Windows では書いてすぐポートを閉じると行が途中で切れ、次に開いたとき古い `ERR` が届く。`close()` は `flush()` してから閉じ、`FPV?` は ERR なら 1 回だけ聞き直す。
- 起動ファイル（`band_monitor.bat` / `.command`）は `--nolcd` 付き。ゲインの既定は 25（ユーザー指定）。
- fft ではウォーターフォールを 1 掃引 = 1 行にし、時間軸は実測の掃引間隔（`sweep_period`）から決める。
- Windows で `tests/test_fpv_link.py` は pty（termios）が無いので読み込めない。他は `-p "test_[!f]*.py"` で通る。

## コミット

- 2026-10-10 から作者はユーザーの git 設定（zubon2003）。それ以前は中立名 `fpv-lcd <fpv-lcd@localhost>`。
- `main` に push 済み（lcd-firmware-stream を早送りで取り込んだ）。push はユーザーに確認してから。
