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


def make(prev_snapshot=None, reason="", data=None, name="バグ対応", conditions=None,
         url_base=""):
    return bwr.generate_weekly3_report(
        [(name, data or basic_data())], "PRJ", "テストプロジェクト",
        PERIOD_START, PERIOD_END, prev_snapshot, reason, conditions, url_base,
    )


def prev_snapshot(counts=None, completed=None, incomplete=None, name="バグ対応",
                  outflow=None):
    entry = {"name": name, "condition": "",
             "completed": completed or [], "incomplete": incomplete or [],
             "outflow": outflow or []}
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
    # 曜日を添える（docmold が期間を組み立てる場合と同じ見え方にする）
    assert "期間: 2026-03-02(月) 〜 2026-03-08(日)" in head


def test_has_exactly_four_level2_headings():
    """weekly3 は見出し 2 が 4 つでないと警告する"""
    assert len(headings(make())) == 4


def test_first_heading_is_topics_and_rest_are_periods():
    first, *columns = headings(make())
    assert first == "トピックス"
    assert columns == ["前週（2/23(月)〜3/1(日)）", "今週（3/2(月)〜3/8(日)）",
                       "来週の予定（3/9(月)〜3/15(日)）"]


def test_column_periods_follow_the_period_length():
    """7 日以外の期間でも、前後の列が同じ長さでずれること"""
    text = bwr.generate_weekly3_report(
        [("バグ対応", basic_data())], "PRJ", "P",
        date(2026, 3, 2), date(2026, 3, 4),   # 3 日間
        None, "",
    )
    _, *columns = headings(text)
    assert columns == ["前週（2/27(金)〜3/1(日)）", "今週（3/2(月)〜3/4(水)）",
                       "来週の予定（3/5(木)〜3/7(土)）"]


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
# 課題番号のリンク
# ==================================================================

SPACE = "https://example.backlog.jp"


def test_issue_keys_are_links_in_every_column():
    """今週・来週の予定に出る課題番号が、Backlog の課題ページへのリンクになること"""
    text = make(url_base=SPACE)
    for column in ("## 今週", "## 来週の予定"):
        body = text.split(column)[1].split("\n## ")[0]
        assert f"- [PRJ-10]({SPACE}/view/PRJ-10)｜" in body


def test_issue_keys_are_links_in_previous_column():
    """前週の列（スナップショットから復元した課題）もリンクにすること"""
    snap = prev_snapshot(
        counts={k: 1 for k in bwr.CATEGORY_KEYS},
        incomplete=[{"id": 7, "issueKey": "PRJ-7", "summary": "残り",
                     "status": "処理中", "dueDate": None}],
    )
    column = make(prev_snapshot=snap, url_base=SPACE) \
        .split("## 前週")[1].split("## 今週")[0]

    assert f"- [PRJ-7]({SPACE}/view/PRJ-7)｜" in column


def test_notice_issue_keys_are_links():
    """トピックスの注意書きに並ぶ課題番号もリンクにすること"""
    data = basic_data()
    data["inflow"] = [issue(11, "入ってきた課題", "未対応")]
    topics = make(data=data, url_base=SPACE).split("## トピックス")[1].split("## 前週")[0]

    assert f"[PRJ-11]({SPACE}/view/PRJ-11)" in topics


def test_issue_keys_stay_plain_without_url_base():
    """スペースの URL が分からないときは、これまでどおり課題番号のまま出すこと"""
    body = make().split("## 今週")[1].split("\n## ")[0]
    assert "- PRJ-10｜" in body
    assert "](" not in body


def test_issue_link_skips_placeholder_keys():
    """課題番号が取れなかった場合にリンクを作らないこと"""
    assert bwr._issue_link("-", SPACE) == "-"
    assert bwr._issue_link("", SPACE) == ""
    assert bwr._issue_link("PRJ-1", "") == "PRJ-1"


def test_issue_link_trims_trailing_slash():
    assert bwr._issue_link("PRJ-1", SPACE + "/") == f"[PRJ-1]({SPACE}/view/PRJ-1)"


# ==================================================================
# 抽出対象から外れた課題の印
# ==================================================================

def test_outflow_issue_is_labeled_in_this_week_column():
    """
    対象から外れて④に入れた課題は、記録されていたステータスのままだと
    「未対応なのに完了に数えられている」ように見えるため、印を付けること。
    """
    left = issue(3, "対象外になった課題", "未対応")
    data = basic_data()
    data["carry_over"] = data["carry_over"] + [left]
    data["completed"] = data["completed"] + [left]
    data["outflow"] = [left]

    body = make(data=data).split("## 今週")[1].split("\n## ")[0]

    assert "- PRJ-3｜期限：なし｜完了扱い｜対象外になった｜対象外になった課題" in body
    # 対象から外れていない課題はこれまでどおり
    assert "- PRJ-2｜期限：3/10｜完了｜残っている課題" in body


def test_outflow_label_is_not_applied_to_other_issues():
    body = make().split("## 今週")[1].split("\n## ")[0]
    assert "完了扱い" not in body


def test_outflow_issue_is_labeled_in_previous_column():
    """前週の列（スナップショットから復元）でも同じ印を付けること"""
    saved = {"id": 9, "issueKey": "PRJ-9", "summary": "外れた課題",
             "status": "処理中", "dueDate": None}
    snap = prev_snapshot(counts={k: 1 for k in bwr.CATEGORY_KEYS},
                         completed=[saved], outflow=[9])

    column = make(prev_snapshot=snap).split("## 前週")[1].split("## 今週")[0]

    assert "- PRJ-9｜期限：なし｜完了扱い｜対象外になった｜外れた課題" in column


def test_previous_column_without_outflow_key_still_works():
    """outflow を持たない以前の形式のスナップショットでも読めること"""
    saved = {"id": 9, "issueKey": "PRJ-9", "summary": "完了した課題",
             "status": "完了", "dueDate": None}
    snap = prev_snapshot(counts={k: 1 for k in bwr.CATEGORY_KEYS}, completed=[saved])
    del snap["filters"][0]["outflow"]

    column = make(prev_snapshot=snap).split("## 前週")[1].split("## 今週")[0]

    assert "- PRJ-9｜期限：なし｜完了｜完了した課題" in column


def test_outflow_issue_keeps_its_link():
    left = issue(3, "対象外になった課題", "未対応")
    data = basic_data()
    data["completed"] = data["completed"] + [left]
    data["outflow"] = [left]

    body = make(data=data, url_base=SPACE).split("## 今週")[1].split("\n## ")[0]

    assert f"- [PRJ-3]({SPACE}/view/PRJ-3)｜期限：なし｜完了扱い｜対象外になった｜" in body


def test_summary_report_keeps_plain_issue_keys():
    """Excel 貼り付け用のサマリーはこれまでどおり（リンクにしない）"""
    from tests.report_fixtures import make_summary
    assert "](" not in make_summary()


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
    left = issue(3, "対象外になった課題", "未対応")
    data = basic_data()
    data["completed"] = data["completed"] + [left]
    data["outflow"] = [left]

    source = tmp_path / "weekly3_report.md"
    source.write_text(make(data=data, url_base=SPACE), encoding="utf-8")

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
        "今週（3/2(月)〜3/8(日)）", "前週（2/23(月)〜3/1(日)）",
        "来週の予定（3/9(月)〜3/15(日)）",
    ]
    assert "dm-entry__key" in body     # 課題がカードになっている
    assert "dm-count__label" in body   # 件数がチップになっている
    # 課題番号のリンクが、カードの見出し欄の中に残っていること
    # （entry_card は欄を文字列のところだけで区切るため、リンクは消えない）
    assert re.search(
        r'dm-entry__key[^>]*><a href="https://example\.backlog\.jp/view/PRJ-10">PRJ-10</a>',
        body,
    ), body
    # 対象から外れた課題は「完了扱い」のバッジと、理由の欄になる
    # （理由の文言に「未対応」などを混ぜると、そちらがバッジとして拾われてしまう）
    assert re.search(
        r'<span class="dm-badge dm-badge--ok">完了扱い</span>'
        r'<span class="dm-entry__meta">対象外になった</span>',
        body,
    ), body


# ==================================================================
# 前週の見出しに出す期間
# ==================================================================

def test_previous_heading_uses_the_recorded_period():
    """
    前週の見出しは、記録された期間をそのまま使うこと。

    今回の長さから逆算すると、前回が違う長さだった場合にずれる。
    """
    snap = prev_snapshot(counts={k: 0 for k in bwr.CATEGORY_KEYS})
    snap["period"] = {"from": "2026-02-27", "to": "2026-03-01"}   # 3 日間
    _, previous, *_ = headings(make(prev_snapshot=snap))          # 今回は 7 日間
    assert previous == "前週（2/27(金)〜3/1(日)）"


@pytest.mark.parametrize("period", [
    None,                                   # キーごと無い
    {},                                     # 空
    {"to": "2026-03-01"},                   # from が無い
    {"from": "", "to": "2026-03-01"},       # 空文字
    {"from": "2026年2月23日"},               # 日付として読めない
])
def test_previous_heading_falls_back_when_period_is_unreadable(period):
    """期間が読めない場合は、今回の長さから逆算した範囲で補うこと"""
    snap = prev_snapshot(counts={k: 0 for k in bwr.CATEGORY_KEYS})
    if period is None:
        del snap["period"]
    else:
        snap["period"] = period
    _, previous, *_ = headings(make(prev_snapshot=snap))
    assert previous == "前週（2/23(月)〜3/1(日)）"


def test_previous_heading_without_snapshot():
    _, previous, *_ = headings(make(reason="記録がありません"))
    assert previous == "前週（2/23(月)〜3/1(日)）"


def test_snapshot_period_start_is_readable_from_the_first_format():
    """最初の形式（件数を持たない）のスナップショットからも期間を読めること"""
    old = {"version": bwr.SNAPSHOT_VERSION,
           "period": {"from": "2026-02-23", "to": "2026-03-01"},
           "filters": [{"name": "バグ対応", "condition": "", "incomplete": []}]}
    assert bwr._snapshot_period_start(old) == date(2026, 2, 23)


# ==================================================================
# 絞り込み条件が変わった場合
# ==================================================================

def test_previous_column_hides_numbers_when_condition_changed():
    """
    条件が変わった分類は、前週の列に数字を出さないこと。

    同じ分類名のまま条件だけ変えることがあり、数えている対象が違う数字が
    横に並ぶと誤読につながる。
    """
    snap = prev_snapshot(counts={k: 9 for k in bwr.CATEGORY_KEYS})
    snap["filters"][0]["condition"] = "種別: タスク"
    column = make(prev_snapshot=snap, conditions={"バグ対応": "種別: バグ"}) \
        .split("## 前週")[1].split("## 今週")[0]

    assert "### バグ対応" in column          # 見出しは残す（列の位置がずれないように）
    assert "絞り込み条件が今回と異なるため表示しません（前回: 種別: タスク）" in column
    assert "残:9" not in column


def test_previous_column_shows_numbers_when_condition_matches():
    snap = prev_snapshot(counts={k: 9 for k in bwr.CATEGORY_KEYS})
    snap["filters"][0]["condition"] = "種別: バグ"
    column = make(prev_snapshot=snap, conditions={"バグ対応": "種別: バグ"}) \
        .split("## 前週")[1].split("## 今週")[0]

    assert "残:9 / 新規:9 / 再オープン:9 / 完了:9 / 未完了:9" in column


def test_condition_check_is_skipped_for_filters_not_in_this_run():
    """今回の実行に無い分類は、比べる相手がいないのでそのまま出すこと"""
    snap = prev_snapshot(counts={k: 5 for k in bwr.CATEGORY_KEYS}, name="消えた分類")
    snap["filters"][0]["condition"] = "種別: 何か"
    column = make(prev_snapshot=snap, conditions={"バグ対応": "種別: バグ"}) \
        .split("## 前週")[1].split("## 今週")[0]

    assert "### 消えた分類" in column
    assert "残:5" in column


def test_empty_condition_on_both_sides_matches():
    """フィルターなしの実行（条件が空）同士は一致とみなすこと"""
    snap = prev_snapshot(counts={k: 2 for k in bwr.CATEGORY_KEYS}, name=bwr.NO_FILTER_NAME)
    snap["filters"][0]["condition"] = ""
    column = make(prev_snapshot=snap, name=None, conditions={bwr.NO_FILTER_NAME: ""}) \
        .split("## 前週")[1].split("## 今週")[0]

    assert "### 全課題" in column
    assert "残:2" in column
