from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class CoordinatorClientError(Exception):
    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status = status
        self.payload = payload
        message = payload.get("error", {}).get("message", f"HTTP {status}")
        super().__init__(message)


class CoordinatorClient:
    def __init__(
        self,
        base_url: str,
        *,
        node_token: str | None = None,
        admin_token: str | None = None,
        node_invite_token: str | None = None,
        timeout: float = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.node_token = node_token
        self.admin_token = admin_token
        self.node_invite_token = node_invite_token
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any | None = None,
        token: str | None = None,
        query: dict[str, str] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return None if response.status == 204 or not raw else json.loads(raw)
        except HTTPError as exc:
            try:
                raw = exc.read()
            finally:
                exc.close()
            try:
                payload = json.loads(raw) if raw else {"error": {"message": str(exc)}}
            except json.JSONDecodeError:
                payload = {"error": {"message": raw.decode("utf-8", errors="replace")}}
            raise CoordinatorClientError(exc.code, payload) from exc
        except URLError as exc:
            raise ConnectionError(f"Could not reach coordinator: {exc.reason}") from exc

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/healthz")

    def register_node(
        self,
        operator_name: str,
        metadata: dict[str, Any] | None = None,
        *,
        registration_id: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/nodes/register",
            body={
                "registration_id": registration_id,
                "operator_name": operator_name,
                "metadata": metadata or {},
            },
            token=self.node_invite_token,
        )

    def create_node_invite(
        self, label: str, *, expires_in_seconds: int = 3600
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/node-invites",
            body={"label": label, "expires_in_seconds": expires_in_seconds},
            token=self.admin_token,
        )

    def publish_offering(self, manifest: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST", "/v1/node/offerings", body=manifest, token=self.node_token
        )

    def heartbeat(self) -> dict[str, Any]:
        return self._request("POST", "/v1/node/heartbeat", body={}, token=self.node_token)

    def list_offerings(self, status: str | None = None) -> dict[str, Any]:
        query = {"status": status} if status else None
        return self._request(
            "GET", "/v1/offerings", token=self.admin_token, query=query
        )

    def approve_offering(self, offering_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/offerings/{quote(offering_id, safe='')}/approve",
            body={},
            token=self.admin_token,
        )

    def lease(self, *, lease_request_id: str) -> dict[str, Any] | None:
        return self._request(
            "POST",
            "/v1/node/leases",
            body={"lease_request_id": lease_request_id},
            token=self.node_token,
        )

    def delegate(
        self,
        task_id: str,
        *,
        idempotency_key: str,
        lease_token: str,
        children: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/node/tasks/{quote(task_id, safe='')}/children",
            body={
                "idempotency_key": idempotency_key,
                "lease_token": lease_token,
                "children": children,
            },
            token=self.node_token,
        )

    def submit(
        self,
        task_id: str,
        *,
        submission_id: str,
        lease_token: str,
        status: str,
        stop_reason: str,
        outputs: dict[str, Any],
        evidence: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/node/tasks/{quote(task_id, safe='')}/submissions",
            body={
                "submission_id": submission_id,
                "lease_token": lease_token,
                "status": status,
                "stop_reason": stop_reason,
                "outputs": outputs,
                "evidence": evidence or {},
                "usage": usage or {},
            },
            token=self.node_token,
        )

    def earnings(self) -> dict[str, Any]:
        return self._request("GET", "/v1/node/earnings", token=self.node_token)

    def list_delegations(self, status: str | None = None) -> dict[str, Any]:
        query = {"status": status} if status else None
        return self._request(
            "GET", "/v1/delegations", token=self.admin_token, query=query
        )

    def approve_delegation(
        self, delegation_id: str, *, allow_self_execution: bool = False
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/delegations/{quote(delegation_id, safe='')}/approve",
            body=(
                {"allow_self_execution": True}
                if allow_self_execution
                else None
            ),
            token=self.admin_token,
        )

    def reject_delegation(
        self, delegation_id: str, *, reason: str
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/delegations/{quote(delegation_id, safe='')}/reject",
            body={"reason": reason},
            token=self.admin_token,
        )

    def create_problem(self, problem: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST", "/v1/problems", body=problem, token=self.admin_token
        )

    def get_problem(
        self,
        problem_id: str,
        *,
        submission_limit: int = 50,
        submission_offset: int = 0,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/problems/{quote(problem_id, safe='')}",
            token=self.admin_token,
            query={
                "submission_limit": str(submission_limit),
                "submission_offset": str(submission_offset),
            },
        )

    def cancel_problem(self, problem_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/problems/{quote(problem_id, safe='')}/cancel",
            body={},
            token=self.admin_token,
        )

    def reframe_problem(
        self, problem_id: str, reframe: dict[str, Any]
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/problems/{quote(problem_id, safe='')}/reframes",
            body=reframe,
            token=self.admin_token,
        )

    def pathways(self, capability: str | None = None) -> dict[str, Any]:
        query = {"capability": capability} if capability else None
        return self._request(
            "GET", "/v1/pathways", token=self.admin_token, query=query
        )
