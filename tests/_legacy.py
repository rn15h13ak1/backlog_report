"""
cfcd9ff 時点の実装を凍結したコピー（差分テスト専用）。

実 API に接続できない環境で「リファクタとコメント取得スキップ最適化が
集計結果を変えていないこと」を機械的に確認するために保持している。
本体を修正してもこのファイルは更新しないこと（更新すると比較対象を失う）。

出典: git show cfcd9ff:backlog_weekly_report.py
      classify_issue_from_comments (350-449) / collect_report_data (575-707)
"""
import sys
from datetime import date

from backlog_weekly_report import BacklogClient


def classify_issue_from_comments(
    issue: dict,
    comments: list,
    week_start: date,
    week_end: date,
    closed_status_names: set,
    open_status_names: set,
) -> dict:
    """
    課題のコメント履歴（changeLog）を基に、対象期間における①〜⑤の分類を返す。

    各カテゴリは独立して判定され、現在のステータスに依存しない。
    期間開始時点のステータスはコメント履歴から正確に導出する。

    Returns:
        is_carry_over   : ① 期間前作成かつ期間開始時オープン
        is_new          : ② 期間中作成
        is_reopened     : ③ 期間開始時は完了系かつ期間中にオープン系へ変化
        is_completed    : ④ 期間中にオープン系から完了系へ変化
        status_at_start : 期間開始時点のステータス名
        status_at_end   : 期間終了時点のステータス名
    """
    ws = week_start.strftime("%Y-%m-%d")
    we = week_end.strftime("%Y-%m-%d")
    created = issue.get("created", "")[:10]

    # コメントの changeLog からステータス変化を抽出（コメントは昇順で渡される前提）
    changes_before: list = []  # 期間前のステータス変化
    changes_in: list = []      # 期間中のステータス変化
    changes_after: list = []   # 期間後のステータス変化

    for comment in comments:
        comment_date = comment.get("created", "")[:10]
        for cl in comment.get("changeLog", []):
            if cl.get("field") != "status":
                continue
            entry = {
                "date": comment_date,
                "from": cl.get("originalValue", ""),
                "to":   cl.get("newValue", ""),
            }
            if comment_date < ws:
                changes_before.append(entry)
            elif comment_date <= we:
                changes_in.append(entry)
            else:
                changes_after.append(entry)

    # 期間開始時点のステータスを確定
    # ・期間前に変化あり        → 最後の変化の to が期間開始時ステータス
    # ・期間中に初めて変化      → 最初の変化の from が期間開始時ステータス（変化前）
    # ・期間後にのみ変化あり    → 最初の期間後変化の from が期間開始時ステータス
    # ・変化なし（全期間同一）  → 現在のステータス
    if changes_before:
        status_at_start = changes_before[-1]["to"]
    elif changes_in:
        status_at_start = changes_in[0]["from"]
    elif changes_after:
        status_at_start = changes_after[0]["from"]
    else:
        status_at_start = issue.get("status", {}).get("name", "")

    # 期間終了時点のステータスを確定
    # ・期間中に変化あり     → 最後の変化の to が期間終了時ステータス
    # ・期間後にのみ変化あり → 最初の期間後変化の from（期間中は変化していないため）
    # ・変化なし             → 期間開始時と同じ
    if changes_in:
        status_at_end = changes_in[-1]["to"]
    elif changes_after:
        status_at_end = changes_after[0]["from"]
    else:
        status_at_end = status_at_start

    is_pre_period = created < ws
    is_new        = ws <= created <= we

    # 期間開始時ステータスの分類（期間前作成の課題のみ意味を持つ）
    was_open_at_start   = is_pre_period and (status_at_start in open_status_names)
    was_closed_at_start = is_pre_period and (status_at_start in closed_status_names)

    # 期間中の変化
    # completed_during: オープン系 → 完了系 の変化のみ対象
    # （完了系 → 完了系 の変化、例: 処理済み → 完了 は除外）
    completed_during = any(
        c["from"] in open_status_names and c["to"] in closed_status_names
        for c in changes_in
    )
    reopened_during  = any(
        c["from"] in closed_status_names and c["to"] in open_status_names
        for c in changes_in
    )

    return {
        "is_carry_over":   was_open_at_start,                        # ①
        "is_new":          is_new,                                    # ②
        "is_reopened":     was_closed_at_start and reopened_during,  # ③
        "is_completed":    completed_during,                          # ④
        "status_at_start": status_at_start,
        "status_at_end":   status_at_end,
    }


def collect_report_data(
    client: BacklogClient,
    project_key: str,
    project_id: int,
    week_start: date,
    week_end: date,
    open_status_ids: list,
    closed_status_ids: list,
    extra_params: dict = None,
) -> dict:
    """
    週次レポートに必要なデータを集計する。

    各課題のコメント履歴（changeLog）を基にステータス変化を判定し、
    現在のステータスに依存しない過去期間の正確な集計を実現する。
    フィルター項目（extra_params）は最新の課題属性を使用する。

    処理フロー:
      1. 最新属性でフィルターした全対象課題を取得（statusId 不問）
      2. 各課題のコメントを取得してステータス変化履歴を構築
      3. classify_issue_from_comments で①〜⑤を独立判定
      4. ⑤当週未完了 = (①+②+③) - ④ で計算
    """
    ws = week_start.strftime("%Y-%m-%d")
    we = week_end.strftime("%Y-%m-%d")
    ep = extra_params or {}

    # ステータス名の取得（changeLog の値との照合に使用）
    try:
        statuses = client.get_statuses(project_key)
        closed_status_names = {s["name"] for s in statuses if s["id"] in closed_status_ids}
        open_status_names   = {s["name"] for s in statuses if s["id"] in open_status_ids}
        if client.debug:
            print(f"  [DEBUG] 完了ステータス名: {closed_status_names}", file=sys.stderr)
            print(f"  [DEBUG] オープンステータス名: {open_status_names}", file=sys.stderr)
    except Exception as e:
        if client.debug:
            print(f"  [DEBUG] ステータス取得失敗: {e}", file=sys.stderr)
        closed_status_names = set()
        open_status_names   = set()

    # ---- 全対象課題を取得（最新属性でフィルター、ステータス不問） ----
    # createdUntil = week_end で期間終了日以前に作成された課題を対象とする
    all_issues = client.get_issues(project_id, {
        **ep,
        "createdUntil": we,
    })
    if client.debug:
        print(f"  [DEBUG] 全対象課題数: {len(all_issues)}件", file=sys.stderr)

    all_issues_map = {i.get("id"): i for i in all_issues}

    # ---- 各課題をコメント履歴から独立分類（①〜⑤） ----
    carry_over_issues: list = []
    new_issues:        list = []
    completed_issues:  list = []
    reopened_issues:   list = []
    status_at_end_map: dict = {}  # issue_id -> 期間終了時点のステータス名

    for issue in all_issues:
        issue_id_val = issue.get("id")

        # コメント取得（ステータス変化履歴を含む）
        comments = client.get_issue_comments(issue_id_val)

        result = classify_issue_from_comments(
            issue, comments, week_start, week_end,
            closed_status_names, open_status_names,
        )

        if client.debug:
            print(
                f"  [DEBUG] {issue.get('issueKey','?')}: "
                f"carry={result['is_carry_over']}, new={result['is_new']}, "
                f"completed={result['is_completed']}, reopened={result['is_reopened']}, "
                f"status_at_start={result['status_at_start']}",
                file=sys.stderr,
            )

        # ① 前週残件
        if result["is_carry_over"]:
            if result["is_completed"]:
                # 表示ステータスを期間開始時点のステータスに差し替え
                issue_copy = {**issue}
                issue_copy["status"] = {
                    **issue_copy.get("status", {}),
                    "name": result["status_at_start"],
                }
                carry_over_issues.append(issue_copy)
            else:
                carry_over_issues.append(issue)

        # ② 新規発生: 表示ステータスを期間終了時点に差し替え（現在のステータス混入を防ぐ）
        if result["is_new"]:
            issue_copy = {**issue}
            issue_copy["status"] = {**issue_copy.get("status", {}), "name": result["status_at_end"]}
            new_issues.append(issue_copy)

        # ③ 再オープン: 表示ステータスを期間終了時点に差し替え（現在のステータス混入を防ぐ）
        if result["is_reopened"]:
            issue_copy = {**issue}
            issue_copy["status"] = {**issue_copy.get("status", {}), "name": result["status_at_end"]}
            reopened_issues.append(issue_copy)

        # 期間終了時点のステータスを記録（④⑤の表示用）
        status_at_end_map[issue_id_val] = result["status_at_end"]

        # ④ 当週完了: 表示ステータスを期間終了時点に差し替え
        if result["is_completed"]:
            issue_copy = {**issue}
            issue_copy["status"] = {**issue_copy.get("status", {}), "name": result["status_at_end"]}
            completed_issues.append(issue_copy)

    # ---- ⑤ 当週未完了 = (① + ② + ③) - ④ ----
    completed_id_set = {i.get("id") for i in completed_issues}
    active_ids       = {i.get("id") for i in carry_over_issues + new_issues + reopened_issues}
    incomplete_ids   = active_ids - completed_id_set
    incomplete_issues = []
    for iid in incomplete_ids:
        if iid not in all_issues_map:
            continue
        issue_copy = {**all_issues_map[iid]}
        if iid in status_at_end_map:
            issue_copy["status"] = {**issue_copy.get("status", {}), "name": status_at_end_map[iid]}
        incomplete_issues.append(issue_copy)

    return {
        "carry_over": carry_over_issues,
        "new_issues": new_issues,
        "completed":  completed_issues,
        "incomplete": incomplete_issues,
        "reopened":   reopened_issues,
    }
