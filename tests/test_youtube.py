"""The YouTube client: two APIs, two credentials, one set of rules about rates."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from seo_mcp import youtube_auth
from seo_mcp.clients import youtube
from seo_mcp.shaping import QueryResult, shape

CHANNEL = "UCVsEgKMBL1DREac01aQf5zA"
KEY = "fixture-key-not-a-real-credential"


# --- identifiers --------------------------------------------------------------


@pytest.mark.parametrize(
    "given",
    [CHANNEL, f"https://www.youtube.com/channel/{CHANNEL}", f"youtube.com/channel/{CHANNEL}/"],
)
def test_a_channel_is_accepted_in_any_form_a_model_might_produce(given):
    assert youtube.channel_id(given) == CHANNEL


def test_a_handle_is_rejected_with_the_reason_and_where_to_look():
    """A handle looks like a perfectly good identifier and is not one."""
    with pytest.raises(ToolError) as exc:
        youtube.channel_id("@SoftwareTechnologyGroup")
    assert "handle, not an id" in str(exc.value)
    assert "YouTube Studio" in str(exc.value)


@pytest.mark.parametrize("bad", ["", "   ", "not-a-channel", "UCtooshort"])
def test_something_that_is_not_a_channel_id_is_explained(bad):
    with pytest.raises(ToolError):
        youtube.channel_id(bad)


def test_the_traffic_source_codes_are_made_readable():
    assert youtube._readable("YT_SEARCH") == "YouTube search"
    assert youtube._readable("RELATED_VIDEO") == "suggested video"
    assert youtube._readable("EXT_URL") == "external link"
    assert youtube._readable("SOMETHING_NEW") == "something new"  # unknown codes degrade
    assert youtube._readable("") == "unknown"


# --- the rate rule, for the fifth time ----------------------------------------


def test_average_view_duration_is_weighted_by_views_not_summed():
    """Summing it reports a channel's attention span as hours. Averaging it unweighted
    is also wrong. Weighted by views it is exactly total seconds / total views.

    100 views at 30s and 900 at 300s is 273s, not 330 (sum) and not 165 (plain mean).
    """
    result = QueryResult(
        rows=[
            {"day": "2026-01-01", "views": 100, "average_view_duration": 30},
            {"day": "2026-01-02", "views": 900, "average_view_duration": 300},
        ],
        dimension_keys=("day",),
        metric_keys=("views", "average_view_duration"),
        source="youtube",
    )
    totals = shape(result, top_n=10)["summary"]["totals"]
    assert totals["views"] == 1000
    assert totals["average_view_duration"] == 273


def test_watch_time_and_subscriber_counts_are_summed():
    """The counts beside the rate must still add up normally."""
    result = QueryResult(
        rows=[
            {"views": 10, "minutes_watched": 100, "subscribers_net": 3},
            {"views": 20, "minutes_watched": 250, "subscribers_net": -1},
        ],
        dimension_keys=(),
        metric_keys=("views", "minutes_watched", "subscribers_net"),
        source="youtube",
    )
    totals = shape(result, top_n=10)["summary"]["totals"]
    assert (totals["views"], totals["minutes_watched"], totals["subscribers_net"]) == (30, 350, 2)


# --- report shaping -----------------------------------------------------------


def test_a_report_row_is_normalised_and_nets_subscribers():
    payload = {
        "columnHeaders": [
            {"name": "day"},
            {"name": "views"},
            {"name": "estimatedMinutesWatched"},
            {"name": "averageViewDuration"},
            {"name": "subscribersGained"},
            {"name": "subscribersLost"},
        ],
        "rows": [["2026-01-01", 120, 400, 200, 7, 2]],
    }
    result = youtube._shape_report(payload, "day", CHANNEL, [])
    assert result.rows[0] == {
        "day": "2026-01-01",
        "views": 120,
        "minutes_watched": 400,
        "average_view_duration": 200,
        "subscribers_gained": 7,
        "subscribers_lost": 2,
        "subscribers_net": 5,
    }
    assert result.source == "youtube"


def test_an_empty_report_does_not_invent_rows():
    result = youtube._shape_report({"columnHeaders": [], "rows": []}, None, CHANNEL, [])
    assert result.rows == [] and result.total_rows == 0


def test_a_report_with_no_dimension_has_no_dimension_keys():
    payload = {"columnHeaders": [{"name": "views"}], "rows": [[5]]}
    assert youtube._shape_report(payload, None, CHANNEL, []).dimension_keys == ()


# --- the credential, which is separate on purpose -----------------------------


@pytest.fixture
def unconfigured(monkeypatch):
    monkeypatch.setattr(
        youtube_auth.config,
        "active_profile",
        lambda: SimpleNamespace(
            youtube_refresh_token=None,
            youtube_client_id=None,
            youtube_client_secret=None,
        ),
    )


def test_youtube_is_not_configured_when_nothing_is_set(unconfigured):
    assert youtube_auth.configured() is False


def test_a_partial_configuration_is_not_treated_as_configured(monkeypatch):
    """Two of three values is the easy mistake when pasting into a .env."""
    monkeypatch.setattr(
        youtube_auth.config,
        "active_profile",
        lambda: SimpleNamespace(
            youtube_refresh_token="r",
            youtube_client_id="c",
            youtube_client_secret=None,
        ),
    )
    assert youtube_auth.configured() is False


async def test_the_missing_credential_names_each_absent_variable(monkeypatch):
    monkeypatch.setattr(
        youtube_auth.config,
        "active_profile",
        lambda: SimpleNamespace(
            youtube_refresh_token="r",
            youtube_client_id=None,
            youtube_client_secret=None,
        ),
    )
    with pytest.raises(ToolError) as exc:
        await youtube_auth.credentials()
    text = str(exc.value)
    assert "YOUTUBE_OAUTH_CLIENT_ID" in text and "YOUTUBE_OAUTH_CLIENT_SECRET" in text
    assert "YOUTUBE_REFRESH_TOKEN" not in text.split(".")[0]  # the one that IS set


async def test_the_setup_message_says_a_service_account_cannot_be_used(unconfigured):
    """The single most likely wrong assumption, given everything else here uses one."""
    with pytest.raises(ToolError) as exc:
        await youtube_auth.credentials()
    assert "service account cannot read a YouTube channel" in str(exc.value)


def test_an_invalid_grant_is_blamed_on_the_testing_app_first():
    """It is by far the most common cause, and the fix is different from the others:
    re-authorising an unpublished app just buys another 7 days."""
    message = youtube_auth._explain(Exception("('invalid_grant: Token has been expired')"))
    assert "still in Testing" in message and "7 days" in message
    assert "publish it" in message.lower()


def test_an_invalid_client_points_at_the_mismatched_pair():
    message = youtube_auth._explain(Exception("invalid_client"))
    assert "same OAuth client" in message


def test_an_unrecognised_refresh_failure_is_passed_through_not_swallowed():
    assert "connection reset" in youtube_auth._explain(Exception("connection reset"))


# --- scopes -------------------------------------------------------------------


def test_youtube_scopes_are_not_in_the_local_install_consent():
    """Someone running `seo-analytics-mcp auth` for Search Console must not be asked
    to approve YouTube as well. They opt in separately with `youtube-auth`.

    This is deliberately the opposite of the hosted Google sign-in, which does ask for
    YouTube up front - see test_per_user. The two consents answer different questions.
    One person at a terminal can run a second command whenever they feel like it; a
    team cannot be sent back through a consent screen cheaply, so the hosted flow asks
    once for everything. Do not make these two agree without reading both reasons.
    """
    from seo_mcp.auth import scopes

    assert not scopes.for_youtube() & scopes.for_read()
    assert not scopes.for_youtube() & scopes.for_write()


def test_both_youtube_scopes_are_read_only():
    from seo_mcp.auth import scopes

    for scope in scopes.for_youtube():
        assert scope.endswith(".readonly"), scope
        assert scope in scopes.DESCRIPTIONS


# --- what a public request is signed with -------------------------------------


async def test_public_counts_prefer_the_api_key_because_they_are_the_same_for_everyone(
    monkeypatch,
):
    """Subscriber counts are on the channel page for anybody to read, so there is no
    per-person answer to give. Spending somebody's consent on public data, or refusing
    it because they have not signed in, would both be wrong."""
    from seo_mcp.tools import youtube_tools

    monkeypatch.setenv("YOUTUBE_API_KEY", "the-key")
    monkeypatch.setattr(youtube_tools.youtube_auth, "source", lambda: "person")

    assert await youtube_tools._public_access() == "the-key"


async def test_without_a_key_public_counts_fall_back_to_the_caller(monkeypatch):
    """This is what lets a deployment read its own channel without anybody first
    going to the Cloud console to mint an API key. The sign-in already asked for the
    scope; using it is the difference between these tools working and not."""
    from seo_mcp.tools import youtube_tools

    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    sentinel = object()

    async def fake_credentials():
        return sentinel

    monkeypatch.setattr(youtube_tools.youtube_auth, "source", lambda: "person")
    monkeypatch.setattr(youtube_tools.youtube_auth, "credentials", fake_credentials)

    assert await youtube_tools._public_access() is sentinel


async def test_with_neither_a_key_nor_a_caller_it_says_both_ways_out(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from seo_mcp.tools import youtube_tools

    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.setattr(youtube_tools.youtube_auth, "source", lambda: "none")

    with pytest.raises(ToolError) as caught:
        await youtube_tools._public_access()
    message = str(caught.value)
    assert "sign in" in message
    assert "YOUTUBE_API_KEY" in message


def test_the_data_client_accepts_a_key_or_a_credential(monkeypatch):
    """One helper decides which, so neither call site can get it wrong."""
    from seo_mcp.clients import youtube

    built = {}

    def fake_build(api, version, **kw):
        built.update(kw)
        return object()

    monkeypatch.setattr(youtube, "build", fake_build)

    youtube._data_service("a-key")
    assert built == {"developerKey": "a-key", "cache_discovery": False}

    built.clear()
    creds = object()
    youtube._data_service(creds)
    assert built == {"credentials": creds, "cache_discovery": False}
