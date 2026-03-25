#!/usr/bin/env python3
"""
Backlog 週次レポート生成スクリプト
====================================
前週（または当週）の課題集計をMarkdownファイルとして出力します。
config.yaml の filters に複数のフィルターを定義すると、
フィルターごとに個別のレポートファイルが生成されます。

集計内容:
  - 前週からの残件数（前週開始時点で未完了だった課題数）
  - 新規発生件数（対象週に作成された課題数）
  - 当週完了件数（対象週に完了した課題数）
  - 未完了件数（現在オープンの課題数）
  - 各カテゴリのBacklog課題番号一覧

使い方:
  python backlog_weekly_report.py
  python backlog_weekly_report.py --config path/to/config.yaml
  python backlog_weekly_report.py --week current
"""

import argparse
import sys
import time
import urllib.request
import urllib.parse
import json
import yaml
from datetime import datetime, timedelta, date
from pathlib import Path


# ==============================================================
# Backlog API クライアント
# ==============================================================

class BacklogClient:
    def __init__(self, space_host: str, api_key: str):
        self.base_url = f"https://{space_host}/api/v2"
        self.api_key = api_key

    def _get(self, endpoint: str, params: dict = None) -> dict | list:
        """GETリクエストを送信してJSONを返す"""
        params = params or {}
        params["apiKey"] = self.api_key

        # リストパラメータを展開（例: statusId[] → statusId[]=1&statusId[]=2）
        query_parts = []
        for key, value in params.items():
            if isinstance(value, list):
                for v in value:
                    query_parts.append(f"{urllib.parse.quote(key)}[]={urllib.parse.quote(str(v))}")
            else:
                query_parts.append(f"{urllib.parse.quote(key)}={urllib.parse.quote(str(value))}")
        query_string = "&".join(query_parts)

        url = f"{self.base_url}{endpoint}?{query_string}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read().decode("utf-8"))

    def get_project(self, project_key: str) -> dict:
        """プロジェクト情報を取得"""
        return self._get(f"/projects/{project_key}")

    def get_issue_types(self, project_id_or_key: str | int) -> list:
        """プロジェクトの種別一覧を取得"""
        return self._get(f"/projects/{project_id_or_key}/issueTypes")

    def get_custom_fields(self, project_id_or_key: str | int) -> list:
        """プロジェクトのカスタム属性一覧を取得"""
        return self._get(f"/projects/{project_id_or_key}/customFields")

    def get_issues(self, project_id: int, params: dict = None) -> list:
        """
        課題一覧を全件取得（ページネーション対応）
        Backlog APIは1回最大100件のため、自動的に繰り返し取得します。
        """
        all_issues = []
        offset = 0
        count = 100

        base_params = params or {}
        base_params["projectId"] = [project_id]
        base_params["count"] = count

        while True:
            base_params["offset"] = offset
            issues = self._get("/issues", base_params.copy())
            if not issues:
                break
            all_issues.extend(issues)
            if len(issues) < count:
                break
            offset += count
            time.sleep(0.3)  # APIレート制限を考慮

        return all_issues


# ==============================================================
# 週の日付範囲計算
# ==============================================================

def get_week_range(target_week: str, week_start: str) -> tuple[date, date]:
    """
    対象週の開始日と終了日を返す（date型）

    target_week: "previous" or "current"
    week_start:  "monday" or "sunday"
    """
    today = date.today()

    if week_start == "monday":
        days_since_start = today.weekday()  # 月=0, 日=6
    else:  # sunday
        days_since_start = (today.weekday() + 1) % 7

    this_week_start = today - timedelta(days=days_since_start)

    if target_week == "previous":
        week_start_date = this_week_start - timedelta(weeks=1)
        week_end_date = this_week_start - timedelta(days=1)
    else:  # current
        week_start_date = this_week_start
        week_end_date = today

    return week_start_date, week_end_date


# ==============================================================
# フィルターパラメータ解決
# ==============================================================

def resolve_filter_params(
    filter_cfg: dict,
    issue_type_map: dict,   # {名前: ID}
    custom_field_map: dict, # {名前: {id, typeId}}
) -> dict:
    """
    config の filters[i] から Backlog API クエリパラメータを構築して返す。

    Returns:
        dict: get_issues() に追加で渡すパラメータ
              例: {"issueTypeId": [1, 2], "customField_123": ["Aチーム"]}
    """
    extra = {}

    # ---- 種別フィルター ----
    issue_types = filter_cfg.get("issue_types") or []
    if issue_types:
        ids = []
        for name in issue_types:
            if name in issue_type_map:
                ids.append(issue_type_map[name])
            else:
                print(f"  ⚠ 種別「{name}」が見つかりません（スキップ）", file=sys.stderr)
        if ids:
            extra["issueTypeId"] = ids

    # ---- カスタム属性フィルター ----
    custom_fields = filter_cfg.get("custom_fields") or []
    for cf in custom_fields:
        values = cf.get("values") or []
        if not values:
            continue

        # field_id 直接指定 or field_name から解決
        if "field_id" in cf:
            field_id = cf["field_id"]
            type_id = None
            # typeId を custom_field_map から引く（名前→IDの逆引き）
            for info in custom_field_map.values():
                if info["id"] == field_id:
                    type_id = info.get("typeId")
                    break
        elif "field_name" in cf:
            name = cf["field_name"]
            if name not in custom_field_map:
                print(f"  ⚠ カスタム属性「{name}」が見つかりません（スキップ）", file=sys.stderr)
                continue
            field_id = custom_field_map[name]["id"]
            type_id = custom_field_map[name].get("typeId")
        else:
            print("  ⚠ custom_fields に field_name または field_id が必要です（スキップ）",
                  file=sys.stderr)
            continue

        # typeId 5=単一リスト, 6=複数リスト, 7=チェックボックス, 8=ラジオ
        # → これらはリスト型パラメータ（[] 付き）
        # typeId 1=テキスト, 2=文章, 3=数値, 4=日付 → 単一値
        list_types = {5, 6, 7, 8}
        if type_id in list_types or len(values) > 1:
            extra[f"customField_{field_id}"] = values  # リスト → []付きで送信
        else:
            extra[f"customField_{field_id}"] = values[0]

    return extra


# ==============================================================
# 集計ロジック
# ==============================================================

def collect_report_data(
    client: BacklogClient,
    project_id: int,
    week_start: date,
    week_end: date,
    open_status_ids: list,
    closed_status_ids: list,
    extra_params: dict = None,
) -> dict:
    """
    週次レポートに必要なデータを集計する。
    extra_params にフィルター（種別・カスタム属性）を渡す。
    """
    ws = week_start.strftime("%Y-%m-%d")
    we = week_end.strftime("%Y-%m-%d")
    ep = extra_params or {}

    # ① 前週からの残件（week_start より前に作成され、現在も未完了）
    carry_over_issues = client.get_issues(project_id, {
        **ep,
        "statusId": open_status_ids,
        "createdUntil": (week_start - timedelta(days=1)).strftime("%Y-%m-%d"),
    })

    # ② 新規発生（対象週に作成された課題、ステータス問わず）
    new_issues = client.get_issues(project_id, {
        **ep,
        "createdSince": ws,
        "createdUntil": we,
    })

    # ③ 当週完了（対象週に更新された完了ステータスの課題）
    #    ※ Backlog APIに「完了日」フィルタがないため updatedSince/Until で近似
    completed_issues = client.get_issues(project_id, {
        **ep,
        "statusId": closed_status_ids,
        "updatedSince": ws,
        "updatedUntil": we,
    })

    # ④ 未完了（現在オープンの全課題）
    incomplete_issues = client.get_issues(project_id, {
        **ep,
        "statusId": open_status_ids,
    })

    return {
        "carry_over": carry_over_issues,
        "new_issues": new_issues,
        "completed": completed_issues,
        "incomplete": incomplete_issues,
    }


# ==============================================================
# Markdownレポート生成
# ==============================================================

def format_issue_table(issues: list, max_display: int = 30) -> str:
    """課題リストをMarkdown表形式にフォーマット"""
    if not issues:
        return "_（該当なし）_\n"

    lines = [
        "| 課題番号 | 件名 | ステータス | 担当者 |",
        "|---------|------|-----------|-------|",
    ]
    for issue in issues[:max_display]:
        issue_key = issue.get("issueKey", "-")
        summary = issue.get("summary", "-").replace("|", "｜")
        status = issue.get("status", {}).get("name", "-")
        assignee = issue.get("assignee")
        assignee_name = assignee.get("name", "-") if assignee else "_未割当_"
        lines.append(f"| {issue_key} | {summary} | {status} | {assignee_name} |")

    if len(issues) > max_display:
        lines.append(f"\n_...他 {len(issues) - max_display} 件（表示上限 {max_display} 件）_")

    return "\n".join(lines) + "\n"


def keys_str(issues: list) -> str:
    """課題番号のみのコンパクト表示"""
    keys = [i.get("issueKey", "?") for i in issues]
    if not keys:
        return "_（なし）_"
    return "、".join(keys[:20]) + (f" 他{len(keys) - 20}件" if len(keys) > 20 else "")


def generate_markdown_report(
    data: dict,
    project_key: str,
    project_name: str,
    week_start: date,
    week_end: date,
    filter_name: str = None,
    filter_description: str = None,
    filter_summary: str = None,
) -> str:
    """Markdownレポートを生成"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    ws_str = week_start.strftime("%Y/%m/%d")
    we_str = week_end.strftime("%Y/%m/%d")

    carry_over = data["carry_over"]
    new_issues = data["new_issues"]
    completed = data["completed"]
    incomplete = data["incomplete"]

    title_suffix = f" — {filter_name}" if filter_name else ""
    lines = [
        f"# 週次レポート{title_suffix} — {ws_str} 〜 {we_str}",
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
        f"| 前週からの残件数 | **{len(carry_over)}** 件 |",
        f"| 新規発生件数 | **{len(new_issues)}** 件 |",
        f"| 当週完了件数 | **{len(completed)}** 件 |",
        f"| 現在の未完了件数 | **{len(incomplete)}** 件 |",
        "",
        "---",
        "",
        "## 前週からの残件",
        f"**{len(carry_over)} 件** — {week_start.strftime('%Y/%m/%d')} より前に作成され、現在も未完了の課題",
        "",
        keys_str(carry_over),
        "",
        "<details>",
        "<summary>詳細一覧を表示</summary>",
        "",
        format_issue_table(carry_over),
        "</details>",
        "",
        "---",
        "",
        "## 新規発生",
        f"**{len(new_issues)} 件** — {ws_str} 〜 {we_str} に作成された課題",
        "",
        keys_str(new_issues),
        "",
        "<details>",
        "<summary>詳細一覧を表示</summary>",
        "",
        format_issue_table(new_issues),
        "</details>",
        "",
        "---",
        "",
        "## 当週完了",
        f"**{len(completed)} 件** — {ws_str} 〜 {we_str} に完了した課題",
        "",
        keys_str(completed),
        "",
        "<details>",
        "<summary>詳細一覧を表示</summary>",
        "",
        format_issue_table(completed),
        "</details>",
        "",
        "---",
        "",
        "## 現在の未完了一覧",
        f"**{len(incomplete)} 件** — 現在オープン（未対応・処理中・処理済み）の課題",
        "",
        keys_str(incomplete),
        "",
        "<details>",
        "<summary>詳細一覧を表示</summary>",
        "",
        format_issue_table(incomplete, max_display=50),
        "</details>",
        "",
        "---",
        "",
        "_このレポートは backlog_weekly_report.py により自動生成されました。_",
    ]

    return "\n".join(lines)


def build_filter_summary(filter_cfg: dict) -> str:
    """フィルター条件の人間向け要約文字列を生成"""
    parts = []
    issue_types = filter_cfg.get("issue_types") or []
    if issue_types:
        parts.append(f"種別: {', '.join(issue_types)}")
    for cf in filter_cfg.get("custom_fields") or []:
        label = cf.get("field_name") or f"field_id={cf.get('field_id')}"
        vals = cf.get("values") or []
        parts.append(f"{label}: {', '.join(str(v) for v in vals)}")
    return " / ".join(parts) if parts else "（なし）"


def safe_filename(name: str) -> str:
    """ファイル名に使えない文字を除去"""
    for ch in r'\/:*?"<>|　':
        name = name.replace(ch, "_")
    return name


# ==============================================================
# メイン処理
# ==============================================================

def load_config(config_path: str) -> dict:
    """設定ファイルを読み込む"""
    path = Path(config_path)
    if not path.exists():
        print(f"エラー: 設定ファイルが見つかりません: {config_path}", file=sys.stderr)
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser(description="Backlog 週次レポート生成")
    parser.add_argument("--config", default="config.yaml",
                        help="設定ファイルのパス（デフォルト: config.yaml）")
    parser.add_argument("--week", choices=["previous", "current"],
                        help="対象週の指定（設定ファイルの値を上書き）")
    args = parser.parse_args()

    # 設定読み込み
    config = load_config(args.config)
    backlog_cfg = config.get("backlog", {})
    report_cfg = config.get("report", {})
    filters_cfg = config.get("filters") or []

    space_host = backlog_cfg.get("space_host", "")
    api_key = backlog_cfg.get("api_key", "")
    project_key = backlog_cfg.get("project_key", "")

    if not space_host or space_host == "yourcompany.backlog.com":
        print("エラー: config.yaml の space_host を設定してください", file=sys.stderr)
        sys.exit(1)
    if not api_key or api_key == "YOUR_API_KEY_HERE":
        print("エラー: config.yaml の api_key を設定してください", file=sys.stderr)
        sys.exit(1)
    if not project_key or project_key == "YOUR_PROJECT_KEY":
        print("エラー: config.yaml の project_key を設定してください", file=sys.stderr)
        sys.exit(1)

    target_week = args.week or report_cfg.get("target_week", "previous")
    week_start_day = report_cfg.get("week_start", "monday")
    output_dir = Path(report_cfg.get("output_dir", "./reports"))
    open_status_ids = report_cfg.get("open_status_ids", [1, 2, 3])
    closed_status_ids = report_cfg.get("closed_status_ids", [4])

    week_start, week_end = get_week_range(target_week, week_start_day)

    print("=" * 55)
    print("Backlog 週次レポート生成")
    print("=" * 55)
    print(f"スペース  : {space_host}")
    print(f"プロジェクト: {project_key}")
    print(f"対象週    : {week_start} 〜 {week_end}")
    print(f"フィルター数: {len(filters_cfg) if filters_cfg else 0}（0=フィルターなし）")
    print()

    client = BacklogClient(space_host, api_key)

    # プロジェクト情報取得
    print("プロジェクト情報を取得中...")
    try:
        project = client.get_project(project_key)
    except Exception as e:
        print(f"エラー: プロジェクト情報の取得に失敗しました: {e}", file=sys.stderr)
        print("  → space_host、api_key、project_key の設定を確認してください", file=sys.stderr)
        sys.exit(1)

    project_id = project["id"]
    project_name = project["name"]
    print(f"プロジェクト名: {project_name} (ID: {project_id})")

    # 種別・カスタム属性マスターを取得（フィルターがある場合のみ）
    issue_type_map = {}    # {名前: ID}
    custom_field_map = {}  # {名前: {id, typeId}}

    if filters_cfg:
        print("種別・カスタム属性マスターを取得中...")
        try:
            issue_types = client.get_issue_types(project_key)
            issue_type_map = {it["name"]: it["id"] for it in issue_types}
            print(f"  種別: {list(issue_type_map.keys())}")
        except Exception as e:
            print(f"  ⚠ 種別マスターの取得に失敗: {e}", file=sys.stderr)

        try:
            custom_fields = client.get_custom_fields(project_key)
            custom_field_map = {
                cf["name"]: {"id": cf["id"], "typeId": cf.get("typeId")}
                for cf in custom_fields
            }
            print(f"  カスタム属性: {list(custom_field_map.keys())}")
        except Exception as e:
            print(f"  ⚠ カスタム属性マスターの取得に失敗: {e}", file=sys.stderr)

    print()
    output_dir.mkdir(parents=True, exist_ok=True)

    # ---- フィルターなし（filters が空の場合）----
    if not filters_cfg:
        print("【フィルターなし】全課題を集計中...")
        data = collect_report_data(
            client, project_id, week_start, week_end,
            open_status_ids, closed_status_ids
        )
        report_md = generate_markdown_report(
            data, project_key, project_name, week_start, week_end
        )
        filename = f"weekly_report_{week_start.strftime('%Y%m%d')}_{week_end.strftime('%Y%m%d')}.md"
        output_path = output_dir / filename
        output_path.write_text(report_md, encoding="utf-8")
        _print_summary(output_path, data)
        return

    # ---- フィルターごとに集計・出力 ----
    for i, filter_cfg in enumerate(filters_cfg, 1):
        filter_name = filter_cfg.get("name") or f"filter_{i}"
        filter_desc = filter_cfg.get("description") or ""
        filter_summary = build_filter_summary(filter_cfg)

        print(f"[{i}/{len(filters_cfg)}] フィルター「{filter_name}」を集計中...")
        print(f"         条件: {filter_summary}")

        # フィルターパラメータを解決
        extra_params = resolve_filter_params(filter_cfg, issue_type_map, custom_field_map)

        data = collect_report_data(
            client, project_id, week_start, week_end,
            open_status_ids, closed_status_ids,
            extra_params=extra_params,
        )

        report_md = generate_markdown_report(
            data, project_key, project_name, week_start, week_end,
            filter_name=filter_name,
            filter_description=filter_desc,
            filter_summary=filter_summary,
        )

        safe_name = safe_filename(filter_name)
        filename = (
            f"weekly_report_{week_start.strftime('%Y%m%d')}_"
            f"{week_end.strftime('%Y%m%d')}_{safe_name}.md"
        )
        output_path = output_dir / filename
        output_path.write_text(report_md, encoding="utf-8")
        _print_summary(output_path, data)
        print()


def _print_summary(output_path: Path, data: dict) -> None:
    print(f"  ✅ 保存: {output_path}")
    print(f"     前週残件: {len(data['carry_over'])} 件 / "
          f"新規: {len(data['new_issues'])} 件 / "
          f"完了: {len(data['completed'])} 件 / "
          f"未完了: {len(data['incomplete'])} 件")


if __name__ == "__main__":
    main()
