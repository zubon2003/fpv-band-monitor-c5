# fpv-band-monitor-c5

**ESP32-C5** で FPV 5.8GHz の 4 波（既定は **E2 / E1 / F3 / F5**）を監視するバンドモニタ。
**アナログ FPV の映像電波（FM-ATV）専用**。
HackRF One 版の fpv-band-monitor から C5 専用に切り出したもの。
解析（重心による中心周波数、SNR による在り判定）と GUI は同じ。

- スペクトラム + ウォーターフォール、9MHz / 15MHz の帯域幅マーク
- 各チャンネルの **実測中心周波数** をリアルタイム表示

## 必要なもの

- **ESP32-C5** の基板（例: ESP32-C5-DevKitC-1）。5GHz 帯を受けられる ESP32 は C5 だけ
  （C3/C6/S3 などは 2.4GHz のみ）
- [ESP-SDR](https://github.com/ESPARGOS/esp-sdr) ファームウェア
- Python 3.11 以上（3.13 推奨）。Windows は [python.org](https://www.python.org/downloads/)、
  Mac は python.org のインストーラーか `brew install python@3.13`
  （Mac 標準の `/usr/bin/python3` は 3.9 のことが多く使えない）

## インストール

このフォルダの中に仮想環境 `.venv` を作り、`requirements.txt` の版（動作確認済みの版に固定）を入れる。
システムの Python には何も入れない。もう一度実行すると修復・更新になる。やり直すときは `.venv` を消してから実行。

- **Windows**: `install.bat` をダブルクリック
- **Mac**: ターミナルでこのフォルダに移り `bash install.command`
  （Windows からコピーしたファイルは実行権限が外れているため、初回はこの形で。インストール時に
  他の `.command` に実行権限を付けるので、以後はダブルクリックで起動できる。ダウンロードしたファイルが
  開けないと言われたら、右クリック →「開く」）

### ファームウェアの書き込み

ブラウザ（Chrome / Edge）の [firmware installer](https://espargos.net/espsdr/app/flash.html) で
ESP32-C5 を選ぶのが簡単。esptool を使う場合は
[esp-web-sdr](https://github.com/ESPARGOS/esp-web-sdr) の `firmware/` で:

```powershell
python -m esptool --chip esp32c5 --port COMx write-flash @esp32c5/flash_args
```

（ESP-SDR にはライセンス表記が無いので、バイナリはこのリポジトリに同梱していない）

## 使い方

C5 の **ネイティブ USB 側の端子**を PC に挿す（VID 303A のポートを自動で探す）。
ブラウザの ESP-WebSDR など、ポートを掴む他のプログラムは閉じておく。

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

## 液晶版ファームとの接続（掃引ストリーム）

XIAO ESP32-C5 の液晶版ファーム（ESP-SDR に液晶モニタを組み込んだもの）は、チップ上で FFT とつなぎ合わせを済ませた
**掃引を 1 回ごとに USB で送れる**。I/Q を送る従来方式（1 ホップ約 20 KB）に比べ、4CH の掃引 1 回が約 4 KB で済むので、
USB の転送待ちが無くなる。液晶側でも同じ掃引を表示し続ける。

| `--receiver` | 動作 |
|---|---|
| `auto`（既定） | 起動時に `FPV?` を送り、液晶版ファームなら `fw`、通常の ESP-SDR なら `iq` |
| `fw` | 掃引ストリームを受け取る。ビン幅はファームと同じ 78.125 kHz。平均・重心・判定は従来どおり PC で行う |
| `iq` | 従来どおり I/Q を受け取って PC で FFT（液晶版ファームでもこちらは使える。その間、液晶の掃引は止まる） |

`fw` では、PC のスパンとチャンネル（最大 4）を液晶にも送り、液晶は「PC」モードで同じものを表示する。
Span ボタンで 1 チャンネル表示にすると液晶も同じスパンになる。ゲインも PC の値が送られる。
コマンドとフレームの形式は液晶版ファームの `main/fpv/link.h` にある。

## ライセンス

MIT License（[LICENSE](LICENSE)）。ESP-SDR ファームウェアは別プロジェクト（同梱していない）。
