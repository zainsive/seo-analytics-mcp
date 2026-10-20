"""One case per status and reason, built from REAL exception classes.

googleapiclient puts the status on resp.status; the GA4 gRPC client raises
google.api_core.exceptions subclasses carrying .code. A hand-rolled fake hides
that difference, which is the whole thing translate() has to get right.
"""

import json

import httplib2
import pytest
from google.api_core import exceptions as api_core
from googleapiclient.errors import HttpError
from mcp.server.mcpserver.exceptions import ToolError

from seo_mcp.auth import scopes
from seo_mcp.errors import (
    AuthMissing,
    ClientSecretMissing,
    Ctx,
    ScopeMissing,
    TokenCorrupt,
    WrongClientType,
    translate,
)


def http_error(status: int, reason: str) -> HttpError:
    return HttpError(
        httplib2.Response({"status": status}),
        json.dumps({"error": {"code": status, "errors": [{"reason": reason}]}}).encode(),
        uri="https://example.googleapis.com/",
    )


CTX = Ctx(profile="default", site_url="sc-domain:example.com", api="searchconsole.googleapis.com")


def test_403_names_where_the_grant_lives():
    message = str(translate(http_error(403, "forbidden"), CTX))
    assert "Users and permissions" in message
    assert "sc-domain:example.com" in message


def test_403_for_a_disabled_api_says_enable_it_rather_than_check_access():
    message = str(translate(http_error(403, "accessNotConfigured"), CTX))
    assert "not enabled" in message
    assert "Users and permissions" not in message


def test_403_with_a_known_scope_requirement_points_at_enable_writes():
    ctx = Ctx(profile="default", scope_needed=scopes.GSC_WRITE)
    assert "--enable-writes" in str(translate(http_error(403, "forbidden"), ctx))


def test_404_explains_the_sc_domain_spelling():
    assert "sc-domain:example.com" in str(translate(http_error(404, "notFound"), CTX))


def test_429_says_how_long_to_wait():
    assert "15 minutes" in str(translate(http_error(429, "rateLimitExceeded"), CTX))


def test_400_points_at_incompatible_ga4_fields():
    message = str(translate(api_core.InvalidArgument("bad pair"), CTX))
    assert "incompatible dimension/metric" in message


def test_401_says_reauthorise():
    assert "auth --reauth" in str(translate(http_error(401, "authError"), CTX))


def test_5xx_is_reported_as_transient():
    assert "transient" in str(translate(http_error(503, "backendError"), CTX))


def test_invalid_grant_is_the_seven_day_testing_token_message():
    message = str(translate(api_core.Unauthenticated("invalid_grant: expired"), CTX))
    assert "Publish app" in message and "7 days" in message


@pytest.mark.parametrize(
    "exc,expected",
    [
        (api_core.PermissionDenied("no"), "no access"),
        (api_core.NotFound("no"), "was not found"),
        (api_core.TooManyRequests("no"), "rate-limited"),
        (api_core.InternalServerError("no"), "transient"),
    ],
)
def test_ga4_grpc_exceptions_translate_by_their_code_attribute(exc, expected):
    assert expected in str(translate(exc, CTX))


def test_missing_token_tells_the_user_the_exact_command():
    message = str(translate(AuthMissing("default"), Ctx()))
    assert "seo-analytics-mcp auth --client-secret" in message
    assert "cannot open a browser from inside a tool call" in message


def test_missing_write_scope_asks_for_writes_specifically():
    message = str(translate(ScopeMissing([scopes.GSC_WRITE], "default"), Ctx()))
    assert "--enable-writes" in message


def test_missing_read_scope_does_not_ask_for_writes():
    message = str(translate(ScopeMissing([scopes.GA4_READ], "default"), Ctx()))
    assert "--enable-writes" not in message


def test_a_web_client_is_named_as_the_mistake():
    assert "Desktop app" in str(translate(WrongClientType(["web"]), Ctx()))


def test_corrupt_token_says_how_to_start_over():
    assert "profiles rm" in str(translate(TokenCorrupt("/t/token.json", "bad json"), Ctx()))


def test_missing_client_secret_names_the_path():
    assert "/t/cs.json" in str(translate(ClientSecretMissing("/t/cs.json"), Ctx()))


def test_an_already_actionable_toolerror_is_never_rewrapped():
    original = ToolError("already good")
    assert translate(original, CTX) is original


def test_an_unrecognised_exception_still_asks_for_doctor_output():
    message = str(translate(RuntimeError("something odd"), CTX))
    assert "seo-analytics-mcp doctor" in message and "something odd" in message


def test_no_message_leaks_a_traceback():
    for exc in (http_error(403, "forbidden"), api_core.InvalidArgument("x"), RuntimeError("y")):
        assert "Traceback" not in str(translate(exc, CTX))


def test_a_disabled_api_error_uses_googles_own_project_specific_enable_link():
    """Google's SERVICE_DISABLED text carries a one-click URL with the project baked in."""
    link = (
        "https://console.developers.google.com/apis/api/analyticsadmin.googleapis.com"
        "/overview?project=485850269158"
    )
    # Faithful to the real thing: api_core stringifies the reason alongside the message.
    exc = api_core.PermissionDenied(
        "Google Analytics Admin API has not been used in project 485850269158 before or "
        f'it is disabled. Enable it by visiting {link} then retry. [reason: "SERVICE_DISABLED"]'
    )
    message = str(translate(exc, Ctx(api="analyticsadmin.googleapis.com")))
    assert "overview?project=485850269158" in message
    assert "wait a minute" in message


def test_a_disabled_api_error_without_a_link_falls_back_to_the_api_library():
    exc = http_error(403, "accessNotConfigured")
    assert "console.cloud.google.com/apis/library" in str(translate(exc, CTX))
