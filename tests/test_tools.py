"""Tool-level behaviour with no credentials present.

MCPServer.call_tool() RAISES ToolError in-process; the is_error=True result is produced
by the protocol layer above it. So assert on the exception, not on a returned result.
"""

import datetime
import json

import httplib2
import pytest
from googleapiclient.errors import HttpError
from mcp.server.mcpserver.exceptions import ToolError

from seo_mcp import config, server
from seo_mcp.auth import scopes, store
from seo_mcp.clients import gsc, indexnow
from seo_mcp.shaping import QueryResult

mcp = server.mcp


def http_error(status, reason="forbidden"):
    return HttpError(
        httplib2.Response({"status": status}),
        json.dumps({"error": {"code": status, "errors": [{"reason": reason}]}}).encode(),
        uri="u",
    )


def authorise(scopes_held=None):
    """Write a usable token so tools get past the auth gate without any network call."""
    profile = config.active_profile()
    expiry = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    store._write_private(
        profile.token_path,
        {
            "token": "t",
            "refresh_token": "r",
            "client_id": "c",
            "client_secret": "s",
            "scopes": scopes_held if scopes_held is not None else sorted(scopes.for_read()),
            "expiry": expiry.replace(tzinfo=None).isoformat(timespec="seconds") + "Z",
            "_seo_mcp": {"granted_at": 0, "version": 1},
        },
    )
    return profile


def raises(exc):
    async def boom(*a, **kw):
        raise exc

    return boom


def returns(result):
    async def ok(*a, **kw):
        return result

    return ok


async def call(name, args):
    """The structured payload a client would show the model.

    call_tool() RAISES ToolError in-process rather than returning is_error=True, so
    every failure case below asserts on the exception instead of on this value.
    """
    result = await mcp.call_tool(name, args)
    assert result.is_error is not True, result.content
    return result.structured_content


# --- registration ------------------------------------------------------------


async def test_all_thirteen_tools_are_registered():
    names = {t.name for t in await mcp.list_tools()}
    assert names == {
        "auth_status",
        "gsc_list_sites",
        "gsc_search_analytics",
        "gsc_compare_periods",
        "gsc_inspect_url",
        "gsc_list_sitemaps",
        "gsc_submit_sitemap",
        "ga4_list_properties",
        "ga4_run_report",
        "ga4_landing_pages",
        "indexnow_verify_key",
        "indexnow_submit",
        "page_report",
    }


async def test_the_two_tools_that_touch_the_live_web_are_marked_destructive():
    """A `confirm` argument is model-controlled; annotations are what a client can gate on."""
    tools = {t.name: t for t in await mcp.list_tools()}
    for name in ("gsc_submit_sitemap", "indexnow_submit"):
        assert tools[name].annotations.destructive_hint is True
        assert tools[name].annotations.read_only_hint is False
    for name in ("gsc_search_analytics", "page_report", "indexnow_verify_key"):
        assert tools[name].annotations.read_only_hint is True


async def test_schemas_come_from_type_hints():
    tool = {t.name: t for t in await mcp.list_tools()}["gsc_search_analytics"]
    props = tool.input_schema["properties"]
    assert set(props) == {
        "site_url",
        "start_date",
        "end_date",
        "dimensions",
        "filters",
        "search_type",
        "row_limit",
        "data_state",
    }
    assert props["row_limit"]["default"] == 250


async def test_every_docstring_carrying_query_rows_warns_about_anonymisation():
    tools = {t.name: t for t in await mcp.list_tools()}
    assert "anonymised" in tools["gsc_search_analytics"].description


# --- the unauthenticated cold start -----------------------------------------


@pytest.mark.parametrize(
    "name,args",
    [
        ("gsc_list_sites", {}),
        ("gsc_search_analytics", {"site_url": "sc-domain:x.com"}),
        ("gsc_inspect_url", {"inspection_url": "https://x.com/a", "site_url": "sc-domain:x.com"}),
        ("ga4_list_properties", {}),
        ("ga4_run_report", {"metrics": ["sessions"], "property_id": "1"}),
        ("page_report", {"url": "https://x.com/a", "site_url": "sc-domain:x.com"}),
    ],
)
async def test_with_no_token_every_tool_returns_an_instruction_not_a_traceback(name, args):
    with pytest.raises(ToolError) as exc:
        await call(name, args)
    message = str(exc.value)
    assert "seo-analytics-mcp auth" in message
    assert "Traceback" not in message


async def test_auth_status_still_answers_with_no_token_at_all():
    """The most likely first-run state. It must never fail."""
    payload = await call("auth_status", {})
    text = json.dumps(payload, default=str)
    assert "seo-analytics-mcp auth --client-secret" in text
    assert '"authorised"' in text


# --- error translation reaches the model ------------------------------------


async def test_403_reaches_the_model_as_an_instruction(monkeypatch):
    authorise()
    monkeypatch.setattr(gsc, "search_analytics", raises(http_error(403)))
    with pytest.raises(ToolError) as exc:
        await call("gsc_search_analytics", {"site_url": "sc-domain:x.com"})
    assert "Users and permissions" in str(exc.value)  # names the fix
    assert "Traceback" not in str(exc.value)


async def test_a_quota_error_names_the_wait(monkeypatch):
    authorise()
    monkeypatch.setattr(gsc, "list_sites", raises(http_error(429, "rateLimitExceeded")))
    with pytest.raises(ToolError, match="15 minutes"):
        await call("gsc_list_sites", {})


# --- argument validation happens before any credential is loaded ------------


async def test_a_missing_identifier_names_the_env_var_that_sets_a_default():
    with pytest.raises(ToolError) as exc:
        await call("gsc_search_analytics", {})
    assert "GSC_DEFAULT_SITE" in str(exc.value)
    assert "gsc_list_sites" in str(exc.value)


async def test_a_configured_default_removes_the_need_for_the_argument(monkeypatch):
    monkeypatch.setenv("GSC_DEFAULT_SITE", "sc-domain:x.com")
    authorise()
    monkeypatch.setattr(
        gsc,
        "search_analytics",
        returns(
            QueryResult(
                rows=[{"query": "a", "clicks": 1}],
                dimension_keys=("query",),
                metric_keys=("clicks",),
                source="gsc",
            )
        ),
    )
    payload = await call("gsc_search_analytics", {})
    assert "sc-domain:x.com" not in json.dumps(payload)  # it was resolved, not echoed as an error


async def test_an_unusable_date_is_rejected_before_the_api_is_called(monkeypatch):
    monkeypatch.setattr(gsc, "search_analytics", raises(AssertionError("must not be called")))
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        await call(
            "gsc_search_analytics", {"site_url": "sc-domain:x.com", "start_date": "last tuesday"}
        )


async def test_a_bad_filter_names_the_accepted_form():
    authorise()
    with pytest.raises(ToolError, match="dimension operator value"):
        await call("gsc_search_analytics", {"site_url": "sc-domain:x.com", "filters": ["page"]})


async def test_ga4_requires_at_least_one_metric():
    with pytest.raises(ToolError, match="At least one metric"):
        await call("ga4_run_report", {"metrics": [], "property_id": "123"})


async def test_a_non_numeric_ga4_property_is_rejected_with_an_example():
    with pytest.raises(ToolError, match="properties/123456789"):
        await call("ga4_run_report", {"metrics": ["sessions"], "property_id": "my site"})


# --- the two tools that write to the outside world --------------------------


async def test_sitemap_submission_is_a_dry_run_unless_confirmed(monkeypatch):
    authorise(scopes_held=sorted(scopes.for_write()))
    monkeypatch.setattr(gsc, "submit_sitemap", raises(AssertionError("must not be called")))
    payload = await call(
        "gsc_submit_sitemap",
        {"feedpath": "https://x.com/sitemap.xml", "site_url": "sc-domain:x.com"},
    )
    assert payload["dry_run"] is True
    assert "confirm=true" in payload["next_step"]


async def test_sitemap_submission_without_the_write_scope_asks_for_it():
    authorise(scopes_held=sorted(scopes.for_read()))
    with pytest.raises(ToolError, match="--enable-writes"):
        await call(
            "gsc_submit_sitemap",
            {
                "feedpath": "https://x.com/sitemap.xml",
                "site_url": "sc-domain:x.com",
                "confirm": True,
            },
        )


async def test_a_relative_feedpath_is_rejected():
    authorise(scopes_held=sorted(scopes.for_write()))
    with pytest.raises(ToolError, match="full sitemap URL"):
        await call(
            "gsc_submit_sitemap", {"feedpath": "/sitemap.xml", "site_url": "sc-domain:x.com"}
        )


async def test_indexnow_needs_a_configured_key_and_never_takes_one_as_an_argument():
    tool = {t.name: t for t in await mcp.list_tools()}["indexnow_submit"]
    assert "key" not in tool.input_schema["properties"]
    with pytest.raises(ToolError, match="INDEXNOW_KEY"):
        await call("indexnow_submit", {"urls": ["https://x.com/a"], "host": "x.com"})


async def test_indexnow_dry_run_returns_a_token_and_submits_nothing(monkeypatch):
    monkeypatch.setenv("INDEXNOW_KEY", "abcdef1234567890")
    monkeypatch.setenv("INDEXNOW_HOST", "x.com")
    monkeypatch.setattr(indexnow, "verify_key", returns(indexnow.KeyCheck(True, "verified")))
    monkeypatch.setattr(indexnow, "submit", raises(AssertionError("must not be called")))
    payload = await call("indexnow_submit", {"urls": ["https://x.com/a", "https://x.com/b"]})
    assert payload["dry_run"] is True
    assert len(payload["submission_token"]) == 16
    assert "Google does not participate" in payload["google_note"]


async def test_confirm_alone_is_not_enough_to_submit(monkeypatch):
    """confirm is a model-controlled argument. The token proves a dry run actually
    happened and that the URL list has not changed since the user saw it."""
    monkeypatch.setenv("INDEXNOW_KEY", "abcdef1234567890")
    monkeypatch.setenv("INDEXNOW_HOST", "x.com")
    monkeypatch.setattr(indexnow, "verify_key", returns(indexnow.KeyCheck(True, "verified")))
    monkeypatch.setattr(indexnow, "submit", raises(AssertionError("must not be called")))
    with pytest.raises(ToolError, match="submission_token"):
        await call("indexnow_submit", {"urls": ["https://x.com/a"], "confirm": True})


async def test_a_token_from_a_different_url_set_is_rejected(monkeypatch):
    monkeypatch.setenv("INDEXNOW_KEY", "abcdef1234567890")
    monkeypatch.setenv("INDEXNOW_HOST", "x.com")
    monkeypatch.setattr(indexnow, "verify_key", returns(indexnow.KeyCheck(True, "verified")))
    monkeypatch.setattr(indexnow, "submit", raises(AssertionError("must not be called")))
    dry = await call("indexnow_submit", {"urls": ["https://x.com/a"]})
    with pytest.raises(ToolError, match="does not match"):
        await call(
            "indexnow_submit",
            {
                "urls": ["https://x.com/a", "https://x.com/evil"],
                "confirm": True,
                "submission_token": dry["submission_token"],
            },
        )


async def test_the_matching_token_does_submit(monkeypatch):
    monkeypatch.setenv("INDEXNOW_KEY", "abcdef1234567890")
    monkeypatch.setenv("INDEXNOW_HOST", "x.com")
    sent = []

    async def fake_submit(host, key, urls, **kw):
        sent.append(urls)
        return [
            indexnow.SubmitResult(
                endpoint=kw.get("endpoint", indexnow.SHARED_ENDPOINT),
                count=len(urls),
                status=202,
                meaning=indexnow.STATUS_MEANING[202],
            )
        ]

    monkeypatch.setattr(indexnow, "verify_key", returns(indexnow.KeyCheck(True, "verified")))
    monkeypatch.setattr(indexnow, "submit", fake_submit)
    dry = await call("indexnow_submit", {"urls": ["https://x.com/a"]})
    done = await call(
        "indexnow_submit",
        {
            "urls": ["https://x.com/a"],
            "confirm": True,
            "submission_token": dry["submission_token"],
        },
    )
    assert sent == [["https://x.com/a"]]
    assert done["results"][0]["http_status"] == 202  # never flattened to a boolean
    assert done["results"][0]["meaning"].startswith("Accepted")


async def test_a_failing_key_check_aborts_before_any_submission(monkeypatch):
    monkeypatch.setenv("INDEXNOW_KEY", "abcdef1234567890")
    monkeypatch.setenv("INDEXNOW_HOST", "x.com")
    monkeypatch.setattr(
        indexnow, "verify_key", returns(indexnow.KeyCheck(False, "404 at key file"))
    )
    monkeypatch.setattr(indexnow, "submit", raises(AssertionError("must not be called")))
    with pytest.raises(ToolError, match="nothing was submitted"):
        await call("indexnow_submit", {"urls": ["https://x.com/a"], "confirm": True})


async def test_urls_on_another_host_are_refused(monkeypatch):
    monkeypatch.setenv("INDEXNOW_KEY", "abcdef1234567890")
    monkeypatch.setenv("INDEXNOW_HOST", "x.com")
    with pytest.raises(ToolError, match="not on host"):
        await call("indexnow_submit", {"urls": ["https://evil.com/a"]})


async def test_an_unknown_endpoint_is_refused(monkeypatch):
    monkeypatch.setenv("INDEXNOW_KEY", "abcdef1234567890")
    monkeypatch.setenv("INDEXNOW_HOST", "x.com")
    with pytest.raises(ToolError, match="not a known IndexNow endpoint"):
        await call(
            "indexnow_submit",
            {"urls": ["https://x.com/a"], "endpoint": "https://evil.example/indexnow"},
        )


# --- composite tools --------------------------------------------------------


async def test_page_report_survives_a_ga4_failure_and_says_what_it_lost(monkeypatch):
    authorise()
    monkeypatch.setenv("GA4_DEFAULT_PROPERTY", "properties/1")
    monkeypatch.setattr(
        gsc,
        "search_analytics",
        returns(
            QueryResult(
                rows=[{"date": "2026-08-01", "clicks": 5, "impressions": 50}],
                dimension_keys=("date",),
                metric_keys=("clicks", "impressions"),
                source="gsc",
            )
        ),
    )
    monkeypatch.setattr(gsc, "inspect_url", returns({"verdict": "PASS"}))
    from seo_mcp.clients import ga4

    monkeypatch.setattr(ga4, "run_report", raises(http_error(403)))
    payload = await call(
        "page_report", {"url": "https://x.com/pricing", "site_url": "sc-domain:x.com"}
    )
    assert payload["search_console"]["totals"]["clicks"] == 5
    assert payload["index_status"]["verdict"] == "PASS"
    assert any("GA4 engagement could not be included" in n for n in payload["notes"])


async def test_page_report_says_so_when_no_ga4_property_is_configured(monkeypatch):
    authorise()
    monkeypatch.setattr(
        gsc,
        "search_analytics",
        returns(
            QueryResult(rows=[], dimension_keys=("date",), metric_keys=("clicks",), source="gsc")
        ),
    )
    monkeypatch.setattr(gsc, "inspect_url", returns({"verdict": "PASS"}))
    payload = await call("page_report", {"url": "https://x.com/a", "site_url": "sc-domain:x.com"})
    assert any("GA4_DEFAULT_PROPERTY" in n for n in payload["notes"])


async def test_compare_periods_reports_both_directions_and_the_position_caveat(monkeypatch):
    authorise()
    # The two windows are fetched concurrently, so the fake keys on the date it is
    # given: /up gains clicks in the recent window, /down loses them.
    anchor = datetime.date.today() - datetime.timedelta(days=3)
    current_start = (anchor - datetime.timedelta(days=27)).isoformat()

    async def fake(creds, site_url, start, end, **kw):
        recent = start == current_start
        rows = [
            {"page": "https://x.com/up", "clicks": 100 if recent else 1},
            {"page": "https://x.com/down", "clicks": 1 if recent else 100},
        ]
        return QueryResult(
            rows=rows, dimension_keys=("page",), metric_keys=("clicks",), source="gsc"
        )

    monkeypatch.setattr(gsc, "search_analytics", fake)
    payload = await call(
        "gsc_compare_periods", {"site_url": "sc-domain:x.com", "dimension": "page"}
    )
    assert payload["gained"] and payload["declined"]
    assert payload["gained"][0]["delta"] > 0 >= payload["declined"][0]["delta"]
    # Normalised for a reliable join, but the host is KEPT: a domain property spans
    # subdomains, so x.com/ and blog.x.com/ are different pages.
    assert payload["gained"][0]["page"] == "x.com/up"


async def test_compare_periods_rejects_a_dimension_google_does_not_have():
    with pytest.raises(ToolError, match="not a Search Console dimension"):
        await call("gsc_compare_periods", {"site_url": "sc-domain:x.com", "dimension": "browser"})


async def test_hiding_write_tools_needs_a_token_to_decide_on():
    """With no token every tool stays visible so the model can explain what to do."""
    assert server.hide_ungranted_write_tools() == []
    assert "gsc_submit_sitemap" in {t.name for t in await mcp.list_tools()}


async def test_two_subdomains_are_not_merged_into_one_page(monkeypatch):
    """On a domain property Search Console returns every subdomain. Stripping the host
    turned x.com/ and blogs.x.com/ into one row, and the last one written won - which
    reported a real 713-click homepage as a 2-click collapse."""
    authorise()
    anchor = datetime.date.today() - datetime.timedelta(days=3)
    current_start = (anchor - datetime.timedelta(days=27)).isoformat()

    async def fake(creds, site_url, start, end, **kw):
        recent = start == current_start
        rows = [
            {"page": "https://x.com/", "clicks": 713 if recent else 689},
            {"page": "https://blogs.x.com/", "clicks": 2 if recent else 40},
        ]
        return QueryResult(
            rows=rows, dimension_keys=("page",), metric_keys=("clicks",), source="gsc"
        )

    monkeypatch.setattr(gsc, "search_analytics", fake)
    args = {"site_url": "sc-domain:x.com", "dimension": "page"}
    payload = await call("gsc_compare_periods", args)
    pages = {r["page"]: r for r in payload["gained"] + payload["declined"]}
    assert set(pages) == {"x.com/", "blogs.x.com/"}
    assert pages["x.com/"]["clicks_current"] == 713  # not 2
    assert pages["x.com/"]["delta"] == 24
    assert pages["blogs.x.com/"]["clicks_current"] == 2  # its own page, its own numbers


async def test_rows_that_do_collapse_onto_one_key_are_summed_not_dropped(monkeypatch):
    authorise()

    anchor = datetime.date.today() - datetime.timedelta(days=3)
    current_start = (anchor - datetime.timedelta(days=27)).isoformat()

    async def fake(creds, site_url, start, end, **kw):
        # Same page reached with and without a query string: one real page, two rows.
        recent = start == current_start
        return QueryResult(
            rows=[
                {"page": "https://x.com/p", "clicks": 10 if recent else 4},
                {"page": "https://x.com/p?utm=ads", "clicks": 5 if recent else 2},
            ],
            dimension_keys=("page",),
            metric_keys=("clicks",),
            source="gsc",
        )

    monkeypatch.setattr(gsc, "search_analytics", fake)
    args = {"site_url": "sc-domain:x.com", "dimension": "page"}
    payload = await call("gsc_compare_periods", args)
    rows = payload["gained"] + payload["declined"]
    assert [r["page"] for r in rows] == ["x.com/p"]
    assert rows[0]["clicks_current"] == 15  # 10 + 5, not 5
    assert rows[0]["clicks_previous"] == 6  # 4 + 2, not 2
    assert any("were summed into it" in n for n in payload["notes"])


async def test_a_page_that_did_not_move_is_in_neither_list(monkeypatch):
    authorise()

    async def fake(creds, site_url, start, end, **kw):
        return QueryResult(
            rows=[{"page": "https://x.com/flat", "clicks": 7}],
            dimension_keys=("page",),
            metric_keys=("clicks",),
            source="gsc",
        )

    monkeypatch.setattr(gsc, "search_analytics", fake)
    payload = await call("gsc_compare_periods", {"site_url": "sc-domain:x.com"})
    assert payload["gained"] == [] and payload["declined"] == []
    assert payload["summary"]["rows_compared"] == 1
    assert payload["summary"]["rows_that_moved"] == 0
