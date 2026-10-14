# fpv-band-monitor-c5

**ESP32-C5** で FPV 5.8GHz の映像チャンネル（既定は **E2 / E1 / F3 / F5**）を監視する PC 用バンドモニタ。
**アナログ FPV の映像電波（FM-ATV）専用**。
HackRF One 版の fpv-band-monitor から C5 専用に切り出したもの。
解析（重心による中心周波数、SNR による在り判定）と GUI は同じ。

- スペクトラム + ウォーターフォール、9MHz / 17MHz の帯域幅マーク
- 各チャンネルの **実測中心周波数** と **ずれ** をリアルタイム表示（ずれの大きさで色分け）
- **グループ**（4CH / 4CH2 / 4CH3 / 3CH）の切り替え、カードのクリックで 1 チャンネルだけを掃引
- **WiFi check**：グループに影響する Wi-Fi（チャンネルに重なる Direct と、VTX との IMD）を受信だけで調べる
- チップで FFT 済みの掃引を受け取る（USB の転送が少なく速い）

**対応ファームウェアは液晶版ファームのみ**：
**[zubon2003/fpv-lcd](https://github.com/zubon2003/fpv-lcd)**（GPL-3.0、[ESPARGOS/esp-sdr](https://github.com/ESPARGOS/esp-sdr) のフォーク）。
このモニタを XIAO ESP32-C5 単体で動かしたもので、液晶だけでも同じ監視ができる。
[ファーム書き込みページ](https://zubon2003.github.io/fpv-lcd/) からブラウザで書き込める。
**液晶が無くても、XIAO ESP32-C5 とこの PC 用ソフトのセットで使える**（下記）。

## 必要なもの

- **XIAO ESP32-C5**（5GHz 帯を受けられる ESP32 は C5 だけ。C3/C6/S3 などは 2.4GHz のみ）
- **液晶版ファーム**（[zubon2003/fpv-lcd](https://github.com/zubon2003/fpv-lcd)、GPL-3.0）
- 3.5" 液晶（Waveshare Pico-ResTouch-LCD-3.5 か 3.5inch Capacitive Touch LCD）は**無くてもよい**。
  液晶なしで PC 用ソフトとだけ使うときは、書き込みページで **3.5" Capacitive Touch LCD** 用を選ぶ
  （タッチの部品が見つからなければタッチを使わず、校正画面も出ない。ResTouch 用はタッチを SPI で読むので、
  液晶が無いと不定な値をタッチと取り違えるおそれがある）。電池も無くてよい（USB 給電として動く）
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

ブラウザ（Chrome / Edge）で [ファーム書き込みページ](https://zubon2003.github.io/fpv-lcd/) を開き、
液晶（3.5" Pico-ResTouch-LCD-3.5 か 3.5" Capacitive Touch LCD）を選んで書き込む。
結線図：[ResTouch](https://zubon2003.github.io/fpv-lcd/wiring/restouch35.html) ・
[静電容量式](https://zubon2003.github.io/fpv-lcd/wiring/captouch35.html)。
ソースとビルド方法は [zubon2003/fpv-lcd](https://github.com/zubon2003/fpv-lcd)（GPL-3.0）。
ファームのバイナリはこのリポジトリには入れていない。

## 使い方

C5 の **ネイティブ USB 側の端子**を PC に挿す（VID 303A のポートを自動で探す）。
ブラウザの ESP-WebSDR など、ポートを掴む他のプログラムは閉じておく。

| Windows | Mac | 内容 |
| --- | --- | --- |
| `band_monitor.bat` | `band_monitor.command` | GUI 起動（`--nolcd` 付き = No LCD オン）。引数はそのまま渡る（例: `band_monitor.bat --gain 55`）|
| `band_monitor_sim.bat` | `band_monitor_sim.command` | 疑似 C5 で動作確認（ハード不要）|
| `band_monitor_headless.bat` | `band_monitor_headless.command` | GUI 無し、コンソールに実測中心を表示 |
| `check_esp.bat` | `check_esp.command` | 接続確認: ファームウェアの応答、ゲイン別 ADC レベル、ホップの所要時間、ホップ内のノイズ形状 |

起動ファイルは `.venv` の Python を使う。インストール前に起動すると、インストールを促して止まる。

コマンドで直接動かすときも `.venv` の Python を使う:

```powershell
.venv\Scripts\python band_monitor.py --channels E1,A8,A5,F5   # Windows（4CH2 グループで起動）
.venv/bin/python band_monitor.py --port /dev/cu.usbmodem1101   # Mac（ポート名の例）
.venv\Scripts\python band_monitor.py --help             # オプション一覧（区分別）
```

### 画面の操作

| 場所 | 動作 |
|---|---|
| Port / Connect | ポートの選択（開くたびに一覧を読み直す、`[ESP32]` が C5）と接続・切断 |
| Rx: auto / iq / fft・No LCD | 受信方式（下記「液晶版ファームとの接続」） |
| **Group** 4CH / 4CH2 / 4CH3 / 3CH | 監視するチャンネルの組を切り替える（下記） |
| **WiFi check** | 今のグループに影響する Wi-Fi を調べる（下記） |
| Span（チャンネル名 / ALL） | そのチャンネルの前後 ±15 MHz だけを掃引（ホップが減って速い）/ グループ全体 |
| 下段のカード | クリックでそのチャンネルだけを掃引、もう一度で ALL |
| Gain idx・Apply gain | ゲイン（テーブル番号、AGC も可）。近くの VTX で山が横に広がる・**SAT** が出るなら下げる |
| Pause・Peak hold・Clear | 表示の一時停止、ピークホールド、平均とウォーターフォールのクリア |
| detect SNR | 信号ありと判定する、ノイズからの高さ |
| Y / WF | スペクトラムの縦軸、ウォーターフォールの色の範囲（Auto で自動） |

## インストーラーの動作確認（2026-10-03）

**Windows（確認済み、Windows 11 / Python 3.13.2）**

- インストール前に `band_monitor.bat` を起動すると「先に install.bat を実行」と案内して止まる
- `install.bat` で `.venv` が作られ、`requirements.txt` の固定版
  （numpy 2.4.3、PyQt6 6.10.2、PyQt6-Qt6 6.10.2、PyQt6-sip 13.11.0、pyqtgraph 0.14.0、
  colorama 0.4.6、pyserial 3.5）がすべて入る
- `band_monitor.bat` から `.venv` の Python で起動し、疑似 C5 で受信用の子プロセスも含めて正常に動く
- 液晶版ファームを書いた XIAO ESP32-C5 の実機で `fft` / `iq` / No LCD を確認（2026-10-09）
- 同じく実機で、グループの切り替え（12 回、毎回 1〜1.5 秒で受信再開）と WiFi check（4CH で 18 ch・22 秒）を確認（2026-10-14）

**Mac（確認済み、2026-10-10）**

- Mac 実機で動作を確認
- 初回はターミナルで `bash install.command`（Windows からコピーしたファイルは実行権限が外れているため）。
  インストール時に他の `.command` に実行権限を付けるので、以後はダブルクリックで起動できる
- ダウンロードしたファイルが開けないと言われたら、右クリック →「開く」
- ポート名は `/dev/cu.usbmodem…` の形。画面の Port 欄から選べる

## 液晶版ファームとの接続（掃引ストリーム）

液晶版ファーム（ESP-SDR に液晶モニタを組み込んだもの）は、チップ上で FFT とつなぎ合わせを済ませた
**掃引を 1 回ごとに USB で送れる**。I/Q を送る従来方式（1 ホップ約 20 KB）に比べ、4CH の掃引 1 回が約 4 KB で済むので、
USB の転送待ちが無くなる。液晶側でも同じ掃引を表示し続ける。

| 受信方式（`--receiver` / 画面の Rx ボタン） | 動作 |
|---|---|
| `auto`（既定） | 接続するたびに `FPV?` を送り、答えれば `fft`、答えなければ `iq` |
| `fft` | チップで FFT 済みの掃引を受け取る。ビン幅はファームと同じ 78.125 kHz。平均・重心・判定は従来どおり PC で行う（旧名 `fw` も可） |
| `iq` | I/Q を受け取って PC で FFT（その間、液晶の掃引は止まる。SAT 表示は出ない。WiFi check は使える） |

画面上部の **Rx: auto / iq / fft** ボタンで実行中に切り替えられる。auto のときは右に実際の方式（`-> fft` / `-> iq`）が出る。
方式が変わるとビン幅が変わるので、スペクトラムとウォーターフォールは新しい格子で描き直しになる。

`fft` では、PC のスパンとチャンネル（最大 4）を液晶にも送り、液晶は「PC」モードで同じものを表示する。
Span ボタンで 1 チャンネル表示にすると液晶も同じスパンになる。ゲインも PC の値が送られる。

**No LCD**（チェックボックス、または `--nolcd`）: `fft` のとき `FPV NOLCD` を送り、液晶を消してチップは掃引と FFT だけを全力で行う
（描画の分だけ掃引が速くなる）。受信をやめる（Disconnect・終了・USB が切れる）、チェックを外す、液晶をタップする、のどれかで表示が戻る。
`iq` では使えない（灰色）。起動ファイル（`band_monitor.bat` / `.command`）は `--nolcd` 付きで起動するので、最初からオンになっている。

`fft` ではウォーターフォールを掃引 1 回ごとに 1 行進める（時間軸は実測の掃引間隔から決める）。`iq` では従来どおり `--wf-row-ms` ごとに 1 行。

コマンドとフレームの形式は液晶版ファームの `main/fpv/link.h` にある。

## グループ・カード・WiFi check（液晶版と同じ機能）

- **Group**（4CH / 4CH2 / 4CH3 / 3CH）：液晶版と同じチャンネルの組。押すとチャンネル・カード・Span ボタン・掃引範囲が切り替わる。
  `--channels` がどのグループとも違うときは、どれも押されていない状態（custom）で始まる

  | グループ | チャンネル |
  |---|---|
  | 4CH | E2 E1 F3 F5 |
  | 4CH2 | E1 A8 A5 F5 |
  | 4CH3 | R2 F1 R4 R5 |
  | 3CH | E1 F1 F4 |
- 色は周波数の低い順に 赤・緑・青・黄（液晶と同じ）
- カード：ずれ（MHz）を大きく、カードの地もずれの色。電波が無ければ NO SIGNAL、SNR 6 dB 以上だが閾値未満なら WEAK。
  **カードをクリックするとそのチャンネルだけを掃引**（Span のそのボタンと同じ）、もう一度で ALL
- **SAT nn%**（赤）：ADC が飽和している（ゲインを下げる）。液晶版ファーム（fft）のとき、1 秒ごとに `FPV STATE?` の `clip` を読んで出す
- **WiFi check**：表示中のグループに効く Wi-Fi チャンネルだけ（ch100〜177 のうち、FPV の 17 MHz に **Direct** で重なるか、
  **IMD**＝2×VTX−WiFi・2×WiFi−VTX が乗るもの。4CH なら 18 ch）を 1.2 秒ずつ**受信だけで**聞く（液晶版ファームの `FPV WIFI`。
  電波は出さない）。その間 PC の受信は止まる（約 15〜22 秒）。結果は FPV チャンネルごとに OK / Direct / IMD と、原因の
  Wi-Fi ch・dBm・IMD の組み合わせ・AP 名。Wi-Fi が弱いほど色が薄い（-50 dBm で最も濃く、-85 dBm 以下で薄い）。
  液晶版ファームの `FPV WIFI` を使う

## ライセンス

このモニタ（PC 版）は MIT License（[LICENSE](LICENSE)）。
液晶版ファームは別リポジトリ [zubon2003/fpv-lcd](https://github.com/zubon2003/fpv-lcd)（GPL-3.0、ESP-SDR のフォーク）で、
ここには同梱していない（USB でつながるだけ）。
