# backlog_report

共通規約: [../ws-conventions/README.md](../ws-conventions/README.md) に従う（`~/ws` 配下の全リポジトリ共通）。

## 実装の置き場所

入口は `backlog_weekly_report.py`（通しの処理と、これまでどおりの名前での公開）。
中身は `backlog_report/` に役割ごとに分けてある（`core` / `client` / `period` /
`collect` / `snapshot` / `report` / `weekly3`）。

テストは入口の名前（`bwr.<名前>`）で参照する。モジュールの中の値を差し替える場合
（`time.sleep` や `datetime` の凍結）は、その値を持つモジュールを直接指す。

## 実装を変えたときに実行する

```bash
python -m pytest tests/ -q
python -m ruff check .
python -m mypy
```

`mypy` は集計データ（`ReportData`）のキー名の打ち間違いと、値の型の取り違えを見る。
レポートの出力を意図的に変えた場合は、比較用ファイルを作り直して差分を目視する。

```bash
python -m tests.regen_golden
```

Markdown を編集したときの検査は共通規約 D に従う。
