# backlog_report

共通規約: [../ws-conventions/README.md](../ws-conventions/README.md) に従う（`~/ws` 配下の全リポジトリ共通）。

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
