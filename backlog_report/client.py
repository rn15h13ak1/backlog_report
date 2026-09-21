"""Backlog API クライアント。通信・リトライ・取得結果のキャッシュ。"""
import json
import random
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from backlog_report.core import (
    API_MAX_RETRIES,
    API_PAGE_SIZE,
    API_TIMEOUT,
    RETRY_MAX_DELAY,
    RETRYABLE_STATUS,
)


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
        # 課題ページの URL（レポートのリンクに使う）。API ではなく画面側のパス。
        self.web_url = f"https://{space_host}{base_path}"
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

    def _get(self, endpoint: str, params: dict | None = None) -> Any:
        """
        GETリクエストを送信してJSONを返す。

        429 / 5xx / 接続エラーは指数バックオフで最大 API_MAX_RETRIES 回リトライする。
        最終的に失敗した場合は BacklogAPIError を送出する（プロセスは終了しない）。
        """
        url, debug_parts = self._build_url(endpoint, params or {})

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
        all_issues: list = []
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
