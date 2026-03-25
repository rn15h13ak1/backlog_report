#!/usr/bin/env python3
"""
Backlog 週次レポート生成スクリプト
====================================
前週（または当週）の課題集計をMarkdownファイルとして出力します。

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
import os
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

    def get_issue_count(self, project_id: int, params: dict = None) -> int:
        """課題件数を取得"""
        base_params = params or {}
        base_params["projectId"] = [project_id]
        result = self._get("/issues/count", base_params)
        return result.get("count", 0)


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

    # 今週月曜日（または日曜日）を求める
    if week_start == "monday":
        days_since_start = today.weekday()  # 月=0, 日=6
    else:  # sunday
        days_since_start = (today.weekday() + 1) % 7  # 日=0, 月=1, ..., 土=6

    this_week_start = today - timedelta(days=days_since_start)
    this_week_end = this_week_start + timedelta(days=6)

    if target_week == "previous":
        week_start_date = this_week_start - timedelta(weeks=1)
        week_end_date = this_week_start - timedelta(days=1)
    else:  # current
        week_start_date = this_week_start
        week_end_date = today

    return week_start_date, week_end_date


# ==============================================================
# 集計ロジック
# ==============================================================

def collect_report_data(client: BacklogClient, project_id: int,
                        week_start: date, week_end: date,
                        open_status_ids: list, closed_status_ids: list) -> dict:
    """
    週次レポートに必要なデータを集計する

    Returns:
        dict with keys:
            carry_over   : 前週からの残件（課題リスト）
            new_issues   : 新規発生（課題リスト）
            completed    : 当週完了（課題リスト）
            incomplete   : 未完了（課題リスト）
    """
    ws = week_start.strftime("%Y-%m-%d")
    we = week_end.strftime("%Y-%m-%d")

    print(f"  対象期間: {ws} 〜 {we}")
    print("  課題データを取得中...")

    # ① 前週からの残件
    #    week_start より前に作成され、かつ現在も未完了のもの
    print("  [1/4] 前週からの残件を取得中...")
    carry_over_issues = client.get_issues(project_id, {
        "statusId": open_status_ids,
        "createdUntil": (week_start - timedelta(days=1)).strftime("%Y-%m-%d"),
    })

    # ② 新規発生
    #    対象週に作成されたすべての課題（ステータス問わず）
    print("  [2/4] 新規発生課題を取得中...")
    new_issues = client.get_issues(project_id, {
        "createdSince": ws,
        "createdUntil": we,
    })

    # ③ 当週完了
    #    対象週に更新（クローズ）された完了ステータスの課題
    #    ※ Backlog APIには「完了日」フィルタがないため、
    #      「対象週に更新された完了課題」で近似します
    print("  [3/4] 当週完了課題を取得中...")
    completed_issues = client.get_issues(project_id, {
        "statusId": closed_status_ids,
        "updatedSince": ws,
        "updatedUntil": we,
    })

    # ④ 未完了（現在オープンの全課題）
    print("  [4/4] 未完了課題を取得中...")
    incomplete_issues = client.get_issues(project_id, {
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

def format_issue_list(issues: list, project_key: str, max_display: int = 30) -> str:
    """課題リストをMarkdown表形式にフォーマット"""
    if not issues:
        return "_（該当なし）_\n"

    lines = []
    lines.append("| 課題番号 | 件名 | ステータス | 担当者 |")
    lines.append("|---------|------|-----------|-------|")

    for issue in issues[:max_display]:
        issue_key = issue.get("issueKey", "-")
        summary = issue.get("summary", "-").replace("|", "｜")  # パイプをエスケープ
        status = issue.get("status", {}).get("name", "-")
        assignee = issue.get("assignee")
        assignee_name = assignee.get("name", "-") if assignee else "_未割当_"
        lines.append(f"| {issue_key} | {summary} | {status} | {assignee_name} |")

    if len(issues) > max_display:
        lines.append(f"\n_...他 {len(issues) - max_display} 件（表示上限 {max_display} 件）_")

    return "\n".join(lines) + "\n"


def generate_markdown_report(data: dict, project_key: str, project_name: str,
                              week_start: date, week_end: date) -> str:
    """Markdownレポートを生成"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    ws_str = week_start.strftime("%Y/%m/%d")
    we_str = week_end.strftime("%Y/%m/%d")

    carry_over = data["carry_over"]
    new_issues = data["new_issues"]
    completed = data["completed"]
    incomplete = data["incomplete"]

    # 課題番号のみの一覧（コンパクト表示用）
    def keys_str(issues):
        keys = [i.get("issueKey", "?") for i in issues]
        if not keys:
            return "_（なし）_"
        return "、".join(keys[:20]) + (f" 他{len(keys)-20}件" if len(keys) > 20 else "")

    lines = [
        f"# 週次レポート — {ws_str} 〜 {we_str}",
        "",
        f"> プロジェクト: **{project_name}** (`{project_key}`)  ",
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
        format_issue_list(carry_over, project_key),
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
        format_issue_list(new_issues, project_key),
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
        format_issue_list(completed, project_key),
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
        format_issue_list(incomplete, project_key, max_display=50),
        "</details>",
        "",
        "---",
        "",
        "_このレポートは backlog_weekly_report.py により自動生成されました。_",
    ]

    return "\n".join(lines)


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
    parser.add_argument("--config", default="config.yaml", help="設定ファイルのパス（デフォルト: config.yaml）")
    parser.add_argument("--week", choices=["previous", "current"],
                        help="対象週の指定（設定ファイルの値を上書き）")
    args = parser.parse_args()

    # 設定読み込み
    config = load_config(args.config)
    backlog_cfg = config.get("backlog", {})
    report_cfg = config.get("report", {})

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

    # 対象週を計算
    week_start, week_end = get_week_range(target_week, week_start_day)

    print("=" * 50)
    print("Backlog 週次レポート生成")
    print("=" * 50)
    print(f"スペース: {space_host}")
    print(f"プロジェクト: {project_key}")
    print(f"対象週: {week_start} 〜 {week_end}")
    print()

    # Backlog APIクライアント初期化
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
    print()

    # データ収集
    print("集計データを取得中...")
    data = collect_report_data(
        client, project_id,
        week_start, week_end,
        open_status_ids, closed_status_ids
    )
    print()

    # レポート生成
    print("レポートを生成中...")
    report_md = generate_markdown_report(
        data, project_key, project_name, week_start, week_end
    )

    # 出力先ディレクトリ作成
    output_dir.mkdir(parents=True, exist_ok=True)

    # ファイル保存
    filename = f"weekly_report_{week_start.strftime('%Y%m%d')}_{week_end.strftime('%Y%m%d')}.md"
    output_path = output_dir / filename
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report_md)

    print(f"✅ レポートを保存しました: {output_path}")
    print()
    print("--- サマリー ---")
    print(f"  前週からの残件数: {len(data['carry_over'])} 件")
    print(f"  新規発生件数:     {len(data['new_issues'])} 件")
    print(f"  当週完了件数:     {len(data['completed'])} 件")
    print(f"  未完了件数:       {len(data['incomplete'])} 件")


if __name__ == "__main__":
    main()
