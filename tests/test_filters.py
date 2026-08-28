"""resolve_filter_params / build_filter_summary のテスト"""
from backlog_weekly_report import build_filter_summary, resolve_filter_params

ISSUE_TYPES = {"バグ": 101, "タスク": 102, "要望": 103}

CUSTOM_FIELDS = {
    # typeId 5 = 単一リスト（選択肢名 → ID 変換が必要）
    "対応チーム": {"id": 500, "typeId": 5, "items": {"Aチーム": 11, "Bチーム": 12}},
    # typeId 1 = テキスト（変換不要）
    "備考": {"id": 600, "typeId": 1, "items": {}},
}


def test_keyword_only():
    assert resolve_filter_params({"keyword": "【障害】"}, {}, {}) == {"keyword": "【障害】"}


def test_issue_types_resolved_to_ids():
    result = resolve_filter_params({"issue_types": ["バグ", "タスク"]}, ISSUE_TYPES, {})
    assert result == {"issueTypeId": [101, 102]}


def test_unknown_issue_type_is_skipped():
    result = resolve_filter_params({"issue_types": ["バグ", "存在しない"]}, ISSUE_TYPES, {})
    assert result == {"issueTypeId": [101]}


def test_all_issue_types_unknown_omits_param():
    assert resolve_filter_params({"issue_types": ["存在しない"]}, ISSUE_TYPES, {}) == {}


def test_list_type_custom_field_converts_names_to_ids():
    """typeId 5〜8 は選択肢名を数値IDに変換してリスト型で送る"""
    cfg = {"custom_fields": [{"field_name": "対応チーム", "values": ["Aチーム"]}]}
    assert resolve_filter_params(cfg, {}, CUSTOM_FIELDS) == {"customField_500": [11]}


def test_list_type_with_multiple_values():
    cfg = {"custom_fields": [{"field_name": "対応チーム", "values": ["Aチーム", "Bチーム"]}]}
    assert resolve_filter_params(cfg, {}, CUSTOM_FIELDS) == {"customField_500": [11, 12]}


def test_scalar_type_custom_field_sent_as_single_value():
    """typeId 1〜4（テキスト等）で値が1つなら単一値で送る"""
    cfg = {"custom_fields": [{"field_name": "備考", "values": ["メモ"]}]}
    assert resolve_filter_params(cfg, {}, CUSTOM_FIELDS) == {"customField_600": "メモ"}


def test_scalar_type_with_multiple_values_becomes_list():
    cfg = {"custom_fields": [{"field_name": "備考", "values": ["A", "B"]}]}
    assert resolve_filter_params(cfg, {}, CUSTOM_FIELDS) == {"customField_600": ["A", "B"]}


def test_field_id_lookup_resolves_type_and_items():
    cfg = {"custom_fields": [{"field_id": 500, "values": ["Bチーム"]}]}
    assert resolve_filter_params(cfg, {}, CUSTOM_FIELDS) == {"customField_500": [12]}


def test_unknown_field_name_is_skipped():
    cfg = {"custom_fields": [{"field_name": "存在しない", "values": ["X"]}]}
    assert resolve_filter_params(cfg, {}, CUSTOM_FIELDS) == {}


def test_empty_values_is_skipped():
    cfg = {"custom_fields": [{"field_name": "対応チーム", "values": []}]}
    assert resolve_filter_params(cfg, {}, CUSTOM_FIELDS) == {}


def test_multiple_custom_fields_are_combined():
    cfg = {"custom_fields": [
        {"field_name": "対応チーム", "values": ["Aチーム"]},
        {"field_name": "備考", "values": ["メモ"]},
    ]}
    assert resolve_filter_params(cfg, {}, CUSTOM_FIELDS) == {
        "customField_500": [11],
        "customField_600": "メモ",
    }


def test_build_filter_summary():
    cfg = {
        "keyword": "【障害】",
        "issue_types": ["バグ"],
        "custom_fields": [{"field_name": "対応チーム", "values": ["Aチーム"]}],
    }
    summary = build_filter_summary(cfg)
    assert "件名キーワード: 【障害】" in summary
    assert "種別: バグ" in summary
    assert "対応チーム: Aチーム" in summary


def test_build_filter_summary_empty():
    assert build_filter_summary({}) == "（なし）"
