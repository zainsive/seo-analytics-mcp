"""The token gate. No valid token, no response - and no token in any log line."""

import logging

import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from seo_mcp import bearer

GOOD = "A" * 40
OTHER = "B" * 40


@pytest.fixture
def client():
    async def ok(request):
        return JSONResponse({"reached": True, "who": getattr(request.state, "token_label", None)})

    async def health(_request):
        return JSONResponse({"status": "ok"})

    app = Starlette(routes=[Route("/mcp/x", ok, methods=["POST"]), Route("/healthz", health)])
    env_tokens = {GOOD: "alice", OTHER: "bob"}
    app.add_middleware(
        BaseHTTPMiddleware,
        dispatch=bearer.make_middleware(lambda t: bearer.identify_env(t, env_tokens)),
    )
    return TestClient(app)


# --- parsing ---------------------------------------------------------------


def test_labelled_pairs_parse():
    assert bearer.parse_tokens(f"alice:{GOOD}, bob:{OTHER}") == {GOOD: "alice", OTHER: "bob"}


def test_a_bare_token_still_works_rather_than_failing_to_boot():
    assert bearer.parse_tokens(GOOD) == {GOOD: "unnamed"}


def test_blank_and_malformed_entries_are_skipped():
    assert bearer.parse_tokens("") == {}
    assert bearer.parse_tokens(" , ,alice: ,") == {}


def test_a_short_token_is_accepted_but_warned_about(caplog):
    with caplog.at_level(logging.WARNING):
        assert bearer.parse_tokens("alice:short") == {"short": "alice"}
    assert "at least" in caplog.text


def test_generated_tokens_are_long_and_unique():
    a, b = bearer.generate(), bearer.generate()
    assert a != b and len(a) >= bearer.MIN_TOKEN_LEN


# --- the gate --------------------------------------------------------------


def test_a_valid_token_reaches_the_app_and_is_attributed(client):
    r = client.post("/mcp/x", headers={"Authorization": f"Bearer {GOOD}"})
    assert r.status_code == 200
    assert r.json() == {"reached": True, "who": "alice"}


@pytest.mark.parametrize(
    "headers,why",
    [
        ({}, "no header at all"),
        ({"Authorization": ""}, "empty header"),
        ({"Authorization": "Bearer "}, "bearer with no token"),
        ({"Authorization": f"Basic {GOOD}"}, "right token, wrong scheme"),
        ({"Authorization": GOOD}, "token with no scheme"),
        ({"Authorization": "Bearer " + "C" * 40}, "unknown token"),
        ({"Authorization": f"Bearer {GOOD[:-1]}"}, "token off by one character"),
        ({"Authorization": f"Bearer {GOOD} extra"}, "trailing junk"),
    ],
)
def test_nothing_else_gets_through(client, headers, why):
    r = client.post("/mcp/x", headers=headers)
    assert r.status_code == 401, why
    assert r.json()["error"] == "unauthorized"
    assert "reached" not in r.text


def test_the_token_cannot_be_passed_in_the_query_string(client):
    assert client.post(f"/mcp/x?token={GOOD}").status_code == 401


def test_a_401_carries_a_bearer_challenge(client):
    r = client.post("/mcp/x")
    assert r.headers["www-authenticate"].startswith("Bearer ")


def test_the_401_body_says_how_to_authenticate(client):
    assert "Authorization: Bearer" in client.post("/mcp/x").json()["detail"]


def test_health_stays_open_for_uptime_checks(client):
    """It reveals only a version string, and a monitor cannot hold a credential."""
    assert client.get("/healthz").status_code == 200


def test_each_token_is_attributed_to_its_own_person(client):
    r = client.post("/mcp/x", headers={"Authorization": f"Bearer {OTHER}"})
    assert r.json()["who"] == "bob"


def test_no_token_value_ever_reaches_the_logs(client, caplog):
    with caplog.at_level(logging.DEBUG):
        client.post("/mcp/x", headers={"Authorization": f"Bearer {GOOD}"})
        client.post("/mcp/x", headers={"Authorization": "Bearer " + "C" * 40})
    assert GOOD not in caplog.text
    assert "C" * 40 not in caplog.text
    assert "alice" in caplog.text  # the person is logged, the secret is not


def test_identify_env_is_constant_time_by_construction():
    """Environment tokens are compared as plaintext, so a dict lookup would leak their
    length and prefix through response timing. The database path matches on a hash and
    needs no such care."""
    import inspect

    assert "compare_digest" in inspect.getsource(bearer.identify_env)
