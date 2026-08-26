"""The one-time browser flow. Called ONLY from cli.py - the server never imports this.

A tool call that blocks on a consent screen looks like a hung server and the model
has no way to help. One CLI command, run once, is the whole difference.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from ..config import Profile
from ..errors import ClientSecretMissing, WrongClientType
from . import store


def resolve_client_secret(source: Path) -> Path:
    """Google's download ends in '.apps.googleusercontent.com.json' and shell
    tab-completion stops at the '.com', so a path one suffix short is the single most
    common way this command fails. Accept it rather than lecturing about it."""
    if source.exists():
        return source
    with_json = Path(str(source) + ".json")
    if with_json.exists():
        return with_json
    raise ClientSecretMissing(str(source))


def adopt_client_secret(profile: Profile, source: Path) -> Path:
    """Copy the downloaded client secret into the profile so nothing outside the
    profile directory is load-bearing later."""
    source = resolve_client_secret(source)
    data = json.loads(source.read_text(encoding="utf-8"))
    if "installed" not in data:
        raise WrongClientType(sorted(data))  # they downloaded a Web client
    profile.root.mkdir(parents=True, exist_ok=True)
    if source.resolve() != profile.client_path.resolve():
        shutil.copyfile(source, profile.client_path)
    profile.client_path.chmod(0o600)
    return profile.client_path


def authorise(profile: Profile, scopes: list[str], *, port: int = 0) -> Credentials:
    """Loopback redirect on an ephemeral port. PKCE is generated automatically by
    google-auth-oauthlib (Flow.autogenerate_code_verifier defaults True), so there is
    nothing to wire up. The OOB paste flow is no longer supported by Google - do not
    add a fallback to it."""
    if not profile.client_path.exists():
        raise ClientSecretMissing(str(profile.client_path))

    data = json.loads(profile.client_path.read_text(encoding="utf-8"))
    if "installed" not in data:
        raise WrongClientType(sorted(data))  # they downloaded a Web client

    flow = InstalledAppFlow.from_client_secrets_file(str(profile.client_path), scopes=scopes)
    creds = flow.run_local_server(
        port=port,  # 0 = ephemeral; Google accepts any localhost port
        open_browser=True,
        access_type="offline",  # required, or there is no refresh token
        prompt="consent",  # required on re-auth, or Google reuses the grant
        include_granted_scopes="true",  # incremental auth when adding --enable-writes
        authorization_prompt_message=(
            "Opening your browser. If Google shows 'this app isn't verified', that is "
            "expected for your own client: choose Advanced, then continue.\n{url}"
        ),
        success_message="Authorised. Close this tab and return to your terminal.",
    )
    store.save(profile, creds)
    return creds
