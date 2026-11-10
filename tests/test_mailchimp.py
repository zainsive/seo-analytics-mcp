"""Mailchimp client and tools. No network: every HTTP call is intercepted."""

import json

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from seo_mcp.clients import mailchimp
from seo_mcp.server import mcp
from seo_mcp.shaping import QueryResult, shape

# Deliberately NOT shaped like a real key. A fixture of 32 hex characters plus a
# data-centre suffix is indistinguishable from a live credential, and GitHub's secret
# scanner blocks the push - correctly, since no scanner can tell a fake from the real
# thing. Only the suffix matters to the code under test.
KEY = "fixture-key-not-a-real-credential-us7"


# --- data centre routing ---------------------------------------------------


def test_the_base_url_comes_from_the_key_suffix():
    """A key ending -us7 must go to us7.api.mailchimp.com; the bare host 404s."""
    assert mailchimp.data_center(KEY) == "us7"
    assert mailchimp._base(KEY) == "https://us7.api.mailchimp.com/3.0"


@pytest.mark.parametrize("bad", ["nosuffix", "", "abc-xx", "key-"])
def test_a_key_with_no_data_centre_is_rejected_with_the_reason(bad):
    with pytest.raises(ToolError, match="data-centre suffix"):
        mailchimp.data_center(bad)


# --- error translation -----------------------------------------------------


def _resp(status, body=None):
    return httpx.Response(
        status,
        content=json.dumps(body or {}).encode(),
        headers={"content-type": "application/json"},
    )


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (401, {"detail": "Your API key may be invalid"}, "rejected the API key"),
        (403, {"detail": "forbidden"}, "scoped to one account"),
        (404, {"detail": "not found"}, "mailchimp_list_campaigns"),
        (429, {}, "10 simultaneous connections"),
        (500, {}, "transient"),
        (400, {"detail": "Bad date"}, "Bad date"),
    ],
)
def test_every_failure_explains_itself(status, body, expected):
    assert expected in str(mailchimp._fail(_resp(status, body)))


def test_a_non_json_error_body_still_produces_a_message():
    r = httpx.Response(400, content=b"<html>nope</html>")
    assert "400" in str(mailchimp._fail(r))


# --- the client, against a stubbed transport -------------------------------


def _transport(routes):
    def handler(request: httpx.Request):
        for path, payload in routes.items():
            if request.url.path.endswith(path):
                return _resp(200, payload)
        return _resp(404, {"detail": f"no stub for {request.url.path}"})

    return httpx.MockTransport(handler)


@pytest.fixture
def stub(monkeypatch):
    """Intercept the client's own AsyncClient so nothing leaves the machine."""

    def install(routes):
        real = httpx.AsyncClient

        def factory(*a, **kw):
            kw["transport"] = _transport(routes)
            return real(*a, **kw)

        monkeypatch.setattr(mailchimp.httpx, "AsyncClient", factory)

    return install


async def test_audiences_are_normalised_and_rates_converted(stub):
    stub(
        {
            "/lists": {
                "total_items": 1,
                "lists": [
                    {
                        "id": "abc",
                        "name": "Newsletter",
                        "date_created": "2025-02-03T10:00:00+00:00",
                        "stats": {
                            "member_count": 1200,
                            "unsubscribe_count": 30,
                            "open_rate": 42.5,  # Mailchimp sends percentages here
                            "click_rate": 3.25,
                        },
                    }
                ],
            }
        }
    )
    result = await mailchimp.list_audiences(KEY)
    row = result.rows[0]
    assert row["audience_id"] == "abc" and row["members"] == 1200
    assert row["created"] == "2025-02-03"
    assert row["open_rate"] == 0.425  # converted to a fraction, like every other source
    assert any("lifetime averages" in n for n in result.notes)


async def test_an_empty_account_says_why_rather_than_returning_nothing(stub):
    stub({"/lists": {"total_items": 0, "lists": []}})
    result = await mailchimp.list_audiences(KEY)
    assert result.rows == []
    assert any("different account" in n for n in result.notes)


async def test_campaigns_only_include_finished_sends(stub):
    captured = {}
    real = httpx.AsyncClient

    def handler(request):
        captured.update(dict(request.url.params))
        return _resp(200, {"total_items": 0, "campaigns": []})

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    import seo_mcp.clients.mailchimp as m

    m.httpx.AsyncClient = factory
    try:
        result = await mailchimp.list_campaigns(KEY, since="2026-01-01", limit=10)
    finally:
        m.httpx.AsyncClient = real
    assert captured["status"] == "sent"
    assert captured["since_send_time"].startswith("2026-01-01T")
    assert any("drafts and" in n for n in result.notes)


async def test_clicked_links_carry_a_path_that_joins_to_the_other_sources(stub):
    stub(
        {
            "/click-details": {
                "total_items": 2,
                "urls_clicked": [
                    {
                        "url": "https://stgconsulting.com/services/fractional-cto/?utm_source=mc",
                        "total_clicks": 40,
                        "unique_clicks": 31,
                    },
                    {
                        "url": "https://stgconsulting.com/about-us",
                        "total_clicks": 9,
                        "unique_clicks": 8,
                    },
                ],
            }
        }
    )
    result = await mailchimp.campaign_links(KEY, "camp1")
    # The same normalisation Search Console pages and GA4 landing pages go through.
    assert result.rows[0]["path"] == "/services/fractional-cto"
    assert result.rows[1]["path"] == "/about-us"
    assert any("directly comparable" in n for n in result.notes)


async def test_a_campaign_report_recomputes_rates_from_components(stub):
    stub(
        {
            "/reports/camp1": {
                "emails_sent": 1000,
                "subject_line": "November update",
                "send_time": "2026-11-02T09:00:00+00:00",
                "list_name": "Newsletter",
                "opens": {"unique_opens": 410, "open_rate": 0.41},
                "clicks": {"unique_subscriber_clicks": 62, "click_rate": 0.062},
                "unsubscribed": 7,
                "bounces": {"hard_bounces": 2, "soft_bounces": 3},
                "abuse_reports": 1,
            }
        }
    )
    r = await mailchimp.campaign_report(KEY, "camp1")
    assert r["emails_sent"] == 1000 and r["unique_opens"] == 410
    assert r["bounces"] == 5 and r["bounce_rate"] == 0.005
    assert r["unsubscribes"] == 7 and r["unsubscribe_rate"] == 0.007
    assert r["sent"] == "2026-11-02"


async def test_a_campaign_with_no_sends_does_not_divide_by_zero(stub):
    stub({"/reports/camp0": {"emails_sent": 0, "opens": {}, "clicks": {}, "bounces": {}}})
    r = await mailchimp.campaign_report(KEY, "camp0")
    assert r["bounce_rate"] is None and r["unsubscribe_rate"] is None


# --- rate arithmetic -------------------------------------------------------


def test_rates_across_campaigns_are_recomputed_not_averaged():
    """Five campaigns at 40% do not total 200%. This is the bug class that has already
    appeared twice, for Search Console CTR and GA4 engagement rate."""
    rows = [
        {"c": "A", "emails_sent": 9000, "unique_opens": 900, "open_rate": 0.10},
        {"c": "B", "emails_sent": 1000, "unique_opens": 600, "open_rate": 0.60},
    ]
    totals = shape(
        QueryResult(
            rows=rows,
            dimension_keys=("c",),
            metric_keys=("emails_sent", "unique_opens", "open_rate"),
            source="gsc",
        )
    )["summary"]["totals"]
    assert totals["emails_sent"] == 10000
    assert totals["open_rate"] == pytest.approx(0.15)  # 1500/10000, not 0.35 or 0.70


# --- tools -----------------------------------------------------------------


async def test_the_api_key_is_never_a_tool_argument():
    """Tool arguments are visible in the transcript and may be logged by the client."""
    tools = {t.name: t for t in await mcp.list_tools()}
    for name in (
        "mailchimp_list_audiences",
        "mailchimp_list_campaigns",
        "mailchimp_campaign_report",
        "mailchimp_campaign_links",
    ):
        props = tools[name].input_schema.get("properties", {})
        assert not any("key" in p.lower() for p in props), name


async def test_every_mailchimp_tool_is_marked_read_only():
    tools = {t.name: t for t in await mcp.list_tools()}
    for name, tool in tools.items():
        if name.startswith("mailchimp_"):
            assert tool.annotations.read_only_hint is True, name


@pytest.mark.parametrize(
    "name,args",
    [
        ("mailchimp_list_audiences", {}),
        ("mailchimp_list_campaigns", {}),
        ("mailchimp_campaign_report", {"campaign_id": "x"}),
        ("mailchimp_campaign_links", {"campaign_id": "x"}),
    ],
)
async def test_with_no_key_configured_every_tool_names_the_setting(name, args):
    with pytest.raises(ToolError) as exc:
        await mcp.call_tool(name, args)
    assert "MAILCHIMP_API_KEY" in str(exc.value)
    assert "Account & billing" in str(exc.value)


async def test_a_reversed_date_range_is_rejected_before_the_api_is_called(monkeypatch):
    monkeypatch.setenv("MAILCHIMP_API_KEY", KEY)
    with pytest.raises(ToolError, match="Swap them"):
        await mcp.call_tool(
            "mailchimp_list_campaigns", {"since": "2026-06-01", "before": "2026-01-01"}
        )


async def test_an_unusable_date_names_the_accepted_forms(monkeypatch):
    monkeypatch.setenv("MAILCHIMP_API_KEY", KEY)
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        await mcp.call_tool("mailchimp_list_campaigns", {"since": "last spring"})
