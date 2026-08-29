"""
週を連鎖させた通し検証。

このツールの目的である「前週⑤ = 今週①」が、実際に run() を複数回実行したときに
成立することを確認する。前の週が書き出した snapshot.json を次の週が読む、という
実運用と同じ流れをそのまま再現する。

課題の種別を週の途中で変えることで、抽出対象への出入り（流入・流出）も含めて検証する。
"""
import contextlib
import io
import json
import re
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

import backlog_weekly_report as bwr

STATUSES = [
    {"id": 1, "name": "未対応"}, {"id": 2, "name": "処理中"},
    {"id": 3, "name": "処理済み"}, {"id": 4, "name": "完了"},
]
STATUS_ID = {s["name"]: s["id"] for s in STATUSES}
ISSUE_TYPES = [{"id": 10, "name": "バグ"}, {"id": 11, "name": "タスク"}]

WEEKS = [
    (date(2026, 2, 23), date(2026, 3, 1)),    # 第1週
    (date(2026, 3, 2), date(2026, 3, 8)),     # 第2週
    (date(2026, 3, 9), date(2026, 3, 15)),    # 第3週
]


class Scenario:
    """週ごとの種別と、ステータス変更履歴を持つ課題"""

    def __init__(self, key: str, created: str, types: list, changes=None, note=""):
        self.key = key
        self.created = created
        self.types = types            # 週ごとの種別（レポート実行時点の値）
        self.changes = changes or []  # (日付, 変更前, 変更後)
        self.note = note or key

    def current_status(self) -> str:
        return self.changes[-1][2] if self.changes else "未対応"

    def updated(self) -> str:
        return max([self.created] + [d for d, _, _ in self.changes])


def build_client(scenarios: list, week_index: int):
    """指定した週の時点での属性を返す疑似クライアント"""
    payload, comments = [], {}
    for number, sc in enumerate(scenarios, 1):
        if sc.types[week_index] != "バグ":
            continue          # フィルター条件から外れているので API は返さない
        payload.append({
            "id": number, "issueKey": sc.key, "summary": sc.note,
            "created": f"{sc.created}T02:00:00Z",
            "updated": f"{sc.updated()}T02:00:00Z",
            "status": {"name": sc.current_status()},
            "assignee": None, "dueDate": None,
        })
        comments[number] = [
            {"id": i, "created": f"{day}T02:00:00Z",
             "changeLog": [{"field": "status", "originalValue": src, "newValue": dst}]}
            for i, (day, src, dst) in enumerate(sc.changes)
        ]

    class StubClient(bwr.BacklogClient):
        def _get(self, endpoint, params=None):
            params = params or {}
            if endpoint == "/issues":
                selected = list(payload)
                if params.get("statusId") is not None:
                    selected = [x for x in selected
                                if STATUS_ID[x["status"]["name"]] in params["statusId"]]
                if params.get("updatedSince") is not None:
                    selected = [x for x in selected
                                if bwr.to_local_date(x["updated"]) >= params["updatedSince"]]
                if params.get("createdUntil") is not None:
                    selected = [x for x in selected
                                if bwr.to_local_date(x["created"]) <= params["createdUntil"]]
                offset = params.get("offset", 0)
                return selected[offset:offset + bwr.API_PAGE_SIZE]
            if endpoint.endswith("/statuses"):
                return STATUSES
            if endpoint.endswith("/issueTypes"):
                return ISSUE_TYPES
            if endpoint.endswith("/customFields"):
                return []
            if "/comments" in endpoint:
                return comments.get(int(endpoint.split("/")[2]), [])
            return {"id": 1, "name": "PRJプロジェクト"}

    return StubClient


def period_dir(week: tuple) -> str:
    start, end = week
    return f"{start:%Y%m%d}_{end:%Y%m%d}"


def run_week(monkeypatch, tmp_path: Path, out: Path, scenarios: list, week_index: int) -> None:
    start, end = WEEKS[week_index]
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "backlog": {"space_host": "example.backlog.com", "api_key": "K", "project_key": "PRJ"},
        "report": {"period": {"from": start.isoformat(), "to": end.isoformat()},
                   "output_dir": str(out)},
        "filters": [{"name": "バグ対応", "issue_types": ["バグ"]}],
    }, allow_unicode=True), encoding="utf-8")

    monkeypatch.setattr(bwr, "BacklogClient", build_client(scenarios, week_index))
    with contextlib.redirect_stdout(io.StringIO()):
        bwr.run(["--config", str(config)])


def read_counts(out: Path, week_index: int) -> dict:
    md = (out / period_dir(WEEKS[week_index]) / "weekly_report_バグ対応.md").read_text(encoding="utf-8")
    return {m.group(1): int(m.group(2))
            for m in re.finditer(r"\| (①|②|③|④|⑤)[^|]*\| \*\*(\d+)\*\*", md)}


def read_section_keys(out: Path, week_index: int, heading: str) -> set:
    md = (out / period_dir(WEEKS[week_index]) / "weekly_report_バグ対応.md").read_text(encoding="utf-8")
    block = md.split(heading)[1].split("</details>")[0]
    return set(re.findall(r"PRJ-\d+", block))


def read_snapshot_incomplete(out: Path, week_index: int) -> set:
    path = out / period_dir(WEEKS[week_index]) / bwr.SNAPSHOT_FILENAME
    snap = json.loads(path.read_text(encoding="utf-8"))
    return {i["issueKey"] for i in snap["filters"][0]["incomplete"]}


# ------------------------------------------------------------------
# シナリオ
# ------------------------------------------------------------------

def scenarios_with_flows() -> list:
    return [
        Scenario("PRJ-1", "2026-01-10", ["バグ", "バグ", "バグ"], note="ずっと未対応"),
        Scenario("PRJ-2", "2026-01-10", ["バグ", "バグ", "バグ"],
                 [("2026-03-04", "未対応", "完了")], note="第2週に完了"),
        Scenario("PRJ-3", "2026-03-05", ["バグ", "バグ", "バグ"], note="第2週に作成"),
        Scenario("PRJ-4", "2026-01-10", ["バグ", "バグ", "バグ"],
                 [("2026-03-04", "未対応", "完了"), ("2026-03-06", "完了", "処理中")],
                 note="第2週に完了して同じ週に再オープン"),
        Scenario("PRJ-6", "2026-01-10", ["タスク", "タスク", "バグ"], note="第3週に流入"),
        Scenario("PRJ-7", "2026-01-10", ["バグ", "バグ", "タスク"], note="第3週に流出"),
    ]


# ------------------------------------------------------------------
# 本題: 週をまたいだ照合
# ------------------------------------------------------------------

def test_previous_incomplete_equals_next_carry_over(tmp_path, monkeypatch):
    """3週連続で実行し、毎週 前週⑤ = 今週① が成立すること"""
    out = tmp_path / "out"
    scenarios = scenarios_with_flows()

    for week in range(3):
        run_week(monkeypatch, tmp_path, out, scenarios, week)

    for week in range(2):
        prev_incomplete = read_snapshot_incomplete(out, week)
        next_carry_over = read_section_keys(out, week + 1, "## ① 前週残件")
        assert prev_incomplete == next_carry_over, (
            f"第{week + 1}週の⑤と第{week + 2}週の①が一致しない: "
            f"{sorted(prev_incomplete)} vs {sorted(next_carry_over)}"
        )


def test_equation_holds_every_week(tmp_path, monkeypatch):
    """3週とも ①+②+③ = ④+⑤ が成立すること"""
    out = tmp_path / "out"
    scenarios = scenarios_with_flows()
    for week in range(3):
        run_week(monkeypatch, tmp_path, out, scenarios, week)

    for week in range(3):
        c = read_counts(out, week)
        assert c["①"] + c["②"] + c["③"] == c["④"] + c["⑤"], f"第{week + 1}週で等式が崩れた: {c}"


def test_inflow_and_outflow_are_reflected_in_the_chain(tmp_path, monkeypatch):
    """連鎖の中で、流入が②に、流出が④に入ること"""
    out = tmp_path / "out"
    scenarios = scenarios_with_flows()
    for week in range(3):
        run_week(monkeypatch, tmp_path, out, scenarios, week)

    md = (out / period_dir(WEEKS[2]) / "weekly_report_バグ対応.md").read_text(encoding="utf-8")
    assert "期間中に対象へ入った **1** 件は ② 新規発生に含めています" in md
    assert "PRJ-6" in md.split("② 新規発生に含めています")[1][:60]
    assert "期間中に対象から外れた **1** 件は ④ 当週完了に含めています" in md
    assert "PRJ-7" in md.split("④ 当週完了に含めています")[1][:60]

    # 流出した課題は⑤に残らない
    assert "PRJ-7" not in read_snapshot_incomplete(out, 2)


def test_completed_then_reopened_stays_incomplete(tmp_path, monkeypatch):
    """同じ週に完了して再オープンした課題は④ではなく⑤に入り、翌週①へ引き継がれること"""
    out = tmp_path / "out"
    scenarios = scenarios_with_flows()
    for week in range(3):
        run_week(monkeypatch, tmp_path, out, scenarios, week)

    assert "PRJ-4" in read_snapshot_incomplete(out, 1)                    # 第2週の⑤
    assert "PRJ-4" not in read_section_keys(out, 1, "## ④ 当週完了")       # 第2週の④ではない
    assert "PRJ-4" in read_section_keys(out, 2, "## ① 前週残件")           # 第3週の①


# ------------------------------------------------------------------
# 実行順
# ------------------------------------------------------------------

def test_execution_order_does_not_change_incomplete(tmp_path, monkeypatch):
    """
    実行順を変えても⑤の顔ぶれは変わらないこと。

    次の期間が参照するのは⑤だけなので、この性質があるかぎり
    順不同で実行しても後続の集計は壊れない。
    """
    scenarios = scenarios_with_flows()

    in_order = tmp_path / "in_order"
    for week in (0, 1, 2):
        run_week(monkeypatch, tmp_path, in_order, scenarios, week)

    shuffled = tmp_path / "shuffled"
    for week in (1, 0, 2):        # 前週 → 前々週 → 今週
        run_week(monkeypatch, tmp_path, shuffled, scenarios, week)

    for week in range(3):
        assert read_snapshot_incomplete(in_order, week) == read_snapshot_incomplete(shuffled, week)


def test_later_week_is_correct_even_if_run_out_of_order(tmp_path, monkeypatch):
    """先に実行してしまった週があっても、最後に実行した週は正しく照合されること"""
    scenarios = scenarios_with_flows()
    out = tmp_path / "out"
    for week in (1, 0, 2):
        run_week(monkeypatch, tmp_path, out, scenarios, week)

    assert read_snapshot_incomplete(out, 1) == read_section_keys(out, 2, "## ① 前週残件")
    md = (out / period_dir(WEEKS[2]) / "weekly_report_バグ対応.md").read_text(encoding="utf-8")
    assert "抽出対象への出入りを判定していない" not in md


def test_out_of_order_run_notes_that_flows_were_not_judged(tmp_path, monkeypatch):
    """直前の期間より先に実行した週には、判定していない旨が注記されること"""
    scenarios = scenarios_with_flows()
    out = tmp_path / "out"
    run_week(monkeypatch, tmp_path, out, scenarios, 1)     # 第2週を先に実行

    md = (out / period_dir(WEEKS[1]) / "weekly_report_バグ対応.md").read_text(encoding="utf-8")
    assert "抽出対象への出入りを判定していない" in md


def test_rerunning_a_week_fixes_its_breakdown(tmp_path, monkeypatch):
    """先に実行してしまった週も、あとで実行し直せば内訳が正しくなること"""
    # 第2週の期間中に流入する課題を用意する
    scenarios = [
        Scenario("PRJ-1", "2026-01-10", ["バグ", "バグ", "バグ"], note="ずっと未対応"),
        Scenario("PRJ-6", "2026-01-10", ["タスク", "バグ", "バグ"], note="第2週に流入"),
    ]
    out = tmp_path / "out"

    run_week(monkeypatch, tmp_path, out, scenarios, 1)     # 第1週より先に第2週
    before = read_counts(out, 1)
    assert before["①"] == 2 and before["②"] == 0           # 流入を判定できず①に入る

    run_week(monkeypatch, tmp_path, out, scenarios, 0)     # 第1週を実行
    run_week(monkeypatch, tmp_path, out, scenarios, 1)     # 第2週を実行し直す
    after = read_counts(out, 1)
    assert after["①"] == 1 and after["②"] == 1             # 流入が②に移る
    assert read_snapshot_incomplete(out, 1) == {"PRJ-1", "PRJ-6"}   # ⑤は変わらない


# ------------------------------------------------------------------
# ① の表示ステータス
# ------------------------------------------------------------------

def test_carry_over_shows_status_at_period_start(tmp_path, monkeypatch):
    """
    期間より後に完了した残件は、①に期間開始時点のステータスで表示されること。

    以前は現在のステータスが出ていたため、同じ課題が①と⑤で違って見えていた。
    """
    scenarios = [
        Scenario("PRJ-1", "2026-01-10", ["バグ", "バグ", "バグ"],
                 [("2026-03-12", "処理中", "完了")], note="第3週に完了した残件"),
    ]
    out = tmp_path / "out"
    run_week(monkeypatch, tmp_path, out, scenarios, 1)     # 第2週を集計（完了は第3週）

    md = (out / period_dir(WEEKS[1]) / "weekly_report_バグ対応.md").read_text(encoding="utf-8")

    def status_in(heading: str) -> str:
        block = md.split(heading)[1].split("</details>")[0]
        row = next(line for line in block.splitlines() if line.startswith("| PRJ-1 "))
        return row.split("|")[3].strip()

    assert status_in("## ① 前週残件") == "処理中"      # 期間開始時点
    assert status_in("## ⑤ 当週未完了") == "処理中"    # 期間終了時点
    assert status_in("## ① 前週残件") == status_in("## ⑤ 当週未完了")


@pytest.mark.parametrize("heading", ["## ② 新規発生", "## ④ 当週完了", "## ⑤ 当週未完了"])
def test_other_sections_show_status_at_period_end(tmp_path, monkeypatch, heading):
    """②④⑤は期間終了時点のステータスで表示されること"""
    scenarios = [
        Scenario("PRJ-2", "2026-03-04", ["バグ", "バグ", "バグ"],
                 [("2026-03-05", "未対応", "完了"), ("2026-03-12", "完了", "処理中")],
                 note="第2週に作成して完了、第3週に再オープン"),
        Scenario("PRJ-3", "2026-03-04", ["バグ", "バグ", "バグ"], note="未完了のまま"),
    ]
    out = tmp_path / "out"
    run_week(monkeypatch, tmp_path, out, scenarios, 1)

    md = (out / period_dir(WEEKS[1]) / "weekly_report_バグ対応.md").read_text(encoding="utf-8")
    block = md.split(heading)[1].split("</details>")[0]
    for line in block.splitlines():
        if line.startswith("| PRJ-2 "):
            # 第3週の再オープンは反映されず、第2週終了時点の「完了」が出る
            assert line.split("|")[3].strip() == "完了"


def test_period_boundary_is_one_day_before_next_start(tmp_path, monkeypatch):
    """期間フォルダの探索が、終了日＝次の開始日の前日で行われること"""
    scenarios = scenarios_with_flows()
    out = tmp_path / "out"
    run_week(monkeypatch, tmp_path, out, scenarios, 0)

    snapshot, reason = bwr.find_previous_snapshot(out, WEEKS[1][0])
    assert snapshot is not None and reason == ""
    assert snapshot["period"]["to"] == (WEEKS[1][0] - timedelta(days=1)).isoformat()
