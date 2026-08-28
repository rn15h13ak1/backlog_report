"""レポート出力テストで共有する固定データと生成ヘルパー"""
from datetime import date, datetime

import backlog_weekly_report as bwr

PERIOD_START = date(2026, 3, 2)
PERIOD_END = date(2026, 3, 8)
FROZEN_NOW = datetime(2026, 3, 9, 10, 30)

PROJECT_KEY = "PRJ"
PROJECT_NAME = "テストプロジェクト"


def issue(number: int, summary: str, status: str,
          assignee: str | None = None, due: str | None = None) -> dict:
    return {
        "id": number,
        "issueKey": f"{PROJECT_KEY}-{number}",
        "summary": summary,
        "status": {"name": status},
        "assignee": {"name": assignee} if assignee else None,
        "dueDate": due,
    }


def basic_data() -> dict:
    """
    ①〜⑤が一通り埋まり、等式が成立する最小データ。

    - PRJ-2 : 期間前からの残件で、期間中に完了（①と④）
    - PRJ-10: 期間中に作成され、未完了のまま（②と⑤）。件名に | と担当者なしを含む
    """
    carried = issue(2, "残っている課題", "処理中", "山田 太郎", "2026-03-10T00:00:00Z")
    completed = issue(2, "残っている課題", "完了", "山田 太郎", "2026-03-10T00:00:00Z")
    created = issue(10, "新しい課題 | 記号入り", "未対応")
    return {
        "carry_over": [carried],
        "new_issues": [created],
        "reopened": [],
        "completed": [completed],
        "incomplete": [created],
        "unknown_statuses": set(),
        "comment_failures": set(),
    }


def make_report(data: dict, **kwargs) -> str:
    return bwr.generate_markdown_report(
        data, PROJECT_KEY, PROJECT_NAME, PERIOD_START, PERIOD_END, **kwargs
    )


def make_summary(filter_name: str = "フィルタA") -> str:
    return bwr.generate_summary_report(
        [(filter_name, basic_data())], PERIOD_START, PERIOD_END
    )
