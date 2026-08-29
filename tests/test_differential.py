"""
旧実装（cfcd9ff）との差分テスト。

実 API に接続できない環境で回帰を検出するための代替手段。
合成データを網羅的に生成し、新旧の実装を突き合わせて次の2点を機械的に確認する。

  1. JST 補正以外にロジックの変化がないこと
     新実装の結果 == 旧実装に「JST に補正済みの入力」を与えた結果

  2. コメント取得スキップ最適化が集計結果を変えていないこと
     タイムゾーン変換が無関係になる時刻（UTC 15時未満）だけで構成した
     データセットでは、新旧の集計結果が完全一致すること

比較対象の旧実装は tests/_legacy.py に凍結してある。

合成データは Backlog の実データが満たす不変条件に従って生成する:
  - ステータス変化は連鎖する（各変化の from は直前の変化の to と一致）
  - 現在のステータスは最後の変化の to と一致する
  - updated は「作成日と最終変化日の遅い方」以上である
最後の条件はコメント取得スキップ最適化が依拠する前提そのものであり、
これを破るデータでは新旧が食い違う（test_skip_optimization_precondition 参照）。
"""
import itertools
from datetime import date

import backlog_weekly_report as bwr
from tests import _legacy

PERIOD_START = date(2026, 3, 2)
PERIOD_END = date(2026, 3, 8)

STATUSES = [
    {"id": 1, "name": "未対応"},
    {"id": 2, "name": "処理中"},
    {"id": 3, "name": "処理済み"},
    {"id": 4, "name": "完了"},
]
OPEN_NAMES = {"未対応", "処理中"}
CLOSED_NAMES = {"処理済み", "完了"}
ALL_NAMES = ["未対応", "処理中", "処理済み", "完了"]

CLASSIFY_KEYS = ("is_carry_over", "is_new", "is_reopened", "is_completed",
                 "status_at_start", "status_at_end")

# 旧実装はオープン系のIDを明示的に受け取っていた
LEGACY_OPEN_STATUS_IDS = [1, 2]


def is_intended_completion_change(new_result: dict, old_result: dict) -> bool:
    """
    ④の定義変更による意図的な差かどうかを判定する。

    旧: 期間中にオープン系→完了系の変更があれば④
    新: 加えて期間終了時点も完了系であること（期間中に完了して同じ期間中に
        再オープンされた課題は、期末が未完了なので④に含めない）

    したがって差は「旧が④、新が④でない、かつ期末がオープン系」のときだけ許される。
    """
    diff = {k for k in CLASSIFY_KEYS if new_result[k] != old_result[k]}
    if diff != {"is_completed"}:
        return False
    return (old_result["is_completed"] and not new_result["is_completed"]
            and new_result["status_at_end"] not in CLOSED_NAMES)

# 期間: 2026-03-02 〜 2026-03-08。境界日を意図的に含める。
D_OLD = "2026-01-15"        # ずっと前
D_PREV = "2026-03-01"       # 期間開始の前日（JST 補正で期間内に入りうる）
D_START = "2026-03-02"      # 期間開始日
D_MID = "2026-03-05"        # 期間中
D_END = "2026-03-08"        # 期間終了日（JST 補正で期間外に出うる）
D_NEXT = "2026-03-09"       # 期間終了の翌日
D_LATER = "2026-03-20"      # ずっと後

CREATED_DAYS = [D_OLD, D_PREV, D_START, D_MID, D_END, D_NEXT]

# ステータス変化のシナリオ: (日付, 遷移後ステータス) の列。
# from は直前の状態から自動的に連鎖させる。
STEP_SCENARIOS = [
    [],
    [(D_OLD, "処理中")],
    [(D_PREV, "完了")],
    [(D_START, "完了")],
    [(D_MID, "完了")],
    [(D_MID, "処理済み")],
    [(D_END, "完了")],
    [(D_NEXT, "完了")],
    [(D_LATER, "完了")],
    [(D_MID, "処理済み"), (D_END, "完了")],            # 完了系→完了系
    [(D_OLD, "完了"), (D_MID, "処理中")],              # 期間中に再オープン
    [(D_MID, "完了"), (D_END, "未対応")],              # 完了後に再オープン
    [(D_OLD, "完了"), (D_MID, "処理中"), (D_END, "完了")],   # 再オープン後に再完了
    [(D_PREV, "処理中"), (D_LATER, "完了")],           # 期間前と期間後のみ
    [(D_START, "処理中"), (D_END, "完了")],            # 境界日で開始・終了
]

HOURS = ["02", "14", "15", "23"]   # UTC 15時以降は JST では翌日になる


# ==================================================================
# 合成データ生成
# ==================================================================

def at(day: str, hour: str) -> str:
    return f"{day}T{hour}:00:00Z"


def to_jst_iso(iso: str) -> str:
    """
    旧実装に食わせるための JST 補正済み入力を作る。
    旧実装は先頭10文字しか見ないため、日付部分だけ JST に直せば等価。
    """
    return bwr.to_local_date(iso) + "T00:00:00Z"


def build_chain(initial: str, steps: list) -> tuple[list, str]:
    """(日付, 遷移後) の列から (日付, from, to) の連鎖と最終ステータスを作る"""
    current = initial
    changes = []
    for day, to_status in steps:
        changes.append((day, current, to_status))
        current = to_status
    return changes, current


def scenarios(hour: str):
    """
    不変条件を満たす (issue, comments) を網羅的に生成する。
    作成日より前の変化を持つ組み合わせは非現実的なので除外する。
    """
    issue_id = 0
    for created_day, initial, steps in itertools.product(
        CREATED_DAYS, ALL_NAMES, STEP_SCENARIOS
    ):
        if any(day < created_day for day, _ in steps):
            continue
        changes, final_status = build_chain(initial, steps)
        issue_id += 1
        last_day = max([created_day] + [day for day, _, _ in changes])
        issue = {
            "id": issue_id,
            "issueKey": f"PRJ-{issue_id}",
            "created": at(created_day, hour),
            "updated": at(last_day, hour),
            "status": {"name": final_status},
            "summary": f"課題{issue_id}",
            "assignee": None,
            "dueDate": None,
        }
        comments = [
            {
                "id": idx + 1,
                "created": at(day, hour),
                "changeLog": [{"field": "status", "originalValue": src, "newValue": dst}],
            }
            for idx, (day, src, dst) in enumerate(changes)
        ]
        yield issue, comments


def shift_issue(issue: dict) -> dict:
    shifted = {**issue, "created": to_jst_iso(issue["created"])}
    if "updated" in issue:
        shifted["updated"] = to_jst_iso(issue["updated"])
    return shifted


def shift_comments(comments: list) -> list:
    return [{**c, "created": to_jst_iso(c["created"])} for c in comments]


def new_classify(issue, comments):
    return bwr.classify_issue_from_comments(
        issue, comments, PERIOD_START, PERIOD_END, CLOSED_NAMES
    )


def old_classify(issue, comments):
    return _legacy.classify_issue_from_comments(
        issue, comments, PERIOD_START, PERIOD_END, CLOSED_NAMES, OPEN_NAMES
    )


def describe(issue, comments) -> str:
    changes = [(c["created"][:10], c["changeLog"][0]["originalValue"],
                c["changeLog"][0]["newValue"]) for c in comments]
    return (f"{issue['issueKey']} created={issue['created']} "
            f"updated={issue['updated']} status={issue['status']['name']} changes={changes}")


# ==================================================================
# 1. 分類ロジック: JST 補正以外に差がないこと
# ==================================================================

def test_classification_differs_only_by_timezone():
    """新実装 == 旧実装（入力を JST に補正したもの）を全シナリオで確認する"""
    mismatches = []
    total = 0
    intended = 0
    for hour in HOURS:
        for issue, comments in scenarios(hour):
            total += 1
            new_result = new_classify(issue, comments)
            old_result = old_classify(shift_issue(issue), shift_comments(comments))
            if is_intended_completion_change(new_result, old_result):
                intended += 1
                continue
            diff = {k: (new_result[k], old_result[k])
                    for k in CLASSIFY_KEYS if new_result[k] != old_result[k]}
            if diff:
                mismatches.append((describe(issue, comments), diff))

    # ジェネレータが黙って空を返す退行の検出用（実測 832 件）
    assert total > 500, f"生成ケースが少なすぎる: {total}"
    assert not mismatches, (
        f"{len(mismatches)}/{total} 件で新旧が不一致:\n"
        + "\n".join(f"  {d}\n    {diff}" for d, diff in mismatches[:5])
    )
    assert intended > 0, "④の定義変更による差が1件も無い（データが不十分）"


def test_corpus_actually_exercises_the_timezone_boundary():
    """
    上のテストが素通りしていないことの確認。
    UTC 15時以降のケースでは、補正しない旧実装と新実装が実際に食い違う組み合わせが
    存在しなければならない（存在しなければ境界日を踏めていない）。
    """
    divergences = 0
    for hour in ("15", "23"):
        for issue, comments in scenarios(hour):
            new_result = new_classify(issue, comments)
            old_result = old_classify(issue, comments)   # 補正せずそのまま渡す
            if any(new_result[k] != old_result[k] for k in CLASSIFY_KEYS):
                divergences += 1
    assert divergences > 0, "JST 境界を踏むケースが1件も無い（テストデータが不十分）"


def test_no_divergence_when_timezone_conversion_is_a_noop():
    """UTC 15時未満なら JST 変換は日付を動かさないので、補正なしでも新旧一致する"""
    for hour in ("02", "14"):
        for issue, comments in scenarios(hour):
            new_result = new_classify(issue, comments)
            old_result = old_classify(issue, comments)
            if is_intended_completion_change(new_result, old_result):
                continue
            for key in CLASSIFY_KEYS:
                assert new_result[key] == old_result[key], describe(issue, comments)


# ==================================================================
# 2. 集計全体: 取得の絞り込みが結果を変えないこと
# ==================================================================

STATUS_ID_BY_NAME = {s["name"]: s["id"] for s in STATUSES}
STATUS_ID_BY_NAME["レビュー中"] = 5   # config のどちらにも属さないステータスのテスト用


class RecordingClient(bwr.BacklogClient):
    """
    合成データを返す疑似クライアント。

    get_issues は Backlog API と同じように statusId / updatedSince /
    createdUntil を実際に適用する。これにより、新実装が投げる絞り込み
    クエリと旧実装が投げる全件クエリの結果を突き合わせられる。
    """

    def __init__(self, issues: list, comments_by_id: dict, statuses: list | None = None):
        super().__init__("example.test", "key")
        self.issues = issues
        self.comments_by_id = comments_by_id
        self.statuses = STATUSES if statuses is None else statuses
        self.fetch_count = 0
        self.issued_queries: list = []

    def get_statuses(self, project_id_or_key):
        return self.statuses

    def get_issues(self, project_id, params=None):
        params = params or {}
        self.issued_queries.append(params)

        status_ids = params.get("statusId")
        updated_since = params.get("updatedSince")
        created_until = params.get("createdUntil")

        result = []
        for issue in self.issues:
            if status_ids is not None:
                if STATUS_ID_BY_NAME[issue["status"]["name"]] not in status_ids:
                    continue
            if updated_since is not None:
                if bwr.to_local_date(issue["updated"]) < updated_since:
                    continue
            if created_until is not None:
                if bwr.to_local_date(issue["created"]) > created_until:
                    continue
            result.append(issue)
        return result

    def get_issue_comments(self, issue_id):
        self.fetch_count += 1
        return self.comments_by_id.get(issue_id, [])


def build_corpus(hour: str) -> tuple[list, dict]:
    issues, comments_by_id = [], {}
    for issue, comments in scenarios(hour):
        issues.append(issue)
        comments_by_id[issue["id"]] = comments
    return issues, comments_by_id


def categorize(data: dict) -> dict:
    """比較用に {カテゴリ: [(課題番号, 表示ステータス)]} へ正規化"""
    return {
        key: sorted((i["issueKey"], i["status"]["name"]) for i in data[key])
        for key in ("carry_over", "new_issues", "reopened", "completed", "incomplete")
    }


def run_new(issues, comments_by_id):
    client = RecordingClient(issues, comments_by_id)
    data = bwr.collect_report_data(
        client, "PRJ", 1, PERIOD_START, PERIOD_END,
        bwr.DEFAULT_CLOSED_STATUS_IDS, max_workers=4,
    )
    return data, client


def run_old(issues, comments_by_id):
    client = RecordingClient(issues, comments_by_id)
    data = _legacy.collect_report_data(
        client, "PRJ", 1, PERIOD_START, PERIOD_END,
        LEGACY_OPEN_STATUS_IDS, bwr.DEFAULT_CLOSED_STATUS_IDS,
    )
    return data, client


def moved_from_completed_to_incomplete(issues, comments_by_id) -> set:
    """④の定義変更により ④→⑤ へ移る課題番号（期間中に完了して同じ期間中に再オープン）"""
    moved = set()
    for issue in issues:
        comments = comments_by_id.get(issue["id"], [])
        new_result = new_classify(issue, comments)
        old_result = old_classify(issue, comments)
        if is_intended_completion_change(new_result, old_result):
            moved.add(issue["issueKey"])
    return moved


def assert_equivalent_except_completion_rule(new_data, old_data, moved):
    """
    ①②③の顔ぶれは完全一致し、④⑤の差は④の定義変更ぶんだけであること。

    ①の表示ステータスは比較しない。①は「④にも入る場合のみ期間開始時点、
    それ以外は現在の値」という実装のため、④の判定が変わると表示も連動して変わる。
    表示内容そのものは tests/test_report_format.py のゴールデン比較で担保している。
    """
    new_c, old_c = categorize(new_data), categorize(old_data)
    for key in ("carry_over", "new_issues", "reopened"):
        assert {k for k, _ in new_c[key]} == {k for k, _ in old_c[key]}, key
    assert {k for k, _ in new_c["completed"]} == {k for k, _ in old_c["completed"]} - moved
    assert {k for k, _ in new_c["incomplete"]} == {k for k, _ in old_c["incomplete"]} | moved


def test_aggregation_identical_when_timezone_is_irrelevant():
    """
    UTC 02:00 のデータのみ（JST に直しても日付が変わらない）で新旧を比較する。
    タイムゾーン補正の影響が消えるため、④の定義変更ぶん以外に差が出れば退行。
    """
    issues, comments_by_id = build_corpus("02")
    new_data, _ = run_new(issues, comments_by_id)
    old_data, _ = run_old(issues, comments_by_id)
    moved = moved_from_completed_to_incomplete(issues, comments_by_id)
    assert moved, "④の定義変更で移る課題が1件も無い（データが不十分）"
    assert_equivalent_except_completion_rule(new_data, old_data, moved)


def test_aggregation_identical_with_shifted_input_at_boundary():
    """UTC 23:00 のデータでも、旧実装に JST 補正済み入力を与えれば一致する"""
    issues, comments_by_id = build_corpus("23")
    new_data, _ = run_new(issues, comments_by_id)
    shifted_issues = [shift_issue(i) for i in issues]
    shifted_comments = {k: shift_comments(v) for k, v in comments_by_id.items()}
    old_data, _ = run_old(shifted_issues, shifted_comments)
    moved = moved_from_completed_to_incomplete(issues, comments_by_id)
    assert_equivalent_except_completion_rule(new_data, old_data, moved)


def test_optimization_actually_skips_fetches():
    """スキップ最適化が実際に効いていること（効いていなければ比較の意味がない）"""
    issues, comments_by_id = build_corpus("02")
    _, new_client = run_new(issues, comments_by_id)
    _, old_client = run_old(issues, comments_by_id)
    assert new_client.fetch_count < old_client.fetch_count


# ------------------------------------------------------------------
# 課題取得の絞り込み
# ------------------------------------------------------------------

def test_narrowed_fetch_returns_fewer_issues_than_full_fetch():
    """絞り込みによって取得件数が実際に減っていること"""
    issues, comments_by_id = build_corpus("02")
    new_client = RecordingClient(issues, comments_by_id)
    old_client = RecordingClient(issues, comments_by_id)

    narrowed = bwr._fetch_target_issues(
        new_client, 1, PERIOD_START, PERIOD_END, {}, STATUSES,
        bwr.DEFAULT_CLOSED_STATUS_IDS,
    )
    full = old_client.get_issues(1, {"createdUntil": PERIOD_END.strftime("%Y-%m-%d")})

    assert len(narrowed) < len(full)
    # 絞り込みは2クエリ、全件は1クエリ
    assert len(new_client.issued_queries) == 2
    assert "statusId" in new_client.issued_queries[0]
    assert "updatedSince" in new_client.issued_queries[1]


def test_excluded_issues_are_only_stale_and_closed():
    """取得から外れた課題が「現在完了系」かつ「期間開始以降 更新なし」だけであること"""
    issues, comments_by_id = build_corpus("02")
    client = RecordingClient(issues, comments_by_id)
    narrowed_ids = {
        i["id"] for i in bwr._fetch_target_issues(
            client, 1, PERIOD_START, PERIOD_END, {}, STATUSES,
            bwr.DEFAULT_CLOSED_STATUS_IDS,
        )
    }
    ws = PERIOD_START.strftime("%Y-%m-%d")
    created_until = PERIOD_END.strftime("%Y-%m-%d")

    excluded = [i for i in issues
                if i["id"] not in narrowed_ids
                and bwr.to_local_date(i["created"]) <= created_until]
    assert excluded, "除外された課題が1件も無い（テストデータが不十分）"
    for issue in excluded:
        assert issue["status"]["name"] in CLOSED_NAMES
        assert bwr.to_local_date(issue["updated"]) < ws


def test_issues_with_unlisted_status_are_treated_as_open():
    """
    完了系に登録されていないステータスは、更新が古くても取得され、
    オープン系として集計されること。
    """
    stale_unknown = {
        "id": 1, "issueKey": "PRJ-1",
        "created": "2024-01-01T02:00:00Z",
        "updated": "2024-01-01T02:00:00Z",       # 期間よりずっと前
        "status": {"name": "レビュー中"},          # config の open/closed どちらにも無い
        "summary": "設定外ステータス", "assignee": None, "dueDate": None,
    }
    statuses = STATUSES + [{"id": 5, "name": "レビュー中"}]
    client = RecordingClient([stale_unknown], {}, statuses=statuses)
    fetched = bwr._fetch_target_issues(
        client, 1, PERIOD_START, PERIOD_END, {}, statuses, bwr.DEFAULT_CLOSED_STATUS_IDS,
    )
    assert [i["id"] for i in fetched] == [1]

    data = bwr.collect_report_data(
        client, "PRJ", 1, PERIOD_START, PERIOD_END,
        bwr.DEFAULT_CLOSED_STATUS_IDS, max_workers=2,
    )
    # 完了系ではないのでオープン系扱い → ①に入り、完了していないので⑤にも入る
    assert [i["issueKey"] for i in data["carry_over"]] == ["PRJ-1"]
    assert [i["issueKey"] for i in data["incomplete"]] == ["PRJ-1"]
    # プロジェクトのステータス一覧には存在するので「一覧に無い名前」の警告は出ない
    assert data["unknown_statuses"] == set()


def test_status_name_missing_from_project_list_is_reported():
    """changeLog にプロジェクトの一覧に無いステータス名が現れたら警告すること"""
    issue = {
        "id": 1, "issueKey": "PRJ-1", "created": "2026-01-10T02:00:00Z",
        "updated": "2026-03-05T02:00:00Z", "status": {"name": "処理中"},
        "summary": "改名されたステータス", "assignee": None, "dueDate": None,
    }
    comments = [{
        "id": 1, "created": "2026-03-05T02:00:00Z",
        "changeLog": [{"field": "status", "originalValue": "旧ステータス名", "newValue": "処理中"}],
    }]
    client = RecordingClient([issue], {1: comments})
    data = bwr.collect_report_data(
        client, "PRJ", 1, PERIOD_START, PERIOD_END,
        bwr.DEFAULT_CLOSED_STATUS_IDS, max_workers=1,
    )
    assert data["unknown_statuses"] == {"旧ステータス名"}


def test_falls_back_to_full_fetch_without_statuses():
    """ステータス一覧が取れなければ絞り込みを諦めて全件取得すること"""
    issues, comments_by_id = build_corpus("02")
    client = RecordingClient(issues, comments_by_id)
    fetched = bwr._fetch_target_issues(
        client, 1, PERIOD_START, PERIOD_END, {}, [], bwr.DEFAULT_CLOSED_STATUS_IDS,
    )
    assert len(client.issued_queries) == 1
    assert "statusId" not in client.issued_queries[0]
    assert "updatedSince" not in client.issued_queries[0]
    assert len(fetched) == len(client.get_issues(1, client.issued_queries[0]))


def test_filter_params_apply_to_both_queries():
    """フィルター条件は絞り込みの2つのクエリ両方に適用されること"""
    client = RecordingClient([], {})
    bwr._fetch_target_issues(
        client, 1, PERIOD_START, PERIOD_END,
        {"issueTypeId": [7], "keyword": "障害"}, STATUSES, bwr.DEFAULT_CLOSED_STATUS_IDS,
    )
    assert len(client.issued_queries) == 2
    for query in client.issued_queries:
        assert query["issueTypeId"] == [7]
        assert query["keyword"] == "障害"
        assert query["createdUntil"] == PERIOD_END.strftime("%Y-%m-%d")


def test_updated_since_has_one_day_margin():
    """updatedSince はタイムゾーン差で取りこぼさないよう1日前を指定すること"""
    client = RecordingClient([], {})
    bwr._fetch_target_issues(
        client, 1, PERIOD_START, PERIOD_END, {}, STATUSES, bwr.DEFAULT_CLOSED_STATUS_IDS,
    )
    assert client.issued_queries[1]["updatedSince"] == "2026-03-01"   # 開始日は 03-02


def test_equation_holds_across_corpus():
    """整合したデータでは全シナリオで ①+②+③ = ④+⑤ が成立すること"""
    for hour in HOURS:
        issues, comments_by_id = build_corpus(hour)
        data, _ = run_new(issues, comments_by_id)
        lhs = len(data["carry_over"]) + len(data["new_issues"]) + len(data["reopened"])
        rhs = len(data["completed"]) + len(data["incomplete"])
        assert lhs == rhs, f"hour={hour}: {lhs} != {rhs}"


def test_skip_optimization_precondition():
    """
    スキップ最適化が依拠する前提を明示しておく。

    updated が最終ステータス変更より古い（Backlog では起きない）データでは、
    新実装はコメントを取得せず現在のステータスを採用するため、
    履歴から導出する旧実装と結果が食い違う。
    この差は最適化の欠陥ではなく前提条件の破れであることを記録しておく。
    """
    issue = {
        "id": 1, "issueKey": "PRJ-1",
        "created": "2026-01-15T02:00:00Z",
        "updated": "2026-01-15T02:00:00Z",     # 変化を反映していない古い値
        "status": {"name": "処理済み"},          # 履歴の最終状態（処理中）と矛盾
        "summary": "不整合データ", "assignee": None, "dueDate": None,
    }
    comments = [{
        "id": 1, "created": "2026-01-15T02:00:00Z",
        "changeLog": [{"field": "status", "originalValue": "未対応", "newValue": "処理中"}],
    }]

    new_data, new_client = run_new([issue], {1: comments})
    old_data, _ = run_old([issue], {1: comments})

    assert new_client.fetch_count == 0                      # スキップされる
    assert categorize(new_data) != categorize(old_data)     # よって食い違う

    # updated を正しく（最終変化以降に）すれば一致する
    fixed = {**issue, "updated": "2026-03-05T02:00:00Z"}
    fixed_data, fixed_client = run_new([fixed], {1: comments})
    assert fixed_client.fetch_count == 1
    assert categorize(fixed_data) == categorize(old_data)
