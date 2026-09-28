"""Authenticated client for the ERCOT Public API.

Handles the B2C token flow, the subscription key header, pacing, retries with back-off,
pagination, and the archive listing that live ingest and backfill both read. This is the only
module that waits on the network: nothing else in the pipeline sleeps.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from ercot_lake.timeutil import utc_to_ct
from ingest.config import Credentials, ErcotConfig
from ingest.timeutil import is_repeated_local, local_to_utc, parse_post_local

log = logging.getLogger(__name__)

_TOKEN_REFRESH_MARGIN_S = 60
_MAX_BACKOFF_S = 60.0
# Worth retrying: ERCOT's gateway returns these under load, and they clear on their own.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class RetriesExhaustedError(RuntimeError):
    """Every attempt was throttled, failed server-side or never got a response."""

    def __init__(self, msg: str, *, last_status: int | None) -> None:
        super().__init__(msg)
        self.last_status = last_status  # None: no response at all (timeouts, resets)


class DocumentNotReadyError(RuntimeError):
    """The archive lists the document but ``download=`` still returns 400/404.

    A posting appears in the listing about a minute before its zip is downloadable. Not an
    error; the next run gets it.
    """


@dataclass(frozen=True)
class ArchiveDoc:
    """One ERCOT posting as listed by ``/archive/<ID>``."""

    archive_id: str
    doc_id: int
    posted_at: datetime  # UTC
    friendly_name: str


@dataclass
class ErcotClient:
    """``live=True`` for scheduled runs: they give up after ``live_max_retries`` because the
    next run is the retry. Backfills and explicit windows keep trying up to ``max_retries``."""

    cfg: ErcotConfig
    creds: Credentials
    live: bool = False
    _http: httpx.Client = field(init=False, repr=False)
    _token: str | None = field(default=None, init=False, repr=False)
    _token_expires: float = field(default=0.0, init=False, repr=False)
    _last_request: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._http = httpx.Client(timeout=self.cfg.timeout_s)

    @property
    def max_retries(self) -> int:
        return self.cfg.live_max_retries if self.live else self.cfg.max_retries

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ErcotClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- auth -----------------------------------------------------------------

    def _refresh_token(self) -> None:
        resp = self._http.post(
            self.cfg.auth.token_url,
            data={
                "grant_type": "password",
                "username": self.creds.username,
                "password": self.creds.password.get_secret_value(),
                "scope": self.cfg.auth.scope,
                "client_id": self.cfg.auth.client_id,
                "response_type": "id_token",
            },
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["id_token"]
        self._token_expires = time.time() + int(body.get("expires_in", 3600))
        log.debug("refreshed ERCOT token")

    def _headers(self) -> dict[str, str]:
        if not self._token or time.time() > self._token_expires - _TOKEN_REFRESH_MARGIN_S:
            self._refresh_token()
        assert self._token is not None
        return {
            "Authorization": f"Bearer {self._token}",
            "Ocp-Apim-Subscription-Key": self.creds.subscription_key.get_secret_value(),
        }

    # -- requests -------------------------------------------------------------

    def _pace(self) -> None:
        wait = self.cfg.min_interval_s - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        problem, last_status = "no attempt", None
        for attempt in range(self.max_retries + 1):
            self._pace()
            try:
                resp = self._http.request(method, url, headers=self._headers(), **kw)
            except httpx.TransportError as exc:  # timeouts, resets, DNS
                problem, last_status = type(exc).__name__, None
                self._backoff(url, problem, attempt, hinted=0.0)
                continue
            if resp.status_code in _RETRY_STATUS:
                # ERCOT's Retry-After is a few seconds even while it keeps refusing; grow the
                # wait exponentially (capped) so a burst drains instead of exhausting retries.
                problem, last_status = str(resp.status_code), resp.status_code
                self._backoff(url, problem, attempt, hinted=_retry_after(resp))
                continue
            if resp.status_code == httpx.codes.UNAUTHORIZED and attempt == 0:
                self._token = None  # expired early or revoked: one fresh token, then give up
                continue
            resp.raise_for_status()
            return resp
        msg = f"gave up on {url} after {self.max_retries + 1} attempts (last: {problem})"
        raise RetriesExhaustedError(msg, last_status=last_status)

    def _backoff(self, url: str, problem: str, attempt: int, *, hinted: float) -> None:
        if attempt >= self.max_retries:
            return  # no attempt left to wait for
        wait = min(max(hinted, 2.0**attempt), _MAX_BACKOFF_S)
        log.warning("%s from %s; sleeping %.1fs (attempt %d)", problem, url, wait, attempt + 1)
        time.sleep(wait)

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """One page of a report endpoint. ``path`` is relative to ``api_base``."""
        url = f"{self.cfg.api_base}{path}"
        result: dict[str, Any] = self._request("GET", url, params=params or {}).json()
        return result

    def iter_pages(
        self, path: str, params: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        """Yield every page of a report endpoint, following ``_meta.totalPages``."""
        base = dict(params or {})
        base.setdefault("size", self.cfg.page_size)
        page = 1
        while True:
            body = self.get(path, {**base, "page": page})
            yield body
            if page >= int(body.get("_meta", {}).get("totalPages", 1)):
                return
            page += 1

    # -- archives -------------------------------------------------------------

    def list_archives(
        self, archive_id: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = f"{self.cfg.archive_base}/{archive_id}"
        result: dict[str, Any] = self._request("GET", url, params=params or {}).json()
        return result

    def download_archive(self, archive_id: str, doc_id: str | int) -> bytes:
        url = f"{self.cfg.archive_base}/{archive_id}"
        try:
            return self._request("GET", url, params={"download": doc_id}).content
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (httpx.codes.BAD_REQUEST, httpx.codes.NOT_FOUND):
                code = exc.response.status_code
                msg = f"{archive_id} doc {doc_id} listed but not downloadable yet ({code})"
                raise DocumentNotReadyError(msg) from exc
            raise

    def iter_archive_docs(
        self, archive_id: str, post_from: datetime, post_to: datetime
    ) -> Iterator[ArchiveDoc]:
        """Postings with ``post_from < posted_at <= post_to``, oldest first.

        ERCOT filters on CT wall-clock time with no DST flag, and lists newest first. On the
        fall-back night the wall clock runs 01:00-02:00 twice, so a window that starts in the
        first pass and ends in the second is empty on the wall clock (from 01:55 to 01:10).
        The query therefore starts an hour early on the wall clock, the two passes are told
        apart by listing order, and the exact window is applied in UTC.
        """
        fmt = "%Y-%m-%dT%H:%M:%S"
        params: dict[str, Any] = {
            "postDatetimeFrom": utc_to_ct(post_from - timedelta(hours=1)).strftime(fmt),
            "postDatetimeTo": utc_to_ct(post_to).strftime(fmt),
            "size": self.cfg.page_size,
        }
        listed: list[tuple[int, datetime, str]] = []
        page = 1
        while True:
            body = self.list_archives(archive_id, {**params, "page": page})
            listed.extend(
                (int(d["docId"]), parse_post_local(d["postDatetime"]), d.get("friendlyName", ""))
                for d in body.get("archives", [])
            )
            if page >= int(body.get("_meta", {}).get("totalPages", 1)):
                break
            page += 1
        docs = [
            ArchiveDoc(archive_id, doc_id, posted, name)
            for doc_id, posted, name in _resolve_repeated_hour(listed)
            if post_from < posted <= post_to
        ]
        yield from sorted(docs, key=lambda d: (d.posted_at, d.doc_id))

    def list_bundles(
        self, product_key: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = f"{self.cfg.api_base}/bundle/{product_key}"
        result: dict[str, Any] = self._request("GET", url, params=params or {}).json()
        return result

    def download_bundle(self, product_key: str, doc_id: str | int) -> bytes:
        url = f"{self.cfg.api_base}/bundle/{product_key}"
        return self._request("GET", url, params={"download": doc_id}).content

    def probe(self, path: str, params: dict[str, Any] | None = None) -> int:
        """The HTTP status for a path, without raising, for endpoint checks. Retries on
        throttling like any request, so a probe result is never a rate-limit artefact."""
        url = path if path.startswith("http") else f"{self.cfg.api_base}{path}"
        try:
            return self._request("GET", url, params=params or {}).status_code
        except httpx.HTTPStatusError as exc:
            return exc.response.status_code
        except RetriesExhaustedError as exc:
            return exc.last_status or 0  # 0: no response at all


def _retry_after(resp: httpx.Response) -> float:
    try:
        return float(resp.headers.get("Retry-After", 0))
    except ValueError:  # an HTTP date instead of seconds: fall back to exponential back-off
        return 0.0


def _resolve_repeated_hour(
    listed: list[tuple[int, datetime, str]],
) -> list[tuple[int, datetime, str]]:
    """(doc id, CT wall-clock time, name) -> (doc id, UTC, name).

    ERCOT's doc IDs grow with posting order. Inside the repeated fall-back hour, walking the
    postings by doc ID, the wall clock steps back (or repeats) when the second pass begins;
    from there on that hour is read as the second occurrence.
    """
    latest: dict[date, datetime] = {}
    second_pass: set[date] = set()
    out = []
    for doc_id, local, name in sorted(listed, key=lambda t: t[0]):
        repeated = False
        if is_repeated_local(local):
            day = local.date()
            if day not in second_pass and day in latest and local <= latest[day]:
                second_pass.add(day)
            repeated = day in second_pass
            latest[day] = max(latest.get(day, local), local)
        out.append((doc_id, local_to_utc(local, repeated_hour=repeated), name))
    return out
