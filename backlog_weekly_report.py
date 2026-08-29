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
  ④ 当週完了件数 : 期間中にオープン系から完了系へ変化し、期間終了時点も完了系の課題
  ⑤ 当週未完了件数: ① + ② + ③ のうち④で完了しなかった課題（等式: ① + ② + ③ = ④ + ⑤）
  各カテゴリのBacklog課題番号一覧も出力

日時の扱い:
  Backlog API は日時を UTC で返すため、日付の判定はすべて JST（UTC+9）に
  変換してから行います。

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
import json
import random
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import yaml

# ==============================================================
# 定数
# ==============================================================

# Backlog API は UTC で日時を返すため、日付判定は JST に変換して行う
JST = timezone(timedelta(hours=9))

# 完了系とみなすステータスIDの既定値（3=処理済み, 4=完了）。
# ここに登録されていないステータスは、すべてオープン系として扱う。
DEFAULT_CLOSED_STATUS_IDS = [3, 4]

# コメント取得の並列度の既定値
DEFAULT_MAX_WORKERS = 4
# 設定ミスで API を叩きすぎないための上限。Backlog のレート制限に配慮する。
MAX_WORKERS_LIMIT = 8

API_TIMEOUT = 30       # 1リクエストのタイムアウト（秒）
API_MAX_RETRIES = 3    # 一時的な失敗に対する最大リトライ回数
API_PAGE_SIZE = 100    # Backlog API の1回あたり最大取得件数
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RETRY_MAX_DELAY = 60.0  # リトライ1回あたりの最大待機秒数

# レポート表示上限
TABLE_MAX_DISPLAY = 30
TABLE_MAX_DISPLAY_INCOMPLETE = 50
KEYS_MAX_DISPLAY = 20


# JST は UTC+9 なので、UTC のこの時刻以降は JST では翌日になる
_NEXT_DAY_FROM_UTC_HOUR = 24 - int(JST.utcoffset(None).total_seconds() // 3600)


@lru_cache(maxsize=100_000)
def to_local_date(iso: str) -> str:
    """
    Backlog が返す UTC の ISO 日時を JST の 'YYYY-MM-DD' 文字列に変換する。

    例: '2026-04-07T15:30:00Z' → '2026-04-08'（JST では翌日）

    パースできない値は従来どおり先頭10文字をそのまま返す。

    課題1件あたりコメント数ぶん呼ばれ、フィルターごとに同じ値を繰り返し変換するため、
    処理時間に効く。Backlog が返す 'YYYY-MM-DDTHH:MM:SSZ' 形式は文字列のまま判定し、
    さらに結果をキャッシュする（strptime 経由に比べて約58倍）。
    それ以外の形式は従来どおり strptime で解釈する。
    """
    if not iso:
        return ""

    if len(iso) == 20 and iso[4] == "-" and iso[7] == "-" and iso[10] == "T" and iso[19] == "Z":
        try:
            if int(iso[11:13]) < _NEXT_DAY_FROM_UTC_HOUR:
                return iso[:10]
            next_day = date(int(iso[:4]), int(iso[5:7]), int(iso[8:10])) + timedelta(days=1)
            return next_day.isoformat()
        except ValueError:
            pass   # 桁は合っているが値が不正。下の strptime に委ねる。

    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return iso[:10]
    return dt.astimezone(JST).date().isoformat()


# ==============================================================
# Backlog API クライアント
# ==============================================================

class BacklogAPIError(Exception):
    """Backlog API 呼び出しの失敗を表す例外"""

    def __init__(self, endpoint: str, status_code: int | None = None,
                 detail: str = "", raw_body: str = ""):
        self.endpoint = endpoint
        self.status_code = status_code
        self.detail = detail
        self.raw_body = raw_body
        super().__init__(f"{endpoint} (HTTP {status_code})" if status_code else f"{endpoint}: {detail}")


def format_api_error(err: BacklogAPIError) -> str:
    """BacklogAPIError を利用者向けの日本語メッセージに整形する"""
    # エンドポイントのみ表示（APIキーを含むURLは表示しない）
    if err.status_code is None:
        lines = [f"エラー: API へ接続できませんでした: {err.endpoint}"]
        if err.detail:
            lines.append(f"  詳細: {err.detail}")
        lines.append("  → space_host / base_path / ネットワーク接続を確認してください。")
        return "\n".join(lines)

    lines = [f"エラー: API呼び出しに失敗しました（HTTP {err.status_code}）: {err.endpoint}"]
    if err.detail:
        lines.append(f"  詳細: {err.detail}")
    elif err.raw_body:
        # detailが取れない場合はボディをそのまま表示（デバッグ用）
        lines.append(f"  レスポンス: {err.raw_body[:500]}")

    if err.status_code == 400:
        lines.append("  → リクエストパラメータを確認してください。")
        lines.append("    フィルターの field_name / field_id や values の値が正しいか確認してください。")
    elif err.status_code == 401:
        lines.append("  → api_key を確認してください。")
    elif err.status_code == 403:
        lines.append("  → api_key の権限を確認してください。")
    elif err.status_code == 404:
        lines.append("  → space_host または project_key を確認してください。")
    elif err.status_code in RETRYABLE_STATUS:
        lines.append(f"  → リトライ({API_MAX_RETRIES}回)しても回復しませんでした。時間をおいて再実行してください。")
    return "\n".join(lines)


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

        # 実行中のコメントキャッシュ（フィルター間で課題が重複しても取得は1回）
        self._comment_cache: dict[int, list] = {}
        self._comment_lock = threading.Lock()
        # ステータス一覧のキャッシュ（同一プロジェクトのフィルターが複数あっても取得は1回）
        self._status_cache: dict[str | int, list] = {}
        # コメント取得に失敗した課題ID（集計後に警告表示する）
        self.comment_failures: set[int] = set()

    # ---------------- 低レベル HTTP ----------------

    def _build_url(self, endpoint: str, params: dict) -> tuple[str, list[str]]:
        """URL とデバッグ表示用のクエリ部品リストを組み立てる"""
        params = dict(params or {})
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

        url = f"{self.base_url}{endpoint}?" + "&".join(query_parts)
        # APIキーを除いた部品（デバッグ表示用）
        return url, [p for p in query_parts if not p.startswith("apiKey=")]

    @staticmethod
    def _http_error_to_api_error(e: urllib.error.HTTPError, endpoint: str) -> BacklogAPIError:
        """HTTPError からレスポンスボディの詳細を取り出して BacklogAPIError に変換する"""
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
        return BacklogAPIError(endpoint, status_code=e.code, detail=detail, raw_body=raw_body)

    def _sleep_before_retry(self, attempt: int, retry_after: str | None) -> None:
        """
        指数バックオフ（Retry-After ヘッダがあれば優先）。

        並列でコメントを取得しているため、複数のワーカーが同時にレート制限に
        掛かると全員が同じ秒数だけ待って同時に再送し、また衝突する。
        これを避けるため待ち時間にばらつきを加える。
        サーバーの指示を下回らないよう、上乗せのみで短くはしない。
        """
        base = 2 ** attempt  # 1, 2, 4 秒
        if retry_after:
            try:
                base = max(base, min(float(retry_after), 60.0))
            except ValueError:
                pass
        delay = min(base + random.uniform(0, base / 2), RETRY_MAX_DELAY)
        if self.debug:
            print(f"  [DEBUG] {delay:.1f}秒待機してリトライします（{attempt + 1}/{API_MAX_RETRIES}）",
                  file=sys.stderr)
        time.sleep(delay)

    def _get(self, endpoint: str, params: dict | None = None) -> dict | list:
        """
        GETリクエストを送信してJSONを返す。

        429 / 5xx / 接続エラーは指数バックオフで最大 API_MAX_RETRIES 回リトライする。
        最終的に失敗した場合は BacklogAPIError を送出する（プロセスは終了しない）。
        """
        url, debug_parts = self._build_url(endpoint, params)

        if self.debug:
            print(f"  [DEBUG] {endpoint} ?" + "&".join(debug_parts), file=sys.stderr)

        for attempt in range(API_MAX_RETRIES + 1):
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=API_TIMEOUT, context=self.ssl_context) as res:
                    return json.loads(res.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                err = self._http_error_to_api_error(e, endpoint)
                if err.status_code in RETRYABLE_STATUS and attempt < API_MAX_RETRIES:
                    self._sleep_before_retry(attempt, retry_after)
                    continue
                raise err from None
            except (urllib.error.URLError, TimeoutError) as e:
                reason = getattr(e, "reason", e)
                if attempt < API_MAX_RETRIES:
                    self._sleep_before_retry(attempt, None)
                    continue
                raise BacklogAPIError(endpoint, status_code=None, detail=str(reason)) from None

        # ここには到達しない（ループ内で return または raise される）
        raise BacklogAPIError(endpoint, status_code=None, detail="リトライ上限に達しました")

    # ---------------- エンドポイント ----------------

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
        """プロジェクトのステータス一覧を取得（実行中はキャッシュする）"""
        if project_id_or_key not in self._status_cache:
            self._status_cache[project_id_or_key] = self._get(
                f"/projects/{project_id_or_key}/statuses"
            )
        return self._status_cache[project_id_or_key]

    def get_issues(self, project_id: int, params: dict | None = None) -> list:
        """
        課題一覧を全件取得（ページネーション対応）
        Backlog APIは1回最大100件のため、自動的に繰り返し取得します。
        """
        all_issues = []
        offset = 0

        # 呼び出し元の dict を書き換えないようコピーする
        base_params = dict(params or {})
        base_params["projectId"] = [project_id]
        base_params["count"] = API_PAGE_SIZE

        while True:
            base_params["offset"] = offset
            issues = self._get("/issues", base_params)
            if not issues:
                break
            all_issues.extend(issues)
            if len(issues) < API_PAGE_SIZE:
                break
            offset += API_PAGE_SIZE

        return all_issues

    def get_issue_comments(self, issue_id: int) -> list:
        """
        課題のコメントを全件取得（ページネーション対応）。
        コメントの changeLog にステータス変化履歴が含まれる。

        取得に失敗した場合は comment_failures に課題IDを記録し、
        その時点までに取得できた分を返す（1課題の失敗で全体を止めない）。
        """
        with self._comment_lock:
            cached = self._comment_cache.get(issue_id)
        if cached is not None:
            return cached

        all_comments: list = []
        min_id = None
        failed = False

        while True:
            params: dict = {"count": API_PAGE_SIZE, "order": "asc"}
            if min_id is not None:
                params["minId"] = min_id
            try:
                comments = self._get(f"/issues/{issue_id}/comments", params)
            except BacklogAPIError as e:
                failed = True
                if self.debug:
                    print(f"  [DEBUG] get_issue_comments({issue_id}) 失敗: {e}", file=sys.stderr)
                break
            if not comments:
                break
            all_comments.extend(comments)
            if len(comments) < API_PAGE_SIZE:
                break
            min_id = max(c["id"] for c in comments) + 1

        if failed:
            with self._comment_lock:
                self.comment_failures.add(issue_id)
            # 不完全な結果はキャッシュしない
            return all_comments

        with self._comment_lock:
            self._comment_cache[issue_id] = all_comments
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

        def resolve_value(v, _items_map=items_map):
            """選択肢名 → 数値ID に変換（items_mapにあれば）"""
            if isinstance(v, str) and v in _items_map:
                return _items_map[v]
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

def _is_closed(status_name: str, closed_status_names: set) -> bool:
    return status_name in closed_status_names


def _is_open(status_name: str, closed_status_names: set) -> bool:
    """完了系に登録されていないステータスは、すべてオープン系として扱う"""
    return bool(status_name) and status_name not in closed_status_names


def classify_issue_from_comments(
    issue: dict,
    comments: list,
    period_start: date,
    period_end: date,
    closed_status_names: set,
) -> dict:
    """
    課題のコメント履歴（changeLog）を基に、対象期間における①〜⑤の分類を返す。

    各カテゴリは独立して判定され、現在のステータスに依存しない。
    期間開始時点のステータスはコメント履歴から正確に導出する。
    日付の比較はすべて JST に変換して行う。

    Returns:
        is_carry_over   : ① 期間前作成かつ期間開始時オープン
        is_new          : ② 期間中作成
        is_reopened     : ③ 期間開始時は完了系かつ期間中にオープン系へ変化
        is_completed    : ④ 期間中にオープン系から完了系へ変化
        status_at_start : 期間開始時点のステータス名
        status_at_end   : 期間終了時点のステータス名
        seen_statuses   : この課題で観測されたステータス名の集合（設定漏れ検出用）
    """
    ws = period_start.strftime("%Y-%m-%d")
    we = period_end.strftime("%Y-%m-%d")
    created = to_local_date(issue.get("created", ""))

    # コメントの changeLog からステータス変化を抽出（コメントは昇順で渡される前提）
    changes_before: list = []  # 期間前のステータス変化
    changes_in: list = []      # 期間中のステータス変化
    changes_after: list = []   # 期間後のステータス変化
    seen_statuses: set = set()

    for comment in comments:
        comment_date = to_local_date(comment.get("created", ""))
        for cl in comment.get("changeLog", []):
            if cl.get("field") != "status":
                continue
            entry = {
                "date": comment_date,
                "from": cl.get("originalValue", ""),
                "to":   cl.get("newValue", ""),
            }
            seen_statuses.update(v for v in (entry["from"], entry["to"]) if v)
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
        if status_at_start:
            seen_statuses.add(status_at_start)

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
    was_open_at_start   = is_pre_period and _is_open(status_at_start, closed_status_names)
    was_closed_at_start = is_pre_period and _is_closed(status_at_start, closed_status_names)

    # 期間中の変化
    # completed_during: オープン系 → 完了系 の変化があり、かつ期間終了時点も完了系。
    # 期間中に完了して同じ期間中に再オープンされた課題は、期末時点で未完了なので④に含めない。
    # （完了系 → 完了系 の変化、例: 処理済み → 完了 も④には含めない）
    completed_during = (
        any(
            _is_open(c["from"], closed_status_names) and _is_closed(c["to"], closed_status_names)
            for c in changes_in
        )
        and _is_closed(status_at_end, closed_status_names)
    )
    reopened_during  = any(
        _is_closed(c["from"], closed_status_names) and _is_open(c["to"], closed_status_names)
        for c in changes_in
    )

    return {
        "is_carry_over":   was_open_at_start,                        # ①
        "is_new":          is_new,                                    # ②
        "is_reopened":     was_closed_at_start and reopened_during,  # ③
        "is_completed":    completed_during,                          # ④
        "status_at_start": status_at_start,
        "status_at_end":   status_at_end,
        "seen_statuses":   seen_statuses,
    }


def _with_status(issue: dict, status_name: str) -> dict:
    """表示ステータスを差し替えた課題のコピーを返す（元の課題は変更しない）"""
    issue_copy = {**issue}
    issue_copy["status"] = {**issue_copy.get("status", {}), "name": status_name}
    return issue_copy


def _fetch_comments_bulk(
    client: BacklogClient,
    issues: list,
    period_start: date,
    max_workers: int,
) -> dict:
    """
    分類に必要な課題のコメントだけを並列取得して {issue_id: comments} を返す。

    期間開始より前から更新されていない課題は、期間中も期間後もステータスが
    変化していないことが確定するため、コメントを取得しない（空リスト扱い）。
    空リストを classify_issue_from_comments に渡した結果は
    「開始時＝終了時＝現在のステータス、完了・再オープンなし」となり、
    実際の履歴から導出した結果と一致する。
    """
    ws = period_start.strftime("%Y-%m-%d")

    targets = []
    for issue in issues:
        updated = to_local_date(issue.get("updated", "")) or to_local_date(issue.get("created", ""))
        if updated and updated < ws:
            continue  # 期間開始以降の更新なし → コメント取得不要
        targets.append(issue.get("id"))

    if client.debug:
        print(f"  [DEBUG] コメント取得対象: {len(targets)}件 / 全{len(issues)}件 "
              f"（{len(issues) - len(targets)}件はスキップ）", file=sys.stderr)

    comments_map: dict = {}
    if not targets:
        return comments_map

    workers = max(1, min(max_workers, len(targets)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(client.get_issue_comments, iid): iid for iid in targets}
        for future in as_completed(futures):
            comments_map[futures[future]] = future.result()

    return comments_map


def validate_status_config(statuses: list, closed_status_ids: list, project_key: str) -> None:
    """
    closed_status_ids の妥当性を検証する。

    課題は必ずプロジェクトの先頭ステータス（既定では「未対応」）で作成される。
    これを完了系に登録すると、すべての課題が「作成時点で完了していた」ことになり、
    ①〜⑤ の集計が破綻する。設定ミスなので実行前に止める。
    """
    if not statuses:
        return
    initial = statuses[0]
    if initial["id"] in closed_status_ids:
        print(
            f"エラー: config.yaml の closed_status_ids に「{initial['name']}」"
            f"（id={initial['id']}）が含まれています。\n"
            f"  これはプロジェクト「{project_key}」で課題が新規作成されるときのステータスです。\n"
            "  完了系に登録すると、すべての課題が作成時点で完了していた扱いになり集計が成り立ちません。\n"
            "  closed_status_ids から取り除いてください。",
            file=sys.stderr,
        )
        sys.exit(1)


def _fetch_target_issues(
    client: BacklogClient,
    project_id: int,
    period_start: date,
    period_end: date,
    extra_params: dict,
    statuses: list,
    closed_status_ids: list,
) -> list:
    """
    集計に寄与しうる課題だけを取得する。

    「現在のステータスが完了系」かつ「期間開始以降まったく更新されていない」課題は、
    期間中も期間後もステータスが動いていないため、①〜⑤のいずれにも入らない。
    よって次の和集合だけを取得すれば足りる。

      A. 現在のステータスが完了系ではない課題（オープン系＋設定外のステータス）
      B. 期間開始以降に更新された課題

    A に設定外のステータスも含めるのは、集計漏れの警告（unknown_statuses）を
    従来どおり出せるようにするため。

    ステータス一覧が取得できなかった場合は絞り込みを諦めて全件取得する。
    """
    we = period_end.strftime("%Y-%m-%d")
    base = {**extra_params, "createdUntil": we}

    if not statuses:
        return client.get_issues(project_id, base)

    non_closed_ids = [s["id"] for s in statuses if s["id"] not in closed_status_ids]
    if not non_closed_ids:
        # 全ステータスが完了系という設定。B だけで足りる。
        non_closed_ids = None

    # updatedSince はサーバー側のタイムゾーンで解釈されるため、
    # 取りこぼさないよう1日ぶん余裕を持たせる（多めに取っても集計結果は変わらない）。
    since = (period_start - timedelta(days=1)).strftime("%Y-%m-%d")

    merged: dict = {}
    if non_closed_ids:
        for issue in client.get_issues(project_id, {**base, "statusId": non_closed_ids}):
            merged[issue.get("id")] = issue
    for issue in client.get_issues(project_id, {**base, "updatedSince": since}):
        merged[issue.get("id")] = issue

    if client.debug:
        print(f"  [DEBUG] 対象課題の絞り込み: 未完了系={non_closed_ids} / "
              f"updatedSince={since} → {len(merged)}件", file=sys.stderr)

    return list(merged.values())


def collect_report_data(
    client: BacklogClient,
    project_key: str,
    project_id: int,
    period_start: date,
    period_end: date,
    closed_status_ids: list,
    extra_params: dict | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> dict:
    """
    週次レポートに必要なデータを集計する。

    各課題のコメント履歴（changeLog）を基にステータス変化を判定し、
    現在のステータスに依存しない過去期間の正確な集計を実現する。
    フィルター項目（extra_params）は最新の課題属性を使用する。

    処理フロー:
      1. 集計に寄与しうる課題だけを取得（_fetch_target_issues 参照）
      2. 期間中に更新された課題のコメントを並列取得
      3. classify_issue_from_comments で①〜⑤を独立判定
      4. ⑤当週未完了 = (①+②+③) - ④ で計算
    """
    ep = extra_params or {}

    # ステータス名の取得（changeLog の値との照合に使用）
    # 完了系に登録されていないステータスは、すべてオープン系として扱う。
    statuses: list = []
    try:
        statuses = client.get_statuses(project_key)
        validate_status_config(statuses, closed_status_ids, project_key)
        closed_status_names = {s["name"] for s in statuses if s["id"] in closed_status_ids}
        if client.debug:
            others = {s["name"] for s in statuses} - closed_status_names
            print(f"  [DEBUG] 完了ステータス名: {closed_status_names}", file=sys.stderr)
            print(f"  [DEBUG] オープンステータス名（完了系以外すべて）: {others}", file=sys.stderr)
    except BacklogAPIError as e:
        print(f"  ⚠ ステータス一覧の取得に失敗しました（{project_key}）: "
              "すべてオープン系として集計されます", file=sys.stderr)
        print(format_api_error(e), file=sys.stderr)
        closed_status_names = set()

    project_status_names = {s["name"] for s in statuses}

    # ---- 集計対象の課題を取得 ----
    all_issues = _fetch_target_issues(
        client, project_id, period_start, period_end, ep, statuses, closed_status_ids
    )
    if client.debug:
        print(f"  [DEBUG] 全対象課題数: {len(all_issues)}件", file=sys.stderr)

    all_issues_map = {i.get("id"): i for i in all_issues}

    # ---- コメントを並列取得（更新のない課題はスキップ） ----
    failures_before = set(client.comment_failures)
    comments_map = _fetch_comments_bulk(client, all_issues, period_start, max_workers)

    # ---- 各課題をコメント履歴から独立分類（①〜⑤） ----
    carry_over_issues: list = []
    new_issues:        list = []
    completed_issues:  list = []
    reopened_issues:   list = []
    status_at_end_map: dict = {}  # issue_id -> 期間終了時点のステータス名
    unknown_statuses:  set  = set()

    for issue in all_issues:
        issue_id_val = issue.get("id")
        comments = comments_map.get(issue_id_val, [])

        result = classify_issue_from_comments(
            issue, comments, period_start, period_end, closed_status_names,
        )

        # プロジェクトのステータス一覧に無い名前を収集（改名・削除された可能性がある）
        if project_status_names:
            unknown_statuses |= (result["seen_statuses"] - project_status_names)

        if client.debug:
            print(
                f"  [DEBUG] {issue.get('issueKey','?')}: "
                f"carry={result['is_carry_over']}, new={result['is_new']}, "
                f"completed={result['is_completed']}, reopened={result['is_reopened']}, "
                f"status_at_start={result['status_at_start']}",
                file=sys.stderr,
            )

        # ① 前週残件（完了済みなら表示ステータスを期間開始時点に差し替え）
        if result["is_carry_over"]:
            if result["is_completed"]:
                carry_over_issues.append(_with_status(issue, result["status_at_start"]))
            else:
                carry_over_issues.append(issue)

        # ②③④ は表示ステータスを期間終了時点に差し替え（現在のステータス混入を防ぐ）
        if result["is_new"]:
            new_issues.append(_with_status(issue, result["status_at_end"]))
        if result["is_reopened"]:
            reopened_issues.append(_with_status(issue, result["status_at_end"]))
        if result["is_completed"]:
            completed_issues.append(_with_status(issue, result["status_at_end"]))

        # 期間終了時点のステータスを記録（⑤の表示用）
        status_at_end_map[issue_id_val] = result["status_at_end"]

    # ---- ⑤ 当週未完了 = (① + ② + ③) - ④ ----
    completed_id_set = {i.get("id") for i in completed_issues}
    active_ids       = {i.get("id") for i in carry_over_issues + new_issues + reopened_issues}
    incomplete_ids   = active_ids - completed_id_set
    incomplete_issues = []
    for iid in incomplete_ids:
        if iid not in all_issues_map:
            continue
        base = all_issues_map[iid]
        if iid in status_at_end_map:
            incomplete_issues.append(_with_status(base, status_at_end_map[iid]))
        else:
            incomplete_issues.append({**base})

    new_failures = client.comment_failures - failures_before

    return {
        "carry_over": carry_over_issues,
        "new_issues": new_issues,
        "completed":  completed_issues,
        "incomplete": incomplete_issues,
        "reopened":   reopened_issues,
        "unknown_statuses":  unknown_statuses,
        "comment_failures":  new_failures,
    }


# ==============================================================
# Markdownレポート生成
# ==============================================================

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


def keys_str(issues: list) -> str:
    """課題番号のみのコンパクト表示"""
    keys = [i.get("issueKey", "?") for i in issues]
    if not keys:
        return "_（なし）_"
    return "、".join(keys[:KEYS_MAX_DISPLAY]) + (
        f" 他{len(keys) - KEYS_MAX_DISPLAY}件" if len(keys) > KEYS_MAX_DISPLAY else ""
    )


def generate_markdown_report(
    data: dict,
    project_key: str,
    project_name: str,
    period_start: date,
    period_end: date,
    filter_name: str = None,
    filter_description: str = None,
    filter_summary: str = None,
) -> str:
    """Markdownレポートを生成"""
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    ws_str = period_start.strftime("%Y/%m/%d")
    we_str = period_end.strftime("%Y/%m/%d")

    carry_over = data["carry_over"]
    new_issues = data["new_issues"]
    completed = data["completed"]
    incomplete = data["incomplete"]
    reopened = data.get("reopened", [])
    unknown_statuses = data.get("unknown_statuses") or set()
    comment_failures = data.get("comment_failures") or set()

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
        ]
        if unknown_statuses:
            lines.append(
                "> 次のステータス名が現在のプロジェクトのステータス一覧に存在しません"
                f"（改名または削除された可能性があります）: {'、'.join(sorted(unknown_statuses))}"
            )
        lines.append("")

    if unknown_statuses and lhs == rhs:
        lines += [
            "> ⚠️ **注意**: 次のステータス名が現在のプロジェクトのステータス一覧に存在しません"
            f"（改名または削除された可能性があります）: {'、'.join(sorted(unknown_statuses))}",
            "",
        ]

    if comment_failures:
        lines += [
            f"> ⚠️ **注意**: {len(comment_failures)} 件の課題でコメント履歴の取得に失敗しました。"
            "該当課題は「期間中にステータス変化なし」として集計されています。",
            "",
        ]

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
         f"{we_str} 時点でオープン（未対応・処理中）の課題", TABLE_MAX_DISPLAY_INCOMPLETE),
    ]
    for title, issues, description, max_display in sections:
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


def _issue_sort_key(issue: dict) -> tuple:
    """課題番号を (プロジェクトキー, 番号) のタプルで返す数値ソート用キー"""
    raw = issue.get("issueKey", "")
    parts = raw.rsplit("-", 1)
    if len(parts) == 2:
        prefix, num_str = parts
        try:
            return (prefix, int(num_str))
        except ValueError:
            return (prefix, 0)
    return (raw, 0)


def _fmt_due(due_raw: str | None) -> str:
    """期限日を m/d 形式に変換（例: '2026-04-07T...' → '4/7'）"""
    if not due_raw:
        return "なし"
    d = due_raw[:10]  # "YYYY-MM-DD"（日付のみのフィールドのため変換しない）
    m, day = int(d[5:7]), int(d[8:10])
    return f"{m}/{day}"


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
        carry_over = data["carry_over"]
        new_issues = data["new_issues"]
        reopened   = data["reopened"]
        completed  = data["completed"]
        incomplete = data["incomplete"]

        lines.append(filter_name)
        lines.append(
            f"残:{len(carry_over)} / 新規:{len(new_issues)} / "
            f"再オープン:{len(reopened)} / 完了:{len(completed)} / 未完了:{len(incomplete)}"
        )

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


# ==============================================================
# 設定・引数
# ==============================================================

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
    default_config = str(Path(__file__).parent / "config.yaml")
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


# ==============================================================
# プロジェクト情報キャッシュ
# ==============================================================

class ProjectInfoCache:
    """
    同一 project_key に対する API 呼び出しを1回に抑えるキャッシュ。

    保持する情報: {id, name, issue_type_map, custom_field_map, master_loaded}
    """

    def __init__(self, client: BacklogClient, debug: bool = False):
        self.client = client
        self.debug = debug
        self._cache: dict = {}

    def get(self, project_key: str, need_master: bool = False) -> dict:
        """プロジェクト情報をキャッシュ付きで取得する。"""
        if project_key not in self._cache:
            print(f"プロジェクト情報を取得中... ({project_key})")
            try:
                proj = self.client.get_project(project_key)
            except BacklogAPIError as e:
                print(f"エラー: プロジェクト情報の取得に失敗しました ({project_key})", file=sys.stderr)
                print(format_api_error(e), file=sys.stderr)
                sys.exit(1)
            self._cache[project_key] = {
                "id":               proj["id"],
                "name":             proj["name"],
                "issue_type_map":   {},
                "custom_field_map": {},
                "master_loaded":    False,
            }
            info = self._cache[project_key]
            print(f"プロジェクト名: {info['name']} (ID: {info['id']})")

        info = self._cache[project_key]
        if need_master and not info["master_loaded"]:
            self._load_master(project_key, info)
        return info

    def _load_master(self, project_key: str, info: dict) -> None:
        """種別・カスタム属性のマスターを取得して info に格納する"""
        print(f"種別・カスタム属性マスターを取得中... ({project_key})")
        try:
            issue_types = self.client.get_issue_types(project_key)
            info["issue_type_map"] = {it["name"]: it["id"] for it in issue_types}
            if self.debug:
                print(f"  [DEBUG] 種別マップ（名前→ID）: {info['issue_type_map']}", file=sys.stderr)
            else:
                print(f"  種別: {list(info['issue_type_map'].keys())}")
        except BacklogAPIError as e:
            print(f"  ⚠ 種別マスターの取得に失敗: {e}", file=sys.stderr)

        try:
            custom_fields = self.client.get_custom_fields(project_key)
            info["custom_field_map"] = {
                cf["name"]: {
                    "id":     cf["id"],
                    "typeId": cf.get("typeId"),
                    # リスト型（typeId 5/6/7/8）の選択肢を {名前: ID} で保持
                    "items":  {item["name"]: item["id"] for item in cf.get("items", [])},
                }
                for cf in custom_fields
            }
            if self.debug:
                for fname, finfo in info["custom_field_map"].items():
                    print(f"  [DEBUG] カスタム属性「{fname}」: id={finfo['id']}, "
                          f"typeId={finfo['typeId']}, items={finfo['items']}", file=sys.stderr)
            else:
                print(f"  カスタム属性: {list(info['custom_field_map'].keys())}")
        except BacklogAPIError as e:
            print(f"  ⚠ カスタム属性マスターの取得に失敗: {e}", file=sys.stderr)

        info["master_loaded"] = True


# ==============================================================
# メイン処理
# ==============================================================

def _print_summary(output_path: Path, data: dict) -> None:
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

    print("=" * 55)
    print("Backlog レポート生成")
    print("=" * 55)
    print(f"スペース    : {space_host}")
    print(f"プロジェクト : {project_key}（デフォルト）")
    print(f"対象期間    : {period_start} 〜 {period_end}（{period_label} / JST基準）")
    print(f"フィルター数 : {len(filters_cfg) if filters_cfg else 0}（0=フィルターなし）")
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

    # ---- フィルターなし（filters が空の場合）----
    if not filters_cfg:
        default_info = projects.get(project_key)
        print("【フィルターなし】全課題を集計中...")
        data = collect_report_data(
            client, project_key, default_info["id"], period_start, period_end,
            closed_status_ids, max_workers=max_workers,
        )
        report_md = generate_markdown_report(
            data, project_key, default_info["name"], period_start, period_end
        )
        output_path = output_dir / "weekly_report.md"
        output_path.write_text(report_md, encoding="utf-8")
        _print_summary(output_path, data)
        return

    # ---- フィルターごとに集計・出力 ----
    all_filter_data = []
    for i, filter_cfg in enumerate(filters_cfg, 1):
        filter_name = filter_cfg.get("name") or f"filter_{i}"
        filter_desc = filter_cfg.get("description") or ""

        # フィルター個別の project_key（未指定ならデフォルトを使用）
        filter_project_key = filter_cfg.get("project_key") or project_key

        # プロジェクト情報をキャッシュ付きで取得（初回のみ API 呼び出し）
        proj_info = projects.get(filter_project_key, need_master=True)
        filter_summary = build_filter_summary(filter_cfg)

        print(f"[{i}/{len(filters_cfg)}] フィルター「{filter_name}」を集計中...")
        if filter_project_key != project_key:
            print(f"         プロジェクト: {filter_project_key}")
        print(f"         条件: {filter_summary}")

        # フィルターパラメータを解決
        extra_params = resolve_filter_params(
            filter_cfg, proj_info["issue_type_map"], proj_info["custom_field_map"]
        )
        if args.debug:
            print(f"  [DEBUG] 解決済みフィルターパラメータ: {extra_params}", file=sys.stderr)

        data = collect_report_data(
            client, filter_project_key, proj_info["id"], period_start, period_end,
            closed_status_ids,
            extra_params=extra_params,
            max_workers=max_workers,
        )

        report_md = generate_markdown_report(
            data, filter_project_key, proj_info["name"], period_start, period_end,
            filter_name=filter_name,
            filter_description=filter_desc,
            filter_summary=filter_summary,
        )

        all_filter_data.append((filter_name, data))

        safe_name = safe_filename(filter_name)
        output_path = output_dir / f"weekly_report_{safe_name}.md"
        output_path.write_text(report_md, encoding="utf-8")
        _print_summary(output_path, data)
        print()

    # ---- サマリーレポート出力 ----
    if all_filter_data:
        summary_md = generate_summary_report(all_filter_data, period_start, period_end)
        summary_path = output_dir / "summary_report.md"
        summary_path.write_text(summary_md, encoding="utf-8")
        print(f"  ✅ サマリー保存: {summary_path}")


def main():
    try:
        run()
    except BacklogAPIError as e:
        print(format_api_error(e), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
