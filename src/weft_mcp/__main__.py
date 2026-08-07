"""CLI entry point for ``python -m weft_mcp``."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .core import WeftError, WeftStore
from .server import WeftDispatcher, run_http, run_stdio


_LOOPBACK_HTTP_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _resolve_actor_auth(transport: str, actor_auth: str, host: str) -> bool:
    """Resolve the safe transport default without silently weakening HTTP."""
    if actor_auth == "auto":
        return transport == "http"
    if actor_auth == "required":
        return True
    if transport == "http":
        if host.strip().lower() not in _LOOPBACK_HTTP_HOSTS:
            raise WeftError("actor_auth_trust_forbidden", "--actor-auth trust is only allowed for loopback HTTP")
        print(
            "Weft security warning: loopback HTTP is running in trusted actor mode; "
            "any local client that reaches this endpoint can act as a registered agent.",
            file=sys.stderr,
        )
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Weft MCP: connect two agents and get a team")
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--state", default=os.environ.get("WEFT_STATE", ".weft/state.db"), help="SQLite state file")
    parser.add_argument("--workspace", default=os.environ.get("WEFT_WORKSPACE", "."), help="Workspace root for scope and artifact checks")
    parser.add_argument("--heartbeat-timeout", type=int, default=int(os.environ.get("WEFT_HEARTBEAT_TIMEOUT", "1800")))
    parser.add_argument("--host", default=os.environ.get("WEFT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WEFT_PORT", "8787")))
    parser.add_argument("--allowed-origin", action="append", default=None, help="Allowed browser Origin; repeat for more than one")
    parser.add_argument("--team-id", default=os.environ.get("WEFT_TEAM_ID"), help="Optional hard boundary for this coordinator's team/workspace")
    parser.add_argument("--token-env", default="WEFT_HTTP_TOKEN", help="Environment variable containing the HTTP bearer token")
    parser.add_argument("--public-url", default=os.environ.get("WEFT_PUBLIC_URL"), help="Public base URL embedded in generated pairing links")
    parser.add_argument(
        "--actor-auth",
        choices=("auto", "required", "trust"),
        default=os.environ.get("WEFT_ACTOR_AUTH", "auto"),
        help="Actor credential policy: auto (HTTP required, stdio trust), required, or loopback-only trust",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store: WeftStore | None = None
    try:
        require_actor_auth = _resolve_actor_auth(args.transport, args.actor_auth, args.host)
        store = WeftStore(
            args.state,
            args.workspace,
            args.heartbeat_timeout,
            args.public_url,
            require_actor_auth=require_actor_auth,
        )
        dispatcher = WeftDispatcher(store, team_scope=args.team_id)
        if args.transport == "stdio":
            run_stdio(dispatcher)
        else:
            token = os.environ.get(args.token_env) or None
            origins = set(args.allowed_origin or ["http://localhost", "http://127.0.0.1"])
            run_http(dispatcher, args.host, args.port, token, origins)
    except WeftError as exc:
        raise SystemExit(f"Weft: {exc.message}") from exc
    finally:
        if store is not None:
            store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
