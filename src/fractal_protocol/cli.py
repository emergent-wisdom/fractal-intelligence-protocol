from __future__ import annotations

import argparse
import ipaddress
import os

from .api import CoordinatorHTTPServer
from .database import Database
from .service import CoordinatorService, ServiceConfig


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _serve(args: argparse.Namespace) -> int:
    admin_token = args.admin_token or os.environ.get("FI_ADMIN_TOKEN")
    if not admin_token:
        raise SystemExit("Set --admin-token or FI_ADMIN_TOKEN.")
    if not admin_token.isascii():
        raise SystemExit("The admin token must contain ASCII characters only.")
    if not _is_loopback_host(args.host) and not args.allow_insecure_http:
        raise SystemExit(
            "Refusing non-loopback plaintext HTTP. Bind to loopback behind a TLS "
            "reverse proxy, or use --allow-insecure-http only on an isolated "
            "development network."
        )
    service = CoordinatorService(
        Database(args.database),
        config=ServiceConfig(
            lease_seconds=args.lease_seconds,
            platform_fee_bps=args.platform_fee_bps,
            max_depth=args.max_depth,
            max_inflight_per_node=args.max_inflight_per_node,
            max_delegation_proposals=args.max_delegation_proposals,
            max_tasks_per_problem=args.max_tasks_per_problem,
            max_offerings_per_node=args.max_offerings_per_node,
        ),
    )
    server = CoordinatorHTTPServer(
        (args.host, args.port),
        service,
        admin_token=admin_token,
    )
    display_host = f"[{args.host}]" if ":" in args.host else args.host
    print(
        f"Fractal coordinator listening on http://{display_host}:{args.port} "
        f"using {args.database}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fractal-coordinator")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="run the centralized coordinator")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--allow-insecure-http", action="store_true")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--database", default="fractal-coordinator.db")
    serve.add_argument("--admin-token")
    serve.add_argument("--lease-seconds", type=int, default=60)
    serve.add_argument("--platform-fee-bps", type=int, default=0)
    serve.add_argument("--max-depth", type=int, default=12)
    serve.add_argument("--max-inflight-per-node", type=int, default=1)
    serve.add_argument("--max-delegation-proposals", type=int, default=3)
    serve.add_argument("--max-tasks-per-problem", type=int, default=100)
    serve.add_argument("--max-offerings-per-node", type=int, default=50)
    serve.set_defaults(function=_serve)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.function(args)


if __name__ == "__main__":
    raise SystemExit(main())
