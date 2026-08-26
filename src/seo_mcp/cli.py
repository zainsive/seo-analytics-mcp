"""Command line: auth | doctor | profiles | serve.

No arguments means `serve`, because that is what the MCP client config invokes.
This is the ONLY module allowed to import auth.flow or open a browser (RULE 2).

Everything except the JSON-RPC stream is printed to stderr in serve mode; in the
other subcommands stdout is a normal terminal and safe to write to.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from . import __version__, cache, config
from .auth import scopes, store
from .clients import ga4, gsc, indexnow
from .errors import Ctx, translate

CONSOLE_WALKTHROUGH = """\
No OAuth client secret found for profile '{profile}'.

Everything below happens in your own Google Cloud project, so no credential of
yours ever reaches anyone else. Six clicks, once:

 1. Create or pick a project:
    https://console.cloud.google.com/projectcreate

 2. Enable the APIs you want (Search Console is required, GA4 is optional):
    https://console.cloud.google.com/apis/library/searchconsole.googleapis.com
    https://console.cloud.google.com/apis/library/analyticsdata.googleapis.com
    https://console.cloud.google.com/apis/library/analyticsadmin.googleapis.com

 3. Configure the OAuth consent screen, choose External, and then press
    PUBLISH APP. This step is not optional: an app left in "Testing" issues
    refresh tokens that expire after 7 days, and this server will appear to
    break a week from now. Because you are the only user of your own app, no
    Google verification is needed.
    https://console.cloud.google.com/auth/overview

 4. Create credentials -> OAuth client ID -> application type DESKTOP APP.
    A "Web application" client cannot do the loopback flow this server needs.
    https://console.cloud.google.com/auth/clients

 5. Download the client JSON, then run:
    seo-mcp auth --client-secret /path/to/client_secret.json

You will see a "Google hasn't verified this app" screen once. That is expected
for your own client: choose Advanced, then continue.
"""


def _out(*parts: str) -> None:
    print(*parts)


# --- auth --------------------------------------------------------------------


def cmd_auth(args: argparse.Namespace) -> int:
    from .auth import flow  # imported here so the server can never reach it

    profile = config.profile_named(args.profile)
    if args.client_secret:
        try:
            flow.adopt_client_secret(profile, Path(args.client_secret).expanduser())
        except Exception as exc:
            _out(f"error: {translate(exc, Ctx(profile=profile.name))}")
            return 2
    if not profile.client_path.exists():
        _out(CONSOLE_WALKTHROUGH.format(profile=profile.name))
        return 2

    requested = scopes.requested(args.enable_writes)
    if args.reauth and profile.token_path.exists() and not args.enable_writes:
        # Keep whatever was granted before rather than silently downgrading.
        held = set(store.scopes_held(profile))
        requested = sorted(set(requested) | held)

    _out(f"Profile '{profile.name}' -> {profile.root}")
    _out("Requesting scopes:")
    for scope in requested:
        _out(f"  - {scope}  ({scopes.DESCRIPTIONS.get(scope, 'unknown scope')})")
    if not args.enable_writes:
        _out("Read-only. Add --enable-writes if you want sitemap submission.")

    try:
        creds = flow.authorise(profile, requested, port=args.port)
    except KeyboardInterrupt:
        _out("\nCancelled. Nothing was written.")
        return 1
    except Exception as exc:
        _out(f"\nAuthorisation failed: {translate(exc, Ctx(profile=profile.name))}")
        return 1

    _out(f"\nToken written to {profile.token_path} (permissions 0600).")
    granted = sorted(creds.scopes or [])
    _out(f"Scopes granted: {len(granted)}")
    if not creds.refresh_token:
        _out(
            "\nWARNING: Google returned no refresh token, so this will stop working in "
            "about an hour. Re-run with --reauth to force a fresh consent."
        )
    _out("\nNext: run `seo-mcp doctor` to confirm, then add the server to your MCP client.")
    return 0


# --- doctor ------------------------------------------------------------------


def _check(name: str, ok: bool | None, detail: str) -> dict[str, Any]:
    return {"check": name, "ok": ok, "detail": detail}


async def _doctor(profile: config.Profile) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    # 1-2. client secret present, and of the right type
    if not profile.client_path.exists():
        checks.append(
            _check(
                "client secret",
                False,
                "not installed yet - run `seo-mcp auth --client-secret "
                "~/Downloads/client_secret_*.json`, which copies it to "
                f"{profile.client_path} and then authorises",
            )
        )
        checks.append(_check("client type", None, "skipped - no client secret"))
    else:
        checks.append(_check("client secret", True, str(profile.client_path)))
        try:
            data = json.loads(profile.client_path.read_text(encoding="utf-8"))
            installed = "installed" in data
            checks.append(
                _check(
                    "client type",
                    installed,
                    "Desktop app ('installed')"
                    if installed
                    else f"found {sorted(data)} - this is a Web client and cannot do the "
                    "loopback flow. Create a Desktop app client instead.",
                )
            )
        except ValueError as exc:
            checks.append(_check("client type", False, f"unreadable: {exc}"))

    # 3. token present
    held = store.scopes_held(profile)
    if not store.has_token(profile):
        checks.append(_check("token", False, "no token stored - run `seo-mcp auth`"))
        for name in ("token refreshable", "scopes", "token age"):
            checks.append(_check(name, None, "skipped - no token"))
        creds = None
    else:
        checks.append(_check("token", True, str(profile.token_path)))
        # 4. refreshable
        try:
            creds = store.load(profile, set())
            checks.append(
                _check(
                    "token refreshable",
                    True,
                    "valid" + ("" if creds.refresh_token else " but has NO refresh token"),
                )
            )
        except Exception as exc:
            creds = None
            checks.append(_check("token refreshable", False, f"{type(exc).__name__}: {exc}"))
        # 5. scopes
        missing_read = scopes.missing(held, scopes.for_read())
        checks.append(
            _check(
                "scopes",
                not missing_read,
                f"{len(held)} held; writes "
                + ("enabled" if scopes.satisfies(held, {scopes.GSC_WRITE}) else "not granted")
                + (f"; missing {missing_read}" if missing_read else ""),
            )
        )
        # 6. token age / Testing heuristic
        warning = store.testing_status_warning(profile)
        age = store.token_age_days(profile)
        checks.append(
            _check(
                "token age",
                warning is None,
                warning
                or f"granted {age:.1f} days ago - past day {store.TESTING_TOKEN_DAYS}, "
                "so the OAuth app is published and refresh tokens persist.",
            )
        )

    # 7-9. do the APIs actually answer
    if creds is not None:
        if scopes.satisfies(held, {scopes.GSC_READ}):
            try:
                sites = await gsc.list_sites(creds)
                checks.append(
                    _check("searchconsole API", True, f"{len(sites.rows)} properties visible")
                )
                checks.append(
                    _check(
                        "GSC properties",
                        bool(sites.rows),
                        ", ".join(str(r["site_url"]) for r in sites.rows[:5])
                        or "none visible - check the account has been granted access",
                    )
                )
            except Exception as exc:
                checks.append(
                    _check("searchconsole API", False, f"{type(exc).__name__}: {exc}"[:200])
                )
                checks.append(_check("GSC properties", None, "skipped"))
        else:
            checks.append(_check("searchconsole API", None, "skipped - scope not granted"))
            checks.append(_check("GSC properties", None, "skipped - scope not granted"))

        if scopes.satisfies(held, {scopes.GA4_READ}):
            try:
                props = await ga4.list_properties(creds)
                checks.append(
                    _check("GA4 properties", True, f"{len(props.rows)} properties visible")
                )
            except Exception as exc:
                checks.append(_check("GA4 properties", False, f"{type(exc).__name__}: {exc}"[:200]))
        else:
            checks.append(_check("GA4 properties", None, "skipped - scope not granted"))
    else:
        for name in ("searchconsole API", "GSC properties", "GA4 properties"):
            checks.append(_check(name, None, "skipped - no usable token"))

    # 10. IndexNow key file
    host, key = profile.indexnow_host, profile.indexnow_key
    if host and key:
        result = await indexnow.verify_key(host, key)
        checks.append(_check("IndexNow key", result.ok, result.detail))
    else:
        checks.append(
            _check(
                "IndexNow key",
                None,
                "not configured - set INDEXNOW_HOST and INDEXNOW_KEY to use IndexNow",
            )
        )

    # 11. paths, cache size, version - so an issue report can be pasted straight in
    checks.append(
        _check(
            "environment",
            True,
            f"seo-mcp {__version__}, python {sys.version.split()[0]}, "
            f"profile dir {profile.root}, cache {cache.size_bytes(profile) // 1024} KiB",
        )
    )
    return checks


def cmd_doctor(args: argparse.Namespace) -> int:
    profile = config.profile_named(args.profile)
    checks = asyncio.run(_doctor(profile))
    failed = [c for c in checks if c["ok"] is False]

    if args.json:
        _out(
            json.dumps(
                {
                    "profile": profile.name,
                    "version": __version__,
                    "python": sys.version.split()[0],
                    "platform": sys.platform,
                    "ok": not failed,
                    "checks": checks,
                },
                indent=2,
            )
        )
    else:
        _out(f"seo-mcp {__version__} - profile '{profile.name}'\n")
        for c in checks:
            mark = {True: "PASS", False: "FAIL", None: "----"}[c["ok"]]
            _out(f"  [{mark}] {c['check']:<20} {c['detail']}")
        if failed:
            _out(f"\n{len(failed)} check(s) failed. The server will not work until they pass.")
            if not store.has_token(profile) and not profile.client_path.exists():
                _out("\n" + CONSOLE_WALKTHROUGH.format(profile=profile.name))
        else:
            _out("\nAll checks passed. Add the server to your MCP client config:")
            _out(f"  claude mcp add seo -e SEO_MCP_PROFILE={profile.name} -- uvx seo-mcp")
    return 1 if failed else 0


# --- profiles ----------------------------------------------------------------


def cmd_profiles(args: argparse.Namespace) -> int:
    if args.action == "list":
        found = config.list_profiles()
        if not found:
            _out(f"No profiles yet under {config.config_root()}")
            return 0
        for p in found:
            state = "authorised" if store.has_token(p) else "not authorised"
            _out(f"  {p.name:<16} {state:<16} {p.root}")
        return 0

    if not args.name:
        _out(f"error: `seo-mcp profiles {args.action}` needs a profile name")
        return 2
    profile = config.profile_named(args.name)

    if args.action == "show":
        _out(f"profile      {profile.name}")
        _out(f"directory    {profile.root}")
        _out(f"client       {'present' if profile.client_path.exists() else 'missing'}")
        _out(f"token        {'present' if store.has_token(profile) else 'missing'}")
        _out(f"scopes       {len(store.scopes_held(profile))}")
        _out(f"cache        {cache.size_bytes(profile) // 1024} KiB")
        for label, value in (
            ("default site", profile.default_site),
            ("default GA4", profile.default_property),
            ("indexnow host", profile.indexnow_host),
        ):
            if value:
                _out(f"{label:<12} {value}")
        return 0

    if args.action == "rm":
        if not profile.root.exists():
            _out(f"Nothing to remove at {profile.root}")
            return 0
        if not args.yes:
            _out(f"This will delete {profile.root}, including its stored Google token.")
            _out("Re-run with --yes to confirm.")
            return 2
        shutil.rmtree(profile.root)
        _out(f"Removed {profile.root}. Run `seo-mcp auth` to start over.")
        return 0
    return 2


# --- serve -------------------------------------------------------------------


def cmd_serve(_args: argparse.Namespace) -> int:
    # Imported lazily: importing the server registers 13 tools, which the other
    # subcommands have no use for.
    from .server import main as serve_main

    serve_main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seo-mcp",
        description="MCP server for Google Search Console, GA4 and IndexNow. "
        "With no arguments it starts the stdio server.",
    )
    parser.add_argument("--version", action="version", version=f"seo-mcp {__version__}")
    subs = parser.add_subparsers(dest="command")

    p_auth = subs.add_parser("auth", help="authorise a Google account (opens a browser once)")
    p_auth.add_argument("--client-secret", help="path to the downloaded Desktop-app client JSON")
    p_auth.add_argument("--profile", default="default")
    p_auth.add_argument(
        "--enable-writes",
        action="store_true",
        help="also request the Search Console write scope (sitemap submission)",
    )
    p_auth.add_argument("--reauth", action="store_true", help="force a fresh consent screen")
    p_auth.add_argument("--port", type=int, default=0, help="loopback port (0 = ephemeral)")
    p_auth.set_defaults(func=cmd_auth)

    p_doc = subs.add_parser("doctor", help="diagnose this installation")
    p_doc.add_argument("--profile", default="default")
    p_doc.add_argument(
        "--json", action="store_true", help="machine-readable output for issue reports"
    )
    p_doc.set_defaults(func=cmd_doctor)

    p_prof = subs.add_parser("profiles", help="list, show or remove profiles")
    p_prof.add_argument("action", choices=["list", "show", "rm"])
    p_prof.add_argument("name", nargs="?")
    p_prof.add_argument("--yes", action="store_true", help="confirm removal")
    p_prof.set_defaults(func=cmd_profiles)

    p_serve = subs.add_parser("serve", help="start the stdio server (the default)")
    p_serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    # No arguments at all means serve - that is how the MCP client invokes us. An
    # unrecognised argument must NOT fall through to serve, or a typo starts a server
    # that then fails mysteriously, so anything non-empty goes through argparse.
    if not argv:
        return cmd_serve(argparse.Namespace())
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
