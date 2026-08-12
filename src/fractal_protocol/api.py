from __future__ import annotations

import hmac
import ipaddress
import json
import socket
import threading
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import DomainError
from .service import CoordinatorService


MAX_BODY_BYTES = 512 * 1024
_NO_BODY = object()


def _constant_time_token_match(supplied: str | None, expected: str) -> bool:
    return bool(
        supplied is not None
        and supplied.isascii()
        and expected.isascii()
        and hmac.compare_digest(supplied, expected)
    )


class CoordinatorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        service: CoordinatorService,
        *,
        admin_token: str,
    ) -> None:
        if not admin_token:
            raise ValueError("admin_token is required")
        if not admin_token.isascii():
            raise ValueError("admin_token must contain ASCII characters only")
        self.service = service
        self.admin_token = admin_token
        self._reaper_stop = threading.Event()
        self._reaper_thread: threading.Thread | None = None
        try:
            address_ip = ipaddress.ip_address(address[0])
        except ValueError:
            address_ip = None
        self.address_family = (
            socket.AF_INET6
            if address_ip is not None and address_ip.version == 6
            else socket.AF_INET
        )
        super().__init__(address, CoordinatorRequestHandler)
        self._reaper_thread = threading.Thread(
            target=self._reap_loop,
            name="fractal-coordinator-reaper",
            daemon=True,
        )
        self._reaper_thread.start()

    def _reap_loop(self) -> None:
        while not self._reaper_stop.wait(1.0):
            try:
                self.service.reap_expired()
            except Exception:
                traceback.print_exc()

    def server_close(self) -> None:
        self._reaper_stop.set()
        if (
            self._reaper_thread is not None
            and self._reaper_thread.is_alive()
            and threading.current_thread() is not self._reaper_thread
        ):
            self._reaper_thread.join(timeout=2)
        super().server_close()


class CoordinatorRequestHandler(BaseHTTPRequestHandler):
    server: CoordinatorHTTPServer
    server_version = "FractalCoordinator/0.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            parsed = urlsplit(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)

            if method == "GET" and path == "/healthz":
                self._json(HTTPStatus.OK, {"status": "ok", "protocol_version": "1"})
                return

            if method == "POST" and path == "/v1/node-invites":
                self._require_token(self.server.admin_token, "admin")
                self._json(
                    HTTPStatus.CREATED,
                    self.server.service.create_node_invite(self._body()),
                )
                return

            if method == "POST" and path == "/v1/nodes/register":
                self._json(
                    HTTPStatus.CREATED,
                    self.server.service.register_node(self._bearer(), self._body()),
                )
                return

            if method == "POST" and path == "/v1/node/offerings":
                self._json(
                    HTTPStatus.CREATED,
                    self.server.service.publish_offering(self._bearer(), self._body()),
                )
                return

            if method == "POST" and path == "/v1/node/heartbeat":
                self._json(HTTPStatus.OK, self.server.service.heartbeat(self._bearer()))
                return

            if method == "GET" and path == "/v1/offerings":
                self._require_token(self.server.admin_token, "admin")
                status = query.get("status", [None])[0]
                self._json(
                    HTTPStatus.OK,
                    self.server.service.list_offerings(status),
                )
                return

            offering_approve_suffix = "/approve"
            if (
                method == "POST"
                and path.startswith("/v1/offerings/")
                and path.endswith(offering_approve_suffix)
            ):
                self._require_token(self.server.admin_token, "admin")
                offering_id = unquote(
                    path[
                        len("/v1/offerings/") : -len(offering_approve_suffix)
                    ].rstrip("/")
                )
                self._json(
                    HTTPStatus.OK,
                    self.server.service.approve_offering(offering_id),
                )
                return

            if method == "POST" and path == "/v1/node/leases":
                body = self._body()
                if not isinstance(body, dict):
                    raise DomainError("invalid_body", "The request body must be an object")
                lease_request_id = body.get("lease_request_id")
                if not isinstance(lease_request_id, str) or not lease_request_id.strip():
                    raise DomainError(
                        "invalid_lease_request",
                        "lease_request_id is required for replay-safe leasing",
                    )
                lease = self.server.service.lease_work(
                    self._bearer(), lease_request_id
                )
                if lease is None:
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self.end_headers()
                else:
                    self._json(HTTPStatus.OK, lease)
                return

            task_suffix = "/children"
            if method == "POST" and path.startswith("/v1/node/tasks/") and path.endswith(task_suffix):
                task_id = unquote(path[len("/v1/node/tasks/") : -len(task_suffix)].rstrip("/"))
                self._json(
                    HTTPStatus.CREATED,
                    self.server.service.delegate_children(
                        self._bearer(), task_id, self._body()
                    ),
                )
                return

            submission_suffix = "/submissions"
            if method == "POST" and path.startswith("/v1/node/tasks/") and path.endswith(submission_suffix):
                task_id = unquote(
                    path[len("/v1/node/tasks/") : -len(submission_suffix)].rstrip("/")
                )
                self._json(
                    HTTPStatus.OK,
                    self.server.service.submit_result(
                        self._bearer(), task_id, self._body()
                    ),
                )
                return

            if method == "GET" and path == "/v1/node/earnings":
                self._json(
                    HTTPStatus.OK,
                    self.server.service.get_node_earnings(self._bearer()),
                )
                return

            if method == "GET" and path == "/v1/delegations":
                self._require_token(self.server.admin_token, "admin")
                status = query.get("status", [None])[0]
                self._json(
                    HTTPStatus.OK,
                    self.server.service.list_delegations(status),
                )
                return

            approve_suffix = "/approve"
            if method == "POST" and path.startswith("/v1/delegations/") and path.endswith(approve_suffix):
                self._require_token(self.server.admin_token, "admin")
                delegation_id = unquote(
                    path[len("/v1/delegations/") : -len(approve_suffix)].rstrip("/")
                )
                body = self._body(required=False)
                if body is _NO_BODY:
                    response = self.server.service.approve_delegation(delegation_id)
                else:
                    response = self.server.service.approve_delegation(
                        delegation_id, body
                    )
                self._json(
                    HTTPStatus.OK,
                    response,
                )
                return

            reject_suffix = "/reject"
            if method == "POST" and path.startswith("/v1/delegations/") and path.endswith(reject_suffix):
                self._require_token(self.server.admin_token, "admin")
                delegation_id = unquote(
                    path[len("/v1/delegations/") : -len(reject_suffix)].rstrip("/")
                )
                self._json(
                    HTTPStatus.OK,
                    self.server.service.reject_delegation(
                        delegation_id, self._body()
                    ),
                )
                return

            if method == "POST" and path == "/v1/problems":
                self._require_token(self.server.admin_token, "admin")
                self._json(
                    HTTPStatus.CREATED,
                    self.server.service.create_problem(self._body()),
                )
                return

            reframe_suffix = "/reframes"
            if (
                method == "POST"
                and path.startswith("/v1/problems/")
                and path.endswith(reframe_suffix)
            ):
                self._require_token(self.server.admin_token, "admin")
                problem_id = unquote(
                    path[
                        len("/v1/problems/") : -len(reframe_suffix)
                    ].rstrip("/")
                )
                self._json(
                    HTTPStatus.CREATED,
                    self.server.service.reframe_problem(
                        problem_id, self._body()
                    ),
                )
                return

            cancel_suffix = "/cancel"
            if method == "POST" and path.startswith("/v1/problems/") and path.endswith(cancel_suffix):
                self._require_token(self.server.admin_token, "admin")
                problem_id = unquote(
                    path[len("/v1/problems/") : -len(cancel_suffix)].rstrip("/")
                )
                self._json(
                    HTTPStatus.OK,
                    self.server.service.cancel_problem(problem_id),
                )
                return

            if method == "GET" and path.startswith("/v1/problems/"):
                self._require_token(self.server.admin_token, "admin")
                problem_id = unquote(path[len("/v1/problems/") :])
                try:
                    submission_limit = int(query.get("submission_limit", ["50"])[0])
                    submission_offset = int(
                        query.get("submission_offset", ["0"])[0]
                    )
                except ValueError as exc:
                    raise DomainError(
                        "invalid_pagination",
                        "Submission pagination values must be integers",
                    ) from exc
                self._json(
                    HTTPStatus.OK,
                    self.server.service.get_problem(
                        problem_id,
                        submission_limit=submission_limit,
                        submission_offset=submission_offset,
                    ),
                )
                return

            if method == "GET" and path == "/v1/pathways":
                self._require_token(self.server.admin_token, "admin")
                capability = query.get("capability", [None])[0]
                self._json(
                    HTTPStatus.OK,
                    self.server.service.pathway_summary(capability),
                )
                return

            raise DomainError("not_found", "Route not found", status=404)
        except DomainError as exc:
            self._json(exc.status, exc.as_dict())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": {
                        "code": "invalid_json",
                        "message": "The request body is not valid JSON",
                        "details": {"reason": str(exc)},
                    }
                },
            )
        except Exception:
            traceback.print_exc()
            self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": {
                        "code": "internal_error",
                        "message": "The coordinator failed to process the request",
                        "details": {},
                    }
                },
            )

    def _body(self, *, required: bool = True) -> Any:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            if not required:
                return _NO_BODY
            raise DomainError("missing_body", "A JSON request body is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise DomainError("invalid_body", "Content-Length is invalid") from exc
        if length < 0 or length > MAX_BODY_BYTES:
            raise DomainError(
                "body_too_large",
                f"The JSON body must be at most {MAX_BODY_BYTES} bytes",
                status=413,
            )
        if length == 0 and not required:
            return _NO_BODY
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _bearer(self) -> str | None:
        value = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not value.startswith(prefix):
            return None
        return value[len(prefix) :]

    def _require_token(self, expected: str, kind: str) -> None:
        supplied = self._bearer()
        if not _constant_time_token_match(supplied, expected):
            raise DomainError(
                "unauthorized", f"A valid {kind} bearer token is required", status=401
            )

    def _json(self, status: int | HTTPStatus, value: Any) -> None:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)
