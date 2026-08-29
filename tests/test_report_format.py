"""
レポート出力の形式を固定するテスト。

決まった入力に対する出力を tests/golden/ に保存し、丸ごと比較する。
見出し・表・警告文・サマリーの並びが意図せず変わったらここで落ちる。

とくに summary_report は Excel へコピー＆ペーストして使うため、
「1課題につき ●課題番号｜期限｜ステータス 行と件名行の2行」という
形が崩れると実務に直接響く。この不変条件は個別のテストでも押さえている。

ゴールデンファイルを意図的に更新するとき:
    python -m tests.regen_golden
"""
from datetime import datetime
from pathlib import Path

import pytest

import backlog_weekly_report as bwr
from tests.report_fixtures import (
    FROZEN_NOW,
    PERIOD_END,
    PERIOD_START,
    basic_data,
    issue,
    make_report,
    make_summary,
)

GOLDEN_DIR = Path(__file__).parent / "golden"


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """生成日時を固定して出力を再現可能にする"""
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return FROZEN_NOW.replace(tzinfo=tz) if tz else FROZEN_NOW

    monkeypatch.setattr(bwr, "datetime", FrozenDatetime)


def read_golden(name: str) -> str:
    return (GOLDEN_DIR / name).read_text(encoding="utf-8")


# ==================================================================
# ゴールデン比較
# ==================================================================

def test_weekly_report_matches_golden():
    """週次レポートの全文が保存済みの内容と一致すること"""
    actual = make_report(basic_data())
    # 生成される文字列は末尾に改行を持たないため、ファイル側の1つ分を足して比較する
    assert actual + "\n" == read_golden("weekly_report_basic.md")


def test_weekly_report_with_filter_matches_golden():
    """フィルター名・説明・条件がヘッダーに載ること"""
    actual = make_report(
        basic_data(),
        filter_name="バグ対応",
        filter_description="バグ種別の課題集計",
        filter_summary="種別: バグ",
    )
    assert actual + "\n" == read_golden("weekly_report_filtered.md")


def test_summary_report_matches_golden():
    """サマリーレポート（Excel 貼り付け用）の全文が一致すること"""
    assert make_summary() == read_golden("summary_report_basic.md")


# ==================================================================
# Excel 貼り付け形式の不変条件
# ==================================================================

def test_summary_report_is_two_lines_per_issue():
    """1課題につき「●…」行と件名行の2行で構成されること"""
    lines = make_summary().splitlines()
    body = lines[lines.index("フィルタA") + 2:]   # 見出しと件数行を飛ばす
    assert len(body) % 2 == 0
    for marker, summary in zip(body[0::2], body[1::2], strict=True):
        assert marker.startswith("●")
        assert marker.count("｜") == 2          # 課題番号｜期限｜ステータス
        assert not summary.startswith("●")


def test_summary_report_sorts_issue_numbers_numerically():
    """PRJ-2 が PRJ-10 より先に並ぶこと（文字列順では逆になる）"""
    keys = [ln for ln in make_summary().splitlines() if ln.startswith("●")]
    assert keys[0].startswith("●PRJ-2｜")
    assert keys[1].startswith("●PRJ-10｜")


def test_summary_report_shows_due_date_placeholder():
    """期限なしの課題は「期限：なし」と表示されること"""
    assert "●PRJ-10｜期限：なし｜未対応" in make_summary()


def test_summary_report_separates_multiple_filters():
    """複数フィルターは ---- 行で区切られること"""
    text = bwr.generate_summary_report(
        [("フィルタA", basic_data()), ("フィルタB", basic_data())],
        PERIOD_START, PERIOD_END,
    )
    assert text.count("----") == 1
    assert text.count("残:1 / 新規:1 / 再オープン:0 / 完了:1 / 未完了:1") == 2


# ==================================================================
# 警告ブロック
# ==================================================================

def test_equation_mismatch_shows_warning():
    """①+②+③ ≠ ④+⑤ のとき警告が出ること"""
    data = basic_data()
    data["completed"] = []            # 等式を意図的に崩す
    text = make_report(data)
    assert "⚠️ **注意**" in text
    assert "一致しません" in text


def test_no_warning_when_equation_holds():
    assert "⚠️" not in make_report(basic_data())


def test_unknown_status_shows_warning_with_names():
    """設定外ステータスがあれば、その名前を挙げて警告すること"""
    data = basic_data()
    data["unknown_statuses"] = {"レビュー中", "保留"}
    text = make_report(data)
    assert "ステータス一覧" in text
    # sorted() はコードポイント順のため カタカナ(U+30EC) が 漢字(U+4FDD) より先に来る
    assert "レビュー中、保留" in text


def test_comment_failure_shows_warning_with_count():
    data = basic_data()
    data["comment_failures"] = {101, 102, 103}
    assert "3 件の課題でコメント履歴の取得に失敗" in make_report(data)


# ==================================================================
# 表の描画
# ==================================================================

def test_empty_category_shows_placeholder():
    data = basic_data()
    assert "_（なし）_" in make_report(data)       # 課題番号の一覧
    assert "_（該当なし）_" in make_report(data)   # 詳細テーブル


def test_pipe_in_summary_is_escaped_in_table():
    """件名の | は表を壊すため全角 ｜ に置換されること"""
    text = make_report(basic_data())
    assert "| PRJ-10 | 新しい課題 ｜ 記号入り |" in text


def test_unassigned_issue_is_labeled():
    assert "_未割当_" in make_report(basic_data())


def test_table_truncates_at_limit():
    """詳細テーブルは上限件数で打ち切られ、残数が表示されること"""
    many = [issue(n, f"課題{n}", "処理中") for n in range(1, 41)]
    table = bwr.format_issue_table(many)
    assert table.count("| PRJ-") == bwr.TABLE_MAX_DISPLAY
    assert f"他 {40 - bwr.TABLE_MAX_DISPLAY} 件" in table


def test_incomplete_section_uses_larger_limit():
    """⑤当週未完了だけは上限が 50 件であること"""
    data = basic_data()
    data["incomplete"] = [issue(n, f"課題{n}", "処理中") for n in range(1, 61)]
    data["carry_over"] = data["incomplete"]      # 等式警告を避ける
    data["new_issues"] = []
    data["completed"] = []
    text = make_report(data)
    assert f"他 {60 - bwr.TABLE_MAX_DISPLAY_INCOMPLETE} 件" in text


def test_key_list_truncates_over_limit():
    """課題番号のコンパクト一覧は上限を超えると「他N件」になること"""
    many = [issue(n, f"課題{n}", "処理中") for n in range(1, 26)]
    text = bwr.keys_str(many)
    assert text.count("、") == bwr.KEYS_MAX_DISPLAY - 1
    assert f"他{25 - bwr.KEYS_MAX_DISPLAY}件" in text


def test_report_has_five_sections():
    text = make_report(basic_data())
    for heading in ("## ① 前週残件", "## ② 新規発生", "## ③ 再オープン",
                    "## ④ 当週完了", "## ⑤ 当週未完了"):
        assert heading in text
    assert text.count("<details>") == 5
    assert text.count("</details>") == 5


def test_period_is_shown_in_header():
    text = make_report(basic_data())
    assert f"# レポート — {PERIOD_START:%Y/%m/%d} 〜 {PERIOD_END:%Y/%m/%d}" in text
    assert "> 生成日時: 2026-03-09 10:30" in text
