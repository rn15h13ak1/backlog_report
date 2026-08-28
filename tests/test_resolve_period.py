"""
集計期間の決定ロジックのテスト。

期間は4通りの指定方法があり、優先順位が決まっている。

    --from/--to  >  --week  >  config.report.period  >  config.report.target_week

ここが狂うとエラーは出ないまま「意図と違う期間のレポート」が出来上がる。
数字自体は正しく見えるため気づきにくい種類の間違いなので、
組み合わせを一通り固定しておく。

前提: resolve_period は --from と --to が揃っていることを前提とする。
      片方だけの指定は run() が argparse の段階で弾く。
"""
from datetime import date, datetime

import pytest

import backlog_weekly_report as bwr

# 基準日は 2026-03-05（木曜）
TODAY = date(2026, 3, 5)


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    """get_week_range が参照する「今日」を固定する"""
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 3, 5, 12, 0, tzinfo=tz)

    monkeypatch.setattr(bwr, "datetime", FrozenDatetime)


def resolve(argv: list, report_cfg: dict | None = None):
    """引数と設定から (開始日, 終了日, ラベル) を求める"""
    parser = bwr.build_arg_parser()
    args = parser.parse_args(argv)
    return bwr.resolve_period(args, report_cfg or {}, parser)


CONFIG_PERIOD = {"period": {"from": "2026-01-05", "to": "2026-01-11"}}
FULL_CONFIG = {**CONFIG_PERIOD, "target_week": "current", "week_start": "sunday"}


# ==================================================================
# 優先順位
# ==================================================================

def test_args_win_over_everything():
    """--from/--to は config の period も target_week も上書きする"""
    start, end, label = resolve(["--from", "2026-02-01", "--to", "2026-02-28"], FULL_CONFIG)
    assert (start, end) == (date(2026, 2, 1), date(2026, 2, 28))
    assert label == "指定期間（引数）"


def test_week_option_wins_over_config_period():
    """--week は config の period より優先される"""
    start, end, label = resolve(["--week", "previous"], CONFIG_PERIOD)
    assert (start, end) == (date(2026, 2, 23), date(2026, 3, 1))   # 前週（月〜日）
    assert label == "前週"


def test_config_period_used_when_no_args():
    start, end, label = resolve([], FULL_CONFIG)
    assert (start, end) == (date(2026, 1, 5), date(2026, 1, 11))
    assert label == "指定期間（config）"


def test_target_week_used_when_no_period():
    """period が無ければ target_week の自動計算に落ちる"""
    start, end, label = resolve([], {"target_week": "previous", "week_start": "monday"})
    assert (start, end) == (date(2026, 2, 23), date(2026, 3, 1))
    assert label == "前週"


def test_defaults_to_previous_week_when_config_is_empty():
    """設定が空でも前週・月曜始まりで動くこと"""
    start, end, label = resolve([], {})
    assert (start, end) == (date(2026, 2, 23), date(2026, 3, 1))
    assert label == "前週"


# ==================================================================
# 各分岐の中身
# ==================================================================

def test_week_current_ends_today():
    start, end, label = resolve(["--week", "current"], {"week_start": "monday"})
    assert (start, end) == (date(2026, 3, 2), TODAY)
    assert label == "今週"


def test_week_start_setting_is_respected():
    """week_start: sunday なら日曜始まりで週を切る"""
    start, end, _ = resolve(["--week", "previous"], {"week_start": "sunday"})
    assert (start, end) == (date(2026, 2, 22), date(2026, 2, 28))


def test_target_week_current_label():
    _, _, label = resolve([], {"target_week": "current"})
    assert label == "今週"


def test_single_day_period_is_allowed():
    start, end, _ = resolve(["--from", "2026-03-03", "--to", "2026-03-03"], {})
    assert start == end == date(2026, 3, 3)


# ==================================================================
# config.period の扱い
# ==================================================================

def test_config_period_accepts_yaml_date_objects():
    """
    YAML は引用符なしの 2026-01-05 を date オブジェクトとして読み込む。
    文字列でも date でも同じ結果になること。
    """
    cfg = {"period": {"from": date(2026, 1, 5), "to": date(2026, 1, 11)}}
    start, end, _ = resolve([], cfg)
    assert (start, end) == (date(2026, 1, 5), date(2026, 1, 11))


@pytest.mark.parametrize("period", [
    {"from": "2026-01-05"},                 # to が無い
    {"to": "2026-01-11"},                   # from が無い
    {"from": "", "to": "2026-01-11"},       # from が空
    {},                                     # 両方無い
])
def test_incomplete_config_period_falls_through(period):
    """period が揃っていなければ target_week の自動計算に落ちる"""
    start, end, label = resolve([], {"period": period, "target_week": "previous"})
    assert (start, end) == (date(2026, 2, 23), date(2026, 3, 1))
    assert label == "前週"


# ==================================================================
# 異常系
# ==================================================================

def test_invalid_arg_date_format_exits():
    with pytest.raises(SystemExit):
        resolve(["--from", "2026/03/01", "--to", "2026-03-31"], {})


def test_reversed_arg_dates_exit():
    with pytest.raises(SystemExit):
        resolve(["--from", "2026-03-31", "--to", "2026-03-01"], {})


def test_invalid_config_period_format_exits():
    with pytest.raises(SystemExit):
        resolve([], {"period": {"from": "2026年1月5日", "to": "2026-01-11"}})


def test_reversed_config_period_exits():
    with pytest.raises(SystemExit):
        resolve([], {"period": {"from": "2026-01-11", "to": "2026-01-05"}})


def test_invalid_week_start_exits():
    with pytest.raises(SystemExit):
        resolve(["--week", "previous"], {"week_start": "someday"})


def test_parser_rejects_from_without_to():
    """--from だけの指定は argparse の choices/検証ではなく run() が弾く"""
    parser = bwr.build_arg_parser()
    args = parser.parse_args(["--from", "2026-03-01"])
    assert args.date_from == "2026-03-01"
    assert args.date_to is None      # この状態で resolve_period を呼んではいけない


def test_parser_rejects_unknown_week_value():
    with pytest.raises(SystemExit):
        bwr.build_arg_parser().parse_args(["--week", "nextweek"])
