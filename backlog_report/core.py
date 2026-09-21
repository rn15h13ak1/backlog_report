"""定数・型・共通の小物。ほかのモジュールはここだけに依存する。"""
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import TypedDict

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


# ①〜⑤ の並び（集計結果のキーと、件数表示に使うラベル）
CATEGORY_KEYS = ("carry_over", "new_issues", "reopened", "completed", "incomplete")


CATEGORY_LABELS = ("残", "新規", "再オープン", "完了", "未完了")


# レポート表示上限
TABLE_MAX_DISPLAY = 30


TABLE_MAX_DISPLAY_INCOMPLETE = 50


KEYS_MAX_DISPLAY = 20


class _ReportDataRequired(TypedDict):
    """集計結果のうち、必ず入るもの。`collect_report_data` が組み立てる。"""

    carry_over: list        # ① 前週残件
    new_issues: list        # ② 新規発生
    reopened:   list        # ③ 再オープン
    completed:  list        # ④ 当週完了
    incomplete: list        # ⑤ 当週未完了
    unknown_statuses: set   # プロジェクトのステータス一覧に無い名前
    comment_failures: set   # コメント履歴を取得できなかった課題 ID
    population_ids:   set   # 今回の母集団（抽出対象への出入りの判定に使う）
    inflow:  list           # 期間中に抽出対象へ入った課題（② に含める）
    outflow: list           # 期間中に抽出対象から外れた課題（① と ④ に含める）


class ReportData(_ReportDataRequired, total=False):
    """
    集計結果。

    `flow_unavailable` は、抽出対象への出入りを判定できなかったときだけ入る
    （初回実行、週を飛ばした、絞り込み条件を書き換えたなど）。必須と任意を
    分けてあるのは、読む側が `data["..."]` と `data.get("...")` のどちらを
    使うべきかを宣言から読み取れるようにするため（型検査では強制されない）。

    Python 3.10 を対象にしているため、キーごとの NotRequired ではなく
    total=False の継承で表す。
    """

    flow_unavailable: str


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


def _is_closed(status_name: str, closed_status_names: set) -> bool:
    return status_name in closed_status_names


def _is_open(status_name: str, closed_status_names: set) -> bool:
    """完了系に登録されていないステータスは、すべてオープン系として扱う"""
    return bool(status_name) and status_name not in closed_status_names


def _with_status(issue: dict, status_name: str) -> dict:
    """表示ステータスを差し替えた課題のコピーを返す（元の課題は変更しない）"""
    issue_copy = {**issue}
    issue_copy["status"] = {**issue_copy.get("status", {}), "name": status_name}
    return issue_copy


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
