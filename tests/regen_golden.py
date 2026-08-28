"""
ゴールデンファイルを再生成する。

出力形式を意図的に変更したときだけ実行し、生成された差分を必ず目視で確認すること。

    python -m tests.regen_golden
"""
from datetime import datetime
from pathlib import Path

import backlog_weekly_report as bwr
from tests.report_fixtures import FROZEN_NOW, basic_data, make_report, make_summary

GOLDEN_DIR = Path(__file__).parent / "golden"


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW.replace(tzinfo=tz) if tz else FROZEN_NOW


def main() -> None:
    bwr.datetime = _FrozenDatetime
    GOLDEN_DIR.mkdir(exist_ok=True)

    files = {
        "weekly_report_basic.md": make_report(basic_data()) + "\n",
        "weekly_report_filtered.md": make_report(
            basic_data(),
            filter_name="バグ対応",
            filter_description="バグ種別の課題集計",
            filter_summary="種別: バグ",
        ) + "\n",
        "summary_report_basic.md": make_summary(),
    }
    for name, content in files.items():
        (GOLDEN_DIR / name).write_text(content, encoding="utf-8")
        print(f"書き出し: tests/golden/{name}  ({len(content)} 文字)")


if __name__ == "__main__":
    main()
