"""
run() を通しで動かすテスト。

設定ファイルの読み込みからレポートの書き出しまでを、実際のコードで実行する。
差し替えるのは HTTP 通信（BacklogClient._get）だけで、プロジェクト情報の
キャッシュ・フィルター解決・集計・出力はすべて本物を通す。

出力先は一時ディレクトリに向けるため、リポジトリ配下には何も生成しない。
"""
import json
from pathlib import Path

import pytest
import yaml

import backlog_weekly_report as bwr

PERIOD = {"from": "2026-03-02", "to": "2026-03-08"}
PERIOD_DIR = "20260302_20260308"

STATUSES = [
    {"id": 1, "name": "未対応"}, {"id": 2, "name": "処理中"},
    {"id": 3, "name": "処理済み"}, {"id": 4, "name": "完了"},
]
ISSUE_TYPES = [{"id": 10, "name": "バグ"}, {"id": 11, "name": "タスク"}]
CUSTOM_FIELDS = [
    {"id": 100, "name": "対応チーム", "typeId": 5,
     "items": [{"id": 1, "name": "Aチーム"}, {"id": 2, "name": "Bチーム"}]},
]


def issue(number: int, status: str, created: str, updated: str, summary: str = "課題") -> dict:
    return {
        "id": number, "issueKey": f"PRJ-{number}", "summary": f"{summary}{number}",
        "created": created, "updated": updated, "status": {"name": status},
        "assignee": None, "dueDate": "2026-03-10T00:00:00Z",
    }


# PRJ-1: 期間前からの残件（未完了のまま）→ ①⑤
# PRJ-2: 期間中に完了                     → ①④
# PRJ-3: 期間中に作成、未完了             → ②⑤
DEFAULT_ISSUES = [
    issue(1, "処理中", "2026-01-10T02:00:00Z", "2026-01-10T02:00:00Z", "残件"),
    issue(2, "完了", "2026-01-10T02:00:00Z", "2026-03-04T02:00:00Z", "完了したもの"),
    issue(3, "未対応", "2026-03-03T02:00:00Z", "2026-03-03T02:00:00Z", "新規"),
]
DEFAULT_COMMENTS = {
    2: [{"id": 1, "created": "2026-03-04T02:00:00Z",
         "changeLog": [{"field": "status", "originalValue": "処理中", "newValue": "完了"}]}],
}


def make_stub_client(issues=None, comments=None, fail_on=None, statuses=None):
    """
    HTTP だけを差し替えた BacklogClient を返す。

    fail_on: そのエンドポイントの部分文字列を含む呼び出しで BacklogAPIError を出す
    """
    issues = DEFAULT_ISSUES if issues is None else issues
    comments = DEFAULT_COMMENTS if comments is None else comments
    statuses = STATUSES if statuses is None else statuses
    status_id = {s["name"]: s["id"] for s in statuses}

    class StubClient(bwr.BacklogClient):
        def _get(self, endpoint, params=None):
            if fail_on and fail_on in endpoint:
                raise bwr.BacklogAPIError(endpoint, status_code=403, detail="権限がありません")
            params = params or {}

            if endpoint == "/issues":
                selected = list(issues)
                if params.get("statusId") is not None:
                    selected = [i for i in selected
                                if status_id.get(i["status"]["name"]) in params["statusId"]]
                if params.get("updatedSince") is not None:
                    selected = [i for i in selected
                                if bwr.to_local_date(i["updated"]) >= params["updatedSince"]]
                offset = params.get("offset", 0)
                return selected[offset:offset + bwr.API_PAGE_SIZE]

            if endpoint.endswith("/statuses"):
                return statuses
            if endpoint.endswith("/issueTypes"):
                return ISSUE_TYPES
            if endpoint.endswith("/customFields"):
                return CUSTOM_FIELDS
            if "/comments" in endpoint:
                return comments.get(int(endpoint.split("/")[2]), [])
            if endpoint.startswith("/projects/"):
                key = endpoint.rsplit("/", 1)[1]
                return {"id": 900 + len(key), "name": f"{key}プロジェクト"}
            raise AssertionError(f"想定外のエンドポイント: {endpoint}")

    return StubClient


def write_config(tmp_path: Path, output_dir: Path, **overrides) -> Path:
    report = {"period": dict(PERIOD), "output_dir": str(output_dir)}
    report.update(overrides.pop("report", {}))
    config = {
        "backlog": {
            "space_host": "example.backlog.com",
            "api_key": "REAL_KEY",
            "project_key": "PRJ",
        },
        "report": report,
    }
    config.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture
def stub(monkeypatch):
    """BacklogClient を差し替えるための取り付け口"""
    def install(**kwargs):
        monkeypatch.setattr(bwr, "BacklogClient", make_stub_client(**kwargs))
    return install


# ==================================================================
# フィルターなし
# ==================================================================

def test_run_without_filters_writes_one_report(tmp_path, stub, capsys):
    stub()
    out = tmp_path / "out"
    config = write_config(tmp_path, out)

    bwr.run(["--config", str(config)])

    report = out / PERIOD_DIR / "weekly_report.md"
    assert report.exists()
    assert not (out / PERIOD_DIR / "summary_report.md").exists()   # フィルター無しでは作らない

    text = report.read_text(encoding="utf-8")
    assert "# レポート — 2026/03/02 〜 2026/03/08" in text
    assert "> プロジェクト: **PRJプロジェクト** (`PRJ`)" in text
    assert "| ① 前週残件数 | **2** 件 |" in text      # PRJ-1, PRJ-2
    assert "| ② 新規発生件数 | **1** 件 |" in text     # PRJ-3
    assert "| ④ 当週完了件数 | **1** 件 |" in text     # PRJ-2
    assert "| ⑤ 当週未完了件数 | **2** 件 |" in text   # PRJ-1, PRJ-3
    assert "一致しません" not in text                   # 等式は成立している
    # 前回の集計が無いので、出入りを判定していない旨の注記が出る
    assert "抽出対象への出入りを判定していない" in text

    stdout = capsys.readouterr().out
    assert "Backlog レポート生成" in stdout
    assert "対象期間    : 2026-03-02 〜 2026-03-08（指定期間（config） / JST基準）" in stdout
    assert "①前週残件: 2 件" in stdout


def test_run_creates_period_directory_from_args(tmp_path, stub):
    stub()
    out = tmp_path / "out"
    config = write_config(tmp_path, out)

    bwr.run(["--config", str(config), "--from", "2026-02-01", "--to", "2026-02-28"])

    assert (out / "20260201_20260228" / "weekly_report.md").exists()
    assert not (out / PERIOD_DIR).exists()   # config の period は引数に負ける


# ==================================================================
# フィルターあり
# ==================================================================

FILTERS = [
    {"name": "バグ対応", "description": "バグ種別の課題", "issue_types": ["バグ"]},
    {"name": "A/チーム", "custom_fields": [{"field_name": "対応チーム", "values": ["Aチーム"]}]},
]


def test_run_with_filters_writes_report_per_filter_and_summary(tmp_path, stub, capsys):
    stub()
    out = tmp_path / "out"
    config = write_config(tmp_path, out, filters=FILTERS)

    bwr.run(["--config", str(config)])

    period = out / PERIOD_DIR
    assert (period / "weekly_report_バグ対応.md").exists()
    assert (period / "weekly_report_A_チーム.md").exists()   # / が _ に置換される
    assert (period / "summary_report.md").exists()
    assert not (period / "weekly_report.md").exists()

    bug = (period / "weekly_report_バグ対応.md").read_text(encoding="utf-8")
    assert "# レポート — バグ対応 — 2026/03/02 〜 2026/03/08" in bug
    assert "> フィルター: バグ種別の課題" in bug
    assert "> 絞り込み条件: `種別: バグ`" in bug

    summary = (period / "summary_report.md").read_text(encoding="utf-8")
    assert "# サマリーレポート — 2026/03/02 〜 2026/03/08" in summary
    assert "バグ対応" in summary and "A/チーム" in summary
    assert "●PRJ-2｜期限：3/10｜完了" in summary

    stdout = capsys.readouterr().out
    assert "[1/2] フィルター「バグ対応」を集計中..." in stdout
    assert "種別・カスタム属性マスターを取得中" in stdout
    assert "✅ サマリー保存" in stdout


def test_run_uses_per_filter_project_key(tmp_path, stub, capsys):
    stub()
    out = tmp_path / "out"
    filters = [{"name": "別プロジェクト", "project_key": "OTHER", "issue_types": ["タスク"]}]
    config = write_config(tmp_path, out, filters=filters)

    bwr.run(["--config", str(config)])

    text = (out / PERIOD_DIR / "weekly_report_別プロジェクト.md").read_text(encoding="utf-8")
    assert "(`OTHER`)" in text
    assert "OTHERプロジェクト" in text
    assert "プロジェクト: OTHER" in capsys.readouterr().out


def test_run_names_unnamed_filters(tmp_path, stub):
    stub()
    out = tmp_path / "out"
    config = write_config(tmp_path, out, filters=[{"issue_types": ["バグ"]}])

    bwr.run(["--config", str(config)])

    assert (out / PERIOD_DIR / "weekly_report_filter_1.md").exists()


# ==================================================================
# 警告の出方
# ==================================================================

def test_run_treats_unlisted_status_as_open(tmp_path, stub, capsys):
    """完了系に登録されていないステータスはオープン系として集計されること"""
    statuses = STATUSES + [{"id": 5, "name": "レビュー中"}]
    issues = DEFAULT_ISSUES + [
        issue(4, "レビュー中", "2026-01-10T02:00:00Z", "2026-03-05T02:00:00Z", "完了系に未登録"),
    ]
    stub(issues=issues, statuses=statuses)
    out = tmp_path / "out"
    config = write_config(tmp_path, out)

    bwr.run(["--config", str(config)])

    text = (out / PERIOD_DIR / "weekly_report.md").read_text(encoding="utf-8")
    assert "| ① 前週残件数 | **3** 件 |" in text     # PRJ-1, PRJ-2 に PRJ-4 が加わる
    assert "レビュー中" in text
    assert "ステータス一覧に無い名前" not in capsys.readouterr().err


def test_run_rejects_initial_status_in_closed_ids(tmp_path, stub, capsys):
    """新規作成時のステータスを完了系に登録していたらエラーで止まること"""
    stub()
    config = write_config(tmp_path, tmp_path / "out",
                          report={"closed_status_ids": [1, 3, 4]})   # 1=未対応 を含めてしまった

    with pytest.raises(SystemExit):
        bwr.run(["--config", str(config)])

    err = capsys.readouterr().err
    assert "closed_status_ids に「未対応」" in err
    assert "新規作成されるときのステータス" in err


def test_run_continues_when_master_fetch_fails(tmp_path, monkeypatch, capsys):
    """種別マスターが取れなくても処理を止めないこと"""
    monkeypatch.setattr(bwr, "BacklogClient", make_stub_client(fail_on="/issueTypes"))
    out = tmp_path / "out"
    config = write_config(tmp_path, out, filters=[{"name": "種別なし", "issue_types": ["バグ"]}])

    bwr.run(["--config", str(config)])

    captured = capsys.readouterr()
    assert "種別マスターの取得に失敗" in captured.err
    assert "種別「バグ」が見つかりません" in captured.err     # 解決できずスキップされる
    assert (out / PERIOD_DIR / "weekly_report_種別なし.md").exists()


def test_run_reports_comment_fetch_failures(tmp_path, monkeypatch, capsys):
    """コメント取得に失敗しても集計を続け、件数を警告すること"""
    monkeypatch.setattr(bwr, "BacklogClient", make_stub_client(fail_on="/comments"))
    out = tmp_path / "out"
    config = write_config(tmp_path, out)

    bwr.run(["--config", str(config)])

    assert "コメント履歴の取得に失敗" in capsys.readouterr().err
    text = (out / PERIOD_DIR / "weekly_report.md").read_text(encoding="utf-8")
    assert "件の課題でコメント履歴の取得に失敗" in text


# ==================================================================
# 異常系
# ==================================================================

def test_run_exits_when_config_missing(tmp_path, capsys):
    with pytest.raises(SystemExit):
        bwr.run(["--config", str(tmp_path / "no-such.yaml")])
    assert "設定ファイルが見つかりません" in capsys.readouterr().err


@pytest.mark.parametrize("key,placeholder", [
    ("space_host", "yourcompany.backlog.com"),
    ("api_key", "YOUR_API_KEY_HERE"),
    ("project_key", "YOUR_PROJECT_KEY"),
])
def test_run_exits_on_placeholder_config(tmp_path, key, placeholder, capsys):
    backlog = {"space_host": "example.backlog.com", "api_key": "REAL_KEY", "project_key": "PRJ"}
    backlog[key] = placeholder
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"backlog": backlog}), encoding="utf-8")

    with pytest.raises(SystemExit):
        bwr.run(["--config", str(config_path)])
    assert f"config.yaml の {key} を設定してください" in capsys.readouterr().err


def test_run_rejects_from_without_to(tmp_path, stub):
    stub()
    config = write_config(tmp_path, tmp_path / "out")
    with pytest.raises(SystemExit):
        bwr.run(["--config", str(config), "--from", "2026-03-01"])


def test_run_rejects_from_with_week(tmp_path, stub):
    stub()
    config = write_config(tmp_path, tmp_path / "out")
    with pytest.raises(SystemExit):
        bwr.run(["--config", str(config), "--from", "2026-03-01",
                 "--to", "2026-03-05", "--week", "current"])


def test_run_exits_when_project_fetch_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bwr, "BacklogClient", make_stub_client(fail_on="/projects/PRJ"))
    config = write_config(tmp_path, tmp_path / "out")

    with pytest.raises(SystemExit):
        bwr.run(["--config", str(config)])

    err = capsys.readouterr().err
    assert "プロジェクト情報の取得に失敗しました (PRJ)" in err
    assert "権限がありません" in err


def test_main_converts_api_error_to_exit_code(tmp_path, monkeypatch, capsys):
    """run() を抜けた BacklogAPIError は main() が受け止めて終了コード1にすること"""
    monkeypatch.setattr(bwr, "BacklogClient", make_stub_client(fail_on="/issues"))
    config = write_config(tmp_path, tmp_path / "out")
    monkeypatch.setattr("sys.argv", ["backlog_weekly_report.py", "--config", str(config)])

    with pytest.raises(SystemExit) as exc:
        bwr.main()

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "API呼び出しに失敗しました（HTTP 403）" in err
    assert "api_key の権限を確認してください" in err


def test_run_clamps_max_workers(tmp_path, stub, capsys):
    stub()
    out = tmp_path / "out"
    config = write_config(tmp_path, out, report={"max_workers": 99})

    bwr.run(["--config", str(config)])

    assert "max_workers は 1〜8 の範囲で指定してください" in capsys.readouterr().err
    assert (out / PERIOD_DIR / "weekly_report.md").exists()


def test_run_debug_prints_request_parameters(tmp_path, stub, capsys):
    stub()
    config = write_config(tmp_path, tmp_path / "out", filters=[{"name": "b", "issue_types": ["バグ"]}])

    bwr.run(["--config", str(config), "--debug"])

    err = capsys.readouterr().err
    assert "[DEBUG] 解決済みフィルターパラメータ" in err
    assert "[DEBUG] 対象課題の絞り込み" in err
    assert "REAL_KEY" not in err          # API キーは出力しない


def test_run_continues_when_status_list_fails(tmp_path, monkeypatch, capsys):
    """
    ステータス一覧が取れない場合は絞り込みを諦めて全件取得し、処理は続行する。

    完了系の集合が空になるため、すべてオープン系として扱われる。
    ④は0件になり、対象の課題はすべて⑤に積み上がる。
    レポートに警告は出ないので、実行ログの警告だけが手掛かりになる。
    この「気づきにくさ」を記録しておく。
    """
    monkeypatch.setattr(bwr, "BacklogClient", make_stub_client(fail_on="/statuses"))
    out = tmp_path / "out"
    config = write_config(tmp_path, out)

    bwr.run(["--config", str(config)])

    err = capsys.readouterr().err
    assert "ステータス一覧の取得に失敗しました（PRJ）" in err
    assert "api_key の権限を確認してください" in err

    text = (out / PERIOD_DIR / "weekly_report.md").read_text(encoding="utf-8")
    # 完了系が空になるため、すべてオープン系として扱われる
    assert "| ① 前週残件数 | **2** 件 |" in text
    assert "| ② 新規発生件数 | **1** 件 |" in text
    assert "| ③ 再オープン件数 | **0** 件 |" in text
    assert "| ④ 当週完了件数 | **0** 件 |" in text   # 完了系が無いので完了を判定できない
    assert "| ⑤ 当週未完了件数 | **3** 件 |" in text


def test_run_continues_when_custom_field_master_fails(tmp_path, monkeypatch, capsys):
    """カスタム属性マスターが取れなくても処理を止めないこと"""
    monkeypatch.setattr(bwr, "BacklogClient", make_stub_client(fail_on="/customFields"))
    out = tmp_path / "out"
    filters = [{"name": "属性なし",
                "custom_fields": [{"field_name": "対応チーム", "values": ["Aチーム"]}]}]
    config = write_config(tmp_path, out, filters=filters)

    bwr.run(["--config", str(config)])

    err = capsys.readouterr().err
    assert "カスタム属性マスターの取得に失敗" in err
    assert "カスタム属性「対応チーム」が見つかりません" in err
    assert (out / PERIOD_DIR / "weekly_report_属性なし.md").exists()


def test_run_resolves_relative_output_dir_against_script(tmp_path, stub, monkeypatch):
    """output_dir が相対パスならスクリプトのある場所を基準に解決すること"""
    stub()
    monkeypatch.setattr(bwr, "__file__", str(tmp_path / "backlog_weekly_report.py"))
    config = write_config(tmp_path, Path("./out"))   # 相対パスで指定

    bwr.run(["--config", str(config)])

    assert (tmp_path / "out" / PERIOD_DIR / "weekly_report.md").exists()


# ==================================================================
# 次回照合用のスナップショット
# ==================================================================

def read_snapshot(out: Path) -> dict:
    return json.loads((out / PERIOD_DIR / bwr.SNAPSHOT_FILENAME).read_text(encoding="utf-8"))


def test_snapshot_written_without_filters(tmp_path, stub):
    """フィルターなしでもスナップショットを残すこと"""
    stub()
    out = tmp_path / "out"
    bwr.run(["--config", str(write_config(tmp_path, out))])

    snap = read_snapshot(out)
    assert snap["version"] == bwr.SNAPSHOT_VERSION
    assert snap["period"] == {"from": "2026-03-02", "to": "2026-03-08"}
    assert [f["name"] for f in snap["filters"]] == [bwr.NO_FILTER_NAME]
    keys = sorted(i["issueKey"] for i in snap["filters"][0]["incomplete"])
    assert keys == ["PRJ-1", "PRJ-3"]


def test_snapshot_records_each_filter_with_its_condition(tmp_path, stub):
    """フィルターごとに⑤の課題と絞り込み条件を残すこと"""
    stub()
    out = tmp_path / "out"
    bwr.run(["--config", str(write_config(tmp_path, out, filters=FILTERS))])

    snap = read_snapshot(out)
    assert [f["name"] for f in snap["filters"]] == ["バグ対応", "A/チーム"]
    assert snap["filters"][0]["condition"] == "種別: バグ"
    assert snap["filters"][1]["condition"] == "対応チーム: Aチーム"


def test_snapshot_entry_holds_display_fields(tmp_path, stub):
    """次回そのまま表示できるだけの情報を持つこと（再取得を不要にする）"""
    stub()
    out = tmp_path / "out"
    bwr.run(["--config", str(write_config(tmp_path, out))])

    entry = next(i for i in read_snapshot(out)["filters"][0]["incomplete"]
                 if i["issueKey"] == "PRJ-1")
    assert entry == {
        "id": 1, "issueKey": "PRJ-1", "summary": "残件1",
        "status": "処理中", "dueDate": "2026-03-10T00:00:00Z", "assignee": None,
    }


def test_snapshot_is_valid_json_utf8(tmp_path, stub):
    """日本語がエスケープされずそのまま読めること"""
    stub()
    out = tmp_path / "out"
    bwr.run(["--config", str(write_config(tmp_path, out, filters=FILTERS))])

    raw = (out / PERIOD_DIR / bwr.SNAPSHOT_FILENAME).read_text(encoding="utf-8")
    assert "バグ対応" in raw
    assert raw.endswith("\n")


# ==================================================================
# 抽出対象への出入り（流入・流出）
# ==================================================================

PREV_DIR = "20260223_20260301"      # 直前の期間（2/23〜3/1）


def write_prev_snapshot(out: Path, filters: list) -> None:
    """直前の期間のスナップショットを用意する。filters: [(名前, 条件, [課題])]"""
    (out / PREV_DIR).mkdir(parents=True, exist_ok=True)
    snapshot = {
        "version": bwr.SNAPSHOT_VERSION,
        "period": {"from": "2026-02-23", "to": "2026-03-01"},
        "filters": [
            {"name": name, "condition": cond,
             "incomplete": [bwr._snapshot_entry(i) for i in issues]}
            for name, cond, issues in filters
        ],
    }
    (out / PREV_DIR / bwr.SNAPSHOT_FILENAME).write_text(
        json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")


def test_outflow_is_counted_as_completed(tmp_path, stub, capsys):
    """前回⑤に居たのに今回の母集団から消えた課題は ④ に入ること"""
    gone = issue(99, "処理中", "2026-01-05T02:00:00Z", "2026-02-20T02:00:00Z", "対象から外れた")
    out = tmp_path / "out"
    write_prev_snapshot(out, [(bwr.NO_FILTER_NAME, "", DEFAULT_ISSUES + [gone])])
    stub()   # 今回の母集団に PRJ-99 は含まれない

    bwr.run(["--config", str(write_config(tmp_path, out))])

    text = (out / PERIOD_DIR / "weekly_report.md").read_text(encoding="utf-8")
    assert "| ① 前週残件数 | **3** 件 |" in text     # PRJ-1, PRJ-2 に PRJ-99 が加わる
    assert "| ④ 当週完了件数 | **2** 件 |" in text   # PRJ-2 に PRJ-99 が加わる
    assert "| ⑤ 当週未完了件数 | **2** 件 |" in text  # PRJ-99 は⑤に残らない
    assert "期間中に対象から外れた **1** 件は ④ 当週完了に含めています" in text
    assert "PRJ-99" in text
    assert "対象から外れた99" in text                 # 再取得せず前回の情報で表示できる
    assert "流出 1 件 → ④" in capsys.readouterr().out


def test_inflow_is_counted_as_new(tmp_path, stub):
    """前回⑤に居なかったのに今回①に該当する課題は ② に移ること"""
    out = tmp_path / "out"
    # 前回⑤には PRJ-1 だけ。PRJ-2 は期間中に対象へ入ったとみなされる
    write_prev_snapshot(out, [(bwr.NO_FILTER_NAME, "", [DEFAULT_ISSUES[0]])])
    stub()

    bwr.run(["--config", str(write_config(tmp_path, out))])

    text = (out / PERIOD_DIR / "weekly_report.md").read_text(encoding="utf-8")
    assert "| ① 前週残件数 | **1** 件 |" in text     # PRJ-1 のみ
    assert "| ② 新規発生件数 | **2** 件 |" in text   # PRJ-3 に PRJ-2 が加わる
    assert "期間中に対象へ入った **1** 件は ② 新規発生に含めています" in text


def test_equation_holds_with_flows(tmp_path, stub):
    """流入・流出を反映しても ①+②+③ = ④+⑤ が成立すること"""
    gone = issue(99, "処理中", "2026-01-05T02:00:00Z", "2026-02-20T02:00:00Z", "対象外へ")
    out = tmp_path / "out"
    write_prev_snapshot(out, [(bwr.NO_FILTER_NAME, "", [DEFAULT_ISSUES[0], gone])])
    stub()

    bwr.run(["--config", str(write_config(tmp_path, out))])

    text = (out / PERIOD_DIR / "weekly_report.md").read_text(encoding="utf-8")
    assert "一致しません" not in text


def test_previous_period_must_be_adjacent(tmp_path, stub):
    """直前の期間でないフォルダのスナップショットは使わないこと"""
    out = tmp_path / "out"
    (out / "20260101_20260107").mkdir(parents=True)
    (out / "20260101_20260107" / bwr.SNAPSHOT_FILENAME).write_text("{}", encoding="utf-8")
    stub()

    bwr.run(["--config", str(write_config(tmp_path, out))])

    text = (out / PERIOD_DIR / "weekly_report.md").read_text(encoding="utf-8")
    assert "抽出対象への出入りを判定していない" in text


def test_changed_filter_condition_disables_flow_detection(tmp_path, stub, capsys):
    """絞り込み条件が前回と違う場合は出入りを判定しないこと"""
    out = tmp_path / "out"
    write_prev_snapshot(out, [("バグ対応", "種別: 別のなにか", DEFAULT_ISSUES)])
    stub()

    bwr.run(["--config", str(write_config(tmp_path, out, filters=[FILTERS[0]]))])

    text = (out / PERIOD_DIR / "weekly_report_バグ対応.md").read_text(encoding="utf-8")
    assert "絞り込み条件が前回と異なる" in text


def test_broken_snapshot_is_ignored(tmp_path, stub):
    """壊れたスナップショットがあっても処理を止めないこと"""
    out = tmp_path / "out"
    (out / PREV_DIR).mkdir(parents=True)
    (out / PREV_DIR / bwr.SNAPSHOT_FILENAME).write_text("{ こわれている", encoding="utf-8")
    stub()

    bwr.run(["--config", str(write_config(tmp_path, out))])

    text = (out / PERIOD_DIR / "weekly_report.md").read_text(encoding="utf-8")
    assert "読み込めませんでした" in text
    assert "| ① 前週残件数 | **2** 件 |" in text   # 現行どおり集計は続く


def _write_snapshot_at(out: Path, folder: str, period: dict, issues: list) -> None:
    (out / folder).mkdir(parents=True, exist_ok=True)
    (out / folder / bwr.SNAPSHOT_FILENAME).write_text(json.dumps({
        "version": bwr.SNAPSHOT_VERSION,
        "period": period,
        "filters": [{"name": bwr.NO_FILTER_NAME, "condition": "",
                     "incomplete": [bwr._snapshot_entry(i) for i in issues]}],
    }, ensure_ascii=False), encoding="utf-8")


def test_picks_latest_start_when_end_dates_collide(tmp_path, stub, capsys):
    """
    終了日が同じフォルダが複数ある場合、開始日が最も遅いものを使うこと。

    フォルダの列挙順はファイルシステム依存なので、決め方を固定しておかないと
    実行のたびに結果が変わりうる。
    """
    out = tmp_path / "out"
    gone = issue(99, "処理中", "2026-01-05T02:00:00Z", "2026-02-20T02:00:00Z", "採用されるべき")
    other = issue(98, "処理中", "2026-01-05T02:00:00Z", "2026-02-20T02:00:00Z", "採用されないべき")
    # どちらも 3/1 終わり。開始日が遅いのは 2/23 のほう
    _write_snapshot_at(out, "20260220_20260301", {"from": "2026-02-20", "to": "2026-03-01"}, [other])
    _write_snapshot_at(out, "20260223_20260301", {"from": "2026-02-23", "to": "2026-03-01"}, [gone])
    stub()

    bwr.run(["--config", str(write_config(tmp_path, out))])

    text = (out / PERIOD_DIR / "weekly_report.md").read_text(encoding="utf-8")
    assert "PRJ-99" in text          # 開始日が遅いほうの⑤が流出として反映される
    assert "PRJ-98" not in text
    assert "終了日が同じ期間フォルダが複数あります" in capsys.readouterr().err


def test_single_candidate_produces_no_warning(tmp_path, stub, capsys):
    out = tmp_path / "out"
    _write_snapshot_at(out, PREV_DIR, {"from": "2026-02-23", "to": "2026-03-01"}, DEFAULT_ISSUES)
    stub()

    bwr.run(["--config", str(write_config(tmp_path, out))])

    assert "終了日が同じ期間フォルダが複数あります" not in capsys.readouterr().err
