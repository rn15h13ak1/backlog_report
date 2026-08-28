# Backlog レポート生成ツール

Backlog REST API を使って、指定期間の課題集計を Markdown ファイルとして手動生成します。
種別・カスタム属性・キーワードによるフィルターを複数定義でき、フィルターごとに個別のレポートファイルが出力されます。

---

## 集計内容

| 項目 | 内容 |
|------|------|
| **① 前週残件数** | 期間開始より前に作成され、期間開始時点でオープンだった課題 |
| **② 新規発生件数** | 対象期間に新しく作成された課題 |
| **③ 再オープン件数** | 期間開始時点で完了系だったが、期間中にオープン系へ変化した課題 |
| **④ 当週完了件数** | 期間中にオープン系から完了系へ変化した課題 |
| **⑤ 当週未完了件数** | ① + ② + ③ のうち、④ で完了しなかった課題 |

各カテゴリに対して **Backlog の課題番号一覧（例: PROJ-101, PROJ-102）** も出力されます。

### ステータス定義

| 区分 | 対象ステータス |
|------|--------------|
| オープン系（未完了） | 未対応（1）、処理中（2） |
| 完了系 | 処理済み（3）、完了（4） |

ステータス ID は `config.yaml` の `open_status_ids` / `closed_status_ids` で変更できます。

> プロジェクトに独自のステータス（例: 「レビュー中」）を追加している場合は、必ずどちらかのリストに含めてください。
> どちらにも属さないステータスは集計から漏れます。漏れが検出された場合はレポートと実行ログに警告が表示されます。
> 現在のステータス一覧と分類は `python check_api.py` で確認できます。

### 等式チェック

`① + ② + ③ = ④ + ⑤` が成立しない場合、レポートのサマリーに自動で警告が表示されます。

### ステータス表示について

現在のステータス（レポート実行時点）ではなく、各カテゴリに対応した時点のステータスを表示します。

| カテゴリ | 表示ステータスの基準 |
|---|---|
| ① 前週残件 | 期間開始時点 |
| ② 新規発生 | 期間終了時点 |
| ③ 再オープン | 期間終了時点 |
| ④ 当週完了 | 期間終了時点 |
| ⑤ 当週未完了 | 期間終了時点 |

### 日付の基準（タイムゾーン）

Backlog API は日時を UTC で返しますが、本ツールは課題の作成日・ステータス変化日をすべて
**JST（UTC+9）に変換してから**期間判定を行います。
たとえば UTC `2026-03-08T15:30:00Z` の変更は JST では `2026-03-09` として扱われます。

---

## セットアップ

### 1. 必要ライブラリのインストール

Python 3.10 以上が必要です。

```bash
pip install pyyaml
```

開発用（テスト・Lint）を含めてインストールする場合:

```bash
pip install -e ".[dev]"
```

### 2. Backlog API キーの取得

1. Backlog にログイン
2. 右上のユーザーアイコン → **個人設定**
3. 左メニュー **API** → **API キーを発行する**
4. 発行されたキーをコピー

### 3. `config.yaml` の作成

`config.sample.yaml` をコピーして `config.yaml` を作成し、自分の環境に合わせて編集してください。

```bash
cp config.sample.yaml config.yaml
```

最低限以下の 3 箇所を設定してください。

```yaml
backlog:
  space_host: "yourcompany.backlog.com"  # Backlog のホスト名
  api_key: "YOUR_API_KEY_HERE"            # 取得した API キー
  project_key: "YOUR_PROJECT_KEY"         # プロジェクトキー
```

**プロジェクトキーの確認方法:**
URL が `https://yourcompany.backlog.com/projects/MYAPP` なら、プロジェクトキーは `MYAPP` です。

> `config.yaml` は `.gitignore` で管理対象外になっています（API キーを含むため）。

---

## 実行方法

実行はすべて手動です。`config.yaml` で期間を設定してからスクリプトを起動してください。

### 基本実行

```bash
python backlog_weekly_report.py
```

### 期間の指定方法

期間は以下の優先順位で決定されます。

| 優先 | 方法 | 例 |
|------|------|----|
| 1 | コマンドライン引数 `--from` / `--to` | `--from 2026-03-01 --to 2026-03-31` |
| 2 | コマンドライン引数 `--week` | `--week current` |
| 3 | `config.yaml` の `report.period` | `from: "2026-03-01"` / `to: "2026-03-31"` |
| 4 | `config.yaml` の `report.target_week` | `"previous"`（前週）/ `"current"`（今週） |

**config.yaml で期間を直接指定する場合（推奨）:**

```yaml
report:
  period:
    from: "2026-03-01"
    to:   "2026-03-31"
```

`period` をコメントアウトすると `target_week` の自動計算に切り替わります。

```yaml
report:
  target_week: "previous"   # "previous"（前週）/ "current"（今週）
  week_start: "monday"      # 週の開始曜日
```

**コマンドライン引数で指定する場合:**

```bash
python backlog_weekly_report.py --from 2026-03-01 --to 2026-03-31
python backlog_weekly_report.py --week previous
python backlog_weekly_report.py --week current
```

**設定ファイルを切り替える場合:**

```bash
python backlog_weekly_report.py --config /path/to/other_config.yaml
```

---

## フィルター設定

`config.yaml` の `filters` セクションに複数のフィルターを定義できます。
フィルターごとに個別の Markdown ファイルが生成されます。
`filters` を空にすると、フィルターなしで全課題を集計した 1 ファイルが生成されます。

```yaml
filters:
  - name: "バグ対応"
    description: "バグ種別の課題"   # 任意のメモ
    keyword: "【障害】"             # 件名・詳細のキーワード絞り込み（任意）
    issue_types:
      - "バグ"

  - name: "Aチーム_タスク"
    description: "AチームのタスクおよびAチームへの要望"
    issue_types:
      - "タスク"
      - "要望"
    custom_fields:
      - field_name: "対応チーム"
        values:
          - "Aチーム"

  - name: "別プロジェクト集計"
    project_key: "OTHER_PROJECT"    # このフィルターのみ別プロジェクトを集計（任意）
    issue_types:
      - "タスク"
```

### フィルター設定の仕様

| 項目 | 説明 |
|------|------|
| `name` | レポートファイル名・見出しに使用（必須） |
| `description` | レポートに記載されるメモ（任意） |
| `project_key` | このフィルターのみ別プロジェクトを集計（任意。省略時は `backlog.project_key` を使用） |
| `keyword` | 件名・詳細の部分一致キーワード（任意）。`issue_types` や `custom_fields` と AND 条件で動作 |
| `issue_types` | 絞り込む種別名のリスト。複数指定は OR 条件。省略すると全種別が対象 |
| `custom_fields` | カスタム属性フィルターのリスト。複数指定は AND 条件 |

**カスタム属性の指定方法:**

```yaml
custom_fields:
  # 属性名で指定
  - field_name: "対応チーム"
    values: ["Aチーム"]

  # 属性 ID で直接指定（Backlog の設定画面の URL から確認可能）
  - field_id: 12345
    values: ["高", "最高"]
```

---

## 出力ファイル

`config.yaml` の `report.output_dir`（デフォルト: `./reports`）配下に、期間フォルダを作成してファイルを生成します。

**フィルターなしの場合:**
```
reports/
  20260316_20260322/
    weekly_report.md
```

**フィルターありの場合（フィルターごとに 1 ファイル）:**
```
reports/
  20260316_20260322/
    weekly_report_バグ対応.md
    weekly_report_Aチーム_タスク.md
    weekly_report_優先度_高.md
```

### レポートの構成

各ファイルは以下の構成で出力されます。

**ヘッダー部**
- 対象期間
- プロジェクト名・プロジェクトキー
- フィルター名・絞り込み条件（フィルターあり時のみ）
- 生成日時

**サマリーテーブル**

| 項目 | 件数 |
|------|------|
| ① 前週残件数 | N 件 |
| ② 新規発生件数 | N 件 |
| ③ 再オープン件数 | N 件 |
| ④ 当週完了件数 | N 件 |
| ⑤ 当週未完了件数 | N 件 |

等式 `① + ② + ③ = ④ + ⑤` が成立しない場合は警告をサマリー直下に表示。

**各カテゴリのセクション（5 つ）**

1. **① 前週残件** — 課題番号のコンパクト一覧 ＋ 詳細テーブル（折りたたみ）
2. **② 新規発生** — 同上
3. **③ 再オープン** — 同上
4. **④ 当週完了** — 同上
5. **⑤ 当週未完了** — 同上（詳細テーブルは最大 50 件）

詳細テーブルの列：

| 課題番号 | 件名 | ステータス | 担当者 | 期限日 |
|---------|------|-----------|-------|-------|

詳細テーブルは `<details>` タグで折りたたまれており、GitHub や対応 Markdown ビューアで展開できます。

---

## 注意事項

- **API キーの管理**: `config.yaml` は `.gitignore` で管理対象外になっています。`config.sample.yaml` をコピーして使用してください。
- **処理時間**: 各課題のコメント履歴を取得してステータス変化を判定します。ただし
  「期間開始日以降に一度も更新されていない課題」はコメントを取得しないため、
  実際に API を叩く件数は対象課題数より大幅に少なくなります。取得は並列で行われ、
  同じ課題が複数フィルターに現れても取得は 1 回だけです。
  並列数は `report.max_workers`（デフォルト 4）で調整できます。
- **API エラーの扱い**: 429 / 5xx / 接続エラーは指数バックオフで最大 3 回リトライします。
  個別の課題でコメント履歴の取得に失敗しても処理は継続し、件数がレポート末尾と実行ログに警告表示されます。
- **オンプレミス版について**: `base_path`（例: `"/backlog"`）と `ssl_verify: false` を設定することでオンプレミス版にも対応しています。
- **接続診断**: 設定が正しいか不安な場合は `python check_api.py` を実行してください。
  接続・認証、プロジェクト、ステータス分類、コメントの changeLog をまとめて確認できます。

---

## テスト

```bash
python -m pytest tests/ -v
```

検証している範囲:

- 集計期間の決定（`--from/--to` / `--week` / `config.period` / `target_week` の優先順位）
- フィルター条件の解決（種別・カスタム属性）
- ステータス分類（JST 境界、期間後にのみ変化した課題などの回帰）
- API クライアント（リトライ、コメントキャッシュ、取得スキップ判定）
- レポート出力の形式（`tests/golden/` に保存した出力との全文比較）
- 旧実装との差分比較（`tests/_legacy.py` と突き合わせ、合成データ832ケース）

### カバレッジの計測

```bash
python -m pytest tests/ --cov
```

未到達の行番号まで見る場合は `--cov-report=term-missing`、HTML で見る場合は
`--cov-report=html` を付けます（`htmlcov/index.html` が生成されます）。

### レポート出力の形式を変更したとき

出力を意図的に変えた場合は、保存してある比較用ファイルを作り直します。
生成された差分は必ず目視で確認してください。

```bash
python -m tests.regen_golden
```

---

## ファイル構成

```
backlog_report/
  ├── backlog_weekly_report.py   # メインスクリプト
  ├── check_api.py               # API 接続診断スクリプト
  ├── config.sample.yaml         # 設定ファイルのテンプレート（これをコピーして config.yaml を作成）
  ├── config.yaml                # 実際の設定ファイル（.gitignore で管理対象外）
  ├── pyproject.toml             # 依存関係・pytest / ruff の設定
  ├── tests/                     # 単体テスト
  ├── .gitignore
  ├── README.md                  # このファイル
  └── reports/                   # 生成されたレポートの保存先（.gitignore で管理対象外）
        └── YYYYMMDD_YYYYMMDD/
              ├── weekly_report[_フィルター名].md
              └── summary_report.md   # 全フィルターの横断サマリー
```
