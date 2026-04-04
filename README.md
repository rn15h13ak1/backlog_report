# Backlog レポート生成ツール

Backlog REST API を使って、指定期間の課題集計を Markdown ファイルとして手動生成します。
種別・カスタム属性・キーワードによるフィルターを複数定義でき、フィルターごとに個別のレポートファイルが出力されます。

---

## 集計内容

| 項目 | 内容 |
|------|------|
| **① 前週からの残件数** | 期間開始より前に作成され、期間開始時点でオープンだった課題 |
| **② 新規発生件数** | 対象期間に新しく作成された課題 |
| **⑤ 再オープン件数** | 期間開始時点で完了系だったが、期間中にオープン系へ変化した課題 |
| **③ 期間内完了件数** | 期間中にオープン系から完了系へ変化した課題 |
| **④ 当週未完了件数** | ① + ② + ⑤ のうち、③ で完了しなかった課題 |

各カテゴリに対して **Backlog の課題番号一覧（例: PROJ-101, PROJ-102）** も出力されます。

### ステータス定義

| 区分 | 対象ステータス |
|------|--------------|
| オープン系（未完了） | 未対応（1）、処理中（2） |
| 完了系 | 処理済み（3）、完了（4） |

ステータス ID は `config.yaml` の `open_status_ids` / `closed_status_ids` で変更できます。

### 等式チェック

`① + ② + ⑤ = ③ + ④` が成立しない場合、レポートのサマリーに自動で警告が表示されます。

### ステータス表示について

現在のステータス（レポート実行時点）ではなく、各カテゴリに対応した時点のステータスを表示します。

| カテゴリ | 表示ステータスの基準 |
|---|---|
| ① 前週残件 | 期間開始時点 |
| ② 新規発生 | 期間終了時点 |
| ③ 当週完了 | 現在（完了系のため変化なし） |
| ④ 当週未完了 | 現在 |
| ⑤ 再オープン | 期間終了時点 |

---

## セットアップ

### 1. 必要ライブラリのインストール

```bash
pip install pyyaml
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
| 前週からの残件数 | N 件 |
| 新規発生件数 | N 件 |
| 再オープン件数 | N 件 |
| 当週完了件数 | N 件 |
| 当週未完了件数 | N 件 |

等式 `① + ② + ⑤ = ③ + ④` が成立しない場合は警告をサマリー直下に表示。

**各カテゴリのセクション（5 つ）**

1. **前週からの残件** — 課題番号のコンパクト一覧 ＋ 詳細テーブル（折りたたみ）
2. **新規発生** — 同上
3. **再オープン** — 同上
4. **当週完了** — 同上
5. **当週未完了一覧** — 同上（詳細テーブルは最大 50 件）

詳細テーブルの列：

| 課題番号 | 件名 | ステータス | 担当者 | 期限日 |
|---------|------|-----------|-------|-------|

詳細テーブルは `<details>` タグで折りたたまれており、GitHub や対応 Markdown ビューアで展開できます。

---

## 注意事項

- **API キーの管理**: `config.yaml` は `.gitignore` で管理対象外になっています。`config.sample.yaml` をコピーして使用してください。
- **処理時間**: 各課題のコメント履歴を取得してステータス変化を判定するため、課題数が多いと時間がかかります（100 件程度であれば数分程度）。
- **オンプレミス版について**: `base_path`（例: `"/backlog"`）と `ssl_verify: false` を設定することでオンプレミス版にも対応しています。

---

## ファイル構成

```
backlog_report/
  ├── backlog_weekly_report.py   # メインスクリプト
  ├── config.sample.yaml         # 設定ファイルのテンプレート（これをコピーして config.yaml を作成）
  ├── config.yaml                # 実際の設定ファイル（.gitignore で管理対象外）
  ├── .gitignore
  ├── README.md                  # このファイル
  └── reports/                   # 生成されたレポートの保存先
        └── YYYYMMDD_YYYYMMDD/
              └── weekly_report[_フィルター名].md
```
