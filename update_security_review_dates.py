#!/usr/bin/env python3
"""Bulk-update Drata vendor security review dates (Python 3.9+, standard library only).

Usage:
  python3 update_security_review_dates.py --csv dates.csv           # dry run (default)
  python3 update_security_review_dates.py --csv dates.csv --apply   # write changes

The API token is read from a hidden prompt on every run (DRATA_API_TOKEN is ignored)
and only kept in process memory. A terminal with hidden input is required.

CSV columns: security_review_id,date[,vendor_id]
  - The first row is treated as a header when it contains no digits at all. If it names
    the review id (securityReviewId, security_review_id, review_id or id) and the date
    (date or updatedAt), columns are matched by name in any order and other columns
    (e.g. vendorName) are ignored. Otherwise, and without a header, the first three
    columns are used by position and extra non-empty columns are rejected.
  - The delimiter (comma, semicolon or tab) is detected from the first row.
  - date: YYYY-MM-DD, M/D/YYYY (US order) or an ISO 8601 date-time.
  - Date-only values become 12:00 UTC. Date-times with an offset or Z are converted to
    UTC; date-times without one are read as UTC. Stored with millisecond precision.
  - Accepted range is 2000-01-01 up to today (UTC); later dates need --allow-future.
  - vendor_id is optional; without it the vendor is looked up from the review id.
  - Every row is validated before any API call. Invalid rows (including duplicate
    review ids) are logged and skipped, and the run exits 1 at the end. With --strict
    any invalid row aborts the run before anything is sent (exit 2).

Safety:
  - Dry run still makes read-only API calls. --apply asks for confirmation (--yes
    skips it) and sends PUT with only updatedAt. Use --limit 1 to try a single row.
  - Reviews already at the target date are left alone (no PUT).
  - Every run writes a new timestamped audit log in the current directory. --apply also
    writes a rollback CSV holding each previous updatedAt, in this same input format,
    so `--csv <rollback file> --apply` restores the old dates.
  - The PUT response must echo the new updatedAt; no separate read-after-write is done.
  - The bearer token is only sent to HTTPS drata.com hosts (--allow-custom-host to
    override) and redirects are never followed.
  - Retries (at most 3 attempts, Retry-After honoured and capped at 60s): GET retries on
    HTTP 429/500/502/503/504 and network errors; PUT retries on HTTP 429 only, because
    a PUT that failed in flight may already have been applied. Five consecutive
    transient failures abort the run, as does HTTP 401/403.

Required key permissions: Vendors: Read (VENDORS_GET),
Security Review: Update (VENDORS_PUT_SECURITY_REVIEW).

Exit codes: 0 = every row was updated, unchanged or (dry run) planned;
1 = any row failed, was skipped, was left uncertain or not attempted, or the run aborted;
2 = invalid invocation, configuration or CSV; 130 = cancelled or interrupted.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import http.client
import io
import json
import logging
import math
import os
import re
import sys
import time
import warnings
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MIN_PYTHON = (3, 9)
DEFAULT_BASE = "https://public-api.drata.com/public"
ALLOWED_HOST = "drata.com"
TIMEOUT_SECONDS = 60
# Cloudflare in front of the Drata API rejects the default "Python-urllib" signature (error 1010).
USER_AGENT = "drata-security-review-dates/1.0"
PAGE_SIZE = 100
MAX_PAGES = 10_000
MAX_ATTEMPTS = 3
MAX_RETRY_DELAY = 60.0
MAX_CONSECUTIVE_TRANSIENT = 5
MIN_DATE = datetime(2000, 1, 1, tzinfo=timezone.utc)
RETRY_STATUSES = {"GET": {429, 500, 502, 503, 504}, "PUT": {429}}
TRANSIENT_STATUSES = {429, 500, 502, 503, 504}

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_CANCELLED = 0, 1, 2, 130

LOG = logging.getLogger("security_review_dates")

_ID = re.compile(r"[0-9]{1,18}")
_DATE_ONLY = re.compile(r"([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})")
_US_DATE = re.compile(r"([0-9]{1,2})/([0-9]{1,2})/([0-9]{4})")
_DATE_TIME = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[T ]([0-9]{2}):([0-9]{2})"
    r"(?::([0-9]{2})(?:\.([0-9]+))?)?(Z|[+-][0-9]{2}(?::?[0-9]{2})?)?",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- parsing


def parse_id(raw: object) -> Optional[int]:
    """Positive integer id, or None. Digits only, so no signs, spaces or unicode digits."""
    text = str(raw if raw is not None else "").strip()
    if not _ID.fullmatch(text):
        return None
    return int(text) or None


def _parse_offset(text: Optional[str]) -> timezone:
    if not text or text.upper() == "Z":
        return timezone.utc
    digits = text[1:].replace(":", "")
    hours, minutes = int(digits[:2]), int(digits[2:] or 0)
    if minutes > 59:
        raise ValueError(text)
    delta = timedelta(hours=hours, minutes=minutes)
    return timezone(-delta if text[0] == "-" else delta)


def parse_timestamp(raw: object) -> Optional[datetime]:
    """Parse a CSV date or API timestamp to a UTC datetime truncated to milliseconds."""
    value = str(raw if raw is not None else "").strip()
    try:
        match = _DATE_TIME.fullmatch(value)
        if match:
            year, month, day, hour, minute = (int(part) for part in match.groups()[:5])
            second = int(match.group(6) or 0)
            millis = int((match.group(7) or "")[:3].ljust(3, "0"))
            moment = datetime(year, month, day, hour, minute, second, millis * 1000,
                              tzinfo=_parse_offset(match.group(8)))
            return moment.astimezone(timezone.utc)
        match = _DATE_ONLY.fullmatch(value)
        if match:
            year, month, day = (int(part) for part in match.groups())
        else:
            match = _US_DATE.fullmatch(value)
            if not match:
                return None
            month, day, year = (int(part) for part in match.groups())
        return datetime(year, month, day, 12, tzinfo=timezone.utc)
    except (ValueError, OverflowError):
        return None


def format_timestamp(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def is_ambiguous_slash_date(raw: str) -> bool:
    match = _US_DATE.fullmatch(raw.strip())
    return bool(match) and int(match.group(1)) <= 12 and int(match.group(2)) <= 12 \
        and match.group(1) != match.group(2)


def date_problem(target: datetime, today: date, allow_future: bool) -> Optional[str]:
    if target < MIN_DATE:
        return "date {} is before {:%Y-%m-%d}".format(format_timestamp(target), MIN_DATE)
    if target.date() > today and not allow_future:
        return "date {} is in the future (use --allow-future)".format(format_timestamp(target))
    return None


def short(value: Any, limit: int = 300) -> str:
    """Single-line, escaped, truncated rendering of untrusted data for log messages."""
    text = json.dumps(value, ensure_ascii=True, default=str)
    return text if len(text) <= limit else text[:limit] + "...(truncated)"


# ------------------------------------------------------------------------------- CSV


class InputError(Exception):
    """The CSV could not be read or is malformed (a configuration problem)."""


@dataclass(frozen=True)
class Row:
    line: int
    review_id: int
    target: datetime
    vendor_id: Optional[int]


@dataclass(frozen=True)
class RowError:
    line: int
    message: str


@dataclass
class LoadResult:
    rows: list[Row]
    errors: list[RowError]
    ambiguous_lines: list[int]


def detect_delimiter(text: str) -> str:
    first = next((line for line in text.splitlines() if line.strip()), "")
    best = max(",;\t", key=first.count)
    return best if first.count(best) else ","


_HEADER_FIELDS = {
    "review": ("securityreviewid", "reviewid", "id"),
    "date": ("date", "updatedat"),
    "vendor": ("vendorid", "vendor"),
}


def map_header(cells: list[str]) -> Optional[dict[str, int]]:
    """Column positions by field name, or None when the header isn't recognised."""
    names = [re.sub(r"[^a-z0-9]", "", cell.lower()) for cell in cells]
    positions: dict[str, int] = {}
    for field, aliases in _HEADER_FIELDS.items():
        hits = [index for index, name in enumerate(names) if name in aliases]
        if len(hits) > 1:
            raise InputError("Ambiguous CSV header: several columns could be the {} column: {}"
                             .format(field, [cells[index] for index in hits]))
        if hits:
            positions[field] = hits[0]
    return positions if "review" in positions and "date" in positions else None


def select_columns(cells: list[str],
                   positions: Optional[dict[str, int]]) -> tuple[str, str, str, list[str]]:
    """Return (review, date, vendor, unexpected extra cells) for one record."""
    if positions is None:
        padded = cells + ["", "", ""]
        return padded[0], padded[1], padded[2], [cell for cell in cells[3:] if cell]

    def pick(field: str) -> str:
        index = positions.get(field)
        return cells[index] if index is not None and index < len(cells) else ""

    return pick("review"), pick("date"), pick("vendor"), []


def parse_row(line: int, review_raw: str, date_raw: str, vendor_raw: str, extra: list[str],
              today: date, allow_future: bool) -> "Row | RowError":
    if extra:
        return RowError(line, "unexpected extra column(s): {!r}".format(extra))
    review_id = parse_id(review_raw)
    if review_id is None:
        return RowError(line, "invalid security review id {!r}".format(review_raw))
    target = parse_timestamp(date_raw)
    if target is None:
        return RowError(line, "could not parse date {!r}".format(date_raw))
    problem = date_problem(target, today, allow_future)
    if problem:
        return RowError(line, problem)
    vendor_id = None
    if vendor_raw:
        vendor_id = parse_id(vendor_raw)
        if vendor_id is None:
            return RowError(line, "invalid vendor id {!r}".format(vendor_raw))
    return Row(line, review_id, target, vendor_id)


def load_rows(path: str, today: date, allow_future: bool = False) -> LoadResult:
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError("Cannot read {}: {}".format(path, exc)) from exc

    reader = csv.reader(io.StringIO(text), delimiter=detect_delimiter(text),
                        skipinitialspace=True, strict=True)
    parsed: list[Row] = []
    errors: list[RowError] = []
    ambiguous: list[int] = []
    positions: Optional[dict[str, int]] = None
    first = True
    try:
        for record in reader:
            cells = [cell.strip() for cell in record]
            if not any(cells):
                continue
            if first:
                first = False
                if not any(re.search(r"[0-9]", cell) for cell in cells):
                    positions = map_header(cells)
                    if positions is None:
                        LOG.info("CSV header not recognised; using the first three columns by position")
                    else:
                        LOG.info("CSV header recognised; columns by name: %s; other columns are ignored",
                                 ", ".join("{}={!r}".format(field, cells[index])
                                           for field, index in positions.items()))
                    continue
            review_raw, date_raw, vendor_raw, extra = select_columns(cells, positions)
            result = parse_row(reader.line_num, review_raw, date_raw, vendor_raw, extra,
                               today, allow_future)
            if isinstance(result, RowError):
                errors.append(result)
                continue
            parsed.append(result)
            if is_ambiguous_slash_date(date_raw):
                ambiguous.append(result.line)
    except csv.Error as exc:
        raise InputError("{}: line {}: {}".format(path, reader.line_num, exc)) from exc

    lines_by_id: dict[int, list[int]] = {}
    for row in parsed:
        lines_by_id.setdefault(row.review_id, []).append(row.line)
    rows = []
    for row in parsed:
        lines = lines_by_id[row.review_id]
        if len(lines) > 1:
            errors.append(RowError(row.line, "duplicate security review id {} (csv lines {})"
                                   .format(row.review_id, ", ".join(map(str, lines)))))
        else:
            rows.append(row)
    errors.sort(key=lambda error: error.line)
    return LoadResult(rows, errors, ambiguous)


# ------------------------------------------------------------------------------- HTTP


class ApiError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class FatalApiError(ApiError):
    """Systemic failure; the run must stop."""


class AuthError(FatalApiError):
    """HTTP 401/403: the key is invalid or lacks permission."""


@dataclass
class Response:
    status: int
    data: Any

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def parse_retry_after(value: Optional[str], default: float) -> float:
    if not value:
        return default
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return default
    return max(0.0, seconds) if math.isfinite(seconds) else default


def decode_body(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def block_hint(payload: Any) -> str:
    """Tell a Cloudflare block apart from a genuine API key/permission rejection."""
    if "cloudflare" in json.dumps(payload, default=str).lower():
        return " [Cloudflare block, not an API key problem]"
    return ""


class Client:
    def __init__(self, base_url: str, token: str, min_interval: float = 0.15):
        self.base = base_url.rstrip("/") + "/v2"
        self._token = token
        self._opener = build_opener(NoRedirect())
        self._min_interval = min_interval
        self._last_request = float("-inf")
        self._transient_streak = 0

    def request(self, method: str, path: str, params: Optional[dict] = None,
                data: Optional[dict] = None) -> Response:
        url = self.base + path + ("?" + urlencode(params) if params else "")
        body = None if data is None else json.dumps(data).encode("utf-8")
        retry_statuses = RETRY_STATUSES.get(method, set())
        for attempt in range(1, MAX_ATTEMPTS + 1):
            final = attempt == MAX_ATTEMPTS
            self._pace()
            try:
                status, headers, raw = self._send(method, url, body)
            except (OSError, http.client.HTTPException) as exc:
                reason = "{} {} network error: {}".format(method, path, exc)
                if method == "GET" and not final:
                    self._backoff(attempt, None, reason)
                    continue
                self._record(True, reason)
                raise ApiError(reason) from exc
            if status in retry_statuses and not final:
                self._backoff(attempt, headers.get("Retry-After"),
                              "{} {} returned HTTP {}".format(method, path, status))
                continue
            self._record(status in TRANSIENT_STATUSES, "{} {} returned HTTP {}".format(method, path, status))
            payload = decode_body(raw)
            if status in (401, 403):
                raise AuthError("{} {} rejected (HTTP {}){}: {}".format(
                    method, path, status, block_hint(payload), short(payload)), status)
            if 300 <= status < 400:
                raise ApiError("{} {} returned redirect HTTP {}; redirects are never followed"
                               .format(method, path, status), status)
            return Response(status, payload)
        raise AssertionError("unreachable")

    def pages(self, path: str) -> Iterator[list]:
        cursor = None
        seen: set[str] = set()
        for number in range(1, MAX_PAGES + 1):
            params: dict[str, Any] = {"size": PAGE_SIZE}
            if cursor:
                params["cursor"] = cursor
            res = self.request("GET", path, params=params)
            if not res.ok:
                raise ApiError("GET {} failed (HTTP {}): {}".format(path, res.status, short(res.data)), res.status)
            body = res.data
            if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                raise ApiError("GET {} returned an unexpected list response".format(path))
            LOG.debug("%s page %s: %s row(s)", path, number, len(body["data"]))
            yield body["data"]
            pagination = body.get("pagination") or {}
            if not isinstance(pagination, dict):
                raise ApiError("GET {} returned invalid pagination".format(path))
            cursor = pagination.get("cursor")
            if not cursor:
                return
            if not isinstance(cursor, str) or cursor in seen:
                raise ApiError("GET {} returned an invalid or repeated pagination cursor".format(path))
            seen.add(cursor)
        raise ApiError("GET {} exceeded {} pages".format(path, MAX_PAGES))

    def _send(self, method: str, url: str, body: Optional[bytes]) -> tuple:
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json",
                   "User-Agent": USER_AGENT}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = Request(url, data=body, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=TIMEOUT_SECONDS) as resp:
                return resp.status, resp.headers, resp.read()
        except HTTPError as exc:
            with exc:
                return exc.code, exc.headers, exc.read()

    def _pace(self) -> None:
        wait = self._last_request + self._min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _backoff(self, attempt: int, retry_after: Optional[str], reason: str) -> None:
        delay = min(MAX_RETRY_DELAY, parse_retry_after(retry_after, 2.0 ** (attempt - 1)))
        LOG.warning("%s; retry %s/%s in %.1fs", reason, attempt, MAX_ATTEMPTS - 1, delay)
        time.sleep(delay)

    def _record(self, transient: bool, what: str) -> None:
        self._transient_streak = self._transient_streak + 1 if transient else 0
        if self._transient_streak >= MAX_CONSECUTIVE_TRANSIENT:
            raise FatalApiError("{} consecutive transient failures (last: {})"
                                .format(self._transient_streak, what))


# ------------------------------------------------------------------ vendor resolution


def review_pair(review: Any, vendor_id: Optional[int] = None) -> tuple[Optional[int], Optional[int]]:
    if not isinstance(review, dict):
        raise ApiError("Unexpected review record in list response")
    if vendor_id is None:
        nested = review.get("vendor")
        vendor_id = parse_id(review.get("vendorId")) or parse_id(
            nested.get("id") if isinstance(nested, dict) else None)
    return parse_id(review.get("id")), vendor_id


def pairs_from_review_list(client: Client) -> Iterator[tuple[Optional[int], Optional[int]]]:
    for page in client.pages("/vendor-security-reviews"):
        for review in page:
            yield review_pair(review)


def pairs_from_vendors(client: Client) -> Iterator[tuple[Optional[int], Optional[int]]]:
    for page in client.pages("/vendors"):
        for vendor in page:
            vendor_id = parse_id(vendor.get("id")) if isinstance(vendor, dict) else None
            if not vendor_id:
                raise ApiError("Invalid vendor id in vendors response")
            for reviews in client.pages("/vendors/{}/security-reviews".format(vendor_id)):
                for review in reviews:
                    yield review_pair(review, vendor_id)


def scan_pairs(pairs: Iterable[tuple[Optional[int], Optional[int]]],
               wanted: set[int]) -> tuple[dict[int, int], int, int]:
    """Collect vendor ids for wanted reviews, stopping early once all are found."""
    found: dict[int, int] = {}
    seen = mapped = 0
    for review_id, vendor_id in pairs:
        seen += 1
        if review_id is None or vendor_id is None:
            continue
        mapped += 1
        if review_id in wanted:
            found[review_id] = vendor_id
            if len(found) == len(wanted):
                break
    return found, seen, mapped


def resolve_vendors(client: Client, wanted: set[int]) -> dict[int, int]:
    try:
        found, seen, mapped = scan_pairs(pairs_from_review_list(client), wanted)
        if not seen or mapped:
            return found
        LOG.warning("/vendor-security-reviews returned %s review(s) without vendor ids; "
                    "falling back to per-vendor scan.", seen)
    except ApiError as exc:
        if exc.status != 404 or isinstance(exc, FatalApiError):
            raise
        LOG.warning("/vendor-security-reviews unavailable (404); falling back to per-vendor scan.")
    return scan_pairs(pairs_from_vendors(client), wanted)[0]


# ---------------------------------------------------------------------------- updates


@dataclass
class Summary:
    total: int = 0          # valid rows selected for processing
    planned: int = 0        # dry run: would be updated
    updated: int = 0        # PUT confirmed by the echoed updatedAt
    unchanged: int = 0      # already at the target date; no PUT sent
    skipped: int = 0        # invalid CSV rows dropped (--strict aborts instead)
    failed: int = 0         # confirmed not written
    uncertain: int = 0      # PUT sent but outcome unconfirmed; verify manually
    not_attempted: int = 0  # never reached because the run stopped
    fatal: bool = False

    @property
    def processed(self) -> int:
        return self.planned + self.updated + self.unchanged + self.failed + self.uncertain

    @property
    def ok(self) -> bool:
        return not (self.fatal or self.skipped or self.failed or self.uncertain or self.not_attempted)


class RollbackFile:
    """Previous updatedAt per written review, in the tool's own input format."""

    def __init__(self, path: Path):
        self.path = path
        self._handle = open_private(path)
        self._writer = csv.writer(self._handle, lineterminator="\n")
        self._writer.writerow(["security_review_id", "date", "vendor_id"])
        self._handle.flush()

    def record(self, review_id: int, vendor_id: int, previous: Any) -> None:
        if not isinstance(previous, str) or not previous:
            LOG.warning("review %s: no previous updatedAt available; rollback row is incomplete", review_id)
            previous = ""
        self._writer.writerow([review_id, previous, vendor_id])
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


def update_row(client: Client, row: Row, vendor: int, apply: bool,
               rollback: Optional[RollbackFile], summary: Summary) -> None:
    tag = "review {} (csv line {})".format(row.review_id, row.line)
    path = "/vendors/{}/security-reviews/{}".format(vendor, row.review_id)
    before = client.request("GET", path)
    if not before.ok or not isinstance(before.data, dict):
        LOG.error("FAILED %s: GET returned HTTP %s: %s", tag, before.status, short(before.data))
        summary.failed += 1
        return
    current_raw = before.data.get("updatedAt")
    status = before.data.get("status")
    target_text = format_timestamp(row.target)
    if parse_timestamp(current_raw) == row.target:
        LOG.info("UNCHANGED %s [vendor %s, status %s]: already %s", tag, vendor, status, current_raw)
        summary.unchanged += 1
        return
    if not apply:
        LOG.info("DRY RUN %s [vendor %s, status %s]: %s -> %s", tag, vendor, status, current_raw, target_text)
        summary.planned += 1
        return

    def note_possible_write() -> None:
        if rollback:
            rollback.record(row.review_id, vendor, current_raw)

    def uncertain(reason: str) -> None:
        LOG.error("UNCERTAIN %s: %s. The PUT may have been applied; verify before retrying.", tag, reason)
        summary.uncertain += 1

    try:
        res = client.request("PUT", path, data={"updatedAt": target_text})
    except AuthError as exc:
        LOG.error("FAILED %s: %s", tag, exc)
        summary.failed += 1
        raise
    except KeyboardInterrupt:
        note_possible_write()
        uncertain("interrupted during PUT")
        raise
    except ApiError as exc:
        note_possible_write()
        uncertain(str(exc))
        if isinstance(exc, FatalApiError):
            raise
        return
    if res.status >= 500:
        note_possible_write()
        uncertain("PUT returned HTTP {}: {}".format(res.status, short(res.data)))
    elif not res.ok:
        LOG.error("FAILED %s: PUT returned HTTP %s: %s", tag, res.status, short(res.data))
        summary.failed += 1
    else:
        note_possible_write()
        persisted = res.data.get("updatedAt") if isinstance(res.data, dict) else None
        if parse_timestamp(persisted) == row.target:
            LOG.info("UPDATED %s [vendor %s, status %s]: %s -> %s", tag, vendor, status, current_raw, persisted)
            summary.updated += 1
        else:
            uncertain("PUT returned HTTP {} but updatedAt is {}, expected {}"
                      .format(res.status, short(persisted), target_text))


def process_rows(client: Client, rows: list[Row], vendors: dict[int, int], apply: bool,
                 rollback: Optional[RollbackFile], summary: Summary) -> None:
    for row in rows:
        vendor = row.vendor_id or vendors.get(row.review_id)
        if not vendor:
            LOG.error("FAILED review %s (csv line %s): no vendor found; check key permissions and "
                      "the id, or add the vendor id as CSV column 3.", row.review_id, row.line)
            summary.failed += 1
            continue
        try:
            update_row(client, row, vendor, apply, rollback, summary)
        except FatalApiError:
            raise
        except ApiError as exc:
            LOG.error("FAILED review %s (csv line %s): %s", row.review_id, row.line, exc)
            summary.failed += 1


# -------------------------------------------------------------------------------- CLI


class OwnedStreamHandler(logging.StreamHandler):
    """StreamHandler that closes its stream when closed."""

    def close(self) -> None:
        stream = self.stream
        try:
            super().close()
        finally:
            stream.close()


def open_private(path: Path):
    """Create a new file readable only by the current user; never overwrite or follow links."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8", newline="")


def configure_logging(verbose: bool, path: Path) -> None:
    close_logging()
    LOG.setLevel(logging.DEBUG)
    LOG.propagate = False
    formatter = logging.Formatter("[%(asctime)s.%(msecs)03dZ] [%(levelname)s] %(message)s",
                                  "%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(formatter)
    LOG.addHandler(console)
    # Create the audit log before anything else so no change is ever made without one.
    audit = OwnedStreamHandler(open_private(path))
    audit.setLevel(logging.DEBUG)
    audit.setFormatter(formatter)
    LOG.addHandler(audit)


def close_logging() -> None:
    for handler in LOG.handlers[:]:
        handler.close()
        LOG.removeHandler(handler)


def validate_base_url(url: str, allow_custom_host: bool) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("must be an https:// URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("must not contain credentials, a query or a fragment")
    parts.port  # noqa: B018 - raises ValueError on an invalid port
    host = parts.hostname.lower()
    if not allow_custom_host and host != ALLOWED_HOST and not host.endswith("." + ALLOWED_HOST):
        raise ValueError("host {!r} is not a {} host; the API token would be sent there "
                         "(use --allow-custom-host to allow it)".format(host, ALLOWED_HOST))
    return url


def prompt_token() -> str:
    """Read the token without echo; never fall back to visible input."""
    while True:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            token = getpass.getpass("Drata API token (input hidden): ").strip()
        if token:
            return token
        print("API token cannot be empty. Try again or press Ctrl+C to cancel.", file=sys.stderr)


def confirm_apply(count: int, base_url: str, skipped: int = 0) -> bool:
    note = " ({} invalid row(s) skipped)".format(skipped) if skipped else ""
    answer = input("Update {} security review date(s) at {}{}? Type 'yes' to continue: "
                   .format(count, urlsplit(base_url).hostname, note))
    return answer.strip().lower() == "yes"


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def non_negative_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must be a number >= 0")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", required=True, help="input CSV path")
    parser.add_argument("--apply", action="store_true", help="write changes (default is a dry run)")
    parser.add_argument("--yes", action="store_true", help="with --apply, skip the confirmation prompt")
    parser.add_argument("--limit", type=positive_int, metavar="N", help="only process the first N valid rows")
    parser.add_argument("--strict", action="store_true",
                        help="abort before sending anything if any CSV row is invalid "
                             "(default: skip invalid rows and exit 1 at the end)")
    parser.add_argument("--skip-invalid", action="store_true", help=argparse.SUPPRESS)  # old no-op alias
    parser.add_argument("--allow-future", action="store_true", help="accept dates after today (UTC)")
    parser.add_argument("--base-url", default=os.environ.get("DRATA_API_BASE_URL") or DEFAULT_BASE,
                        help="API base URL (default: %(default)s; env DRATA_API_BASE_URL)")
    parser.add_argument("--allow-custom-host", action="store_true",
                        help="allow a --base-url host outside drata.com")
    parser.add_argument("--delay", type=non_negative_float, default=0.15, metavar="SECONDS",
                        help="minimum pause between API requests (default: %(default)s)")
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug output on the console")
    return parser


def run(args: argparse.Namespace, base_url: str, now: datetime, stamp: str, log_path: Path) -> int:
    mode = "APPLY" if args.apply else "DRY RUN"
    summary = Summary()
    LOG.info("Mode: %s%s", mode, "" if args.apply else " (no writes; pass --apply to write)")
    LOG.info("Base URL: %s/v2", base_url.rstrip("/"))
    try:
        loaded = load_rows(args.csv, now.date(), args.allow_future)
    except InputError as exc:
        LOG.error("%s", exc)
        return EXIT_USAGE
    for error in loaded.errors:
        LOG.error("INVALID csv line %s: %s", error.line, error.message)
    if loaded.ambiguous_lines:
        LOG.warning("%s row(s) use slash dates with an ambiguous day/month order; read as M/D/YYYY "
                    "(csv lines: %s%s)", len(loaded.ambiguous_lines),
                    ", ".join(map(str, loaded.ambiguous_lines[:10])),
                    ", ..." if len(loaded.ambiguous_lines) > 10 else "")
    summary.skipped = len(loaded.errors)
    if loaded.errors and args.strict:
        LOG.error("%s invalid row(s) and --strict is set; nothing was sent. Fix the CSV.",
                  len(loaded.errors))
        return EXIT_USAGE
    if loaded.errors:
        LOG.warning("%s invalid row(s) will be skipped (the run will exit 1; use --strict to "
                    "abort instead).", len(loaded.errors))
    rows = loaded.rows
    if not rows:
        LOG.error("No valid rows to process.")
        return EXIT_USAGE
    if args.limit and len(rows) > args.limit:
        LOG.warning("--limit: processing the first %s of %s valid row(s)", args.limit, len(rows))
        rows = rows[:args.limit]
    summary.total = len(rows)
    LOG.info("Loaded %s row(s) from %s", len(rows), args.csv)

    if args.apply and not args.yes:
        try:
            confirmed = confirm_apply(len(rows), base_url, summary.skipped)
        except (KeyboardInterrupt, EOFError):
            confirmed = False
        if not confirmed:
            LOG.error("Not confirmed; nothing was sent.")
            return EXIT_CANCELLED
    try:
        token = prompt_token()
    except KeyboardInterrupt:
        LOG.error("Cancelled; no API requests made.")
        return EXIT_CANCELLED
    except (EOFError, getpass.GetPassWarning, OSError):
        LOG.error("Cannot read hidden API token. Run from an interactive terminal. "
                  "No API requests made.")
        return EXIT_USAGE

    client = Client(base_url, token, min_interval=args.delay)
    rollback: Optional[RollbackFile] = None
    interrupted = False
    try:
        needs_lookup = {row.review_id for row in rows if row.vendor_id is None}
        vendors: dict[int, int] = {}
        if needs_lookup:
            LOG.info("Resolving vendor ids for %s security review(s)...", len(needs_lookup))
            vendors = resolve_vendors(client, needs_lookup)
            LOG.info("Resolved %s of %s.", len(vendors), len(needs_lookup))
        if args.apply:
            rollback = RollbackFile(Path.cwd() / "security-review-dates-rollback-{}.csv".format(stamp))
            LOG.info("Rollback CSV: %s", rollback.path)
        process_rows(client, rows, vendors, args.apply, rollback, summary)
    except KeyboardInterrupt:
        interrupted = True
        summary.fatal = True
        LOG.error("Interrupted.")
    except ApiError as exc:
        summary.fatal = True
        LOG.error("ABORTED: %s", exc)
    except Exception as exc:  # noqa: BLE001 - last resort so the summary is always written
        summary.fatal = True
        LOG.error("ABORTED: unexpected %s: %s", type(exc).__name__, exc)
        LOG.debug("Traceback", exc_info=True)
    finally:
        summary.not_attempted = summary.total - summary.processed
        if rollback:
            rollback.close()
        LOG.info("=== SECURITY REVIEW DATE UPDATE SUMMARY (%s) ===", mode)
        for name in ("total", "planned", "updated", "unchanged", "skipped", "failed",
                     "uncertain", "not_attempted"):
            LOG.info("%-14s %s", name, getattr(summary, name))
        if summary.uncertain:
            LOG.warning("%s PUT(s) have an unconfirmed outcome: check the UNCERTAIN lines in the log.",
                        summary.uncertain)
        LOG.info("Audit log: %s", log_path)
    if interrupted:
        return EXIT_CANCELLED
    return EXIT_OK if summary.ok else EXIT_FAILED


def main(argv: Optional[list[str]] = None) -> int:
    if sys.version_info < MIN_PYTHON:
        print("Python {}.{}+ is required (running {}).".format(
            *MIN_PYTHON, sys.version.split()[0]), file=sys.stderr)
        return EXIT_USAGE
    parser = build_parser()
    args = parser.parse_args(argv)
    if not Path(args.csv).is_file():
        parser.error("CSV not found: {}".format(args.csv))
    try:
        base_url = validate_base_url(args.base_url, args.allow_custom_host)
    except ValueError as exc:
        parser.error("--base-url: {}".format(exc))
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    log_path = Path.cwd() / "security-review-dates-{}-{}.log".format("apply" if args.apply else "dryrun", stamp)
    try:
        configure_logging(args.verbose, log_path)
    except OSError as exc:
        print("Cannot create audit log {}: {}".format(log_path, exc), file=sys.stderr)
        close_logging()
        return EXIT_USAGE
    try:
        return run(args, base_url, now, stamp, log_path)
    finally:
        close_logging()


if __name__ == "__main__":
    sys.exit(main())
