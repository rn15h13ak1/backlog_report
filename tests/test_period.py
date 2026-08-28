"""期間計算・表示ユーティリティのテスト"""
from datetime import date

import pytest

from backlog_weekly_report import _fmt_due, _issue_sort_key, get_week_range, safe_filename

# 2026-03-05 は木曜日
THURSDAY = date(2026, 3, 5)


@pytest.mark.parametrize("week_start,expected", [
    ("monday",  (date(2026, 2, 23), date(2026, 3, 1))),
    ("sunday",  (date(2026, 2, 22), date(2026, 2, 28))),
    ("thursday", (date(2026, 2, 26), date(2026, 3, 4))),
    ("friday",  (date(2026, 2, 20), date(2026, 2, 26))),
])
def test_previous_week(week_start, expected):
    assert get_week_range("previous", week_start, today=THURSDAY) == expected


@pytest.mark.parametrize("week_start,expected_start", [
    ("monday",   date(2026, 3, 2)),
    ("sunday",   date(2026, 3, 1)),
    ("thursday", date(2026, 3, 5)),
    ("friday",   date(2026, 2, 27)),
])
def test_current_week_ends_today(week_start, expected_start):
    start, end = get_week_range("current", week_start, today=THURSDAY)
    assert start == expected_start
    assert end == THURSDAY


def test_japanese_weekday_matches_english():
    assert get_week_range("previous", "月", today=THURSDAY) == \
           get_week_range("previous", "monday", today=THURSDAY)


def test_previous_week_is_seven_days():
    start, end = get_week_range("previous", "monday", today=THURSDAY)
    assert (end - start).days == 6


def test_invalid_week_start_exits():
    with pytest.raises(SystemExit):
        get_week_range("previous", "someday", today=THURSDAY)


# ------------------------------------------------------------------

@pytest.mark.parametrize("keys,expected_order", [
    (["PRJ-10", "PRJ-2", "PRJ-1"], ["PRJ-1", "PRJ-2", "PRJ-10"]),
    (["B-1", "A-2", "A-10"], ["A-2", "A-10", "B-1"]),
])
def test_issue_sort_key_is_numeric(keys, expected_order):
    issues = [{"issueKey": k} for k in keys]
    assert [i["issueKey"] for i in sorted(issues, key=_issue_sort_key)] == expected_order


def test_issue_sort_key_handles_malformed_keys():
    assert _issue_sort_key({"issueKey": "PRJ-abc"}) == ("PRJ", 0)
    assert _issue_sort_key({"issueKey": "NOKEY"}) == ("NOKEY", 0)
    assert _issue_sort_key({}) == ("", 0)


# ------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("2026-04-07T00:00:00Z", "4/7"),
    ("2026-12-25T00:00:00Z", "12/25"),
    (None, "なし"),
    ("", "なし"),
])
def test_fmt_due(raw, expected):
    assert _fmt_due(raw) == expected


def test_safe_filename_replaces_invalid_chars():
    assert safe_filename('A/B:C*D?E"F<G>H|I') == "A_B_C_D_E_F_G_H_I"
    assert safe_filename("全角　スペース") == "全角_スペース"
    assert safe_filename("バグ対応") == "バグ対応"
