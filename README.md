# fpv-band-monitor-c5

**XIAO ESP32-C5** の液晶版バンドモニタ・ファームから掃引を受け取り、FPV 5.8GHz の 4 波（既定は **E2 / E1 / F3 / F5**）を
PC で監視するバンドモニタ。**アナログ FPV の映像電波（FM-ATV）専用**。
ホップ・FFT・つなぎ合わせはチップ上で済ませてあり、PC は掃引 1 回分（4CH で約 4 KB）を USB で受け取るだけなので、
I/Q を PC に送っていた頃のような USB 転送の待ちが無い。液晶側でも同じ掃引を表示し続ける。
解析（重心による中心周波数、SNR による在り判定）と GUI は HackRF One 版の fpv-band-monitor と同じ。

- スペクトラム + ウォーターフォール、9MHz / 15MHz の帯域幅マーク
- 各チャンネルの **実測中心周波数** をリアルタイム表示

## 必要なもの

- **XIAO ESP32-C5** に **液晶版バンドモニタのファーム**（ESP-SDR に液晶モニタを組み込んだもの）を書いたもの。
  液晶はつながっていなくても PC から使える。通常の ESP-SDR ファームには対応していない
- Python 3.11 以上（3.13 推奨）。Windows は [python.org](https://www.python.org/downloads/)、
Mac は python.org のインストーラーか `brew install python@3.13` （Mac 標準の `/usr/bin/python3` は 3.9 のことが多く使えない）

## インストール

このフォルダの中に仮想環境 `.venv` を作り、`requirements.txt` の版（動作確認済みの版に固定）を入れる。
システムの Python には何も入れない。もう一度実行すると修復・更新になる。やり直すときは `.venv` を消してから実行。

- **Windows**: `install.bat` をダブルクリック
- **Mac**: ターミナルでこのフォルダに移り `bash install.command`
  （Windows からコピーしたファイルは実行権限が外れているため、初回はこの形で。インストール時に
  他の `.command` に実行権限を付けるので、以後はダブルクリックで起動できる。ダウンロードしたファイルが
  開けないと言われたら、右クリック →「開く」）

### ファームウェアの書き込み

液晶版ファームの配布物にある `firmware/fpv-lcd-xiao-c5-merged.bin` を **オフセット 0x0** に書く:

```
python -m esptool --chip esp32c5 write-flash 0x0 fpv-lcd-xiao-c5-merged.bin
```

（ブラウザなら [ESP Tool](https://espressif.github.io/esptool-js/) で Flash Address 0x0 に指定）

## 使い方

XIAO ESP32-C5 の USB を PC に挿す（VID 303A のポートを自動で探す）。
ポートを掴む他のプログラム（シリアルモニタなど）は閉じておく。

| Windows | Mac | 内容 |
| --- | --- | --- |
| `band_monitor.bat` | `band_monitor.command` | GUI 起動。引数はそのまま渡る（例: `band_monitor.bat --gain 55`）|
| `band_monitor_sim.bat` | `band_monitor_sim.command` | 疑似 C5 で動作確認（ハード不要）|
| `band_monitor_headless.bat` | `band_monitor_headless.command` | GUI 無し、コンソールに実測中心を表示 |
| `check_esp.bat` | `check_esp.command` | 接続確認: ファームウェアの応答、ゲイン別 ADC レベル、ホップの所要時間、ホップ内のノイズ形状 |

起動ファイルは `.venv` の Python を使う。インストール前に起動すると、インストールを促して止まる。

コマンドで直接動かすときも `.venv` の Python を使う:

```powershell
.venv\Scripts\python band_monitor.py --channels F3       # Windows
.venv/bin/python band_monitor.py --port /dev/cu.usbmodem1101   # Mac（ポート名の例）
.venv\Scripts\python band_monitor.py --help             # オプション一覧（区分別）
```

## インストーラーの動作確認（2026-10-03）

**Windows（確認済み、Windows 11 / Python 3.13.2）**

- インストール前に `band_monitor.bat` を起動すると「先に install.bat を実行」と案内して止まる
- `install.bat` で `.venv` が作られ、`requirements.txt` の固定版
  （numpy 2.4.3、PyQt6 6.10.2、PyQt6-Qt6 6.10.2、PyQt6-sip 13.11.0、pyqtgraph 0.14.0、
  colorama 0.4.6、pyserial 3.5）がすべて入る
- `band_monitor.bat` から `.venv` の Python で起動し、疑似 C5 で受信用の子プロセスも含めて正常に動く

**Mac（未確認）**

- `.command` の 5 ファイルは、改行コード（LF）と bash の構文チェックまで確認。Mac 実機ではまだ動かしていない
- 初回はターミナルで `bash install.command`（Windows からコピーしたファイルは実行権限が外れているため）。
  インストール時に他の `.command` に実行権限を付けるので、以後はダブルクリックで起動できる
- ダウンロードしたファイルが開けないと言われたら、右クリック →「開く」
- ポート名は `/dev/cu.usbmodem…` の形。画面の Port 欄から選べる

## 液晶版ファームとのやりとり

- 起動時に `FPV?` でファームを確かめ、PC のスパン・チャンネル（最大 4）・ゲインを送ってから掃引を受け取る。
  液晶は「PC」モードになり、PC と同じものを表示する。Span ボタンで 1 チャンネル表示にすると液晶も同じスパンになる。
- スペクトラムのビン幅はファームの FFT と同じ 78.125 kHz（80 MS/s ÷ 1024）。
- 液晶側でモードを変えると、PC は別スパンの掃引が 5 回続いたところで自分のスパンを送り直す（PC が優先）。
- 送られる値は 0.01 dB 単位（誤差 0.005 dB 以内）。平均・重心・判定はこれまでどおり PC で行う。
- コマンドとフレームの形式は液晶版ファームの `main/fpv/link.h`。

## ライセンス

MIT License（[LICENSE](LICENSE)）。液晶版ファーム（ESP-SDR ベース、GPL-3.0）は別配布（同梱していない）。
