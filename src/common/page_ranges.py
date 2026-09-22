"""Shared page-range parsing for parameterized operations (split, rotate).

Syntax (1-indexed, inclusive):
  "5"    -> (5, 5)
  "1-3"  -> (1, 3)
Comma-joined strings are split by the caller into tokens; surrounding
whitespace inside a token is tolerated.

resolve_ranges() additionally enforces, against the document page count:
  * at most max_ranges ranges
  * every page within 1..page_count
  * no duplicate or overlapping ranges (each source page at most once)

Both functions raise RangeError; operations translate it into their own
user-facing error type so handler/result contracts stay per-operation.
"""
import re

_RANGE_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")
_SINGLE_RE = re.compile(r"^\s*(\d+)\s*$")


class RangeError(Exception):
    """Invalid page-range selection (message is safe to surface)."""


def parse_page_range(token):
    """Parse one range token to (start, end), 1-indexed inclusive.

    Raises RangeError for malformed, zero, negative, or reversed ranges.
    Page-count bounds are NOT checked here (see resolve_ranges).
    """
    if not isinstance(token, str):
        raise RangeError("invalid page range: %r" % (token,))
    single = _SINGLE_RE.match(token)
    if single:
        page = int(single.group(1))
        if page < 1:
            raise RangeError("invalid page range %r: pages start at 1" % token)
        return (page, page)
    span = _RANGE_RE.match(token)
    if span:
        start, end = int(span.group(1)), int(span.group(2))
        if start < 1 or end < 1:
            raise RangeError("invalid page range %r: pages start at 1" % token)
        if start > end:
            raise RangeError(
                "invalid page range %r: start page exceeds end page" % token)
        return (start, end)
    raise RangeError(
        "invalid page range %r: use '5' or '1-3'" % token)


def resolve_ranges(tokens, page_count, max_ranges):
    """Validate tokens against the PDF and return [(start, end)] in order.

    Raises RangeError on too many ranges, malformed ranges, pages outside
    1..page_count, or duplicate/overlapping ranges.
    """
    if len(tokens) > max_ranges:
        raise RangeError(
            "too many ranges: %d exceeds the limit of %d"
            % (len(tokens), max_ranges))
    resolved = [parse_page_range(t) for t in tokens]
    covered = set()
    for token, (start, end) in zip(tokens, resolved):
        if end > page_count:
            raise RangeError(
                "page range %r exceeds the document (%d pages)"
                % (token, page_count))
        overlap = [p for p in range(start, end + 1) if p in covered]
        if overlap:
            raise RangeError(
                "page range %r overlaps an earlier range" % token)
        covered.update(range(start, end + 1))
    return resolved
