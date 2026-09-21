"""①〜⑤の集計。課題の取得、コメント履歴からの分類、フィルター条件の解決。"""
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

from backlog_report.client import BacklogAPIError, BacklogClient, format_api_error
from backlog_report.core import (
    DEFAULT_MAX_WORKERS,
    ReportData,
    _is_closed,
    _is_open,
    _with_status,
    safe_filename,
    to_local_date,
)
from backlog_report.snapshot import NO_FILTER_NAME


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

    non_closed_ids: list | None = [s["id"] for s in statuses
                                   if s["id"] not in closed_status_ids]
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
) -> ReportData:
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

        # ① 前週残件: 表示ステータスを常に期間開始時点に差し替える。
        # 差し替えないと、期間より後に変化した課題が現在のステータスで表示され、
        # 同じ課題が①と⑤で違うステータスに見えてしまう。
        if result["is_carry_over"]:
            carry_over_issues.append(_with_status(issue, result["status_at_start"]))

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
        # 抽出対象への出入りの判定に使う（今回の母集団に居るかどうかを調べる）
        "population_ids": set(all_issues_map),
        "inflow":  [],
        "outflow": [],
    }


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


def build_jobs(filters_cfg: list, default_project_key: str) -> list:
    """
    フィルター設定を「集計1回ぶんの仕事」の並びに変換する。

    フィルターを定義していない場合も1件の仕事として扱うことで、
    集計から書き出しまでの流れを1本にまとめられる。

    各要素のキー:
        cfg           : フィルター設定（フィルターなしなら空）
        name          : レポートの見出しに使う名前（フィルターなしなら None）
        description   : レポートに載せるメモ
        condition     : 絞り込み条件の文字列（スナップショットの照合にも使う）
        project_key   : 集計対象のプロジェクト
        need_master   : 種別・カスタム属性のマスターが必要か
        snapshot_name : スナップショット上の名前
        filename      : 出力するファイル名
    """
    if not filters_cfg:
        return [{
            "cfg": {}, "name": None, "description": "", "condition": "",
            "project_key": default_project_key, "need_master": False,
            "snapshot_name": NO_FILTER_NAME, "filename": "weekly_report.md",
        }]

    jobs = []
    for i, cfg in enumerate(filters_cfg, 1):
        name = cfg.get("name") or f"filter_{i}"
        jobs.append({
            "cfg": cfg,
            "name": name,
            "description": cfg.get("description") or "",
            "condition": build_filter_summary(cfg),
            "project_key": cfg.get("project_key") or default_project_key,
            "need_master": True,
            "snapshot_name": name,
            "filename": f"weekly_report_{safe_filename(name)}.md",
        })
    return jobs
