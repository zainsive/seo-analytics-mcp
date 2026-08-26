"""Row shaping: the spine of the server.

RULE 4 - this module imports no Google library. The row logic is the part most
likely to be wrong, so it must be unit-testable with no credentials and no network.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal, TypedDict
from urllib.parse import urlsplit

try:  # pragma: no cover - 3.11+
    from typing import NotRequired
except ImportError:  # pragma: no cover - 3.10
    from typing_extensions import NotRequired

HARD_ROW_CAP = 1000
DEFAULT_TOP_N = 250
MAX_EXPORTS_KEPT = 50

# Metrics that must never be summed. Rates and averages are recomputed from their
# components where possible, and plain-averaged otherwise.
_AVERAGED = frozenset(
    {
        "position",
        "ctr",
        "engagementRate",
        "bounceRate",
        "averageSessionDuration",
        "userEngagementDuration",
        "sessionsPerUser",
        "screenPageViewsPerSession",
    }
)


@dataclass(frozen=True)
class QueryResult:
    """Normalised upstream payload. Every client returns this and nothing else,
    which is why shaping.py never imports a Google library."""

    rows: list[dict[str, Any]]
    dimension_keys: tuple[str, ...]
    metric_keys: tuple[str, ...]
    source: Literal["gsc", "ga4"]
    total_rows: int | None = None  # upstream availability, when known
    meta: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)  # caveats travel WITH the data

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "dimension_keys": list(self.dimension_keys),
            "metric_keys": list(self.metric_keys),
            "source": self.source,
            "total_rows": self.total_rows,
            "meta": self.meta,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> QueryResult:
        """JSON turns tuples into lists; coerce them back so callers can rely on
        the declared types after a cache round-trip."""
        return cls(
            rows=list(raw["rows"]),
            dimension_keys=tuple(raw.get("dimension_keys") or ()),
            metric_keys=tuple(raw.get("metric_keys") or ()),
            source=raw["source"],
            total_rows=raw.get("total_rows"),
            meta=dict(raw.get("meta") or {}),
            notes=list(raw.get("notes") or []),
        )


class ShapedResponse(TypedDict):
    """What every read tool returns. Four keys, always the same shape."""

    summary: dict[str, Any]  # totals, counts, date range, data_state, quota
    rows: list[dict[str, Any]]  # capped at min(top_n, HARD_ROW_CAP)
    notes: list[str]  # anonymisation, truncation, freshness
    export: NotRequired[str]  # CSV path, present only when rows spilled
    export_rows: NotRequired[int]


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


# A rate is never the mean of per-row rates. Where both components are present, the
# rate is recomputed from them; otherwise it is weighted by traffic.
_RATIOS = (("ctr", "clicks", "impressions"), ("engagementRate", "engagedSessions", "sessions"))
_WEIGHT_KEYS = ("impressions", "sessions")


def _totals(rows: list[dict[str, Any]], metric_keys: tuple[str, ...] | list[str]) -> dict[str, Any]:
    """Totals over every row fetched - additive metrics summed, rates recomputed.

    Summing 'position' across 250 rows is meaningless, so averaged metrics are weighted
    by whichever traffic column the source provides (impressions for Search Console,
    sessions for GA4) and plain-averaged only when neither is present.
    """
    out: dict[str, Any] = {}
    weight_key = next((k for k in _WEIGHT_KEYS if any(r.get(k) is not None for r in rows)), None)
    weights = [_num(r.get(weight_key)) or 0.0 if weight_key else 0.0 for r in rows]
    weight_total = sum(weights)

    for key in metric_keys:
        values = [_num(r.get(key)) for r in rows]
        present = [v for v in values if v is not None]
        if not present:
            continue
        if key not in _AVERAGED:
            total = sum(present)
            out[key] = int(total) if total.is_integer() else round(total, 4)
            continue
        if weight_total > 0:
            weighted = sum(
                (v or 0.0) * w for v, w in zip(values, weights, strict=True) if v is not None
            )
            out[key] = round(weighted / weight_total, 4)
        else:
            out[key] = round(sum(present) / len(present), 4)

    # Prefer the exact ratio wherever both of its components were summed above.
    for rate, numerator, denominator in _RATIOS:
        top, bottom = out.get(numerator), out.get(denominator)
        if isinstance(top, (int, float)) and isinstance(bottom, (int, float)) and bottom:
            out[rate] = round(top / bottom, 4)
    return out


def _prune_dir(directory: Path, keep: int) -> None:
    """Exports accumulate forever otherwise; keep the newest `keep` files."""
    try:
        files = sorted(
            (p for p in directory.glob("*.csv") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:  # pragma: no cover - unreadable directory
        return
    for stale in files[keep:]:
        stale.unlink(missing_ok=True)


def _spill(rows: list[dict[str, Any]], columns: list[str], export_dir: Path, digest: str) -> Path:
    """Write the full row set beside the response. This file is for the human -
    the model cannot usually read local paths, so `summary` must stand alone."""
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / f"{digest}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    _prune_dir(export_dir, MAX_EXPORTS_KEPT)
    return path


def shape(
    result: QueryResult,
    *,
    top_n: int = DEFAULT_TOP_N,
    sort_by: str | None = None,
    export_dir: Path | None = None,
) -> ShapedResponse:
    """Cap rows, total over the FULL set, spill the remainder to CSV."""
    top_n = max(1, min(top_n, HARD_ROW_CAP))
    rows = list(result.rows)
    columns = list(result.dimension_keys) + list(result.metric_keys)

    key = sort_by or (result.metric_keys[0] if result.metric_keys else None)
    if key and all(key in r for r in rows):
        rows.sort(key=lambda r: r.get(key) or 0, reverse=True)

    summary: dict[str, Any] = {
        "source": result.source,
        "rows_returned": min(len(rows), top_n),
        "rows_matched": result.total_rows if result.total_rows is not None else len(rows),
        "dimensions": list(result.dimension_keys),
        "totals": _totals(rows, result.metric_keys),  # over every row fetched
        **result.meta,
    }
    out: ShapedResponse = {
        "summary": summary,
        "rows": rows[:top_n],
        "notes": list(result.notes),
    }

    if result.total_rows is not None and result.total_rows > len(rows):
        # Upstream holds more rows than were fetched, so these totals are the totals of
        # the top slice - not of the property. Models will otherwise report them as such.
        out["notes"].append(
            f"Upstream reports {result.total_rows:,} matching rows but only {len(rows):,} "
            "were fetched, so 'totals' covers that slice and is NOT the property total."
        )

    if len(rows) > top_n:
        out["notes"].append(
            f"{len(rows) - top_n} further rows were not included inline. "
            "The totals above cover every row fetched, not just the rows shown."
        )
        if export_dir is not None:
            digest = hashlib.sha256(
                json.dumps(
                    [result.source, columns, len(rows), summary["totals"]],
                    sort_keys=True,
                    default=str,
                ).encode()
            ).hexdigest()[:16]
            out["export"] = str(_spill(rows, columns, export_dir, digest))
            out["export_rows"] = len(rows)
    return out


def normalise_url(value: str, *, strip_query: bool = True, keep_host: bool = False) -> str:
    """Collapse GSC absolute URLs and GA4 paths onto one comparable key.
    'https://x.com/pricing/' and '/pricing' and 'pricing' all become '/pricing'.

    keep_host=True prefixes the host when there is one. Use it whenever both sides of a
    comparison come from Search Console: a DOMAIN property returns rows for every
    subdomain, so 'https://x.com/' and 'https://blog.x.com/' are different pages that
    would otherwise both become '/'. Leave it False to join against GA4, which reports
    paths without a host.
    """
    parts = urlsplit(value if "://" in value or value.startswith("/") else "/" + value)
    path, query = parts.path or "/", parts.query
    if not strip_query and query:
        path = f"{path}?{query}"
    path = (path.rstrip("/") if len(path) > 1 else path) or "/"
    if keep_host and parts.hostname:
        return f"{parts.hostname}{path}"
    return path


def aggregate_rows(
    rows: list[dict[str, Any]],
    *,
    key: str,
    metric_keys: tuple[str, ...] | list[str],
    key_fn: Any = None,
) -> dict[str, dict[str, Any]]:
    """Group rows by a derived key, COMBINING metrics rather than overwriting.

    Any keying that is not one-to-one - normalised URLs above all - will collide, and a
    plain dict comprehension silently keeps whichever row happened to be written last.
    That turns two real pages into one wrong number, so collisions are summed here
    (rates impression-weighted, exactly as summary totals are).
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        derived = key_fn(row) if key_fn else str(row.get(key) or "")
        grouped.setdefault(derived, []).append(row)
    return {
        derived: {key: derived, **_totals(members, metric_keys), "_merged_from": len(members)}
        for derived, members in grouped.items()
    }


# --- dates -------------------------------------------------------------------
# Models are unreliable about today's date, and a silently empty result reads as
# "traffic went to zero". Relative tokens are resolved here, once, and the
# resolved absolute range is echoed back in every response.

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RELATIVE = re.compile(r"^(\d+)daysAgo$", re.IGNORECASE)
GSC_RETENTION_DAYS = 16 * 30  # Search Console keeps ~16 months
GSC_FINAL_LAG_DAYS = 3


class DateRangeError(ValueError):
    """Raised for a range no upstream API can answer. Carries the fix in its text."""


def resolve_date(value: str, *, today: date | None = None) -> str:
    """Accept 'YYYY-MM-DD', 'today', 'yesterday' or 'NdaysAgo' (GA4's own syntax)
    and return an absolute ISO date, so GSC and GA4 can be asked the same question."""
    today = today or date.today()
    text = (value or "").strip()
    if _ISO.match(text):
        try:
            date.fromisoformat(text)
        except ValueError as exc:
            raise DateRangeError(f"{value!r} is not a real date: {exc}.") from exc
        return text
    lowered = text.lower()
    if lowered == "today":
        return today.isoformat()
    if lowered == "yesterday":
        return (today - timedelta(days=1)).isoformat()
    if match := _RELATIVE.match(text):
        return (today - timedelta(days=int(match.group(1)))).isoformat()
    raise DateRangeError(
        f"{value!r} is not a usable date. Use YYYY-MM-DD, 'today', 'yesterday' "
        "or 'NdaysAgo' (for example '28daysAgo')."
    )


def resolve_range(
    start_date: str, end_date: str, *, source: str = "gsc", today: date | None = None
) -> tuple[str, str, list[str]]:
    """Resolve both ends, order-check them, and warn about ranges with no data."""
    today = today or date.today()
    start, end = resolve_date(start_date, today=today), resolve_date(end_date, today=today)
    if start > end:
        raise DateRangeError(f"start_date {start} is after end_date {end}. Swap them.")
    notes: list[str] = []
    if source == "gsc":
        oldest = today - timedelta(days=GSC_RETENTION_DAYS)
        if end < oldest.isoformat():
            raise DateRangeError(
                f"Search Console keeps about 16 months of data; {start}..{end} is "
                f"entirely older than {oldest.isoformat()} and will return no rows."
            )
        if start < oldest.isoformat():
            notes.append(
                f"Search Console retains about 16 months: rows before "
                f"{oldest.isoformat()} are missing from this range."
            )
        fresh_edge = (today - timedelta(days=GSC_FINAL_LAG_DAYS)).isoformat()
        if end > fresh_edge:
            notes.append(
                f"Search Console data lags 2-3 days; {fresh_edge}..{end} may be "
                "incomplete or empty regardless of data_state."
            )
    return start, end, notes
