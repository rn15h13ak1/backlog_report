"""集計期間の決定と、設定・コマンドライン引数の読み取り。"""
import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from backlog_report.core import DEFAULT_MAX_WORKERS, JST, MAX_WORKERS_LIMIT, REPO_ROOT

WEEK_START_MAP = {
    "monday":    0,
    "tuesday":   1,
    "wednesday": 2,
    "thursday":  3,
    "friday":    4,
    "saturday":  5,
    "sunday":    6,
    # 日本語でも指定可能
    "月曜": 0, "月": 0,
    "火曜": 1, "火": 1,
    "水曜": 2, "水": 2,
    "木曜": 3, "木": 3,
    "金曜": 4, "金": 4,
    "土曜": 5, "土": 5,
    "日曜": 6, "日": 6,
}


def get_week_range(target_week: str, week_start: str, today: date | None = None) -> tuple[date, date]:
    """
    対象週の開始日と終了日を返す（date型）

    target_week: "previous" or "current"
    week_start:  曜日名（"monday"〜"sunday" または "月"〜"日"）
    today:       基準日（省略時は JST の今日）
    """
    if today is None:
        today = datetime.now(JST).date()

    start_weekday = WEEK_START_MAP.get(week_start.lower())
    if start_weekday is None:
        print(
            f"エラー: week_start に無効な値 '{week_start}' が指定されています。\n"
            "  有効な値: monday, tuesday, wednesday, thursday, friday, saturday, sunday\n"
            "  （日本語も可: 月, 火, 水, 木, 金, 土, 日）",
            file=sys.stderr,
        )
        sys.exit(1)

    # 今日から直近の week_start 曜日までの日数
    days_since_start = (today.weekday() - start_weekday) % 7
    this_week_start = today - timedelta(days=days_since_start)

    if target_week == "previous":
        period_start = this_week_start - timedelta(weeks=1)
        period_end = this_week_start - timedelta(days=1)
    else:  # current
        period_start = this_week_start
        period_end = today

    return period_start, period_end


def load_config(config_path: str) -> dict:
    """設定ファイルを読み込む"""
    path = Path(config_path)
    if not path.exists():
        print(f"エラー: 設定ファイルが見つかりません: {config_path}", file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_arg_parser() -> argparse.ArgumentParser:
    """コマンドライン引数パーサーを構築する"""
    parser = argparse.ArgumentParser(
        description="Backlog レポート生成",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
期間指定の優先順位:
  1. --from / --to  （最優先）
  2. --week         （前週 or 今週の自動計算）
  3. config.yaml の target_week 設定

例:
  python backlog_weekly_report.py --from 2026-03-01 --to 2026-03-31
  python backlog_weekly_report.py --week current
  python backlog_weekly_report.py
""",
    )
    default_config = str(REPO_ROOT / "config.yaml")
    parser.add_argument("--config", default=default_config,
                        help="設定ファイルのパス（デフォルト: スクリプトと同じディレクトリの config.yaml）")
    parser.add_argument("--week", choices=["previous", "current"],
                        help="対象週の指定（設定ファイルの値を上書き）")
    parser.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD",
                        help="集計開始日（例: 2026-03-01）。--to と併用。")
    parser.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD",
                        help="集計終了日（例: 2026-03-31）。--from と併用。")
    parser.add_argument("--debug", action="store_true",
                        help="APIリクエストのパラメータを表示する（トラブルシューティング用）")
    return parser


def validate_backlog_config(backlog_cfg: dict) -> tuple[str, str, str]:
    """backlog 設定を検証して (space_host, api_key, project_key) を返す"""
    placeholders = [
        ("space_host",  "yourcompany.backlog.com"),
        ("api_key",     "YOUR_API_KEY_HERE"),
        ("project_key", "YOUR_PROJECT_KEY"),
    ]
    values = []
    for key, placeholder in placeholders:
        value = backlog_cfg.get(key, "")
        if not value or value == placeholder:
            print(f"エラー: config.yaml の {key} を設定してください", file=sys.stderr)
            sys.exit(1)
        values.append(value)
    return tuple(values)


def resolve_max_workers(report_cfg: dict) -> int:
    """
    コメント取得の並列数を決める。

    設定ミスで API を叩きすぎないよう 1〜MAX_WORKERS_LIMIT に収める。
    数値として解釈できない値は既定値に戻す。
    """
    raw = report_cfg.get("max_workers", DEFAULT_MAX_WORKERS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        print(f"  ⚠ max_workers に数値以外が指定されています（{raw!r}）。"
              f"既定値 {DEFAULT_MAX_WORKERS} を使用します。", file=sys.stderr)
        return DEFAULT_MAX_WORKERS

    clamped = max(1, min(value, MAX_WORKERS_LIMIT))
    if clamped != value:
        print(f"  ⚠ max_workers は 1〜{MAX_WORKERS_LIMIT} の範囲で指定してください"
              f"（{value} → {clamped} に調整しました）。", file=sys.stderr)
    return clamped


def resolve_period(args, report_cfg: dict, parser: argparse.ArgumentParser) -> tuple[date, date, str]:
    """
    集計期間を決定する。

    優先順位: --from/--to > --week > config.report.period > config.report.target_week
    """
    cfg_period = report_cfg.get("period") or {}

    if args.date_from:
        # 最優先: コマンドライン引数
        try:
            period_start = datetime.strptime(args.date_from, "%Y-%m-%d").date()
            period_end   = datetime.strptime(args.date_to,   "%Y-%m-%d").date()
        except ValueError:
            parser.error("日付は YYYY-MM-DD 形式で入力してください（例: 2026-03-01）")
        if period_start > period_end:
            parser.error("--from は --to より前の日付を指定してください。")
        return period_start, period_end, "指定期間（引数）"

    if args.week:
        # 2番目: --week オプション
        week_start_day = report_cfg.get("week_start", "monday")
        period_start, period_end = get_week_range(args.week, week_start_day)
        return period_start, period_end, "前週" if args.week == "previous" else "今週"

    if cfg_period.get("from") and cfg_period.get("to"):
        # 3番目: config.yaml の period 設定
        try:
            period_start = datetime.strptime(str(cfg_period["from"]), "%Y-%m-%d").date()
            period_end   = datetime.strptime(str(cfg_period["to"]),   "%Y-%m-%d").date()
        except ValueError:
            print("エラー: config.yaml の report.period.from / to は YYYY-MM-DD 形式で記入してください",
                  file=sys.stderr)
            sys.exit(1)
        if period_start > period_end:
            print("エラー: config.yaml の report.period.from は to より前の日付にしてください",
                  file=sys.stderr)
            sys.exit(1)
        return period_start, period_end, "指定期間（config）"

    # 最終フォールバック: target_week の自動計算
    target_week    = report_cfg.get("target_week", "previous")
    week_start_day = report_cfg.get("week_start", "monday")
    period_start, period_end = get_week_range(target_week, week_start_day)
    return period_start, period_end, "前週" if target_week == "previous" else "今週"
