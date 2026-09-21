"""Markdown レポート（個別レポートと、Excel 貼り付け用の横断サマリー）。"""
import urllib.parse
from datetime import date, datetime

from backlog_report.core import (
    CATEGORY_KEYS,
    CATEGORY_LABELS,
    JST,
    KEYS_MAX_DISPLAY,
    TABLE_MAX_DISPLAY,
    TABLE_MAX_DISPLAY_INCOMPLETE,
    ReportData,
    _fmt_due,
    _issue_sort_key,
)


def format_issue_table(issues: list, max_display: int = TABLE_MAX_DISPLAY) -> str:
    """課題リストをMarkdown表形式にフォーマット"""
    if not issues:
        return "_（該当なし）_\n"

    lines = [
        "| 課題番号 | 件名 | ステータス | 担当者 | 期限日 |",
        "|---------|------|-----------|-------|-------|",
    ]
    for issue in issues[:max_display]:
        issue_key = issue.get("issueKey", "-")
        summary = issue.get("summary", "-").replace("|", "｜")
        status = issue.get("status", {}).get("name", "-")
        assignee = issue.get("assignee")
        assignee_name = assignee.get("name", "-") if assignee else "_未割当_"
        due_raw = issue.get("dueDate")
        # 期限日は日付のみのフィールドのためタイムゾーン変換しない
        due_date = due_raw[:10] if due_raw else "-"
        lines.append(f"| {issue_key} | {summary} | {status} | {assignee_name} | {due_date} |")

    if len(issues) > max_display:
        lines.append(f"\n_...他 {len(issues) - max_display} 件（表示上限 {max_display} 件）_")

    return "\n".join(lines) + "\n"


def keys_str(issues: list, url_base: str = "") -> str:
    """課題番号のみのコンパクト表示（`url_base` があれば Backlog へのリンクにする）"""
    keys = [_issue_link(i.get("issueKey", "?"), url_base) for i in issues]
    if not keys:
        return "_（なし）_"
    return "、".join(keys[:KEYS_MAX_DISPLAY]) + (
        f" 他{len(keys) - KEYS_MAX_DISPLAY}件" if len(keys) > KEYS_MAX_DISPLAY else ""
    )


def _build_notice_lines(data: ReportData) -> list:
    """
    サマリーの直下に置く注記を組み立てる。

    警告は集計の状況ごとに独立しており、種類が増えても
    generate_markdown_report 本体は変わらないようにここへ分離している。
    """
    carry_over, new_issues = data["carry_over"], data["new_issues"]
    completed, incomplete = data["completed"], data["incomplete"]
    reopened = data.get("reopened") or []
    unknown_statuses = data.get("unknown_statuses") or set()
    comment_failures = data.get("comment_failures") or set()
    inflow = data.get("inflow") or []
    outflow = data.get("outflow") or []
    flow_unavailable = data.get("flow_unavailable") or ""

    unknown_text = ("次のステータス名が現在のプロジェクトのステータス一覧に存在しません"
                    f"（改名または削除された可能性があります）: {'、'.join(sorted(unknown_statuses))}")

    lines: list = []

    # 等式チェック: ① + ② + ③ = ④ + ⑤
    lhs = len(carry_over) + len(new_issues) + len(reopened)
    rhs = len(completed) + len(incomplete)
    if lhs != rhs:
        lines += [
            f"> ⚠️ **注意**: ①残件（{len(carry_over)}）＋ ②新規（{len(new_issues)}）＋ ③再オープン（{len(reopened)}）"
            f"＝ {lhs} に対し、④完了（{len(completed)}）＋ ⑤未完了（{len(incomplete)}）＝ {rhs} と一致しません。",
            "> 同一課題が複数カテゴリに重複して集計されている可能性があります。",
        ]
        if unknown_statuses:
            lines.append("> " + unknown_text)
        lines.append("")
    elif unknown_statuses:
        lines += ["> ⚠️ **注意**: " + unknown_text, ""]

    if comment_failures:
        lines += [
            f"> ⚠️ **注意**: {len(comment_failures)} 件の課題でコメント履歴の取得に失敗しました。"
            "該当課題は「期間中にステータス変化なし」として集計されています。",
            "",
        ]

    if inflow or outflow:
        lines.append("> 抽出対象への出入りを前回の集計と突き合わせて反映しています。")
        if inflow:
            lines.append(f"> 期間中に対象へ入った **{len(inflow)}** 件は ② 新規発生に含めています: "
                         f"{keys_str(sorted(inflow, key=_issue_sort_key))}")
        if outflow:
            lines.append(f"> 期間中に対象から外れた **{len(outflow)}** 件は ④ 当週完了に含めています: "
                         f"{keys_str(sorted(outflow, key=_issue_sort_key))}")
        lines.append("")

    if flow_unavailable:
        lines += [
            f"> ⚠️ **注意**: {flow_unavailable}。",
            "> 抽出対象への出入りを判定していないため、① と ② の内訳が実態と異なる場合があります。",
            "",
        ]

    return lines


def generate_markdown_report(
    data: ReportData,
    project_key: str,
    project_name: str,
    period_start: date,
    period_end: date,
    filter_name: str | None = None,
    filter_description: str | None = None,
    filter_summary: str | None = None,
) -> str:
    """Markdownレポートを生成"""
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    ws_str = period_start.strftime("%Y/%m/%d")
    we_str = period_end.strftime("%Y/%m/%d")

    carry_over = data["carry_over"]
    new_issues = data["new_issues"]
    completed = data["completed"]
    incomplete = data["incomplete"]
    reopened = data.get("reopened") or []

    title_suffix = f" — {filter_name}" if filter_name else ""
    lines = [
        f"# レポート{title_suffix} — {ws_str} 〜 {we_str}",
        "",
        f"> プロジェクト: **{project_name}** (`{project_key}`)  ",
    ]
    if filter_description:
        lines.append(f"> フィルター: {filter_description}  ")
    if filter_summary:
        lines.append(f"> 絞り込み条件: `{filter_summary}`  ")
    lines += [
        f"> 生成日時: {now}",
        "",
        "---",
        "",
        "## サマリー",
        "",
        "| 項目 | 件数 |",
        "|------|------|",
        f"| ① 前週残件数 | **{len(carry_over)}** 件 |",
        f"| ② 新規発生件数 | **{len(new_issues)}** 件 |",
        f"| ③ 再オープン件数 | **{len(reopened)}** 件 |",
        f"| ④ 当週完了件数 | **{len(completed)}** 件 |",
        f"| ⑤ 当週未完了件数 | **{len(incomplete)}** 件 |",
        "",
    ]

    lines += _build_notice_lines(data)

    sections = [
        ("① 前週残件", carry_over,
         f"{ws_str} より前に作成され、{ws_str} 時点で未完了の課題", TABLE_MAX_DISPLAY),
        ("② 新規発生", new_issues,
         f"{ws_str} 〜 {we_str} に作成された課題", TABLE_MAX_DISPLAY),
        ("③ 再オープン", reopened,
         f"{ws_str} 〜 {we_str} に完了状態から再度オープンになった課題", TABLE_MAX_DISPLAY),
        ("④ 当週完了", completed,
         f"{ws_str} 〜 {we_str} に完了した課題", TABLE_MAX_DISPLAY),
        ("⑤ 当週未完了", incomplete,
         f"{we_str} 時点で完了系でない（オープンな）課題", TABLE_MAX_DISPLAY_INCOMPLETE),
    ]
    for title, issues, description, max_display in sections:
        # 課題番号順に並べる。⑤ は集合から組み立てるため並びが定まらず、課題が
        # 1 件増えるだけで順序が入れ替わって、前の期間のレポートと見比べにくい。
        # 横断サマリーと weekly3 も課題番号順なので、資料ごとの並びを揃える。
        issues = sorted(issues, key=_issue_sort_key)
        lines += [
            "---",
            "",
            f"## {title}",
            f"**{len(issues)} 件** — {description}",
            "",
            keys_str(issues),
            "",
            "<details>",
            "<summary>詳細一覧を表示</summary>",
            "",
            format_issue_table(issues, max_display=max_display),
            "</details>",
            "",
        ]

    lines += [
        "---",
        "",
        "_このレポートは backlog_weekly_report.py により自動生成されました。_",
    ]

    return "\n".join(lines)


def _issue_link(key: str, url_base: str = "") -> str:
    """課題番号を Backlog の課題ページへのリンクにする。

    `url_base` が空（スペースが分からない場合）や課題番号が無い場合は、
    そのままの文字列を返す。
    """
    if not url_base or not key or key in ("-", "?"):
        return key
    return f"[{key}]({url_base.rstrip('/')}/view/{urllib.parse.quote(key)})"


def _weekly3_counts(counts: dict | None) -> str:
    """件数の並び（count_summary が拾う形式）。記録に無い区分は 0 とする。"""
    counts = counts or {}
    return " / ".join(f"{label}:{counts.get(key, 0)}"
                      for key, label in zip(CATEGORY_KEYS, CATEGORY_LABELS, strict=True))

def generate_summary_report(
    all_filter_data: list,
    period_start: date,
    period_end: date,
) -> str:
    """全フィルターをまとめたサマリーレポートを生成"""
    lines = [
        f"# サマリーレポート — {period_start.strftime('%Y/%m/%d')} 〜 {period_end.strftime('%Y/%m/%d')}",
        "",
    ]

    for idx, (filter_name, data) in enumerate(all_filter_data):
        completed  = data["completed"]
        incomplete = data["incomplete"]

        lines.append(filter_name)
        lines.append(_weekly3_counts({key: len(data[key]) for key in CATEGORY_KEYS}))

        for issue in sorted(completed + incomplete, key=_issue_sort_key):
            key    = issue.get("issueKey", "-")
            status = issue.get("status", {}).get("name", "-")
            due    = _fmt_due(issue.get("dueDate"))
            summary = issue.get("summary", "-")
            lines.append(f"●{key}｜期限：{due}｜{status}")
            lines.append(summary)

        if idx < len(all_filter_data) - 1:
            lines.append("")
            lines.append("----")
            lines.append("")

    return "\n".join(lines) + "\n"
