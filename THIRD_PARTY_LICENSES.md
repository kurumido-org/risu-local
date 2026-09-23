# サードパーティのライセンス

RISU 本体は MIT License（`LICENSE`）です．このファイルは，RISU が **同梱している**
ものと，**実行時に利用する** ものの区別と，それぞれのライセンス上の扱いをまとめています．

> これは法的助言ではありません．公開・再配布の判断は，必要に応じて所属機関の
> 担当部署に確認してください．

---

## 1. リポジトリに同梱しているファイル

これらは RISU の配布物に含まれるため，**著作権表示の保持義務があります**．

| ファイル | ライブラリ | バージョン | ライセンス |
|---|---|---|---|
| `static/vendor/marked.umd.min.js` | [marked](https://github.com/markedjs/marked) | 12.0.2 | MIT |
| `static/vendor/purify.min.js` | [DOMPurify](https://github.com/cure53/DOMPurify) | 3.1.6 | Apache-2.0 **OR** MPL-2.0（デュアル） |
| `static/vendor/chart.umd.min.js` | [Chart.js](https://www.chartjs.org/) | 4.5.1 | MIT |

いずれも MIT と両立します．ファイル冒頭のライセンスヘッダはミニファイ済みファイル内に
保持されているので，**ヘッダを削除しないこと**．

DOMPurify はデュアルライセンスなので，利用者は Apache-2.0 か MPL-2.0 のいずれかを
選べます．Apache-2.0 を選べば MPL-2.0 のファイル単位コピーレフトは関係しません．
どちらを選んでも RISU 本体を MIT にすることに支障はありません（改変していないため）．

### 自作の資産

`static/favicon.svg` / `static/risu_logo.svg` / `static/sample_risu.csv` は本プロジェクトの
オリジナルで，MIT に含まれます．

---

## 2. 実行時に読み込む外部リソース（同梱していない）

| リソース | ライセンス | 備考 |
|---|---|---|
| Google Fonts: Inter / JetBrains Mono / Noto Sans JP | SIL Open Font License 1.1 | CSS 経由で読み込むだけ．フォントファイルは同梱していない |

OFL はフォントファイルを再配布する場合に条件が生じますが，RISU は同梱していないため
該当しません．オフライン動作のためにフォントを同梱する場合は，OFL の条件
（著作権表示の保持，予約名称の扱い）を確認してください．

---

## 3. Python の実行時依存（pip でインストールされるもの）

RISU はこれらを**再配布しません**．利用者が自分の環境に `pip install` します．
2026-09-17 時点の依存クロージャは 72 パッケージで，内訳は次の通りです．

| ライセンス | 件数 |
|---|---|
| MIT 系（MIT / MIT-0 / MIT-CMU） | 33 |
| BSD 系（2-Clause / 3-Clause） | 21 |
| Apache-2.0（単独 or 選択可） | 6 |
| PSF（Python Software Foundation） | 3 |
| MPL-2.0 を含むもの | 3 |
| **GPL / LGPL** | **2**（下記参照） |

再現手順:

```powershell
pip install pip-licenses
pip-licenses --format=markdown --with-urls --order=license
```

### 3.1 PyQt5（GPL v3）— uxsim の依存にあるが RISU は使わない

**依存経路**: `uxsim` → `PyQt5` (GPL v3) → `PyQt5-Qt5` (LGPL v3)

UXsim は自身の GUI 用に PyQt5 を必須依存として宣言しています（extra ではありません）．
そのため `pip install uxsim` で PyQt5 が入ります．

RISU への影響と，そう判断した根拠:

1. **RISU は PyQt5 を import しません．** `import uxsim` でも `World()` 構築でも
   `sys.modules` に PyQt5 は現れません（`TestLicenseHygiene` が検証しています）．
2. **PyQt5 が無い環境でも RISU は全機能が動きます．** import を遮断した状態で
   シミュレーション実行・結果の直列化・集計・素の UXsim パイプラインが通ることを
   確認済みです．
3. **RISU は PyQt5 を再配布しません．** GPL の義務は頒布時に生じます．利用者が
   自分で pip 経由で取得する構成では，RISU のソースを MIT で公開することに支障はありません．

したがって **RISU 本体を MIT にすることは可能** と判断しています．

**ただし，以下をする場合は GPL v3 の義務が生じ得ます**（RISU の利用者・再配布者向けの注意）:

- RISU と依存パッケージを**まとめて**配布する（Docker イメージ，PyInstaller 等の
  単一バイナリ，依存同梱の zip など）．この場合，配布物に GPL v3 の PyQt5 が含まれます．
- そのような配布を行う場合は，配布前に PyQt5 を除外するか，Riverbank Computing の
  商用ライセンスを取得するか，GPL v3 の条件に従ってください．

PyQt5 を外して使いたい場合は，RISU の動作には影響しないので次で構いません:

```powershell
pip uninstall PyQt5 PyQt5-Qt5 PyQt5-sip
```

（`pip check` は uxsim の依存が満たされていないと警告しますが，RISU の全テストは通ります．）

### 3.2 MPL-2.0 を含むもの（certifi / orjson / tqdm）

MPL-2.0 は**ファイル単位**のコピーレフトです．MPL が適用されるファイルを改変して
配布する場合にソース公開義務が生じますが，RISU はこれらを改変しておらず，
再配布もしていないため，RISU 本体のライセンスには影響しません．

- `certifi` — MPL-2.0（CA 証明書バンドル）
- `orjson` — MPL-2.0 AND (Apache-2.0 OR MIT)
- `tqdm` — MPL-2.0 AND MIT

---

## 4. OpenStreetMap データの扱い（コードのライセンスとは別問題）

`import_osm_network` は OSMnx 経由で OpenStreetMap からデータを取得します．
OSM データは **ODbL 1.0** です．利用者が実行時に自分で取得するぶんには RISU の
配布物に含まれませんが，**取得したネットワークやシミュレーション結果を公開する場合は
ODbL の帰属表示（© OpenStreetMap contributors）とシェアアライク条項** を確認してください．

## 5. 計算エンジン

- [UXsim](https://github.com/toruseo/UXsim) — MIT License．RISU が利用する交通流シミュレーター．
  RISU は UXsim のコードを複製しておらず，公開 API と一部の内部 API を呼び出しています．
