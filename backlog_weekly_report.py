#!/usr/bin/env python3
"""
Backlog 週次レポート生成ツール（入口）。

実装は `backlog_report/` に役割ごとに置いてある。

    core      定数・型・共通の小物
    client    Backlog API クライアント
    period    集計期間の決定、設定と引数の読み取り
    collect   ①〜⑤の集計
    snapshot  期間をまたいだ照合の記録と、抽出対象への出入り
    report    個別レポートと横断サマリー
    weekly3   docmold の weekly3 に渡す Markdown

このファイルは、通しの処理（run）と、これまでどおりの名前での公開を受け持つ。
`python backlog_weekly_report.py` で動く手軽さを保つため、入口は 1 ファイルのまま。

使い方は README.md を参照。
"""
import sys
from pathlib import Path

from backlog_report.client import (  # noqa: F401  （公開している名前）
    BacklogAPIError,
    BacklogClient,
    ProjectInfoCache,
    format_api_error,
)
from backlog_report.collect import (  # noqa: F401
    _fetch_comments_bulk,
    _fetch_target_issues,
    build_filter_summary,
    build_jobs,
    classify_issue_from_comments,
    collect_report_data,
    resolve_filter_params,
    validate_status_config,
)
from backlog_report.core import (  # noqa: F401
    API_MAX_RETRIES,
    API_PAGE_SIZE,
    CATEGORY_KEYS,
    CATEGORY_LABELS,
    DEFAULT_CLOSED_STATUS_IDS,
    DEFAULT_MAX_WORKERS,
    JST,
    KEYS_MAX_DISPLAY,
    MAX_WORKERS_LIMIT,
    RETRY_MAX_DELAY,
    TABLE_MAX_DISPLAY,
    TABLE_MAX_DISPLAY_INCOMPLETE,
    ReportData,
    _fmt_due,
    _issue_sort_key,
    safe_filename,
    to_local_date,
)
from backlog_report.period import (  # noqa: F401
    build_arg_parser,
    get_week_range,
    load_config,
    resolve_max_workers,
    resolve_period,
    validate_backlog_config,
)
from backlog_report.report import (  # noqa: F401
    _issue_link,
    format_issue_table,
    generate_markdown_report,
    generate_summary_report,
    keys_str,
)
from backlog_report.snapshot import (  # noqa: F401
    NO_FILTER_NAME,
    SNAPSHOT_FILENAME,
    SNAPSHOT_VERSION,
    _snapshot_entry,
    apply_population_flows,
    build_snapshot,
    find_previous_snapshot,
    previous_incomplete,
    write_snapshot,
)
from backlog_report.weekly3 import (  # noqa: F401
    ColumnEntry,
    PlanEntry,
    _snapshot_period_start,
    _weekly3_column,
    generate_weekly3_report,
)


def _apply_flows(data: ReportData, snapshot: dict | None, filter_name: str,
                 condition: str, snapshot_reason: str) -> ReportData:
    """前回⑤と突き合わせて抽出対象への出入りを反映する（できない場合はそのまま返す）"""
    prev, reason = previous_incomplete(snapshot, filter_name, condition)
    if prev is None:
        note = reason or snapshot_reason
        if note:
            data = {**data, "flow_unavailable": note}
        return data
    result = apply_population_flows(data, prev)
    if result["inflow"] or result["outflow"]:
        print(f"         抽出対象への出入り: 流入 {len(result['inflow'])} 件 → ② / "
              f"流出 {len(result['outflow'])} 件 → ④")
    return result


def _print_summary(output_path: Path, data: ReportData) -> None:
    print(f"  ✅ 保存: {output_path}")
    print(f"     ①前週残件: {len(data['carry_over'])} 件 / "
          f"②新規: {len(data['new_issues'])} 件 / "
          f"③再オープン: {len(data['reopened'])} 件 / "
          f"④完了: {len(data['completed'])} 件 / "
          f"⑤未完了: {len(data['incomplete'])} 件")
    unknown = data.get("unknown_statuses") or set()
    if unknown:
        print(f"     ⚠ ステータス一覧に無い名前: {'、'.join(sorted(unknown))}"
              "（改名または削除された可能性があります）", file=sys.stderr)
    failures = data.get("comment_failures") or set()
    if failures:
        print(f"     ⚠ コメント履歴の取得に失敗: {len(failures)} 件", file=sys.stderr)


def run(argv: list | None = None) -> None:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # --from / --to の検証
    if bool(args.date_from) != bool(args.date_to):
        parser.error("--from と --to は両方セットで指定してください。")
    if args.date_from and args.week:
        parser.error("--from/--to と --week は同時に指定できません。")

    # 設定読み込み
    config = load_config(args.config)
    backlog_cfg = config.get("backlog", {})
    report_cfg = config.get("report", {})
    filters_cfg = config.get("filters") or []

    space_host, api_key, project_key = validate_backlog_config(backlog_cfg)

    output_dir_raw = report_cfg.get("output_dir", "./reports")
    output_dir = Path(output_dir_raw)
    if not output_dir.is_absolute():
        # 相対パスはスクリプトと同じディレクトリ基準で解決（フルパス実行対応）
        output_dir = Path(__file__).parent / output_dir
    closed_status_ids = report_cfg.get("closed_status_ids", DEFAULT_CLOSED_STATUS_IDS)
    max_workers = resolve_max_workers(report_cfg)

    period_start, period_end, period_label = resolve_period(args, report_cfg, parser)
    prev_snapshot, snapshot_reason = find_previous_snapshot(output_dir, period_start)

    print("=" * 55)
    print("Backlog レポート生成")
    print("=" * 55)
    print(f"スペース    : {space_host}")
    print(f"プロジェクト : {project_key}（デフォルト）")
    print(f"対象期間    : {period_start} 〜 {period_end}（{period_label} / JST基準）")
    print(f"フィルター数 : {len(filters_cfg) if filters_cfg else 0}（0=フィルターなし）")
    if prev_snapshot:
        prev = prev_snapshot["period"]
        print(f"前回の集計   : {prev['from']} 〜 {prev['to']}（抽出対象への出入りを判定します）")
    else:
        print(f"前回の集計   : なし（{snapshot_reason}）")
    print()

    ssl_verify = backlog_cfg.get("ssl_verify", True)
    base_path  = backlog_cfg.get("base_path", "")
    client = BacklogClient(space_host, api_key, ssl_verify=ssl_verify, base_path=base_path, debug=args.debug)
    projects = ProjectInfoCache(client, debug=args.debug)

    # デフォルトプロジェクトを先に取得（存在確認 + ヘッダー表示）
    projects.get(project_key, need_master=False)
    print()

    # 期間フォルダを output_dir 配下に作成（例: reports/20260101_20260107/）
    period_dir = f"{period_start.strftime('%Y%m%d')}_{period_end.strftime('%Y%m%d')}"
    output_dir = output_dir / period_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # ---- 集計・出力（フィルターなしも「1件の仕事」として同じ流れで扱う）----
    jobs = build_jobs(filters_cfg, project_key)
    all_filter_data = []
    snapshot_entries: list = []

    for i, job in enumerate(jobs, 1):
        proj_info = projects.get(job["project_key"], need_master=job["need_master"])

        if job["name"]:
            print(f"[{i}/{len(jobs)}] フィルター「{job['name']}」を集計中...")
            if job["project_key"] != project_key:
                print(f"         プロジェクト: {job['project_key']}")
            print(f"         条件: {job['condition']}")
        else:
            print("【フィルターなし】全課題を集計中...")

        extra_params = resolve_filter_params(
            job["cfg"], proj_info["issue_type_map"], proj_info["custom_field_map"]
        )
        if args.debug:
            print(f"  [DEBUG] 解決済みフィルターパラメータ: {extra_params}", file=sys.stderr)

        data = collect_report_data(
            client, job["project_key"], proj_info["id"], period_start, period_end,
            closed_status_ids,
            extra_params=extra_params,
            max_workers=max_workers,
        )
        data = _apply_flows(data, prev_snapshot, job["snapshot_name"],
                            job["condition"], snapshot_reason)

        report_md = generate_markdown_report(
            data, job["project_key"], proj_info["name"], period_start, period_end,
            filter_name=job["name"],
            filter_description=job["description"],
            filter_summary=job["condition"] or None,
        )

        all_filter_data.append((job["name"], data))
        snapshot_entries.append((job["snapshot_name"], job["condition"], data))

        output_path = output_dir / job["filename"]
        output_path.write_text(report_md, encoding="utf-8")
        _print_summary(output_path, data)
        if job["name"]:
            print()

    # ---- サマリーレポート出力（フィルターを定義しているときだけ）----
    if filters_cfg:
        summary_md = generate_summary_report(all_filter_data, period_start, period_end)
        summary_path = output_dir / "summary_report.md"
        summary_path.write_text(summary_md, encoding="utf-8")
        print(f"  ✅ サマリー保存: {summary_path}")

    weekly3_md = generate_weekly3_report(
        all_filter_data, project_key, projects.get(project_key)["name"],
        period_start, period_end, prev_snapshot, snapshot_reason,
        conditions={name: condition for name, condition, _ in snapshot_entries},
        url_base=client.web_url,
    )
    weekly3_path = output_dir / "weekly3_report.md"
    weekly3_path.write_text(weekly3_md, encoding="utf-8")
    print(f"  ✅ weekly3 用に保存: {weekly3_path}")

    # 記録はフィルターの有無にかかわらず書き出す。次回の出入りの判定と
    # weekly3 の「前週」の列に使うため。
    snapshot_path = write_snapshot(output_dir, build_snapshot(
        period_start, period_end, snapshot_entries
    ))
    print(f"  ✅ 次回照合用の記録: {snapshot_path}")


def main():
    try:
        run()
    except BacklogAPIError as e:
        print(format_api_error(e), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
