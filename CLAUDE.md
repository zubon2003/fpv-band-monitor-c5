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

## 液晶版に合わせた機能（2026-10-14）

- グループ（`channels.GROUPS`、ツールバー Group）で実行中にチャンネルの組を切り替える（`_select_group`：readouts・markers・span ボタンを作り直し、`_replan`）。色は周波数順（`rank_colors`）。
- カードはずれが主役＋地をずれの色、クリックでそのチャンネルの Span（もう一度で ALL）。WEAK は SNR 6 dB 以上のときだけ。
- `wifi.py`：Direct / IMD の事前計算（液晶版 `scan_prepare` と同じ結果をテストで確認）、`FPV WIFI` の解析、判定、RSSI による濃さ。`gui.WifiDialog` は受信を止めて（`pause_receiver`）自分でポートを開き、終われば `resume_receiver`。
- `fw_source` が 1 秒ごとに `FPV STATE?` の `clip` を読み `DeviceStatus` を流す（子プロセス経由）→ ツールバーの SAT。`clip` が無い古いファームでは止める。
- 実機（COM18、液晶版ファーム）で WiFi check を確認：18 ch で 22 秒、終了後に受信再開。
- グループ切替で接続が切れた件（2026-10-14 修正）：①子プロセスがキューの満杯で終われず 2 s で kill → STREAM OFF が届かずファームが送り続け、次の `FPV?` が遅れる。親は子が終わるまでキューを抜き続ける（5 s）。②`SWEEP ` がペイロード内の改行の後ろにくっつくと二度と同期しない → 行中の `SWEEP ` から読み直す（tests/test_framing.py）。③`next_sweep` はデータが来続けても時間で戻る、ストリーム中の readline は 0.3 s。④`FPV?` は 3 回まで聞き直す。実機で 12 回切替して全部 1.0〜1.4 s で再開。

## テスト

```
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests
```
34 件（Windows で test_fpv_link を除く。test_wifi.py・test_framing.py を追加）。`tests/test_fpv_link.py` は疑似端末（pty、Linux/macOS のみ）で偽ファームと通信する。ファーム側の結合テストは `fpv-lcd/tests/fpv_host/check_link.py <このリポジトリ>`。

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
