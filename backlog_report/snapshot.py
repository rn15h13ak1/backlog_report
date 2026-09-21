"""期間をまたいだ照合の記録（snapshot.json）と、抽出対象への出入りの反映。"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from backlog_report.core import CATEGORY_KEYS, ReportData

SNAPSHOT_FILENAME = "snapshot.json"


NO_FILTER_NAME = "__no_filter__"   # フィルター無しで集計したときの記録上の名前


SNAPSHOT_VERSION = 1


def _snapshot_entry(issue: dict) -> dict:
    """スナップショットに残す最小限の課題情報（次回の表示に使う）"""
    return {
        "id":       issue.get("id"),
        "issueKey": issue.get("issueKey"),
        "summary":  issue.get("summary"),
        "status":   issue.get("status", {}).get("name"),
        "dueDate":  issue.get("dueDate"),
        "assignee": (issue.get("assignee") or {}).get("name"),
    }


def build_snapshot(period_start: date, period_end: date, entries: list) -> dict:
    """
    次回実行時に「抽出対象から外れた課題」を検知するための記録。

    entries: [(フィルター名, 絞り込み条件の文字列, 集計結果)] のリスト。

    incomplete（⑤）は次回の①と突き合わせて出入りを判定するために使う。
    counts と completed（④）と outflow は、次回の weekly3 レポートで「前週」の列を
    組み立てるために使う。読む側は欠けていても動くので、これらを足しても
    以前の形式のスナップショットはそのまま使える。
    """
    return {
        "version": SNAPSHOT_VERSION,
        "period": {
            "from": period_start.isoformat(),
            "to":   period_end.isoformat(),
        },
        "filters": [
            {
                "name": name,
                "condition": condition,
                "counts": {key: len(data[key]) for key in CATEGORY_KEYS},
                "completed":  [_snapshot_entry(i) for i in data["completed"]],
                "incomplete": [_snapshot_entry(i) for i in data["incomplete"]],
                # 抽出対象から外れて④に入れた課題。次回の weekly3 で「前週」の列に
                # 出すときに、完了させたものと区別して印を付けるために使う。
                "outflow": [i.get("id") for i in (data.get("outflow") or [])],
            }
            for name, condition, data in entries
        ],
    }


def write_snapshot(output_dir: Path, snapshot: dict) -> Path:
    path = output_dir / SNAPSHOT_FILENAME
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def find_previous_snapshot(output_dir: Path, period_start: date) -> tuple[dict | None, str]:
    """
    直前の期間のスナップショットを探す。

    期間フォルダ名は YYYYMMDD_YYYYMMDD 形式なので、終了日が period_start の前日に
    あたるフォルダを探す。期間の長さは問わないため、週次でも任意期間でも動く。

    Returns:
        (スナップショット, 見つからなかった理由) — 見つかれば理由は空文字
    """
    if not output_dir.exists():
        return None, "出力先ディレクトリがまだありません"

    wanted_end = (period_start - timedelta(days=1)).strftime("%Y%m%d")
    candidates = [
        child for child in output_dir.iterdir()
        if child.is_dir() and "_" in child.name and child.name.rsplit("_", 1)[-1] == wanted_end
    ]
    if not candidates:
        return None, (f"直前の期間（〜{(period_start - timedelta(days=1)).strftime('%Y/%m/%d')}）"
                      "の集計結果が見つかりません")

    # 終了日が同じフォルダが複数ありうる（例: 3/1〜3/8 と 3/2〜3/8 の両方で実行した場合）。
    # フォルダの列挙順はファイルシステム依存で不定なので、開始日が最も遅いもの
    # （＝期間が最も短い＝直近の集計）を選ぶ。
    candidates.sort(key=lambda c: c.name.rsplit("_", 1)[0], reverse=True)
    chosen = candidates[0]
    if len(candidates) > 1:
        others = "、".join(c.name for c in candidates[1:])
        print(f"  ⚠ 終了日が同じ期間フォルダが複数あります。{chosen.name} を使用します"
              f"（他: {others}）", file=sys.stderr)

    path = chosen / SNAPSHOT_FILENAME
    if not path.exists():
        return None, f"直前の期間のフォルダ（{chosen.name}）に {SNAPSHOT_FILENAME} がありません"
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return None, f"{path} を読み込めませんでした: {e}"
    if snapshot.get("version") != SNAPSHOT_VERSION:
        return None, f"{path} の形式が古いため使用しません"
    return snapshot, ""


def previous_incomplete(snapshot: dict | None, filter_name: str,
                        condition: str) -> tuple[dict | None, str]:
    """
    スナップショットから、指定フィルターの前回⑤を取り出す。

    Returns:
        ({課題ID: 課題情報}, 使えない理由) — 使えれば理由は空文字
    """
    if snapshot is None:
        return None, ""
    for entry in snapshot.get("filters", []):
        if entry.get("name") != filter_name:
            continue
        if entry.get("condition", "") != condition:
            return None, (f"フィルター「{filter_name}」の絞り込み条件が前回と異なるため"
                          "（前回: " + (entry.get("condition") or "（なし）") + "）、"
                          "抽出対象への出入りは判定しません")
        return {i["id"]: i for i in entry.get("incomplete", [])}, ""
    return None, f"フィルター「{filter_name}」は前回の集計に含まれていません"


def apply_population_flows(data: ReportData, prev_incomplete: dict) -> ReportData:
    """
    前回⑤と突き合わせて、抽出対象への出入りを集計に反映する。

    流入（前回⑤に無いのに今回①に居る）: 期首の在庫ではないので ① → ②
    流出（前回⑤に居たのに今回の母集団に居ない）: 期首は在庫、期中に対象外へ → ① かつ ④

    属性そのものは見ず、集合の差分だけで判定するため、種別でもカスタム属性でも
    キーワードでも同じように動く。

    流出の判定は「今回は取得できなかった」という事実だけを見ているため、
    次の3つは区別されず、いずれも流出として扱われる。

      ・種別やカスタム属性の変更で抽出対象から外れた
      ・課題が削除された
      ・課題が別プロジェクトへ移動された

    表示に必要な情報は前回のスナップショットから復元するので、課題が存在しなくても
    API は叩かず、前回時点の件名・ステータス・期限がそのまま表示される。
    """
    prev_ids = set(prev_incomplete)
    present_ids = data["population_ids"]

    carry_over = list(data["carry_over"])
    new_issues = list(data["new_issues"])
    completed  = list(data["completed"])

    inflow = [i for i in carry_over if i.get("id") not in prev_ids]
    if inflow:
        inflow_ids = {i.get("id") for i in inflow}
        carry_over = [i for i in carry_over if i.get("id") not in inflow_ids]
        new_issues = new_issues + inflow

    outflow_ids = prev_ids - present_ids
    outflow = []
    for issue_id in sorted(outflow_ids):
        saved = prev_incomplete[issue_id]
        outflow.append({
            "id":       saved.get("id"),
            "issueKey": saved.get("issueKey"),
            "summary":  saved.get("summary"),
            "status":   {"name": saved.get("status") or "-"},
            "dueDate":  saved.get("dueDate"),
            "assignee": {"name": saved["assignee"]} if saved.get("assignee") else None,
        })
    if outflow:
        # 期間開始時点では在庫だったので①に含め、期中に対象外になったので④にも含める
        carry_over = carry_over + outflow
        completed  = completed + outflow

    active_ids = {i.get("id") for i in carry_over + new_issues + data["reopened"]}
    completed_ids = {i.get("id") for i in completed}
    by_id = {i.get("id"): i for i in carry_over + new_issues + data["reopened"] + data["incomplete"]}
    incomplete = [by_id[i] for i in sorted(active_ids - completed_ids) if i in by_id]

    return {
        **data,
        "carry_over": carry_over,
        "new_issues": new_issues,
        "completed":  completed,
        "incomplete": incomplete,
        "inflow":     inflow,
        "outflow":    outflow,
    }
