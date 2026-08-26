"""Pure functions, no network, no credentials. The bulk of the suite lives here."""

from datetime import date
from pathlib import Path

import pytest

from seo_mcp.shaping import (
    HARD_ROW_CAP,
    DateRangeError,
    QueryResult,
    normalise_url,
    resolve_date,
    resolve_range,
    shape,
)


def gsc_rows(n, clicks=1):
    return [
        {"query": f"q{i}", "clicks": clicks, "impressions": 10, "ctr": 0.1, "position": 5.0}
        for i in range(n)
    ]


def result(rows, **kw):
    kw.setdefault("dimension_keys", ("query",))
    kw.setdefault("metric_keys", ("clicks", "impressions", "ctr", "position"))
    kw.setdefault("source", "gsc")
    return QueryResult(rows=rows, **kw)


def test_rows_are_capped_at_top_n():
    out = shape(result(gsc_rows(500)), top_n=10)
    assert len(out["rows"]) == 10
    assert out["summary"]["rows_returned"] == 10


def test_top_n_cannot_exceed_the_hard_cap():
    out = shape(result(gsc_rows(HARD_ROW_CAP + 500)), top_n=99_999)
    assert len(out["rows"]) == HARD_ROW_CAP


def test_totals_cover_every_row_fetched_not_just_the_rows_shown():
    """A model that sees ten rows and a total for ten cannot tell truncation from reality."""
    out = shape(result(gsc_rows(100, clicks=2)), top_n=5)
    assert out["summary"]["totals"]["clicks"] == 200
    assert out["summary"]["rows_matched"] == 100


def test_truncation_is_announced_in_notes():
    out = shape(result(gsc_rows(50)), top_n=5)
    assert any("45 further rows" in n for n in out["notes"])


def test_averaged_metrics_are_never_summed():
    rows = [
        {"query": "a", "clicks": 1, "impressions": 100, "position": 1.0, "ctr": 0.01},
        {"query": "b", "clicks": 1, "impressions": 900, "position": 11.0, "ctr": 0.0011},
    ]
    totals = shape(result(rows), top_n=10)["summary"]["totals"]
    assert totals["impressions"] == 1000
    assert totals["position"] == pytest.approx(10.0)  # impression-weighted, not 12.0
    assert totals["ctr"] == pytest.approx(0.002)  # recomputed from clicks/impressions


def test_rows_are_sorted_by_the_first_metric_descending():
    rows = [{"query": "low", "clicks": 1}, {"query": "high", "clicks": 99}]
    out = shape(
        QueryResult(rows=rows, dimension_keys=("query",), metric_keys=("clicks",), source="gsc")
    )
    assert out["rows"][0]["query"] == "high"


def test_spill_writes_a_csv_with_every_row_and_reports_the_path(tmp_path):
    out = shape(result(gsc_rows(300)), top_n=10, export_dir=tmp_path)
    assert out["export_rows"] == 300
    lines = Path(out["export"]).read_text().splitlines()
    assert len(lines) == 301  # header + 300 rows
    assert lines[0].startswith("query,clicks")


def test_no_spill_and_no_export_key_when_nothing_was_dropped(tmp_path):
    out = shape(result(gsc_rows(5)), top_n=10, export_dir=tmp_path)
    assert "export" not in out
    assert list(tmp_path.iterdir()) == []


def test_notes_from_the_client_survive_shaping():
    out = shape(result(gsc_rows(3), notes=["anonymised"]), top_n=10)
    assert "anonymised" in out["notes"]


def test_empty_result_does_not_explode():
    out = shape(result([]), top_n=10)
    assert out["rows"] == [] and out["summary"]["rows_returned"] == 0


def test_query_result_survives_a_json_round_trip():
    original = result(gsc_rows(2), notes=["n"], meta={"date_range": "a..b"})
    restored = QueryResult.from_dict(original.to_dict())
    assert restored == original
    assert isinstance(restored.dimension_keys, tuple)  # JSON would have made it a list


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://x.com/pricing/", "/pricing"),
        ("/pricing", "/pricing"),
        ("pricing", "/pricing"),
        ("https://x.com/pricing?utm=1", "/pricing"),
        ("https://x.com/", "/"),
        ("https://x.com", "/"),
        ("/a/b/", "/a/b"),
    ],
)
def test_gsc_urls_and_ga4_paths_collapse_onto_one_key(raw, expected):
    assert normalise_url(raw) == expected


def test_query_string_can_be_kept_when_asked():
    assert normalise_url("https://x.com/s?q=1", strip_query=False) == "/s?q=1"


def test_relative_dates_resolve_against_a_fixed_today():
    today = date(2026, 8, 25)
    assert resolve_date("today", today=today) == "2026-08-25"
    assert resolve_date("yesterday", today=today) == "2026-08-24"
    assert resolve_date("28daysAgo", today=today) == "2026-07-28"
    assert resolve_date("2026-01-01", today=today) == "2026-01-01"


@pytest.mark.parametrize("bad", ["last week", "2026-13-01", "", "28 days ago"])
def test_unusable_dates_are_rejected_with_the_accepted_forms(bad):
    with pytest.raises(DateRangeError) as exc:
        resolve_date(bad)
    assert "YYYY-MM-DD" in str(exc.value) or "not a real date" in str(exc.value)


def test_reversed_range_is_rejected():
    with pytest.raises(DateRangeError, match="Swap them"):
        resolve_range("2026-08-01", "2026-07-01")


def test_range_entirely_outside_gsc_retention_is_rejected():
    with pytest.raises(DateRangeError, match="16 months"):
        resolve_range("2019-01-01", "2019-02-01", source="gsc", today=date(2026, 8, 25))


def test_range_touching_the_freshness_edge_is_flagged_not_rejected():
    _, _, notes = resolve_range("7daysAgo", "today", source="gsc", today=date(2026, 8, 25))
    assert any("lags 2-3 days" in n for n in notes)


def test_keep_host_distinguishes_subdomains_of_a_domain_property():
    assert normalise_url("https://x.com/a", keep_host=True) == "x.com/a"
    assert normalise_url("https://blog.x.com/a", keep_host=True) == "blog.x.com/a"
    assert normalise_url("https://x.com/", keep_host=True) == "x.com/"
    # Without a host there is nothing to keep, so GA4 paths are unaffected.
    assert normalise_url("/a", keep_host=True) == "/a"


def test_aggregate_rows_sums_collisions_rather_than_overwriting_them():
    from seo_mcp.shaping import aggregate_rows

    rows = [
        {"page": "https://x.com/p", "clicks": 10, "impressions": 100},
        {"page": "https://x.com/p?utm=1", "clicks": 5, "impressions": 50},
        {"page": "https://x.com/q", "clicks": 1, "impressions": 10},
    ]
    out = aggregate_rows(
        rows,
        key="page",
        metric_keys=("clicks", "impressions"),
        key_fn=lambda r: normalise_url(r["page"]),
    )
    assert set(out) == {"/p", "/q"}
    assert out["/p"]["clicks"] == 15  # not 5
    assert out["/p"]["impressions"] == 150
    assert out["/p"]["_merged_from"] == 2
    assert out["/q"]["_merged_from"] == 1


def test_aggregate_rows_impression_weights_position_across_a_collision():
    from seo_mcp.shaping import aggregate_rows

    rows = [
        {"page": "/p", "impressions": 100, "position": 1.0, "clicks": 1},
        {"page": "/p/", "impressions": 900, "position": 11.0, "clicks": 1},
    ]
    out = aggregate_rows(
        rows,
        key="page",
        metric_keys=("clicks", "impressions", "position"),
        key_fn=lambda r: normalise_url(r["page"]),
    )
    assert out["/p"]["position"] == pytest.approx(10.0)  # weighted, never summed


def test_ga4_rates_are_session_weighted_not_a_mean_of_row_rates():
    """6,788 sessions with 1,586 engaged is 23.4%, whatever the per-row rates average to."""
    rows = [
        {"path": "/a", "sessions": 2581, "engagedSessions": 51, "engagementRate": 0.0198},
        {"path": "/b", "sessions": 252, "engagedSessions": 110, "engagementRate": 0.4365},
    ]
    totals = shape(
        QueryResult(
            rows=rows,
            dimension_keys=("path",),
            metric_keys=("sessions", "engagedSessions", "engagementRate"),
            source="ga4",
        )
    )["summary"]["totals"]
    assert totals["sessions"] == 2833
    assert totals["engagedSessions"] == 161
    assert totals["engagementRate"] == pytest.approx(161 / 2833, abs=1e-4)  # not 0.228


def test_an_averaged_metric_with_no_traffic_column_falls_back_to_a_plain_mean():
    rows = [{"x": "a", "bounceRate": 0.2}, {"x": "b", "bounceRate": 0.4}]
    totals = shape(
        QueryResult(rows=rows, dimension_keys=("x",), metric_keys=("bounceRate",), source="ga4")
    )["summary"]["totals"]
    assert totals["bounceRate"] == pytest.approx(0.3)


def test_totals_of_a_partial_fetch_are_flagged_as_not_being_property_totals():
    out = shape(
        QueryResult(
            rows=gsc_rows(100),
            dimension_keys=("query",),
            metric_keys=("clicks",),
            source="gsc",
            total_rows=30770,
        ),
        top_n=10,
    )
    assert any("NOT the property total" in n for n in out["notes"])


def test_a_complete_fetch_carries_no_partial_totals_warning():
    out = shape(result(gsc_rows(5), total_rows=5), top_n=10)
    assert not any("NOT the property total" in n for n in out["notes"])
