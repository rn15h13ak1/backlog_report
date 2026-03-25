# Backlog 週次レポート生成ツール

Backlog REST APIを使って、週次報告に必要な集計データをMarkdownファイルとして自動生成します。

---

## 集計内容

| 項目 | 内容 |
|------|------|
| **前週からの残件数** | 対象週の開始より前に作成され、現在も未完了の課題 |
| **新規発生件数** | 対象週（月〜日）に新しく作成された課題 |
| **当週完了件数** | 対象週に更新・完了したステータスの課題 |
| **未完了件数** | 現在オープン（未対応・処理中・処理済み）の課題 |

各カテゴリに対して **BacklogのIssue番号一覧（例: PROJ-101, PROJ-102）** も出力されます。

---

## セットアップ手順

### 1. 必要ライブラリのインストール

```bash
pip install pyyaml
```

### 2. Backlog APIキーの取得

1. Backlogにログイン
2. 右上のユーザーアイコン → **個人設定** を開く
3. 左メニューの **API** → **APIキーを発行する**
4. 発行されたキーをコピー

### 3. `config.yaml` の設定

`config.yaml` をテキストエディタで開き、以下の3箇所を編集してください：

```yaml
backlog:
  space_host: "yourcompany.backlog.com"   # ← あなたのBacklogのホスト名に変更
  api_key: "YOUR_API_KEY_HERE"             # ← 取得したAPIキーに変更
  project_key: "YOUR_PROJECT_KEY"          # ← 対象プロジェクトのキーに変更
```

**プロジェクトキーの確認方法:**
BacklogのプロジェクトURLが `https://yourcompany.backlog.com/projects/MYAPP` なら、プロジェクトキーは `MYAPP` です。

---

## 実行方法

### 前週のレポートを生成（推奨: 毎週月曜日に実行）

```bash
python backlog_weekly_report.py
```

### 今週（月曜〜今日まで）のレポートを生成

```bash
python backlog_weekly_report.py --week current
```

### 別の設定ファイルを指定

```bash
python backlog_weekly_report.py --config /path/to/other_config.yaml
```

---

## 出力ファイル

`reports/` ディレクトリにMarkdownファイルが生成されます：

```
reports/
  weekly_report_20260316_20260322.md   ← 2026年3月16日〜22日のレポート
  weekly_report_20260323_20260329.md   ← 2026年3月23日〜29日のレポート
  ...
```

### レポートの例

```markdown
# 週次レポート — 2026/03/16 〜 2026/03/22

> プロジェクト: **MyApp** (`MYAPP`)
> 生成日時: 2026-03-23 09:00

## サマリー
| 項目 | 件数 |
|------|------|
| 前週からの残件数 | **12** 件 |
| 新規発生件数 | **5** 件 |
| 当週完了件数 | **8** 件 |
| 現在の未完了件数 | **9** 件 |

## 当週完了
MYAPP-101、MYAPP-98、MYAPP-95 ...
```

---

## 定期実行の設定（オプション）

毎週月曜日の朝に自動実行したい場合：

### macOS / Linux（crontab）

```bash
# crontabを編集
crontab -e

# 毎週月曜 9:00 に実行
0 9 * * 1 cd /path/to/backlog_report && python backlog_weekly_report.py
```

### Windows（タスクスケジューラ）

1. 「タスクスケジューラ」を起動
2. 「基本タスクの作成」→ 毎週月曜日を選択
3. 操作: `python C:\path\to\backlog_report\backlog_weekly_report.py`

---

## 注意事項

- **「当週完了件数」の補足**: Backlog APIには「完了した日付」でのフィルタ機能がないため、「対象週に**更新された**完了ステータスの課題」で近似しています。対象週中に完了→再オープンされた課題も含まれる場合があります。
- APIキーはクレデンシャル情報です。`config.yaml` をGitなどで共有する場合は `.gitignore` に追加してください。
- Backlog APIの利用制限（レートリミット）に配慮し、大量の課題がある場合は取得に少し時間がかかります。

---

## ファイル構成

```
backlog_report/
  ├── backlog_weekly_report.py   # メインスクリプト
  ├── config.yaml                # 設定ファイル（編集が必要）
  ├── README.md                  # このファイル
  └── reports/                   # 生成されたレポートの保存先
        └── weekly_report_YYYYMMDD_YYYYMMDD.md
```
