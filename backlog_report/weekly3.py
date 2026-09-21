"""docmold の weekly3 に渡す Markdown（前週・今週・来週の 3 列）。"""
from datetime import date, timedelta
from typing import NamedTuple

from backlog_report.core import (
    CATEGORY_KEYS,
    CATEGORY_LABELS,
    _fmt_due,
    _issue_sort_key,
    _with_status,
)
from backlog_report.report import _issue_link, _weekly3_counts, keys_str
from backlog_report.snapshot import NO_FILTER_NAME

#: 曜日の表記。date.weekday()（月曜が 0）の順。docmold と同じ並び。
WEEKDAYS = "月火水木金土日"


def _weekday(value: date) -> str:
    return WEEKDAYS[value.weekday()]


def _span(begin: date, end: date) -> str:
    """列の見出しに書く期間（`3/2(月)〜3/8(日)`）

    docmold は見出しに区切り（〜）があれば書き換えないため、当ツールが自分で
    書き込む。docmold が組み立てる場合と同じ見え方になるよう曜日を添える。
    """
    return (f"{begin.month}/{begin.day}({_weekday(begin)})"
            f"〜{end.month}/{end.day}({_weekday(end)})")


#: 抽出対象から外れて④に入れた課題の、ステータス欄と理由の欄。
#: 「完了扱い」は docmold で完了のバッジになり、理由はバッジにならない語を選ぶ
#: （「未対応」などを含む文言にすると、そちらがバッジとして拾われてしまう）。
OUTFLOW_STATUS = "完了扱い"


OUTFLOW_NOTE = "対象外になった"


def _weekly3_entry(issue: dict, url_base: str = "") -> str:
    """weekly3 のカード 1 件（`課題番号｜期限：m/d｜ステータス｜件名`）

    課題番号は Markdown のリンクにする。docmold の `entry_card` は欄を文字列の
    ところだけで区切るため、欄の中のリンクはそのまま残る。

    抽出対象から外れて④に入れた課題は、ステータスを「完了扱い」に差し替えて
    理由の欄を足す（`課題番号｜期限｜完了扱い｜対象外になった｜件名`）。
    記録されていたステータス（「未対応」など）のまま完了として数えると、
    なぜ完了なのかが分からないため。
    """
    key = _issue_link(issue.get("issueKey", "-"), url_base)
    status = issue.get("status", {}).get("name", "-")
    summary = (issue.get("summary") or "-").replace("｜", "／")
    note = f"{OUTFLOW_NOTE}｜" if issue.get("_outflow") else ""
    if issue.get("_outflow"):
        status = OUTFLOW_STATUS
    return f"- {key}｜期限：{_fmt_due(issue.get('dueDate'))}｜{status}｜{note}{summary}"


def _mark_outflow(issues: list, outflow_ids: set) -> list:
    """抽出対象から外れた課題に印を付ける（表示のときだけ使う）"""
    if not outflow_ids:
        return issues
    return [{**i, "_outflow": True} if i.get("id") in outflow_ids else i for i in issues]




class ColumnEntry(NamedTuple):
    """weekly3 の 1 列に入る分類 1 つぶん。

    note があるときは、件数と課題の代わりにその注記だけを出す
    （絞り込み条件が前回と違う場合など）。
    """

    name: str
    counts: dict | None
    issues: list
    note: str = ""


class PlanEntry(NamedTuple):
    """「来週の予定」の列に入る分類 1 つぶん"""

    name: str
    issues: list


def _weekly3_column(entries: list[ColumnEntry], note: str = "", url_base: str = "") -> list:
    """1 列ぶんの本文を組み立てる。note があれば列ごとその注記だけを出す。"""
    if note:
        return [f"_（{note}）_", ""]
    if not entries:
        return ["_（対象なし）_", ""]

    lines: list = []
    for entry in entries:
        lines += [f"### {entry.name}", ""]
        if entry.note:
            lines += [f"_（{entry.note}）_", ""]
            continue
        lines += [_weekly3_counts(entry.counts), ""]
        lines += ([_weekly3_entry(i, url_base) for i in entry.issues]
                  if entry.issues else ["_（該当なし）_"])
        lines.append("")
    return lines


def _weekly3_plan_column(entries: list[PlanEntry], next_start: date, url_base: str = "") -> list:
    """
    「来週の予定」の列。⑤ をそのまま持ち越し、期限を過ぎているものを数える。

    期限が次の期間の開始日より前の課題は、ステータスを「期限超過」として出す
    （docmold 側で状態バッジになる）。
    """
    deadline = next_start.isoformat()
    lines: list = []
    for entry in entries:
        issues = entry.issues
        overdue = [i for i in issues if (i.get("dueDate") or "")[:10] < deadline
                   and i.get("dueDate")]
        lines += [f"### {entry.name}", "",
                  f"予定:{len(issues)} / 期限切れ:{len(overdue)}", ""]
        if issues:
            overdue_ids = {i.get("id") for i in overdue}
            lines += [_weekly3_entry(_with_status(i, "期限超過") if i.get("id") in overdue_ids else i,
                                     url_base)
                      for i in issues]
        else:
            lines.append("_（該当なし）_")
        lines.append("")
    return lines or ["_（対象なし）_", ""]


def generate_weekly3_report(
    all_filter_data: list,
    project_key: str,
    project_name: str,
    period_start: date,
    period_end: date,
    prev_snapshot: dict | None,
    snapshot_reason: str = "",
    conditions: dict | None = None,
    url_base: str = "",
) -> str:
    """
    docmold の `weekly3` に渡す Markdown を組み立てる。

    見出し 2 を 4 つ置き、1 つ目をトピックス、2 〜 4 つ目を 3 列として並べる。
    真ん中の列が今回の集計で、左が前回、右が次の期間の予定にあたる。

    列の見出しには期間を自分で書き込む。docmold 側の `column_periods` は
    見出しに区切り（〜）があれば触らないので、7 日以外の期間でも正しく出る。

    conditions: {分類名: 今回の絞り込み条件}。前回と違う分類は、数えている対象が
    違うため前週の列に数字を出さない。

    url_base: Backlog のスペースの URL（`https://example.backlog.jp`）。
    渡すと課題番号を課題ページへのリンクにする。
    """
    length = (period_end - period_start).days + 1
    prev_end = period_start - timedelta(days=1)
    # 前回の期間は記録されたものを使う。今回の長さから逆算すると、前回が違う長さ
    # だった場合に見出しの範囲がずれる。読めない場合だけ今回の長さで補う。
    prev_start = _snapshot_period_start(prev_snapshot) or (period_start - timedelta(days=length))
    next_start, next_end = period_end + timedelta(days=1), period_end + timedelta(days=length)
    span = _span

    # ---- 前週の列は前回のスナップショットから組み立てる ----
    conditions = conditions or {}
    prev_entries: list = []
    prev_note = snapshot_reason or "前回の集計結果が見つかりませんでした"
    if prev_snapshot:
        prev_note = ""
        for entry in prev_snapshot.get("filters", []):
            name = entry.get("name")
            counts = entry.get("counts")
            if counts is None:
                prev_note = "前回の記録に件数が含まれていません（次回の実行から表示されます）"
                prev_entries = []
                break

            # 絞り込み条件が変わっていたら、数えている対象が違うので数字を並べない。
            # 同じ分類名のまま条件だけ変えることがあり、見比べると誤読につながる。
            previous = entry.get("condition", "")
            current = conditions.get(name)
            if current is not None and previous != current:
                prev_entries.append(ColumnEntry(
                    _weekly3_name(name), None, [],
                    "絞り込み条件が今回と異なるため表示しません"
                    f"（前回: {previous or 'なし'}）",
                ))
                continue

            issues = (entry.get("completed") or []) + (entry.get("incomplete") or [])
            prev_entries.append(ColumnEntry(
                _weekly3_name(name), counts,
                _mark_outflow(
                    sorted((_from_snapshot_entry(i) for i in issues), key=_issue_sort_key),
                    set(entry.get("outflow") or []),
                ),
                "",
            ))

    lines = [
        "---",
        "type: weekly3",
        f"title: {project_name} 課題サマリー",
        f"期間: {period_start.isoformat()}({_weekday(period_start)})"
        f" 〜 {period_end.isoformat()}({_weekday(period_end)})",
        "---",
        "",
        "## トピックス",
        "",
        "### 集計の概要",
        "",
        f"プロジェクト **{project_name}**（`{project_key}`）の "
        f"{period_start:%Y/%m/%d} 〜 {period_end:%Y/%m/%d} の集計。",
        "",
        f"| 区分 | {' | '.join(CATEGORY_LABELS)} |",
        "| --- | " + " | ".join("--:" for _ in CATEGORY_LABELS) + " |",
    ]
    for name, data in all_filter_data:
        counts = [str(len(data[key])) for key in CATEGORY_KEYS]
        lines.append(f"| {_weekly3_name(name)} | {' | '.join(counts)} |")
    lines.append("")

    notices = _weekly3_notices(all_filter_data, url_base)
    if notices:
        # docmold の callout_blockquote が枠付きのコールアウトにする。件数が実態と
        # ずれうる事情なので、地の箇条書きのまま他の説明と同じ強さで並べない。
        # 題の次の空の `>` は、箇条書きが題と 1 つの段落にまとまらないように要る。
        lines += ["### 注意", "", f"> [!warning] {NOTICE_TITLE}", ">"]
        lines += [f"> {line}" for line in notices]
        lines.append("")

    lines += [f"## 前週（{span(prev_start, prev_end)}）", ""]
    lines += _weekly3_column(prev_entries, prev_note, url_base)

    lines += [f"## 今週（{span(period_start, period_end)}）", ""]
    lines += _weekly3_column([
        ColumnEntry(_weekly3_name(name), {key: len(data[key]) for key in CATEGORY_KEYS},
                    _mark_outflow(sorted(data["completed"] + data["incomplete"],
                                         key=_issue_sort_key),
                                  {i.get("id") for i in (data.get("outflow") or [])}))
        for name, data in all_filter_data
    ], "", url_base)

    lines += [f"## 来週の予定（{span(next_start, next_end)}）", ""]
    lines += _weekly3_plan_column(
        [PlanEntry(_weekly3_name(name), sorted(data["incomplete"], key=_issue_sort_key))
         for name, data in all_filter_data],
        next_start,
        url_base,
    )

    return "\n".join(lines).rstrip("\n") + "\n"


def _snapshot_period_start(snapshot: dict | None) -> date | None:
    """
    スナップショットに記録された期間の開始日。読めなければ None。

    期間は最初の形式から記録されているので、以前に出力したスナップショットも
    そのまま使える。手で編集された場合などに備えて、読めなければ呼び出し側で補う。
    """
    if not snapshot:
        return None
    raw = (snapshot.get("period") or {}).get("from")
    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def _weekly3_name(name: str | None) -> str:
    """フィルターなしのときの分類名"""
    if not name or name == NO_FILTER_NAME:
        return "全課題"
    return name


def _from_snapshot_entry(saved: dict) -> dict:
    """スナップショットの記録を、表示用の課題の形に戻す"""
    return {
        "id": saved.get("id"),
        "issueKey": saved.get("issueKey"),
        "summary": saved.get("summary"),
        "status": {"name": saved.get("status") or "-"},
        "dueDate": saved.get("dueDate"),
    }


#: 注意書きのコールアウトの題。
NOTICE_TITLE = "件数に影響する事情"


def _weekly3_notices(all_filter_data: list, url_base: str = "") -> list:
    """トピックスに載せる注意書き（該当がなければ空）"""
    lines: list = []
    for name, data in all_filter_data:
        label = _weekly3_name(name)
        if data.get("inflow"):
            lines.append(f"- {label}: 期間中に対象へ入った {len(data['inflow'])} 件を "
                         "② 新規発生に含めた（"
                         f"{keys_str(sorted(data['inflow'], key=_issue_sort_key), url_base)}）")
        if data.get("outflow"):
            lines.append(f"- {label}: 期間中に対象から外れた {len(data['outflow'])} 件を "
                         "④ 当週完了に含めた（"
                         f"{keys_str(sorted(data['outflow'], key=_issue_sort_key), url_base)}）")
        if data.get("comment_failures"):
            lines.append(f"- {label}: {len(data['comment_failures'])} 件の課題で"
                         "コメント履歴を取得できなかった")
        if data.get("unknown_statuses"):
            lines.append(f"- {label}: ステータス一覧に無い名前があった"
                         f"（{'、'.join(sorted(data['unknown_statuses']))}）")
        if data.get("flow_unavailable"):
            lines.append(f"- {label}: {data['flow_unavailable']}")
    return lines
