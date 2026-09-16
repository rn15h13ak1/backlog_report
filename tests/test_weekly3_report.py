"""
docmold の `weekly3` へ渡す Markdown の形式を固定するテスト。

weekly3 は「見出し 2 を 4 つ置き、1 つ目をトピックス、2〜4 つ目を 3 列として組み立てる」
という前提で動く。この形が崩れると docmold 側で警告が出るか、列が壊れる。

列の見出しには期間を自分で書き込む。docmold の column_periods は見出しに区切り（〜）が
あれば触らないため、7 日以外の期間でも正しい範囲が出る。
"""
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

import backlog_weekly_report as bwr
from tests.report_fixtures import PERIOD_END, PERIOD_START, basic_data, issue


def make(prev_snapshot=None, reason="", data=None, name="バグ対応"):
    return bwr.generate_weekly3_report(
        [(name, data or basic_data())], "PRJ", "テストプロジェクト",
        PERIOD_START, PERIOD_END, prev_snapshot, reason,
    )


def prev_snapshot(counts=None, completed=None, incomplete=None, name="バグ対応"):
    entry = {"name": name, "condition": "",
             "completed": completed or [], "incomplete": incomplete or []}
    if counts is not None:
        entry["counts"] = counts
    return {"version": bwr.SNAPSHOT_VERSION,
            "period": {"from": "2026-02-23", "to": "2026-03-01"},
            "filters": [entry]}


def headings(text: str, level: int = 2) -> list:
    mark = "#" * level
    return [ln[len(mark) + 1:] for ln in text.splitlines() if ln.startswith(f"{mark} ")]


# ==================================================================
# 全体の骨格
# ==================================================================

def test_front_matter_declares_weekly3():
    text = make()
    assert text.startswith("---\n")
    head = text.split("---\n")[1]
    assert "type: weekly3" in head
    assert "title: テストプロジェクト 課題サマリー" in head
    assert "期間: 2026-03-02 〜 2026-03-08" in head


def test_has_exactly_four_level2_headings():
    """weekly3 は見出し 2 が 4 つでないと警告する"""
    assert len(headings(make())) == 4


def test_first_heading_is_topics_and_rest_are_periods():
    first, *columns = headings(make())
    assert first == "トピックス"
    assert columns == ["前週（2/23〜3/1）", "今週（3/2〜3/8）", "来週の予定（3/9〜3/15）"]


def test_column_periods_follow_the_period_length():
    """7 日以外の期間でも、前後の列が同じ長さでずれること"""
    text = bwr.generate_weekly3_report(
        [("バグ対応", basic_data())], "PRJ", "P",
        date(2026, 3, 2), date(2026, 3, 4),   # 3 日間
        None, "",
    )
    _, *columns = headings(text)
    assert columns == ["前週（2/27〜3/1）", "今週（3/2〜3/4）", "来週の予定（3/5〜3/7）"]


def test_topics_has_a_level3_heading():
    """トピックスは小見出しごとの枠に分かれるため、見出し 3 が要る"""
    text = make()
    topics = text.split("## トピックス")[1].split("## 前週")[0]
    assert "### 集計の概要" in topics
    assert "| 区分 | 残 | 新規 | 再オープン | 完了 | 未完了 |" in topics
    assert "| バグ対応 | 1 | 1 | 0 | 1 | 1 |" in topics


# ==================================================================
# 列の中身
# ==================================================================

def test_current_column_lists_completed_and_incomplete():
    text = make()
    column = text.split("## 今週")[1].split("## 来週")[0]
    assert "### バグ対応" in column
    assert "残:1 / 新規:1 / 再オープン:0 / 完了:1 / 未完了:1" in column
    assert "- PRJ-2｜期限：3/10｜完了｜残っている課題" in column
    # 半角の | はそのまま。区切りに使う全角の ｜ だけが置換される
    assert "- PRJ-10｜期限：なし｜未対応｜新しい課題 | 記号入り" in column


def test_entries_are_sorted_numerically():
    """PRJ-2 が PRJ-10 より先に来ること"""
    column = make().split("## 今週")[1].split("## 来週")[0]
    keys = re.findall(r"^- (PRJ-\d+)", column, re.M)
    assert keys == ["PRJ-2", "PRJ-10"]


def test_plan_column_carries_incomplete_with_overdue_count():
    """来週の予定は⑤を持ち越し、期限を過ぎたものを数えること"""
    data = basic_data()
    data["incomplete"] = [
        issue(1, "期限切れ", "処理中", due="2026-03-05T00:00:00Z"),   # 来週開始(3/9)より前
        issue(2, "まだ先", "未対応", due="2026-03-20T00:00:00Z"),
        issue(3, "期限なし", "未対応"),
    ]
    data["carry_over"] = data["incomplete"]
    data["new_issues"] = []
    data["completed"] = []
    column = make(data=data).split("## 来週の予定")[1]

    assert "予定:3 / 期限切れ:1" in column
    assert "- PRJ-1｜期限：3/5｜期限超過｜期限切れ" in column      # ステータスを差し替える
    assert "- PRJ-2｜期限：3/20｜未対応｜まだ先" in column
    assert "- PRJ-3｜期限：なし｜未対応｜期限なし" in column       # 期限なしは数えない


def test_empty_category_shows_placeholder():
    data = basic_data()
    data["incomplete"] = []
    data["completed"] = []
    data["carry_over"] = []
    data["new_issues"] = []
    column = make(data=data).split("## 来週の予定")[1]
    assert "_（該当なし）_" in column


# ==================================================================
# 前週の列
# ==================================================================

def test_previous_column_is_built_from_the_snapshot():
    snap = prev_snapshot(
        counts={"carry_over": 3, "new_issues": 2, "reopened": 1,
                "completed": 2, "incomplete": 4},
        completed=[bwr._snapshot_entry(issue(5, "先週完了", "完了"))],
        incomplete=[bwr._snapshot_entry(issue(7, "先週も未完了", "処理中"))],
    )
    column = make(prev_snapshot=snap).split("## 前週")[1].split("## 今週")[0]

    assert "残:3 / 新規:2 / 再オープン:1 / 完了:2 / 未完了:4" in column
    assert "- PRJ-5｜期限：なし｜完了｜先週完了" in column
    assert "- PRJ-7｜期限：なし｜処理中｜先週も未完了" in column


def test_previous_column_notes_when_snapshot_is_missing():
    column = make(reason="直前の期間の集計結果が見つかりません").split("## 前週")[1].split("## 今週")[0]
    assert "_（直前の期間の集計結果が見つかりません）_" in column


def test_previous_column_notes_when_counts_are_absent():
    """以前の形式（件数を持たない）のスナップショットでも壊れないこと"""
    snap = prev_snapshot(counts=None, incomplete=[bwr._snapshot_entry(issue(7, "x", "処理中"))])
    column = make(prev_snapshot=snap).split("## 前週")[1].split("## 今週")[0]
    assert "次回の実行から表示されます" in column


# ==================================================================
# トピックスの注意書き
# ==================================================================

@pytest.mark.parametrize("key,expected", [
    ("inflow", "期間中に対象へ入った 1 件を ② 新規発生に含めた"),
    ("outflow", "期間中に対象から外れた 1 件を ④ 当週完了に含めた"),
])
def test_flows_are_noted_in_topics(key, expected):
    data = basic_data()
    data[key] = [issue(42, "動いた課題", "処理中")]
    topics = make(data=data).split("## トピックス")[1].split("## 前週")[0]
    assert "### 注意" in topics
    assert expected in topics
    assert "PRJ-42" in topics


def test_other_notices_are_listed():
    data = basic_data()
    data["comment_failures"] = {1, 2}
    data["unknown_statuses"] = {"旧ステータス"}
    data["flow_unavailable"] = "直前の期間の集計結果が見つかりません"
    topics = make(data=data).split("## トピックス")[1].split("## 前週")[0]
    assert "2 件の課題でコメント履歴を取得できなかった" in topics
    assert "ステータス一覧に無い名前があった（旧ステータス）" in topics
    assert "直前の期間の集計結果が見つかりません" in topics


def test_no_notice_section_when_nothing_to_report():
    topics = make().split("## トピックス")[1].split("## 前週")[0]
    assert "### 注意" not in topics


def test_filterless_run_is_labeled():
    text = make(name=None)
    assert "### 全課題" in text


# ==================================================================
# docmold での変換（docmold が使える環境でのみ実行）
# ==================================================================

DOCMOLD = Path.home() / "ws" / "docmold" / "docmold.py"


@pytest.mark.skipif(not DOCMOLD.exists(), reason="docmold が無い")
def test_docmold_converts_without_warnings(tmp_path):
    """
    生成した Markdown が docmold の weekly3 で警告なく変換できること。

    見出し 2 の数が 4 つでない、front matter のキーを間違えている、といった
    取りこぼしは --strict の終了コードで分かる。
    """
    source = tmp_path / "weekly3_report.md"
    source.write_text(make(), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(DOCMOLD), str(source), "-o", str(tmp_path / "out"), "--strict"],
        capture_output=True, text=True, cwd=str(DOCMOLD.parent),
    )
    if "必要なライブラリが入っていません" in result.stdout:
        pytest.skip("docmold の依存ライブラリが入っていない")

    assert result.returncode == 0, result.stdout + result.stderr

    html = next((tmp_path / "out").glob("*.html")).read_text(encoding="utf-8")
    body = html.split("</style>", 1)[1]
    # 分類ごとにまとまり、その中に 3 つの期間が列として並ぶ
    assert re.findall(r'dm-group__title[^>]*>(.*?)</', body) == ["バグ対応"]
    assert sorted(set(re.findall(r'dm-column__title[^>]*>(.*?)</', body))) == [
        "今週（3/2〜3/8）", "前週（2/23〜3/1）", "来週の予定（3/9〜3/15）",
    ]
    assert "dm-entry__key" in body     # 課題がカードになっている
    assert "dm-count__label" in body   # 件数がチップになっている
