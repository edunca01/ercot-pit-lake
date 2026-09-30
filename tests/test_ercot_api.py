"""The ERCOT client against a fake transport: auth, paging, pacing, retries, secrets, and the
archive listing across the fall-back night."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

import ingest
from ercot_lake.timeutil import utc_to_ct
from ingest.config import AuthConfig, Credentials, ErcotConfig
from ingest.ercot_api import DocumentNotReadyError, ErcotClient, RetriesExhaustedError

Handler = Callable[[httpx.Request], httpx.Response]

CFG = ErcotConfig(
    api_base="https://api.test/public-reports",
    archive_base="https://api.test/public-reports/archive",
    auth=AuthConfig(token_url="https://auth.test/token", client_id="cid", scope="openid"),
    page_size=2,
    timeout_s=5,
    max_retries=2,
    live_max_retries=1,
)
PASSWORD, SUB_KEY, TOKEN = "pw-s3cret-value", "sub-k3y-value", "tok-3xyz-value"
CREDS = Credentials(username="u", password=SecretStr(PASSWORD), subscription_key=SecretStr(SUB_KEY))


def _client(handler: Handler, cfg: ErcotConfig = CFG, *, live: bool = False) -> ErcotClient:
    c = ErcotClient(cfg, CREDS, live=live)
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    return c


def _authed(api: Handler) -> Handler:
    """Answer the token endpoint, pass everything else to ``api``."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "auth.test":
            return httpx.Response(200, json={"id_token": TOKEN, "expires_in": 3600})
        return api(req)

    return handler


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    out: list[float] = []
    monkeypatch.setattr("ingest.ercot_api.time.sleep", out.append)
    return out


# -- auth and paging -----------------------------------------------------------------------


def test_token_then_paging_with_headers() -> None:
    calls: list[httpx.Request] = []

    def api(req: httpx.Request) -> httpx.Response:
        page = int(req.url.params["page"])
        return httpx.Response(200, json={"data": [[page]], "_meta": {"totalPages": 3}})

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return _authed(api)(req)

    with _client(handler) as c:
        pages = list(c.iter_pages("/np6-905-cd/spp_node_zone_hub", {"deliveryDateFrom": "x"}))

    assert [p["data"][0][0] for p in pages] == [1, 2, 3]
    token_calls = [r for r in calls if r.url.host == "auth.test"]
    assert len(token_calls) == 1, "token fetched once and reused"
    body = dict(httpx.QueryParams(token_calls[0].content.decode()))
    assert (body["grant_type"], body["username"]) == ("password", "u")
    api_call = calls[1]
    assert api_call.headers["Authorization"] == f"Bearer {TOKEN}"
    assert api_call.headers["Ocp-Apim-Subscription-Key"] == SUB_KEY
    assert api_call.url.params["size"] == "2"


def test_401_refreshes_the_token_once(sleeps: list[float]) -> None:
    tokens = iter(["old", "new"])
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "auth.test":
            return httpx.Response(200, json={"id_token": next(tokens)})
        seen.append(req.headers["Authorization"])
        return httpx.Response(401 if len(seen) == 1 else 200, json={"ok": True})

    with _client(handler) as c:
        assert c.get("/x") == {"ok": True}
    assert seen == ["Bearer old", "Bearer new"]
    assert sleeps == []


# -- retries and back-off ------------------------------------------------------------------


def test_429_backs_off_then_succeeds(sleeps: list[float]) -> None:
    attempts = {"n": 0}

    def api(req: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"data": []})

    with _client(_authed(api)) as c:
        assert c.get("/x") == {"data": []}
    assert sleeps == [7.0, 7.0]


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_is_retried(status: int, sleeps: list[float]) -> None:
    replies = iter([httpx.Response(status), httpx.Response(200, json={"ok": 1})])
    with _client(_authed(lambda _: next(replies))) as c:
        assert c.get("/x") == {"ok": 1}
    assert sleeps == [1.0]


def test_a_302_back_to_the_same_url_is_retried(sleeps: list[float]) -> None:
    replies = iter([None, httpx.Response(200, json={"ok": 1})])

    def api(req: httpx.Request) -> httpx.Response:
        reply = next(replies)
        return reply or httpx.Response(302, headers={"Location": str(req.url)})

    with _client(_authed(api)) as c:
        assert c.get("/x", {"page": 1}) == {"ok": 1}
    assert sleeps == [1.0]


def test_a_302_elsewhere_is_not_retried(sleeps: list[float]) -> None:
    api = _authed(lambda _: httpx.Response(302, headers={"Location": "https://elsewhere/"}))
    with _client(api) as c, pytest.raises(httpx.HTTPStatusError):
        c.get("/x")
    assert sleeps == []


def test_transport_errors_are_retried(sleeps: list[float]) -> None:
    attempts = {"n": 0}

    def api(req: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(200, json={"ok": 1})

    with _client(_authed(api)) as c:
        assert c.get("/x") == {"ok": 1}
    assert sleeps == [1.0]


def test_gives_up_after_max_retries_without_a_last_sleep(sleeps: list[float]) -> None:
    with (
        _client(_authed(lambda _: httpx.Response(503))) as c,
        pytest.raises(RetriesExhaustedError, match=r"after 3 attempts \(last: 503\)") as err,
    ):
        c.get("/x")
    assert err.value.last_status == 503
    assert sleeps == [1.0, 2.0]  # waits between attempts, none after the last


def test_live_runs_give_up_sooner(sleeps: list[float]) -> None:
    attempts = {"n": 0}

    def api(req: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(429)

    with _client(_authed(api), live=True) as c, pytest.raises(RetriesExhaustedError):
        c.get("/x")
    assert attempts["n"] == CFG.live_max_retries + 1
    attempts["n"] = 0
    with _client(_authed(api), live=False) as c, pytest.raises(RetriesExhaustedError):
        c.get("/x")
    assert attempts["n"] == CFG.max_retries + 1


def test_429_backoff_grows_past_small_retry_after(sleeps: list[float]) -> None:
    """ERCOT hints 1-6 s while still refusing; the wait must grow (capped) instead."""
    cfg = CFG.model_copy(update={"max_retries": 8})
    api = _authed(lambda _: httpx.Response(429, headers={"Retry-After": "1"}))
    with _client(api, cfg) as c, pytest.raises(RetriesExhaustedError, match="gave up"):
        c.get("/x")
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


def test_retry_after_as_a_date_falls_back_to_backoff(sleeps: list[float]) -> None:
    replies = iter(
        [
            httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            httpx.Response(200, json={}),
        ]
    )
    with _client(_authed(lambda _: next(replies))) as c:
        c.get("/x")
    assert sleeps == [1.0]


def test_pacing_sleeps_between_requests(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    clock = iter([0.0, 0.0, 0.1, 0.1, 0.2, 0.2, 100.0, 100.0])
    monkeypatch.setattr("ingest.ercot_api.time.monotonic", lambda: next(clock))
    paced = CFG.model_copy(update={"min_interval_s": 1.0})
    with _client(_authed(lambda _: httpx.Response(200, json={})), paced) as c:
        c.get("/a")
        c.get("/b")
    assert sleeps
    assert all(0 < s <= 1.0 for s in sleeps)


def test_nothing_else_in_the_pipeline_sleeps() -> None:
    root = Path(ingest.__file__).parent
    offenders = [
        f"{p.relative_to(root)}:{i}"
        for p in sorted(root.rglob("*.py"))
        if p.name != "ercot_api.py"
        for i, line in enumerate(p.read_text().splitlines(), 1)
        if re.search(r"\bsleep\(", line)
    ]
    assert offenders == [], "only the ERCOT client may wait on the network"


# -- archives ------------------------------------------------------------------------------


def test_archive_download_uses_download_param() -> None:
    def api(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/archive/NP6-905-CD")
        if "download" in req.url.params:
            return httpx.Response(200, content=b"PK\x03\x04zip")
        return httpx.Response(200, json={"archives": [{"docId": 42}]})

    with _client(_authed(api)) as c:
        listing = c.list_archives("NP6-905-CD")
        blob = c.download_archive("NP6-905-CD", listing["archives"][0]["docId"])
    assert blob.startswith(b"PK")


@pytest.mark.parametrize("status", [400, 404])
def test_download_not_ready_is_typed(status: int) -> None:
    with (
        _client(_authed(lambda _: httpx.Response(status))) as c,
        pytest.raises(DocumentNotReadyError, match="not downloadable yet"),
    ):
        c.download_archive("NP6-905-CD", 1274522234)


def test_download_other_errors_propagate(sleeps: list[float]) -> None:
    with (
        _client(_authed(lambda _: httpx.Response(403))) as c,
        pytest.raises(httpx.HTTPStatusError),
    ):
        c.download_archive("NP6-905-CD", 1)
    with (
        _client(_authed(lambda _: httpx.Response(500))) as c,
        pytest.raises(RetriesExhaustedError),
    ):
        c.download_archive("NP6-905-CD", 1)


def test_bundles() -> None:
    def api(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/bundle/np6-905-cd")
        if "download" in req.url.params:
            return httpx.Response(200, content=b"PK")
        return httpx.Response(200, json={"bundles": []})

    with _client(_authed(api)) as c:
        assert c.list_bundles("np6-905-cd") == {"bundles": []}
        assert c.download_bundle("np6-905-cd", 7) == b"PK"


def test_probe_reports_status_without_raising(sleeps: list[float]) -> None:
    replies = {"/ok": 200, "/missing": 404, "/busy": 429}

    def api(req: httpx.Request) -> httpx.Response:
        return httpx.Response(replies[req.url.path.removeprefix("/public-reports")], json={})

    with _client(_authed(api)) as c:
        assert [c.probe(p) for p in ("/ok", "/missing", "/busy")] == [200, 404, 429]


def test_iter_archive_docs_filters_window_and_sorts_ascending() -> None:
    listing_pages = {
        1: {
            "_meta": {"totalPages": 2},
            "archives": [
                {"docId": 3, "postDatetime": "2026-09-03T00:32:01.000", "friendlyName": "c"},
                {"docId": 2, "postDatetime": "2026-09-03T00:17:01.000", "friendlyName": "b"},
            ],
        },
        2: {
            "_meta": {"totalPages": 2},
            "archives": [
                {"docId": 1, "postDatetime": "2026-09-03T00:02:01.000", "friendlyName": "a"},
                {"docId": 0, "postDatetime": "2026-09-02T23:47:01.000", "friendlyName": "old"},
            ],
        },
    }
    seen: list[dict[str, str]] = []

    def api(req: httpx.Request) -> httpx.Response:
        seen.append(dict(req.url.params))
        return httpx.Response(200, json=listing_pages[int(req.url.params["page"])])

    # window: (00:02:01 CT, 00:32:01 CT] -> excludes doc 1 (== from) and doc 0; keeps 2, 3
    post_from = datetime(2026, 9, 3, 5, 2, 1, tzinfo=UTC)
    post_to = datetime(2026, 9, 3, 5, 32, 1, tzinfo=UTC)
    with _client(_authed(api)) as c:
        docs = list(c.iter_archive_docs("NP6-905-CD", post_from, post_to))

    assert [d.doc_id for d in docs] == [2, 3]
    assert docs[0].posted_at == datetime(2026, 9, 3, 5, 17, 1, tzinfo=UTC)
    # the wall-clock query starts an hour early; the exact window is applied in UTC
    assert seen[0]["postDatetimeFrom"] == "2026-09-02T23:02:01"
    assert seen[0]["postDatetimeTo"] == "2026-09-03T00:32:01"
    assert seen[0]["size"] == "2"


# -- the fall-back night -------------------------------------------------------------------


def _ercot_night(every_min: int, second: int = 20) -> list[tuple[int, str, datetime]]:
    """Postings every ``every_min`` minutes through 2026-11-01 00:00-03:00 CT as ERCOT lists
    them: (docId, postDatetime on the wall clock without a flag, the true UTC instant)."""
    start = datetime(2026, 11, 1, 5, 0, second, tzinfo=UTC)  # 00:00 CDT
    out = []
    for i in range(0, 4 * 60, every_min):  # 4 UTC hours = 3 wall-clock hours + the repeat
        utc = start + timedelta(minutes=i)
        out.append((1_000 + i, utc_to_ct(utc).strftime("%Y-%m-%dT%H:%M:%S.000"), utc))
    return out


def _wall_clock_server(night: list[tuple[int, str, datetime]]) -> Handler:
    """ERCOT's listing: filter on wall-clock text, newest first, no DST flag."""

    def api(req: httpx.Request) -> httpx.Response:
        lo, hi = req.url.params["postDatetimeFrom"], req.url.params["postDatetimeTo"]
        rows = [
            {"docId": doc, "postDatetime": wall, "friendlyName": str(doc)}
            for doc, wall, _ in sorted(night, reverse=True)
            if lo <= wall[:19] <= hi
        ]
        return httpx.Response(200, json={"archives": rows, "_meta": {"totalPages": 1}})

    return _authed(api)


@pytest.mark.parametrize("every_min", [5, 15, 60], ids=["5-min", "15-min", "hourly"])
def test_fall_back_night_live_window_inside_the_repeat(every_min: int) -> None:
    """Watermark in the first 01:xx (CDT), now in the second (CST). On the wall clock the
    window runs backwards; without the fix ERCOT lists nothing and the hour is lost."""
    night = _ercot_night(every_min, second=0)
    cfg = CFG.model_copy(update={"page_size": 1000})
    post_from = datetime(2026, 11, 1, 6, 55, tzinfo=UTC)  # 01:55 CDT
    post_to = datetime(2026, 11, 1, 7, 55, tzinfo=UTC)  # 01:55 CST
    with _client(_wall_clock_server(night), cfg) as c:
        docs = list(c.iter_archive_docs("NP6-322-CD", post_from, post_to))
    expected = [utc for _, _, utc in night if post_from < utc <= post_to]
    assert expected, "the window must contain second-pass postings"
    assert [d.posted_at for d in docs] == expected


@pytest.mark.parametrize("every_min", [5, 15, 60], ids=["5-min", "15-min", "hourly"])
def test_fall_back_night_backfill_sees_every_posting_once(every_min: int) -> None:
    night = _ercot_night(every_min)
    cfg = CFG.model_copy(update={"page_size": 1000})
    post_from, post_to = (
        datetime(2026, 11, 1, 4, 0, tzinfo=UTC),
        datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
    )
    with _client(_wall_clock_server(night), cfg) as c:
        docs = list(c.iter_archive_docs("NP6-322-CD", post_from, post_to))
    assert [d.posted_at for d in docs] == [utc for _, _, utc in night]
    assert len({d.posted_at for d in docs}) == len(docs), "two postings share a UTC stamp"


# -- secrets -------------------------------------------------------------------------------


def test_secrets_never_reach_logs_or_repr(
    caplog: pytest.LogCaptureFixture, sleeps: list[float]
) -> None:
    caplog.set_level(logging.DEBUG)
    replies = iter(
        [
            httpx.Response(401),
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(503),
            httpx.Response(200, json={"ok": 1}),
            httpx.Response(403),
        ]
    )
    roomy = CFG.model_copy(update={"max_retries": 5})  # 401, 429, 503, then 200
    with _client(_authed(lambda _: next(replies)), roomy) as c:
        c.get("/x", {"deliveryDateFrom": "2026-09-03"})
        with pytest.raises(httpx.HTTPStatusError) as err:
            c.get("/y")
        rendered = [repr(c), str(err.value), *(r.getMessage() for r in caplog.records)]
    assert caplog.records, "the retries above must have logged something"
    for text in rendered:
        for secret in (PASSWORD, SUB_KEY, TOKEN):
            assert secret not in text
