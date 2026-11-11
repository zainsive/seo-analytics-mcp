"""The LinkedIn client."""

from __future__ import annotations

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from seo_mcp.clients import linkedin
from seo_mcp.shaping import shape

# Deliberately not shaped like a real OAuth token, so no scanner can mistake it for one.
TOKEN = "fixture-token-not-a-real-credential"
ORG = "1234567"
URN = f"urn:li:organization:{ORG}"


@pytest.fixture
def stub(monkeypatch):
    """Serve canned JSON by path suffix, and record what was requested."""
    seen: list[httpx.Request] = []

    def install(routes: dict[str, dict], status: int = 200):
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            for suffix, body in routes.items():
                if suffix in str(request.url):
                    return httpx.Response(status, json=body)
            return httpx.Response(404, json={"message": "no stub for this path"})

        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient

        def patched(*a, **kw):
            kw["transport"] = transport
            return original(*a, **kw)

        monkeypatch.setattr(httpx, "AsyncClient", patched)
        return seen

    return install


# --- identifiers --------------------------------------------------------------


@pytest.mark.parametrize(
    "given",
    [ORG, URN, "https://www.linkedin.com/company/1234567", "company/1234567/"],
)
def test_an_organization_is_accepted_in_any_form_a_model_might_produce(given):
    assert linkedin.organization_urn(given) == URN


@pytest.mark.parametrize("bad", ["", "   ", "software-technology-group", "urn:li:person:99"])
def test_something_that_is_not_an_organization_id_is_explained(bad):
    with pytest.raises(ToolError) as exc:
        linkedin.organization_urn(bad)
    assert "linkedin_list_organizations" in str(exc.value)


def test_the_page_name_is_rejected_rather_than_sent_as_an_id():
    """A vanity name looks plausible and comes back as an opaque 400 from LinkedIn."""
    with pytest.raises(ToolError, match="numeric id, not the page name"):
        linkedin.organization_urn("software-technology-group")


# --- request shape ------------------------------------------------------------


async def test_every_call_carries_the_mandatory_version_header(stub):
    seen = stub({"/organizationPageStatistics": {"elements": []}})
    await linkedin.page_stats(TOKEN, ORG, version="202601")
    assert seen[0].headers["linkedin-version"] == "202601"
    assert seen[0].headers["x-restli-protocol-version"] == "2.0.0"
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"


async def test_the_version_is_configurable_because_linkedin_retires_them(stub):
    seen = stub({"/organizationPageStatistics": {"elements": []}})
    await linkedin.page_stats(TOKEN, ORG, version="209901")
    assert seen[0].headers["linkedin-version"] == "209901"


async def test_a_date_range_is_sent_as_epoch_milliseconds(stub):
    seen = stub({"/organizationalEntityShareStatistics": {"elements": []}})
    await linkedin.share_stats(TOKEN, ORG, start="2026-01-01", end="2026-01-31")
    url = str(seen[0].url)
    assert "timeIntervals.timeRange.start=1767225600000" in url
    assert "timeGranularityType=DAY" in url


async def test_with_no_dates_no_time_range_is_sent(stub):
    seen = stub({"/organizationalEntityShareStatistics": {"elements": []}})
    result = await linkedin.share_stats(TOKEN, ORG)
    assert "timeIntervals" not in str(seen[0].url)
    assert any("lifetime totals" in n for n in result.notes)


# --- failures explain themselves ---------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "expire after about 60 days"),
        (403, "ADMINISTRATOR"),
        (404, "linkedin_list_organizations"),
        (426, "LINKEDIN_API_VERSION"),
        (429, "per member per day"),
        (500, "transient"),
    ],
)
async def test_every_failure_names_its_cause(stub, status, expected):
    stub({"/organizationPageStatistics": {"message": "nope"}}, status=status)
    with pytest.raises(ToolError, match=expected):
        await linkedin.page_stats(TOKEN, ORG)


async def test_an_expired_token_is_called_expiry_not_a_bad_value(stub):
    """LinkedIn tokens die on a timer. Saying 'rejected the token' alone sends the
    reader hunting for a typo in a value that was correct when it was pasted."""
    stub({"/organizationPageStatistics": {"message": "Invalid access token"}}, status=401)
    with pytest.raises(ToolError) as exc:
        await linkedin.page_stats(TOKEN, ORG)
    assert "60 days" in str(exc.value) and "LINKEDIN_ACCESS_TOKEN" in str(exc.value)


# --- statistics ---------------------------------------------------------------


async def test_share_statistics_are_normalised(stub):
    stub(
        {
            "/organizationalEntityShareStatistics": {
                "elements": [
                    {
                        "totalShareStatistics": {
                            "impressionCount": 1000,
                            "clickCount": 40,
                            "likeCount": 25,
                            "commentCount": 5,
                            "shareCount": 10,
                            "engagementRate": 0.9,  # LinkedIn's own figure, ignored
                        }
                    }
                ]
            }
        }
    )
    row = (await linkedin.share_stats(TOKEN, ORG)).rows[0]
    assert row["impressions"] == 1000 and row["clicks"] == 40
    assert row["engagements"] == 80  # 40 + 25 + 5 + 10
    assert row["engagement_rate"] == 0.08  # recomputed, not LinkedIn's 0.9


async def test_a_post_with_no_impressions_does_not_divide_by_zero(stub):
    stub(
        {
            "/organizationalEntityShareStatistics": {
                "elements": [{"totalShareStatistics": {"impressionCount": 0, "clickCount": 0}}]
            }
        }
    )
    assert (await linkedin.share_stats(TOKEN, ORG)).rows[0]["engagement_rate"] is None


def test_engagement_rate_across_days_is_recomputed_not_averaged():
    """The fourth outing for this bug: averaging two days of 50% and 1% gives 25.5%
    when the real figure is 1.5%. LinkedIn's own engagement field is dropped at the
    client precisely so this cannot be got wrong downstream."""
    from seo_mcp.shaping import QueryResult

    result = QueryResult(
        rows=[
            {"date": "2026-01-01", "impressions": 100, "engagements": 50, "engagement_rate": 0.5},
            {"date": "2026-01-02", "impressions": 9900, "engagements": 99, "engagement_rate": 0.01},
        ],
        dimension_keys=("date",),
        metric_keys=("impressions", "engagements", "engagement_rate"),
        source="linkedin",
    )
    totals = shape(result, top_n=10)["summary"]["totals"]
    assert totals["impressions"] == 10000
    assert totals["engagements"] == 149
    assert totals["engagement_rate"] == round(149 / 10000, 4) == 0.0149


async def test_follower_gains_are_split_and_summed(stub):
    stub(
        {
            "/organizationalEntityFollowerStatistics": {
                "elements": [{"followerGains": {"organicFollowerGain": 12, "paidFollowerGain": 3}}]
            }
        }
    )
    result = await linkedin.follower_stats(TOKEN, ORG)
    assert result.rows[0] == {"organic_gain": 12, "paid_gain": 3, "total_gain": 15}
    assert any("GAINED" in n for n in result.notes)


async def test_follower_stats_say_they_are_gains_not_the_running_total(stub):
    """Reading a gain of 15 as a total of 15 followers would be a wild misreport."""
    stub({"/organizationalEntityFollowerStatistics": {"elements": []}})
    result = await linkedin.follower_stats(TOKEN, ORG)
    assert any("not the running total" in n for n in result.notes)


async def test_page_views_are_labelled_readably(stub):
    stub(
        {
            "/organizationPageStatistics": {
                "elements": [
                    {
                        "totalPageStatistics": {
                            "views": {
                                "allDesktopPageViews": {"pageViews": 90, "uniquePageViews": 70},
                                "careersPageViews": {"pageViews": 10, "uniquePageViews": 8},
                                "notADict": 5,
                            }
                        }
                    }
                ]
            }
        }
    )
    result = await linkedin.page_stats(TOKEN, ORG)
    sections = {r["section"]: r["views"] for r in result.rows}
    assert sections == {"all desktop": 90, "careers": 10}  # the non-dict is skipped
    assert any("not of the website" in n for n in result.notes)


# --- organizations ------------------------------------------------------------


async def test_organizations_are_listed_with_their_follower_totals(stub):
    stub(
        {
            "/organizationAcls": {"elements": [{"organization": URN}]},
            f"/organizations/{ORG}": {"localizedName": "Acme", "vanityName": "acme"},
            "/networkSizes/": {"firstDegreeSize": 4321},
        }
    )
    orgs = await linkedin.list_organizations(TOKEN)
    assert len(orgs) == 1
    assert (orgs[0].organization_id, orgs[0].name, orgs[0].followers) == (ORG, "Acme", 4321)


async def test_a_token_that_administers_nothing_returns_an_empty_list(stub):
    """It authenticates perfectly and can read nothing. auth_status says so."""
    stub({"/organizationAcls": {"elements": []}})
    assert await linkedin.list_organizations(TOKEN) == []


async def test_a_page_whose_name_cannot_be_read_is_still_listed(stub):
    """An ACL can outlive readability of the Page; losing the whole row would be worse."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/organizationAcls" in url:
            return httpx.Response(200, json={"elements": [{"organization": URN}]})
        if "/networkSizes/" in url:
            return httpx.Response(200, json={"firstDegreeSize": 10})
        return httpx.Response(403, json={"message": "no access to this organization"})

    import seo_mcp.clients.linkedin as mod

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    mod.httpx.AsyncClient = lambda *a, **kw: original(*a, **{**kw, "transport": transport})
    try:
        orgs = await linkedin.list_organizations(TOKEN)
    finally:
        mod.httpx.AsyncClient = original
    assert len(orgs) == 1
    assert orgs[0].organization_id == ORG
    assert "1234567" in orgs[0].name  # falls back to the id rather than vanishing


async def test_an_unreachable_follower_count_does_not_lose_the_organization(stub):
    stub(
        {
            "/organizationAcls": {"elements": [{"organization": URN}]},
            f"/organizations/{ORG}": {"localizedName": "Acme"},
        }
    )
    orgs = await linkedin.list_organizations(TOKEN)
    assert orgs[0].name == "Acme" and orgs[0].followers is None


async def test_rows_are_labelled_linkedin_not_a_borrowed_source(stub):
    stub({"/organizationPageStatistics": {"elements": []}})
    assert (await linkedin.page_stats(TOKEN, ORG)).source == "linkedin"


# --- the tool layer -----------------------------------------------------------


@pytest.fixture
def unconfigured(monkeypatch):
    """No LinkedIn credential and no defaults, like a fresh install."""
    from types import SimpleNamespace

    from seo_mcp.tools import linkedin_tools

    monkeypatch.setattr(
        linkedin_tools.config,
        "active_profile",
        lambda: SimpleNamespace(
            linkedin_token=None, linkedin_organization=None, linkedin_version=None
        ),
    )
    return linkedin_tools


async def test_a_bad_argument_is_reported_before_the_missing_credential(unconfigured):
    """Checking the token first reports a malformed date range as "no token
    configured", which sends the reader after the wrong problem entirely. This
    codebase has made that mistake once already, on GA4 property ids."""
    with pytest.raises(ToolError, match="both start_date and end_date"):
        await unconfigured.linkedin_share_stats(organization_id="123", start_date="2026-01-01")


async def test_a_bad_organization_id_is_called_a_bad_id_not_a_permission_problem(unconfigured):
    with pytest.raises(ToolError, match="numeric id, not the page name"):
        await unconfigured.linkedin_page_stats(organization_id="acme-corp")


async def test_a_reversed_range_is_rejected(unconfigured):
    with pytest.raises(ToolError, match="is after end_date"):
        await unconfigured.linkedin_follower_stats(
            organization_id="123", start_date="2026-05-01", end_date="2026-01-01"
        )


@pytest.mark.parametrize(
    "call",
    ["linkedin_list_organizations", "linkedin_share_stats", "linkedin_page_stats"],
)
async def test_with_no_token_every_tool_names_the_setting(unconfigured, call):
    kwargs = {} if call == "linkedin_list_organizations" else {"organization_id": "123"}
    with pytest.raises(ToolError, match="LINKEDIN_ACCESS_TOKEN"):
        await getattr(unconfigured, call)(**kwargs)


async def test_the_missing_token_message_warns_about_the_sixty_day_expiry(unconfigured):
    """Anyone setting this up should learn about the expiry before it bites, not after."""
    with pytest.raises(ToolError) as exc:
        await unconfigured.linkedin_list_organizations()
    assert "60 days" in str(exc.value)
