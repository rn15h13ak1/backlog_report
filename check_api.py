#!/usr/bin/env python3
"""
Backlog API 接続診断スクリプト
config.yaml の設定を読み込んで実際にリクエストを送信し、
週次レポート生成に必要なエンドポイントが利用できるかを確認します。

  python check_api.py
  python check_api.py --config path/to/config.yaml
"""
import argparse
import sys
from pathlib import Path

from backlog_weekly_report import (
    DEFAULT_CLOSED_STATUS_IDS,
    DEFAULT_OPEN_STATUS_IDS,
    BacklogAPIError,
    BacklogClient,
    format_api_error,
    load_config,
    to_local_date,
    validate_backlog_config,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backlog API 接続診断")
    parser.add_argument("--config", default=str(Path(__file__).parent / "config.yaml"),
                        help="設定ファイルのパス（デフォルト: スクリプトと同じディレクトリの config.yaml）")
    parser.add_argument("--debug", action="store_true", help="APIリクエストのパラメータを表示する")
    args = parser.parse_args()

    config = load_config(args.config)
    backlog_cfg = config.get("backlog", {})
    report_cfg = config.get("report", {})

    space_host, api_key, project_key = validate_backlog_config(backlog_cfg)
    closed_status_ids = report_cfg.get("closed_status_ids", DEFAULT_CLOSED_STATUS_IDS)
    open_status_ids   = report_cfg.get("open_status_ids",   DEFAULT_OPEN_STATUS_IDS)

    client = BacklogClient(
        space_host,
        api_key,
        ssl_verify=backlog_cfg.get("ssl_verify", True),
        base_path=backlog_cfg.get("base_path", ""),
        debug=args.debug,
    )

    print(f"接続先: {client.base_url}")
    print(f"プロジェクト: {project_key}")
    print(f"オープン系ステータスID: {open_status_ids} / 完了系ステータスID: {closed_status_ids}")
    print()

    # ---- Step1: 接続・認証確認（/space は最もシンプルなエンドポイント）----
    print("=== Step1: 接続・認証確認 ===")
    try:
        space = client._get("/space")
        print(f"✅ 接続OK: {space.get('name', space)}")
    except BacklogAPIError as e:
        print("❌ 接続失敗")
        print(format_api_error(e), file=sys.stderr)
        print("  → space_host / base_path / api_key / ssl_verify を確認してください")
        sys.exit(1)
    print()

    # ---- Step2: プロジェクト情報とステータス一覧 ----
    print("=== Step2: プロジェクト情報・ステータス一覧 ===")
    try:
        project = client.get_project(project_key)
        print(f"✅ プロジェクト取得OK: {project.get('name')} (id={project.get('id')})")
    except BacklogAPIError as e:
        print("❌ プロジェクト取得失敗")
        print(format_api_error(e), file=sys.stderr)
        print("  → project_key を確認してください")
        sys.exit(1)
    project_id = project.get("id")

    try:
        statuses = client.get_statuses(project_key)
        known_ids = set(open_status_ids) | set(closed_status_ids)
        for s in statuses:
            if s["id"] in open_status_ids:
                label = "オープン系"
            elif s["id"] in closed_status_ids:
                label = "完了系"
            else:
                label = "⚠ 未分類（config.yaml に未設定）"
            print(f"   id={s['id']}: {s['name']}  → {label}")
        missing = [s for s in statuses if s["id"] not in known_ids]
        if missing:
            print("   ⚠ 未分類のステータスがあります。open_status_ids / closed_status_ids を見直してください。")
    except BacklogAPIError as e:
        print("❌ ステータス一覧の取得に失敗")
        print(format_api_error(e), file=sys.stderr)
    print()

    # ---- Step3: 課題を1件取得 ----
    print("=== Step3: 課題を1件取得 ===")
    # ステータス変化を確認しやすいよう完了済み課題を優先して取得
    issues = client._get("/issues", {"projectId": [project_id], "statusId": closed_status_ids, "count": 1})
    if not issues:
        # 完了済みがなければ全ステータスで取得
        issues = client._get("/issues", {"projectId": [project_id], "count": 1})
    if not issues:
        print("課題が1件も見つかりませんでした。")
        sys.exit(1)

    issue = issues[0]
    issue_id = issue["id"]
    print(f"取得した課題: {issue['issueKey']} (id={issue_id}, "
          f"status={issue.get('status', {}).get('name')}, "
          f"created={to_local_date(issue.get('created', ''))} JST)")
    print()

    # ---- Step4: コメントの changeLog にステータス変化が記録されているか ----
    print("=== Step4: /issues/{id}/comments の changeLog 確認 ===")
    try:
        comments = client._get(f"/issues/{issue_id}/comments", {"count": 20, "order": "desc"})
    except BacklogAPIError as e:
        print("❌ エンドポイント無効またはエラー")
        print(format_api_error(e), file=sys.stderr)
        sys.exit(1)

    print(f"✅ エンドポイント有効。取得件数: {len(comments)}")
    status_changes = [
        {
            "date": to_local_date(c.get("created", "")),
            "from": cl.get("originalValue"),
            "to":   cl.get("newValue"),
        }
        for c in comments
        for cl in c.get("changeLog", [])
        if cl.get("field") == "status"
    ]
    if status_changes:
        print(f"   ステータス変化の記録あり（直近{len(status_changes)}件・日付はJST）:")
        for sc in status_changes:
            print(f"     {sc['date']}: {sc['from']} → {sc['to']}")
    else:
        print("   直近20コメント内にステータス変化なし")
        print("   ※ changeLog フィールドの有無を確認します")
        has_changelog = any("changeLog" in c for c in comments)
        print(f"   changeLog フィールド: {'あり' if has_changelog else 'なし（コメント構造が異なる可能性）'}")
        if comments:
            print(f"   コメント構造サンプル: {list(comments[0].keys())}")


if __name__ == "__main__":
    try:
        main()
    except BacklogAPIError as e:
        print(format_api_error(e), file=sys.stderr)
        sys.exit(1)
