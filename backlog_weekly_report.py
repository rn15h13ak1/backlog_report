#!/usr/bin/env python3
"""
Backlog 週次レポート生成スクリプト
====================================
指定した期間の課題集計をMarkdownファイルとして出力します。
config.yaml の filters に複数のフィルターを定義すると、
フィルターごとに個別のレポートファイルが生成されます。

集計内容:
  - 期間開始前からの残件数（期間開始より前に作成され現在も未完了の課題数）
  - 新規発生件数（対象期間に作成された課題数）
  - 期間内完了件数（対象期間に完了した課題数）
  - 未完了件数（現在オープンの課題数）
  - 各カテゴリのBacklog課題番号一覧

使い方:
  # 前週を自動計算して集計（デフォルト）
  python backlog_weekly_report.py

  # 今週（月曜〜今日）を集計
  python backlog_weekly_report.py --week current

  # 期間を直接指定して集計
  python backlog_weekly_report.py --from 2026-03-01 --to 2026-03-31

  # 設定ファイルを指定
  python backlog_weekly_report.py --config path/to/config.yaml --from 2026-03-01 --to 2026-03-15
"""

import argparse
import sys
import ssl
import time
import urllib.request
import urllib.parse
import urllib.error
import json
import yaml
from datetime import datetime, timedelta, date
from pathlib import Path


# ==============================================================
# Backlog API クライアント
# ==============================================================

class BacklogClient:
    def __init__(self, space_host: str, api_key: str, ssl_verify: bool = True, base_path: str = "", debug: bool = False):
        # base_path の前後スラッシュを正規化（例: "/backlog/" → "/backlog"）
        base_path = "/" + base_path.strip("/") if base_path.strip("/") else ""
        self.base_url = f"https://{space_host}{base_path}/api/v2"
        self.api_key = api_key
        self.debug = debug
        # SSL検証を無効にする場合のコンテキスト
        if ssl_verify:
            self.ssl_context = None
        else:
            self.ssl_context = ssl.create_default_context()
            self.ssl_context.check_hostname = False
            self.ssl_context.verify_mode = ssl.CERT_NONE

    def _get(self, endpoint: str, params: dict = None) -> dict | list:
        """GETリクエストを送信してJSONを返す"""
        params = params or {}
        params["apiKey"] = self.api_key

        # リストパラメータを展開（例: statusId[] → statusId%5B%5D=1&statusId%5B%5D=2）
        # 注意: [ ] はRFC3986のクエリ文字として非合法なため %5B %5D にエンコードする
        query_parts = []
        for key, value in params.items():
            if isinstance(value, list):
                for v in value:
                    query_parts.append(f"{urllib.parse.quote(key)}%5B%5D={urllib.parse.quote(str(v))}")
            else:
                query_parts.append(f"{urllib.parse.quote(key)}={urllib.parse.quote(str(value))}")
        query_string = "&".join(query_parts)

        url = f"{self.base_url}{endpoint}?{query_string}"

        if self.debug:
            # APIキーを除いたパラメータを表示
            debug_parts = [p for p in query_parts if not p.startswith("apiKey=")]
            print(f"  [DEBUG] {endpoint} ?" + "&".join(debug_parts), file=sys.stderr)

        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=30, context=self.ssl_context) as res:
                return json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # レスポンスボディから詳細メッセージを取得
            detail = ""
            raw_body = ""
            try:
                raw_body = e.read().decode("utf-8")
                body = json.loads(raw_body)
                errors = body.get("errors", [])
                if errors:
                    detail = " / ".join(
                        f"{err.get('message', '')}（code={err.get('code')}）"
                        for err in errors
                    )
            except Exception:
                pass

            # エンドポイントのみ表示（APIキーを含むURLは表示しない）
            print(f"エラー: API呼び出しに失敗しました（HTTP {e.code}）: {endpoint}", file=sys.stderr)
            if detail:
                print(f"  詳細: {detail}", file=sys.stderr)
            elif raw_body:
                # detailが取れない場合はボディをそのまま表示（デバッグ用）
                print(f"  レスポンス: {raw_body[:500]}", file=sys.stderr)

            if e.code == 400:
                print("  → リクエストパラメータを確認してください。", file=sys.stderr)
                print("    フィルターの field_name / field_id や values の値が正しいか確認してください。", file=sys.stderr)
            elif e.code == 401:
                print("  → api_key を確認してください。", file=sys.stderr)
            elif e.code == 403:
                print("  → api_key の権限を確認してください。", file=sys.stderr)
            elif e.code == 404:
                print("  → space_host または project_key を確認してください。", file=sys.stderr)
            sys.exit(1)

    def get_project(self, project_key: str) -> dict:
        """プロジェクト情報を取得"""
        return self._get(f"/projects/{project_key}")

    def get_issue_types(self, project_id_or_key: str | int) -> list:
        """プロジェクトの種別一覧を取得"""
        return self._get(f"/projects/{project_id_or_key}/issueTypes")

    def get_custom_fields(self, project_id_or_key: str | int) -> list:
        """プロジェクトのカスタム属性一覧を取得"""
        return self._get(f"/projects/{project_id_or_key}/customFields")

    def get_statuses(self, project_id_or_key: str | int) -> list:
        """プロジェクトのステータス一覧を取得"""
        return self._get(f"/projects/{project_id_or_key}/statuses")

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


def get_week_range(target_week: str, week_start: str) -> tuple[date, date]:
    """
    対象週の開始日と終了日を返す（date型）

    target_week: "previous" or "current"
    week_start:  曜日名（"monday"〜"sunday" または "月"〜"日"）
    """
    today = date.today()

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
    custom_field_map: dict, # {名前: {id, typeId, items: {名前: ID}}}
) -> dict:
    """
    config の filters[i] から Backlog API クエリパラメータを構築して返す。

    Returns:
        dict: get_issues() に追加で渡すパラメータ
              例: {"issueTypeId": [1, 2], "customField_123": [456]}
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
            items_map = {}
            for info in custom_field_map.values():
                if info["id"] == field_id:
                    type_id = info.get("typeId")
                    items_map = info.get("items", {})
                    break
        elif "field_name" in cf:
            name = cf["field_name"]
            if name not in custom_field_map:
                print(f"  ⚠ カスタム属性「{name}」が見つかりません（スキップ）", file=sys.stderr)
                continue
            field_id = custom_field_map[name]["id"]
            type_id = custom_field_map[name].get("typeId")
            items_map = custom_field_map[name].get("items", {})
        else:
            print("  ⚠ custom_fields に field_name または field_id が必要です（スキップ）",
                  file=sys.stderr)
            continue

        # typeId 5=単一リスト, 6=複数リスト, 7=チェックボックス, 8=ラジオ
        # → 選択肢名を数値IDに変換してからリスト型パラメータ（[] 付き）で送信
        # typeId 1=テキスト, 2=文章, 3=数値, 4=日付 → 単一値（変換不要）
        list_types = {5, 6, 7, 8}

        def resolve_value(v):
            """選択肢名 → 数値ID に変換（items_mapにあれば）"""
            if isinstance(v, str) and v in items_map:
                return items_map[v]
            return v

        if type_id in list_types or len(values) > 1:
            resolved = [resolve_value(v) for v in values]
            extra[f"customField_{field_id}"] = resolved
        else:
            extra[f"customField_{field_id}"] = resolve_value(values[0])

    return extra


# ==============================================================
# 集計ロジック
# ==============================================================

def get_completed_issue_ids_from_project_activities(
    client: BacklogClient,
    project_key: str,
    week_start: date,
    week_end: date,
    closed_status_names: set,
) -> set:
    """
    /projects/{key}/activities（typeId=2: 課題更新）を降順に取得し、
    対象期間内にステータスが完了系へ変化した課題の数値IDセットを返す。

    Backlog API には課題単位のアクティビティ取得エンドポイントが
    存在しないオンプレミス版があるため、プロジェクト全体のアクティビティを
    ID降順（新しい順）にページネーションしながら走査する。
    対象期間より古いアクティビティが出た時点で走査を打ち切る。
    """
    completed_ids: set = set()
    params: dict = {
        "activityTypeId": [2],  # ISSUE_UPDATED のみ
        "count": 100,
        "order": "desc",        # 新しい順
    }

    while True:
        activities = client._get(f"/projects/{project_key}/activities", params)
        if not activities:
            break

        stop = False
        for act in activities:
            created_str = act.get("created", "")[:10]  # "YYYY-MM-DD"
            try:
                act_date = datetime.strptime(created_str, "%Y-%m-%d").date()
            except ValueError:
                continue

            # 対象期間より古ければ終了
            if act_date < week_start:
                stop = True
                break

            # 対象期間内のみ処理
            if act_date <= week_end:
                changes = act.get("content", {}).get("changes", [])
                for change in changes:
                    if (change.get("field") == "status"
                            and change.get("new_value") in closed_status_names):
                        issue_id = act.get("content", {}).get("id")
                        if issue_id is not None:
                            completed_ids.add(issue_id)

        if stop or len(activities) < 100:
            break

        # 次ページ: 今回取得した中の最小IDより前を取得
        min_id = min(act["id"] for act in activities)
        params["maxId"] = min_id - 1
        time.sleep(0.3)

    return completed_ids


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
    extra_params にフィルター（種別・カスタム属性）を渡す。
    """
    ws = week_start.strftime("%Y-%m-%d")
    we = week_end.strftime("%Y-%m-%d")
    ep = extra_params or {}

    # ---- ③ 当週完了（先に計算。①の残件判定に使用する） ----
    # プロジェクトアクティビティで期間内のステータス変化を正確に判定
    try:
        statuses = client.get_statuses(project_key)
        closed_status_names = {
            s["name"] for s in statuses if s["id"] in closed_status_ids
        }
    except Exception:
        closed_status_names = set()

    if closed_status_names:
        # プロジェクトアクティビティから期間内に完了へ変化した課題IDを収集
        completed_ids = get_completed_issue_ids_from_project_activities(
            client, project_key, week_start, week_end, closed_status_names
        )
        # 現在も完了ステータスの全課題を取得し、IDで絞り込む
        if completed_ids:
            all_closed = client.get_issues(project_id, {
                **ep,
                "statusId": closed_status_ids,
            })
            completed_issues = [i for i in all_closed if i.get("id") in completed_ids]
        else:
            completed_issues = []
    else:
        # ステータス名が取れない場合は updatedSince/Until で近似（フォールバック）
        completed_issues = client.get_issues(project_id, {
            **ep,
            "statusId": closed_status_ids,
            "updatedSince": ws,
            "updatedUntil": we,
        })
    completed_id_set = {i.get("id") for i in completed_issues}

    # ---- ① 前週からの残件 ----
    # 期間開始前に作成され、期間開始前に完了していない課題
    #   = 現在もオープンで期間前に作成された課題
    #   + 期間中に完了したが期間前に作成された課題（期間開始時はオープンだった）
    carry_over_open = client.get_issues(project_id, {
        **ep,
        "statusId": open_status_ids,
        "createdUntil": (week_start - timedelta(days=1)).strftime("%Y-%m-%d"),
    })
    carry_over_completed = [
        i for i in completed_issues
        if i.get("created", "")[:10] < ws  # 期間開始日より前に作成
    ]
    # 重複排除してマージ
    seen_ids = set()
    carry_over_issues = []
    for i in carry_over_open + carry_over_completed:
        iid = i.get("id")
        if iid not in seen_ids:
            seen_ids.add(iid)
            carry_over_issues.append(i)

    # ---- ② 新規発生（対象期間に作成された課題、ステータス問わず） ----
    new_issues = client.get_issues(project_id, {
        **ep,
        "createdSince": ws,
        "createdUntil": we,
    })

    # ---- ④ 当週未完了 ----
    # 期間最終日時点でオープンの課題 = (残件 + 新規) - 完了
    all_issues_map = {}
    for i in carry_over_issues + new_issues:
        all_issues_map[i.get("id")] = i
    incomplete_ids = set(all_issues_map.keys()) - completed_id_set
    incomplete_issues = [all_issues_map[iid] for iid in incomplete_ids]

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
        due_date = due_raw[:10] if due_raw else "-"  # "YYYY-MM-DDTHH:mm:ss" → "YYYY-MM-DD"
        lines.append(f"| {issue_key} | {summary} | {status} | {assignee_name} | {due_date} |")

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
        f"| 前週からの残件数 | **{len(carry_over)}** 件 |",
        f"| 新規発生件数 | **{len(new_issues)}** 件 |",
        f"| 当週完了件数 | **{len(completed)}** 件 |",
        f"| 当週未完了件数 | **{len(incomplete)}** 件 |",
        "",
    ]

    # 等式チェック: 残件 + 新規 = 完了 + 未完了
    lhs = len(carry_over) + len(new_issues)
    rhs = len(completed) + len(incomplete)
    if lhs != rhs:
        lines += [
            f"> ⚠️ **注意**: 残件数（{len(carry_over)}）＋ 新規発生（{len(new_issues)}）"
            f"＝ {lhs} に対し、完了（{len(completed)}）＋ 未完了（{len(incomplete)}）＝ {rhs} と一致しません。",
            "> 期間中に処理済みから処理中への差し戻しが発生している可能性があります。",
            "",
        ]

    lines += [
        "---",
        "",
        "## 前週からの残件",
        f"**{len(carry_over)} 件** — {week_start.strftime('%Y/%m/%d')} より前に作成され、{week_start.strftime('%Y/%m/%d')} 時点で未完了の課題",
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
        "## 当週未完了一覧",
        f"**{len(incomplete)} 件** — {we_str} 時点でオープン（未対応・処理中）の課題",
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
    default_config = str(Path(__file__).parent / "config.yaml")
    parser.add_argument("--config", default=default_config,
                        help=f"設定ファイルのパス（デフォルト: スクリプトと同じディレクトリの config.yaml）")
    parser.add_argument("--week", choices=["previous", "current"],
                        help="対象週の指定（設定ファイルの値を上書き）")
    parser.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD",
                        help="集計開始日（例: 2026-03-01）。--to と併用。")
    parser.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD",
                        help="集計終了日（例: 2026-03-31）。--from と併用。")
    parser.add_argument("--debug", action="store_true",
                        help="APIリクエストのパラメータを表示する（トラブルシューティング用）")
    args = parser.parse_args()

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

    output_dir_raw = report_cfg.get("output_dir", "./reports")
    output_dir = Path(output_dir_raw)
    if not output_dir.is_absolute():
        # 相対パスはスクリプトと同じディレクトリ基準で解決（フルパス実行対応）
        output_dir = Path(__file__).parent / output_dir
    open_status_ids = report_cfg.get("open_status_ids", [1, 2, 3])
    closed_status_ids = report_cfg.get("closed_status_ids", [4])

    # 期間の決定: --from/--to > --week > config.period > config.target_week
    cfg_period = report_cfg.get("period") or {}

    if args.date_from:
        # 最優先: コマンドライン引数
        try:
            week_start = datetime.strptime(args.date_from, "%Y-%m-%d").date()
            week_end   = datetime.strptime(args.date_to,   "%Y-%m-%d").date()
        except ValueError:
            parser.error("日付は YYYY-MM-DD 形式で入力してください（例: 2026-03-01）")
        if week_start > week_end:
            parser.error("--from は --to より前の日付を指定してください。")
        period_label = "指定期間（引数）"
    elif args.week:
        # 2番目: --week オプション
        week_start_day = report_cfg.get("week_start", "monday")
        week_start, week_end = get_week_range(args.week, week_start_day)
        period_label = "前週" if args.week == "previous" else "今週"
    elif cfg_period.get("from") and cfg_period.get("to"):
        # 3番目: config.yaml の period 設定
        try:
            week_start = datetime.strptime(str(cfg_period["from"]), "%Y-%m-%d").date()
            week_end   = datetime.strptime(str(cfg_period["to"]),   "%Y-%m-%d").date()
        except ValueError:
            print("エラー: config.yaml の report.period.from / to は YYYY-MM-DD 形式で記入してください",
                  file=sys.stderr)
            sys.exit(1)
        if week_start > week_end:
            print("エラー: config.yaml の report.period.from は to より前の日付にしてください",
                  file=sys.stderr)
            sys.exit(1)
        period_label = "指定期間（config）"
    else:
        # 最終フォールバック: target_week の自動計算
        target_week    = report_cfg.get("target_week", "previous")
        week_start_day = report_cfg.get("week_start", "monday")
        week_start, week_end = get_week_range(target_week, week_start_day)
        period_label = "前週" if target_week == "previous" else "今週"

    print("=" * 55)
    print("Backlog レポート生成")
    print("=" * 55)
    print(f"スペース    : {space_host}")
    print(f"プロジェクト : {project_key}")
    print(f"対象期間    : {week_start} 〜 {week_end}（{period_label}）")
    print(f"フィルター数 : {len(filters_cfg) if filters_cfg else 0}（0=フィルターなし）")
    print()

    ssl_verify = backlog_cfg.get("ssl_verify", True)
    base_path  = backlog_cfg.get("base_path", "")
    client = BacklogClient(space_host, api_key, ssl_verify=ssl_verify, base_path=base_path, debug=args.debug)

    # プロジェクト情報取得
    print("プロジェクト情報を取得中...")
    try:
        project = client.get_project(project_key)
    except SystemExit:
        raise
    except Exception as e:
        print(f"エラー: プロジェクト情報の取得に失敗しました: {e}", file=sys.stderr)
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
            if args.debug:
                print(f"  [DEBUG] 種別マップ（名前→ID）: {issue_type_map}", file=sys.stderr)
            else:
                print(f"  種別: {list(issue_type_map.keys())}")
        except Exception as e:
            print(f"  ⚠ 種別マスターの取得に失敗: {e}", file=sys.stderr)

        try:
            custom_fields = client.get_custom_fields(project_key)
            custom_field_map = {
                cf["name"]: {
                    "id": cf["id"],
                    "typeId": cf.get("typeId"),
                    # リスト型（typeId 5/6/7/8）の選択肢を {名前: ID} で保持
                    "items": {
                        item["name"]: item["id"]
                        for item in cf.get("items", [])
                    },
                }
                for cf in custom_fields
            }
            if args.debug:
                # items も含めた詳細マップを表示
                for fname, finfo in custom_field_map.items():
                    print(f"  [DEBUG] カスタム属性「{fname}」: id={finfo['id']}, typeId={finfo['typeId']}, items={finfo['items']}", file=sys.stderr)
            else:
                print(f"  カスタム属性: {list(custom_field_map.keys())}")
        except Exception as e:
            print(f"  ⚠ カスタム属性マスターの取得に失敗: {e}", file=sys.stderr)

    print()
    output_dir.mkdir(parents=True, exist_ok=True)

    # ---- フィルターなし（filters が空の場合）----
    if not filters_cfg:
        print("【フィルターなし】全課題を集計中...")
        data = collect_report_data(
            client, project_key, project_id, week_start, week_end,
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
        if args.debug:
            print(f"  [DEBUG] 解決済みフィルターパラメータ: {extra_params}", file=sys.stderr)

        data = collect_report_data(
            client, project_key, project_id, week_start, week_end,
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
