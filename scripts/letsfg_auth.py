#!/usr/bin/env python3
"""One-time LetsFG OAuth helper for the PFS search lane.

Registers a public client (RFC 7591), runs PKCE S256 against
https://letsfg.co/connect, captures the code on a loopback server, and
prints LETSFG_CLIENT_ID / LETSFG_REFRESH_TOKEN for GitHub Actions secrets.

Standalone: stdlib + httpx only. Never put tokens on argv.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import secrets
import subprocess
import sys
import threading
import urllib.parse
import webbrowser

import httpx

DISCOVERY_URLS = (
    "https://letsfg.co/developers/api/.well-known/oauth-authorization-server",
    "https://letsfg.co/.well-known/oauth-authorization-server",
)
DEFAULT_REDIRECT = "http://127.0.0.1:8765/callback"
# Bare "flights" is dropped by the server, so the grant has no scope.
DEFAULT_SCOPE = "flights:search flights:book profile:read"
CLIENT_NAME = "flightsearch-letsfg"
_FALLBACK = {
    "authorization_endpoint": "https://letsfg.co/connect",
    "token_endpoint": "https://letsfg.co/developers/api/oauth/token",
    "registration_endpoint": "https://letsfg.co/developers/api/oauth/register",
}


def _mask(value: str, show: bool) -> str:
    if show or not value:
        return value
    tail = value[-4:] if len(value) >= 4 else value
    return f"****{tail}"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def discover(client: httpx.Client) -> dict[str, str]:
    meta: dict[str, str] = dict(_FALLBACK)
    for url in DISCOVERY_URLS:
        try:
            resp = client.get(url)
        except httpx.HTTPError:
            continue
        if resp.status_code != 200:
            continue
        try:
            data = resp.json()
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        for key in (
            "authorization_endpoint",
            "token_endpoint",
            "registration_endpoint",
        ):
            value = data.get(key)
            if isinstance(value, str) and value:
                meta[key] = value
        break
    return meta


def register_client(client: httpx.Client, endpoint: str, redirect_uri: str) -> str:
    resp = client.post(
        endpoint,
        json={
            "client_name": CLIENT_NAME,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    if resp.status_code not in (200, 201):
        raise SystemExit(
            f"client registration failed: HTTP {resp.status_code} {resp.text[:300]}"
        )
    data = resp.json()
    client_id = data.get("client_id")
    if not client_id:
        raise SystemExit(f"registration response missing client_id: {data!r}")
    return str(client_id)


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] | None = None
    expected_state: str = ""
    event: threading.Event | None = None

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        qs = urllib.parse.parse_qs(parsed.query)
        state = (qs.get("state") or [""])[0]
        code = (qs.get("code") or [""])[0]
        error = (qs.get("error") or [""])[0]
        if state != self.expected_state:
            self.send_error(400, "state mismatch")
            return
        body: bytes
        # Assign on the class. BaseHTTPRequestHandler instances shadow
        # class attributes, and capture_code reads _CallbackHandler.result.
        if error:
            desc = (qs.get("error_description") or [error])[0]
            type(self).result = {"error": desc}
            body = b"Authorization failed. You can close this tab."
        elif not code:
            type(self).result = {"error": "missing code"}
            body = b"Missing authorization code. You can close this tab."
        else:
            type(self).result = {"code": code}
            body = b"LetsFG connected. You can close this tab and return to the terminal."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        if self.event is not None:
            self.event.set()


def capture_code(redirect_uri: str, state: str, timeout_s: float) -> str:
    parsed = urllib.parse.urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8765
    event = threading.Event()
    _CallbackHandler.result = None
    _CallbackHandler.expected_state = state
    _CallbackHandler.event = event
    server = http.server.HTTPServer((host, port), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not event.wait(timeout_s):
            raise SystemExit(
                f"timed out waiting for the browser callback on {redirect_uri}"
            )
    finally:
        server.shutdown()
        server.server_close()
    result = _CallbackHandler.result or {}
    if result.get("error"):
        raise SystemExit(f"authorization failed: {result['error']}")
    code = result.get("code")
    if not code:
        raise SystemExit("authorization callback did not include a code")
    return code


def exchange_code(
    client: httpx.Client,
    token_endpoint: str,
    *,
    code: str,
    client_id: str,
    redirect_uri: str,
    verifier: str,
) -> dict[str, str]:
    resp = client.post(
        token_endpoint,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code != 200:
        raise SystemExit(
            f"token exchange failed: HTTP {resp.status_code} {resp.text[:300]}"
        )
    data = resp.json()
    if not data.get("refresh_token"):
        raise SystemExit(f"token response missing refresh_token: {sorted(data)}")
    return data


def set_gh_secret(repo: str, name: str, value: str) -> None:
    proc = subprocess.run(
        ["gh", "secret", "set", name, "--repo", repo],
        input=value.encode("utf-8"),
        capture_output=True,
    )
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise SystemExit(f"gh secret set {name} failed: {err or proc.returncode}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Register a public LetsFG OAuth client, complete PKCE in the browser, "
            "and print LETSFG_CLIENT_ID / LETSFG_REFRESH_TOKEN."
        )
    )
    p.add_argument(
        "--show",
        action="store_true",
        help="print full secret values (default: mask all but last 4 chars)",
    )
    p.add_argument(
        "--set-gh-secrets",
        action="store_true",
        help="pipe secrets into `gh secret set` via stdin (never argv)",
    )
    p.add_argument(
        "--repo",
        metavar="OWNER/REPO",
        help="GitHub repo for --set-gh-secrets (e.g. acme/flightsearch)",
    )
    p.add_argument(
        "--redirect-uri",
        default=DEFAULT_REDIRECT,
        help=f"loopback callback (default: {DEFAULT_REDIRECT})",
    )
    p.add_argument(
        "--scope",
        default=DEFAULT_SCOPE,
        help=f"OAuth scope (default: {DEFAULT_SCOPE})",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="seconds to wait for the browser callback (default: 300)",
    )
    p.add_argument(
        "--no-browser",
        action="store_true",
        help="print the authorize URL instead of opening a browser",
    )
    args = p.parse_args(argv)
    if args.set_gh_secrets and not args.repo:
        p.error("--set-gh-secrets requires --repo OWNER/REPO")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with httpx.Client(timeout=30.0) as client:
        meta = discover(client)
        client_id = register_client(
            client, meta["registration_endpoint"], args.redirect_uri
        )
        verifier, challenge = _pkce()
        state = secrets.token_urlsafe(24)
        authorize = (
            f"{meta['authorization_endpoint']}"
            f"?response_type=code"
            f"&client_id={urllib.parse.quote(client_id)}"
            f"&redirect_uri={urllib.parse.quote(args.redirect_uri, safe='')}"
            f"&code_challenge={urllib.parse.quote(challenge)}"
            f"&code_challenge_method=S256"
            f"&state={urllib.parse.quote(state)}"
            f"&scope={urllib.parse.quote(args.scope)}"
            f"&prompt=consent"
        )
        print("Open this URL and approve the LetsFG connection:", file=sys.stderr)
        print(authorize, file=sys.stderr)
        if not args.no_browser:
            webbrowser.open(authorize)
        code = capture_code(args.redirect_uri, state, args.timeout)
        tokens = exchange_code(
            client,
            meta["token_endpoint"],
            code=code,
            client_id=client_id,
            redirect_uri=args.redirect_uri,
            verifier=verifier,
        )

    refresh = str(tokens["refresh_token"])
    print(f"LETSFG_CLIENT_ID={_mask(client_id, args.show)}")
    print(f"LETSFG_REFRESH_TOKEN={_mask(refresh, args.show)}")
    if args.set_gh_secrets:
        set_gh_secret(args.repo, "LETSFG_CLIENT_ID", client_id)
        set_gh_secret(args.repo, "LETSFG_REFRESH_TOKEN", refresh)
        print(f"GitHub secrets set on {args.repo}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
