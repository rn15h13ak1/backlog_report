"""BacklogClient の URL 組み立て・リトライ・キャッシュのテスト"""
import email.message
import io
import json
import urllib.error
from datetime import date

import pytest

import backlog_weekly_report as bwr
from backlog_weekly_report import BacklogAPIError, BacklogClient


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def http_error(code: int, body: str = "", retry_after: str | None = None):
    headers = email.message.Message()
    if retry_after:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        "https://example.test", code, "err", headers, io.BytesIO(body.encode("utf-8"))
    )


@pytest.fixture
def no_sleep(monkeypatch):
    """リトライ待機で実際に待たないようにする"""
    monkeypatch.setattr(bwr.time, "sleep", lambda _s: None)


@pytest.fixture
def client():
    return BacklogClient("example.backlog.com", "SECRET", debug=False)


def patch_urlopen(monkeypatch, handler):
    """urlopen を差し替え、呼ばれた URL を記録するリストを返す"""
    calls = []

    def fake_urlopen(req, timeout=None, context=None):
        calls.append(req.full_url)
        return handler(len(calls) - 1)

    monkeypatch.setattr(bwr.urllib.request, "urlopen", fake_urlopen)
    return calls


# ------------------------------------------------------------------
# URL 組み立て
# ------------------------------------------------------------------

def test_base_url_normalizes_base_path():
    assert BacklogClient("h", "k").base_url == "https://h/api/v2"
    assert BacklogClient("h", "k", base_path="/backlog/").base_url == "https://h/backlog/api/v2"
    assert BacklogClient("h", "k", base_path="backlog").base_url == "https://h/backlog/api/v2"


def test_list_params_use_percent_encoded_brackets(client):
    url, _ = client._build_url("/issues", {"statusId": [1, 2]})
    assert "statusId%5B%5D=1" in url
    assert "statusId%5B%5D=2" in url
    assert "statusId[]" not in url


def test_api_key_is_appended_but_hidden_from_debug_parts(client):
    url, debug_parts = client._build_url("/space", {"count": 1})
    assert "apiKey=SECRET" in url
    assert all("apiKey" not in p for p in debug_parts)
    assert debug_parts == ["count=1"]


def test_build_url_does_not_mutate_caller_params(client):
    params = {"count": 1}
    client._build_url("/space", params)
    assert params == {"count": 1}


# ------------------------------------------------------------------
# リトライ
# ------------------------------------------------------------------

def test_retries_on_429_then_succeeds(client, monkeypatch, no_sleep):
    def handler(attempt):
        if attempt < 2:
            raise http_error(429, retry_after="1")
        return FakeResponse({"ok": True})

    calls = patch_urlopen(monkeypatch, handler)
    assert client._get("/space") == {"ok": True}
    assert len(calls) == 3


def test_retries_on_503(client, monkeypatch, no_sleep):
    def handler(attempt):
        if attempt == 0:
            raise http_error(503)
        return FakeResponse({"ok": True})

    calls = patch_urlopen(monkeypatch, handler)
    client._get("/space")
    assert len(calls) == 2


def test_gives_up_after_max_retries(client, monkeypatch, no_sleep):
    def handler(_attempt):
        raise http_error(429)

    calls = patch_urlopen(monkeypatch, handler)
    with pytest.raises(BacklogAPIError) as exc:
        client._get("/space")
    assert exc.value.status_code == 429
    assert len(calls) == bwr.API_MAX_RETRIES + 1


def test_does_not_retry_on_401(client, monkeypatch, no_sleep):
    def handler(_attempt):
        raise http_error(401, json.dumps({"errors": [{"message": "auth", "code": 11}]}))

    calls = patch_urlopen(monkeypatch, handler)
    with pytest.raises(BacklogAPIError) as exc:
        client._get("/space")
    assert exc.value.status_code == 401
    assert "auth" in exc.value.detail
    assert len(calls) == 1


def test_connection_error_is_retried_then_raised(client, monkeypatch, no_sleep):
    def handler(_attempt):
        raise urllib.error.URLError("no route to host")

    calls = patch_urlopen(monkeypatch, handler)
    with pytest.raises(BacklogAPIError) as exc:
        client._get("/space")
    assert exc.value.status_code is None
    assert len(calls) == bwr.API_MAX_RETRIES + 1


def test_error_message_never_contains_api_key(client, monkeypatch, no_sleep):
    def handler(_attempt):
        raise http_error(404)

    patch_urlopen(monkeypatch, handler)
    with pytest.raises(BacklogAPIError) as exc:
        client._get("/projects/NOPE")
    message = bwr.format_api_error(exc.value)
    assert "SECRET" not in message
    assert "project_key" in message


# ------------------------------------------------------------------
# get_issues / get_issue_comments
# ------------------------------------------------------------------

def test_get_issues_does_not_mutate_caller_params(client, monkeypatch, no_sleep):
    patch_urlopen(monkeypatch, lambda _a: FakeResponse([]))
    params = {"keyword": "x"}
    client.get_issues(1, params)
    assert params == {"keyword": "x"}


def test_get_issues_paginates(client, monkeypatch, no_sleep):
    page1 = [{"id": i} for i in range(bwr.API_PAGE_SIZE)]
    page2 = [{"id": 999}]
    patch_urlopen(monkeypatch, lambda a: FakeResponse(page1 if a == 0 else page2))
    assert len(client.get_issues(1)) == bwr.API_PAGE_SIZE + 1


def test_comments_are_cached(client, monkeypatch, no_sleep):
    calls = patch_urlopen(monkeypatch, lambda _a: FakeResponse([{"id": 1}]))
    first = client.get_issue_comments(42)
    second = client.get_issue_comments(42)
    assert first == second
    assert len(calls) == 1  # 2回目はキャッシュから返る


def test_comment_failure_is_recorded_and_not_cached(client, monkeypatch, no_sleep):
    state = {"fail": True}

    def handler(_attempt):
        if state["fail"]:
            raise http_error(500)
        return FakeResponse([{"id": 1}])

    patch_urlopen(monkeypatch, handler)
    assert client.get_issue_comments(7) == []
    assert 7 in client.comment_failures

    # 失敗した結果はキャッシュされないので、回復後は取得できる
    state["fail"] = False
    assert client.get_issue_comments(7) == [{"id": 1}]


# ------------------------------------------------------------------
# コメント取得のスキップ判定
# ------------------------------------------------------------------

def test_bulk_fetch_skips_issues_not_updated_in_period(client, monkeypatch, no_sleep):
    calls = patch_urlopen(monkeypatch, lambda _a: FakeResponse([]))
    issues = [
        {"id": 1, "created": "2025-01-01T00:00:00Z", "updated": "2025-06-01T00:00:00Z"},  # 期間前
        {"id": 2, "created": "2025-01-01T00:00:00Z", "updated": "2026-03-04T00:00:00Z"},  # 期間中
        {"id": 3, "created": "2025-01-01T00:00:00Z", "updated": "2026-05-01T00:00:00Z"},  # 期間後
    ]
    result = bwr._fetch_comments_bulk(client, issues, date(2026, 3, 2), max_workers=2)
    assert set(result) == {2, 3}
    assert len(calls) == 2


def test_bulk_fetch_includes_issue_updated_on_period_start(client, monkeypatch, no_sleep):
    patch_urlopen(monkeypatch, lambda _a: FakeResponse([]))
    issues = [{"id": 1, "created": "2025-01-01T00:00:00Z", "updated": "2026-03-02T00:00:00Z"}]
    result = bwr._fetch_comments_bulk(client, issues, date(2026, 3, 2), max_workers=2)
    assert set(result) == {1}


def test_bulk_fetch_falls_back_to_fetching_when_updated_missing(client, monkeypatch, no_sleep):
    patch_urlopen(monkeypatch, lambda _a: FakeResponse([]))
    issues = [{"id": 1}]
    result = bwr._fetch_comments_bulk(client, issues, date(2026, 3, 2), max_workers=2)
    assert set(result) == {1}
