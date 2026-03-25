# Backlog レポート生成ツール

Backlog REST API を使って、指定期間の課題集計を Markdown ファイルとして手動生成します。
種別・カスタム属性によるフィルターを複数定義でき、フィルターごとに個別のレポートファイルが出力されます。

---

## 集計内容

| 項目 | 内容 |
|------|------|
| **期間開始前からの残件数** | 期間開始より前に作成され、現在も未完了の課題 |
| **新規発生件数** | 対象期間に新しく作成された課題 |
| **期間内完了件数** | 対象期間に更新された完了ステータスの課題 |
| **未完了件数** | 現在オープンの課題 |

各カテゴリに対して **Backlog の課題番号一覧（例: PROJ-101, PROJ-102）** も出力されます。

### ステータス定義

| 区分 | 対象ステータス |
|------|--------------|
| 未完了 | 未対応（1）、処理中（2） |
| 完了   | 処理済み（3）、完了（4） |

### 差し戻し検知

`残件数 + 新規発生 = 完了 + 未完了` が成立しない場合、レポートのサマリーに以下の警告が自動表示されます。

> ⚠️ **注意**: 残件数（X）＋ 新規発生（Y）＝ N に対し、完了（Z）＋ 未完了（W）＝ M と一致しません。
> 期間中に処理済みから処理中への差し戻しが発生している可能性があります。

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

### 3. `config.yaml` の設定

以下の 3 箇所を自分の環境に合わせて編集してください。

```yaml
backlog:
  space_host: "yourcompany.backlog.com"  # Backlog のホスト名
  api_key: "YOUR_API_KEY_HERE"            # 取得した API キー
  project_key: "YOUR_PROJECT_KEY"         # プロジェクトキー
```

**プロジェクトキーの確認方法:**
URL が `https://yourcompany.backlog.com/projects/MYAPP` なら、プロジェクトキーは `MYAPP` です。

---

## 実行方法

実行はすべて手動です。`config.yaml` で期間を設定してからスクリプトを起動してください。

### 基本実行（config.yaml の期間設定を使用）

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

**config.yaml で期間を指定する場合（推奨）:**

```yaml
report:
  period:
    from: "2026-03-01"
    to:   "2026-03-31"
```

次回の集計期間に変更する際は `from` / `to` の日付を書き換えて実行します。
`period` をコメントアウトすると `target_week` の自動計算に切り替わります。

**コマンドライン引数で期間を指定する場合:**

```bash
# 期間を直接指定
python backlog_weekly_report.py --from 2026-03-01 --to 2026-03-31

# 前週を自動計算（config.yaml の target_week: "previous" と同等）
python backlog_weekly_report.py --week previous

# 今週（月曜〜今日）を自動計算
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

```yaml
filters:
  - name: "バグ対応"
    description: "バグ種別の課題"
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

  - name: "優先度_高"
    custom_fields:
      - field_name: "優先度"
        values:
          - "高"
          - "最高"
```

### フィルター設定の仕様

| 項目 | 説明 |
|------|------|
| `name` | レポートファイル名・見出しに使用（必須） |
| `description` | レポートに記載されるメモ（任意） |
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

`filters` を空にするかコメントアウトすると、フィルターなしで全課題を集計した 1 ファイルが生成されます。

---

## 出力ファイル

`config.yaml` の `report.output_dir`（デフォルト: `./reports`）にファイルが生成されます。

**フィルターなしの場合:**
```
reports/
  report_20260316_20260322.md
```

**フィルターありの場合（フィルターごとに 1 ファイル）:**
```
reports/
  report_20260316_20260322_バグ対応.md
  report_20260316_20260322_Aチーム_タスク.md
  report_20260316_20260322_優先度_高.md
```

---

## 注意事項

- **完了件数の近似について**: Backlog API には「完了した日付」でのフィルタ機能がありません。「対象期間に更新された完了ステータスの課題」で近似しているため、期間外で完了し期間内に別の更新があった課題が含まれる場合があります。
- **API キーの管理**: `config.yaml` に API キーを記載した場合は Git 管理対象から外してください（`.gitignore` に追加）。
- **API レート制限**: 課題数が多い場合、取得に時間がかかることがあります。

---

## ファイル構成

```
backlog_report/
  ├── backlog_weekly_report.py   # メインスクリプト
  ├── config.yaml                # 設定ファイル（編集が必要）
  ├── .gitignore                 # .git-output.log を管理対象外に設定
  ├── README.md                  # このファイル
  └── reports/                   # 生成されたレポートの保存先
        └── report_YYYYMMDD_YYYYMMDD[_フィルター名].md
```
