"""classify_issue_from_comments の分類ロジックのテスト"""
from datetime import date

import pytest

from backlog_weekly_report import classify_issue_from_comments, to_local_date

OPEN = {"未対応", "処理中"}
CLOSED = {"処理済み", "完了"}

PERIOD_START = date(2026, 3, 2)   # 月
PERIOD_END = date(2026, 3, 8)     # 日


def issue(created: str, status: str = "処理中", issue_id: int = 1) -> dict:
    return {"id": issue_id, "issueKey": f"TEST-{issue_id}",
            "created": created, "status": {"name": status}}


def comment(created: str, from_status: str, to_status: str) -> dict:
    return {
        "id": 1,
        "created": created,
        "changeLog": [
            {"field": "status", "originalValue": from_status, "newValue": to_status},
        ],
    }


def classify(iss: dict, comments: list) -> dict:
    return classify_issue_from_comments(
        iss, comments, PERIOD_START, PERIOD_END, CLOSED, OPEN
    )


# ------------------------------------------------------------------
# to_local_date（JST 変換）
# ------------------------------------------------------------------

@pytest.mark.parametrize("iso,expected", [
    ("2026-03-01T00:00:00Z", "2026-03-01"),   # JST 09:00 同日
    ("2026-03-01T14:59:59Z", "2026-03-01"),   # JST 23:59 同日
    ("2026-03-01T15:00:00Z", "2026-03-02"),   # JST 翌日 00:00
    ("2026-03-01T23:30:00Z", "2026-03-02"),
    ("", ""),
    ("2026-03-01", "2026-03-01"),             # パース不能でも先頭10文字
])
def test_to_local_date(iso, expected):
    assert to_local_date(iso) == expected


# ------------------------------------------------------------------
# 基本パターン
# ------------------------------------------------------------------

def test_no_comments_uses_current_status():
    """コメントが無い課題は現在のステータスが期間開始時＝終了時のステータスになる"""
    result = classify(issue("2026-02-01T01:00:00Z", "処理中"), [])
    assert result["is_carry_over"] is True     # ① 期間前作成 + 開始時オープン
    assert result["is_new"] is False
    assert result["is_completed"] is False
    assert result["is_reopened"] is False
    assert result["status_at_start"] == "処理中"
    assert result["status_at_end"] == "処理中"


def test_closed_before_period_is_not_carry_over():
    """期間開始前に完了していれば①に含めない"""
    result = classify(
        issue("2026-02-01T01:00:00Z", "完了"),
        [comment("2026-02-10T01:00:00Z", "処理中", "完了")],
    )
    assert result["is_carry_over"] is False
    assert result["status_at_start"] == "完了"


def test_created_in_period_is_new():
    """② 期間中に作成された課題"""
    result = classify(issue("2026-03-03T01:00:00Z", "処理中"), [])
    assert result["is_new"] is True
    assert result["is_carry_over"] is False


def test_completed_during_period():
    """④ 期間中にオープン系 → 完了系へ変化"""
    result = classify(
        issue("2026-02-01T01:00:00Z", "完了"),
        [comment("2026-03-04T01:00:00Z", "処理中", "完了")],
    )
    assert result["is_completed"] is True
    assert result["is_carry_over"] is True      # 開始時は処理中なので①にも入る
    assert result["status_at_start"] == "処理中"
    assert result["status_at_end"] == "完了"


def test_closed_to_closed_is_not_completed():
    """完了系 → 完了系（処理済み → 完了）は④に含めない"""
    result = classify(
        issue("2026-02-01T01:00:00Z", "完了"),
        [
            comment("2026-02-20T01:00:00Z", "処理中", "処理済み"),
            comment("2026-03-04T01:00:00Z", "処理済み", "完了"),
        ],
    )
    assert result["is_completed"] is False
    assert result["is_carry_over"] is False     # 開始時は処理済み（完了系）
    assert result["status_at_start"] == "処理済み"
    assert result["status_at_end"] == "完了"


def test_reopened_during_period():
    """③ 期間開始時は完了系で、期間中にオープン系へ戻った課題"""
    result = classify(
        issue("2026-02-01T01:00:00Z", "処理中"),
        [
            comment("2026-02-20T01:00:00Z", "処理中", "完了"),
            comment("2026-03-04T01:00:00Z", "完了", "処理中"),
        ],
    )
    assert result["is_reopened"] is True
    assert result["is_carry_over"] is False     # 開始時は完了なので①ではない
    assert result["status_at_start"] == "完了"
    assert result["status_at_end"] == "処理中"


# ------------------------------------------------------------------
# 回帰テスト
# ------------------------------------------------------------------

def test_status_change_only_after_period():
    """
    期間後にのみステータスが変化した課題（コミット cfcd9ff の回帰）。

    現在のステータスは「完了」だが、その変化は期間後なので
    期間開始時・終了時はいずれも変化前の「処理中」でなければならない。
    """
    result = classify(
        issue("2026-02-01T01:00:00Z", "完了"),
        [comment("2026-03-20T01:00:00Z", "処理中", "完了")],
    )
    assert result["status_at_start"] == "処理中"
    assert result["status_at_end"] == "処理中"
    assert result["is_carry_over"] is True
    assert result["is_completed"] is False


def test_first_change_in_period_derives_start_status():
    """期間中に初めて変化した場合、開始時ステータスは変化の from 側"""
    result = classify(
        issue("2026-02-01T01:00:00Z", "完了"),
        [comment("2026-03-05T01:00:00Z", "未対応", "完了")],
    )
    assert result["status_at_start"] == "未対応"
    assert result["status_at_end"] == "完了"
    assert result["is_completed"] is True


# ------------------------------------------------------------------
# JST 境界
# ------------------------------------------------------------------

def test_utc_evening_change_belongs_to_next_jst_day():
    """
    UTC 2026-03-08T15:30 は JST では 2026-03-09（期間終了の翌日）。
    期間内の変化として扱ってはならない。
    """
    result = classify(
        issue("2026-02-01T01:00:00Z", "完了"),
        [comment("2026-03-08T15:30:00Z", "処理中", "完了")],
    )
    assert result["is_completed"] is False     # 期間後の変化
    assert result["status_at_start"] == "処理中"
    assert result["status_at_end"] == "処理中"


def test_utc_evening_creation_belongs_to_next_jst_day():
    """UTC 2026-03-01T15:00 作成は JST 2026-03-02 = 期間初日なので②に入る"""
    result = classify(issue("2026-03-01T15:00:00Z", "処理中"), [])
    assert result["is_new"] is True
    assert result["is_carry_over"] is False


def test_utc_morning_creation_stays_on_same_jst_day():
    """UTC 2026-03-01T14:59 作成は JST でも 2026-03-01 = 期間前なので①"""
    result = classify(issue("2026-03-01T14:59:00Z", "処理中"), [])
    assert result["is_new"] is False
    assert result["is_carry_over"] is True


# ------------------------------------------------------------------
# 未設定ステータスの検出
# ------------------------------------------------------------------

def test_seen_statuses_collects_unknown_names():
    """設定に無いステータス名も seen_statuses に含まれる"""
    result = classify(
        issue("2026-02-01T01:00:00Z", "処理中"),
        [comment("2026-03-04T01:00:00Z", "処理中", "レビュー中")],
    )
    assert "レビュー中" in result["seen_statuses"]
    assert result["seen_statuses"] - (OPEN | CLOSED) == {"レビュー中"}
