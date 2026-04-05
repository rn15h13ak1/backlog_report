#!/usr/bin/env python3
"""
Backlog 週次レポート生成スクリプト
====================================
指定した期間の課題集計をMarkdownファイルとして出力します。
config.yaml の filters に複数のフィルターを定義すると、
フィルターごとに個別のレポートファイルが生成されます。

集計内容:
  ① 前週残件数   : 期間開始より前に作成され、期間開始時点でオープンだった課題
  ② 新規発生件数 : 対象期間に新しく作成された課題
  ③ 再オープン件数: 期間開始時点で完了系だったが、期間中にオープン系へ変化した課題
  ④ 当週完了件数 : 期間中にオープン系から完了系へ変化した課題
  ⑤ 当週未完了件数: ① + ② + ③ のうち④で完了しなかった課題（等式: ① + ② + ③ = ④ + ⑤）
  各カテゴリのBacklog課題番号一覧も出力

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

    def get_issue_by_id(self, issue_id: int) -> dict | None:
        """課題IDで単一課題を取得。取得失敗時は None を返す"""
        try:
            return self._get(f"/issues/{issue_id}")
        except Exception as e:
            if self.debug:
                print(f"  [DEBUG] get_issue_by_id({issue_id}) 失敗: {e}", file=sys.stderr)
            return None

    def get_issue_comments(self, issue_id: int) -> list:
        """
        課題のコメントを全件取得（ページネーション対応）。
        コメントの changeLog にステータス変化履歴が含まれる。
        """
        all_comments = []
        min_id = None

        while True:
            params: dict = {"count": 100, "order": "asc"}
            if min_id is not None:
                params["minId"] = min_id
            try:
                comments = self._get(f"/issues/{issue_id}/comments", params)
            except Exception as e:
                if self.debug:
                    print(f"  [DEBUG] get_issue_comments({issue_id}) 失敗: {e}", file=sys.stderr)
                break
            if not comments:
                break
            all_comments.extend(comments)
            if len(comments) < 100:
                break
            min_id = max(c["id"] for c in comments) + 1
            time.sleep(0.3)

        return all_comments


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

    # ---- 件名キーワードフィルター ----
    keyword = filter_cfg.get("keyword")
    if keyword:
        extra["keyword"] = keyword

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

    # 期間開始時点のステータスを確定
    # ・期間前に変化あり  → 最後の変化の to が期間開始時ステータス
    # ・期間中に初めて変化 → 最初の変化の from が期間開始時ステータス（変化前）
    # ・変化なし         → 現在のステータス（期間中も同じだったため）
    if changes_before:
        status_at_start = changes_before[-1]["to"]
    elif changes_in:
        status_at_start = changes_in[0]["from"]
    else:
        status_at_start = issue.get("status", {}).get("name", "")

    # 期間終了時点のステータスを確定
    # ・期間中に変化あり → 最後の変化の to が期間終了時ステータス
    # ・変化なし         → 期間開始時と同じ
    if changes_in:
        status_at_end = changes_in[-1]["to"]
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


def scan_issue_status_changes_from_activities(
    client: BacklogClient,
    project_key: str,
    week_start: date,
    week_end: date,
    closed_status_names: set,
    open_status_names: set,
    closed_status_ids: list,
    open_status_ids: list,
    status_id_to_name: dict,
) -> tuple:
    """
    /projects/{key}/activities を1回だけ降順スキャンし、
    対象期間内のステータス変化（完了・再オープン両方）を同時に検出する。

    Backlog on-premise では old_value/new_value がステータス名ではなく
    数値ID（文字列）で記録されることがあるため、名前・ID両方で照合する。

    Returns:
        (completed_ids, prev_status_map, reopened_ids)
        completed_ids   : 期間内に完了系へ変化した課題の数値IDセット
        prev_status_map : {issue_id: 完了前のステータス名}
        reopened_ids    : 期間内に完了系→オープン系へ変化した課題の数値IDセット
    """
    completed_ids: set = set()
    prev_status_map: dict = {}
    reopened_ids: set = set()

    # ステータスIDを文字列に変換して照合用セットを作成（APIがIDを文字列で返す場合に対応）
    closed_id_strs: set = {str(sid) for sid in closed_status_ids}
    open_id_strs: set = {str(sid) for sid in open_status_ids}

    def is_closed(val: str) -> bool:
        return val in closed_status_names or val in closed_id_strs

    def is_open(val: str) -> bool:
        return val in open_status_names or val in open_id_strs

    def resolve_name(val: str) -> str:
        """IDまたは名前をステータス名に解決する"""
        if val in status_id_to_name:
            return status_id_to_name[val]
        return val or "処理中"

    params: dict = {
        "activityTypeId": [2, 3],  # type2=課題更新(cloud), type3=課題更新(on-premise) 両対応
        "count": 100,
        "order": "desc",
    }

    if client.debug:
        print(f"  [DEBUG] アクティビティスキャン開始: project={project_key}, "
              f"period={week_start}〜{week_end}", file=sys.stderr)
        print(f"  [DEBUG] 完了ステータス名={closed_status_names} ID={closed_id_strs}",
              file=sys.stderr)
        print(f"  [DEBUG] オープンステータス名={open_status_names} ID={open_id_strs}",
              file=sys.stderr)

    while True:
        activities = client._get(f"/projects/{project_key}/activities", params)
        if not activities:
            break

        stop = False
        for act in activities:
            created_str = act.get("created", "")[:10]
            try:
                act_date = datetime.strptime(created_str, "%Y-%m-%d").date()
            except ValueError:
                continue

            # 対象期間より古ければスキャン終了
            if act_date < week_start:
                stop = True
                break

            # 対象期間内のみ処理
            if act_date <= week_end:
                content = act.get("content", {})
                issue_id = content.get("id")
                key_id = content.get("key_id", "?")
                changes = content.get("changes", [])

                if client.debug and changes:
                    print(f"  [DEBUG] activity {project_key}-{key_id} ({created_str}): "
                          f"type={act.get('type')} changes={changes}", file=sys.stderr)

                for change in changes:
                    if change.get("field") != "status" or issue_id is None:
                        continue
                    old_val = str(change.get("old_value", ""))
                    new_val = str(change.get("new_value", ""))

                    # 完了への変化（名前またはIDで判定）
                    if is_closed(new_val):
                        completed_ids.add(issue_id)
                        if issue_id not in prev_status_map:
                            prev_status_map[issue_id] = resolve_name(old_val)
                        if client.debug:
                            print(f"  [DEBUG] → 完了と判定: {project_key}-{key_id} "
                                  f"(id={issue_id}, {old_val}→{new_val})", file=sys.stderr)

                    # 再オープンへの変化（完了系→オープン系、名前またはIDで判定）
                    if is_closed(old_val) and is_open(new_val):
                        reopened_ids.add(issue_id)
                        if client.debug:
                            print(f"  [DEBUG] → 再オープンと判定: {project_key}-{key_id} "
                                  f"(id={issue_id}, {old_val}→{new_val})", file=sys.stderr)

        if stop or len(activities) < 100:
            break

        min_id = min(act["id"] for act in activities)
        params["maxId"] = min_id - 1
        time.sleep(0.3)

    if client.debug:
        print(f"  [DEBUG] スキャン完了: 完了={len(completed_ids)}件, "
              f"再オープン={len(reopened_ids)}件", file=sys.stderr)

    return completed_ids, prev_status_map, reopened_ids


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

        # ④ 当週完了
        if result["is_completed"]:
            completed_issues.append(issue)

    # ---- ⑤ 当週未完了 = (① + ② + ③) - ④ ----
    completed_id_set = {i.get("id") for i in completed_issues}
    active_ids       = {i.get("id") for i in carry_over_issues + new_issues + reopened_issues}
    incomplete_ids   = active_ids - completed_id_set
    incomplete_issues = [all_issues_map[iid] for iid in incomplete_ids if iid in all_issues_map]

    return {
        "carry_over": carry_over_issues,
        "new_issues": new_issues,
        "completed":  completed_issues,
        "incomplete": incomplete_issues,
        "reopened":   reopened_issues,
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
    reopened = data.get("reopened", [])

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

    # 等式チェック: ① + ② + ③ = ④ + ⑤
    lhs = len(carry_over) + len(new_issues) + len(reopened)
    rhs = len(completed) + len(incomplete)
    if lhs != rhs:
        lines += [
            f"> ⚠️ **注意**: ①残件（{len(carry_over)}）＋ ②新規（{len(new_issues)}）＋ ③再オープン（{len(reopened)}）"
            f"＝ {lhs} に対し、④完了（{len(completed)}）＋ ⑤未完了（{len(incomplete)}）＝ {rhs} と一致しません。",
            "> 同一課題が複数カテゴリに重複して集計されている可能性があります。",
            "",
        ]

    lines += [
        "---",
        "",
        "## ① 前週残件",
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
        "## ② 新規発生",
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
        "## ③ 再オープン",
        f"**{len(reopened)} 件** — {ws_str} 〜 {we_str} に完了状態から再度オープンになった課題",
        "",
        keys_str(reopened),
        "",
        "<details>",
        "<summary>詳細一覧を表示</summary>",
        "",
        format_issue_table(reopened),
        "</details>",
        "",
        "---",
        "",
        "## ④ 当週完了",
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
        "## ⑤ 当週未完了",
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
    keyword = filter_cfg.get("keyword")
    if keyword:
        parts.append(f"件名キーワード: {keyword}")
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
    print(f"プロジェクト : {project_key}（デフォルト）")
    print(f"対象期間    : {week_start} 〜 {week_end}（{period_label}）")
    print(f"フィルター数 : {len(filters_cfg) if filters_cfg else 0}（0=フィルターなし）")
    print()

    ssl_verify = backlog_cfg.get("ssl_verify", True)
    base_path  = backlog_cfg.get("base_path", "")
    client = BacklogClient(space_host, api_key, ssl_verify=ssl_verify, base_path=base_path, debug=args.debug)

    # ---- プロジェクト情報キャッシュ ----
    # 同一 project_key に対する API 呼び出しを1回に抑える。
    # 構造: {project_key: {id, name, issue_type_map, custom_field_map, master_loaded}}
    project_cache: dict = {}

    def get_project_info(pk: str, need_master: bool = False) -> dict:
        """プロジェクト情報をキャッシュ付きで取得する。"""
        if pk not in project_cache:
            print(f"プロジェクト情報を取得中... ({pk})")
            try:
                proj = client.get_project(pk)
            except SystemExit:
                raise
            except Exception as e:
                print(f"エラー: プロジェクト情報の取得に失敗しました ({pk}): {e}", file=sys.stderr)
                sys.exit(1)
            project_cache[pk] = {
                "id":               proj["id"],
                "name":             proj["name"],
                "issue_type_map":   {},
                "custom_field_map": {},
                "master_loaded":    False,
            }
            print(f"プロジェクト名: {project_cache[pk]['name']} (ID: {project_cache[pk]['id']})")

        info = project_cache[pk]

        if need_master and not info["master_loaded"]:
            print(f"種別・カスタム属性マスターを取得中... ({pk})")
            try:
                issue_types = client.get_issue_types(pk)
                info["issue_type_map"] = {it["name"]: it["id"] for it in issue_types}
                if args.debug:
                    print(f"  [DEBUG] 種別マップ（名前→ID）: {info['issue_type_map']}", file=sys.stderr)
                else:
                    print(f"  種別: {list(info['issue_type_map'].keys())}")
            except Exception as e:
                print(f"  ⚠ 種別マスターの取得に失敗: {e}", file=sys.stderr)

            try:
                custom_fields = client.get_custom_fields(pk)
                info["custom_field_map"] = {
                    cf["name"]: {
                        "id":     cf["id"],
                        "typeId": cf.get("typeId"),
                        # リスト型（typeId 5/6/7/8）の選択肢を {名前: ID} で保持
                        "items":  {item["name"]: item["id"] for item in cf.get("items", [])},
                    }
                    for cf in custom_fields
                }
                if args.debug:
                    for fname, finfo in info["custom_field_map"].items():
                        print(f"  [DEBUG] カスタム属性「{fname}」: id={finfo['id']}, typeId={finfo['typeId']}, items={finfo['items']}", file=sys.stderr)
                else:
                    print(f"  カスタム属性: {list(info['custom_field_map'].keys())}")
            except Exception as e:
                print(f"  ⚠ カスタム属性マスターの取得に失敗: {e}", file=sys.stderr)

            info["master_loaded"] = True

        return info

    # デフォルトプロジェクトを先に取得（存在確認 + ヘッダー表示）
    get_project_info(project_key, need_master=False)
    print()

    # 期間フォルダを output_dir 配下に作成（例: reports/20260101_20260107/）
    period_dir = f"{week_start.strftime('%Y%m%d')}_{week_end.strftime('%Y%m%d')}"
    output_dir = output_dir / period_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # ---- フィルターなし（filters が空の場合）----
    if not filters_cfg:
        default_info = get_project_info(project_key)
        print("【フィルターなし】全課題を集計中...")
        data = collect_report_data(
            client, project_key, default_info["id"], week_start, week_end,
            open_status_ids, closed_status_ids
        )
        report_md = generate_markdown_report(
            data, project_key, default_info["name"], week_start, week_end
        )
        output_path = output_dir / "weekly_report.md"
        output_path.write_text(report_md, encoding="utf-8")
        _print_summary(output_path, data)
        return

    # ---- フィルターごとに集計・出力 ----
    for i, filter_cfg in enumerate(filters_cfg, 1):
        filter_name = filter_cfg.get("name") or f"filter_{i}"
        filter_desc = filter_cfg.get("description") or ""

        # フィルター個別の project_key（未指定ならデフォルトを使用）
        filter_project_key = filter_cfg.get("project_key") or project_key

        # プロジェクト情報をキャッシュ付きで取得（初回のみ API 呼び出し）
        proj_info = get_project_info(filter_project_key, need_master=True)
        filter_project_id   = proj_info["id"]
        filter_project_name = proj_info["name"]
        filter_issue_type_map   = proj_info["issue_type_map"]
        filter_custom_field_map = proj_info["custom_field_map"]

        filter_summary = build_filter_summary(filter_cfg)

        print(f"[{i}/{len(filters_cfg)}] フィルター「{filter_name}」を集計中...")
        if filter_project_key != project_key:
            print(f"         プロジェクト: {filter_project_key}")
        print(f"         条件: {filter_summary}")

        # フィルターパラメータを解決
        extra_params = resolve_filter_params(filter_cfg, filter_issue_type_map, filter_custom_field_map)
        if args.debug:
            print(f"  [DEBUG] 解決済みフィルターパラメータ: {extra_params}", file=sys.stderr)

        data = collect_report_data(
            client, filter_project_key, filter_project_id, week_start, week_end,
            open_status_ids, closed_status_ids,
            extra_params=extra_params,
        )

        report_md = generate_markdown_report(
            data, filter_project_key, filter_project_name, week_start, week_end,
            filter_name=filter_name,
            filter_description=filter_desc,
            filter_summary=filter_summary,
        )

        safe_name = safe_filename(filter_name)
        output_path = output_dir / f"weekly_report_{safe_name}.md"
        output_path.write_text(report_md, encoding="utf-8")
        _print_summary(output_path, data)
        print()


def _print_summary(output_path: Path, data: dict) -> None:
    print(f"  ✅ 保存: {output_path}")
    print(f"     ①前週残件: {len(data['carry_over'])} 件 / "
          f"②新規: {len(data['new_issues'])} 件 / "
          f"③再オープン: {len(data['reopened'])} 件 / "
          f"④完了: {len(data['completed'])} 件 / "
          f"⑤未完了: {len(data['incomplete'])} 件")


if __name__ == "__main__":
    main()
