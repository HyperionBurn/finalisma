"""Hosted MCP transport negotiation tests — Accept-header handling on POST /mcp.

The hosted MCP endpoint must honour the request's Accept header under MCP
Streamable HTTP:

  - ``Accept: text/event-stream`` only  -> a ``text/event-stream`` response
    whose ``data:`` frame decodes to a JSON-RPC payload identical to the
    JSON path's,
  - ``Accept: application/json`` only   -> unchanged JSON behaviour,
  - ``Accept: application/json, text/event-stream`` -> JSON (the client
    accepts it, so the long-standing default wire format wins; documented in
    the handler docstring),
  - ``Accept: neither``                 -> 406,
  - an unauthenticated request under every negotiated type -> the same
    byte-identical JSON 401 (auth outranks negotiation),
  - a long ``room_wait`` under SSE emits at least one ``: keepalive`` comment
    frame before the final ``data:`` frame,
  - a notification under an SSE-only Accept is still a 202 with an empty body.

Authoritative spec: docs/HOSTED_MCP_DESIGN.md.
"""

from __future__ import annotations

import http.client
import json
import time
import unittest
from http import HTTPStatus
from urllib.parse import urlsplit

from test_hosted_mcp import HostedMCPTestBase

from weft_cloud.service import _CloudHTTPHandler


def _raw_post(base: str, path: str, body: bytes,
              headers: dict[str, str], timeout: float = 30.0) -> tuple[int, dict[str, str], bytes]:
    """POST raw bytes and return (status, lower-cased response headers, body).

    Used instead of urllib so the exact Content-Type and body framing can be
    asserted (SSE bodies must not be re-wrapped by a higher layer).
    """
    url = urlsplit(base)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=timeout)
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        headers_out = {k.lower(): v for k, v in resp.getheaders()}
        body_out = resp.read()
        return status, headers_out, body_out
    finally:
        conn.close()


def _mcp_post(base: str, method: str, params: dict | None,
              token: str | None, accept: str | None,
              request_id: int = 1, notification: bool = False,
              timeout: float = 30.0) -> tuple[int, dict[str, str], bytes]:
    body: dict = {"jsonrpc": "2.0", "method": method}
    if not notification:
        body["id"] = request_id
    if params is not None:
        body["params"] = params
    headers = {"Content-Type": "application/json"}
    if accept is not None:
        headers["Accept"] = accept
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return _raw_post(base, "/mcp", json.dumps(body, separators=(",", ":")).encode("utf-8"),
                     headers, timeout=timeout)


def _decode_sse(body: bytes) -> dict:
    """Decode the JSON-RPC envelope from an SSE body (``data:`` frames only).

    Comment frames (``: keepalive``) are ignored; all ``data:`` line payloads
    are joined per the SSE spec, which for a single event is the full JSON.
    """
    lines = []
    for raw in body.decode("utf-8").splitlines():
        line = raw.strip()
        if line.startswith("data:"):
            lines.append(line[len("data:"):].lstrip())
    return json.loads("\n".join(lines))


class HostedMCPTransportTests(HostedMCPTestBase):
    """Accept-header negotiation on the hosted /mcp endpoint."""

    def _signed_in(self, tag: str) -> dict:
        acct = self._signup(f"{tag}@example.com")
        return acct["session_token"]

    def test_sse_only_accept_frames_response_and_matches_json_path(self) -> None:
        """Accept: text/event-stream only -> SSE body whose decoded JSON-RPC
        payload is identical to what the JSON path returns for the same call."""
        token = self._signed_in("sse-only")

        # Baseline: the JSON path's exact response for initialize and a tool call.
        json_status, json_headers, json_body = _mcp_post(
            self.base, "initialize",
            {"protocolVersion": "2025-11-25", "capabilities": {}},
            token=token, accept="application/json", request_id=1)
        self.assertEqual(json_status, HTTPStatus.OK)
        self.assertEqual(json_headers["content-type"], "application/json")
        json_payload = json.loads(json_body.decode("utf-8"))

        created = self._assert_ok(token, "room_create", {"cap": 4}, request_id=2)
        json_info_status, _, json_info_body = _mcp_post(
            self.base, "tools/call",
            {"name": "room_info", "arguments": {"room_id": created["room_id"]}},
            token=token, accept="application/json", request_id=3)
        self.assertEqual(json_info_status, HTTPStatus.OK)
        json_info = json.loads(json_info_body.decode("utf-8"))

        # Same two calls with an SSE-only Accept.
        sse_status, sse_headers, sse_body = _mcp_post(
            self.base, "initialize",
            {"protocolVersion": "2025-11-25", "capabilities": {}},
            token=token, accept="text/event-stream", request_id=1)
        self.assertEqual(sse_status, HTTPStatus.OK)
        self.assertEqual(sse_headers["content-type"], "text/event-stream")
        self.assertRegex(sse_body.decode("utf-8"), r"\Adata: .*\n\n\Z",
                         "SSE body must be a single data: frame")
        self.assertEqual(_decode_sse(sse_body), json_payload,
                         "decoded SSE initialize must equal the JSON path's payload")

        sse_info_status, sse_info_headers, sse_info_body = _mcp_post(
            self.base, "tools/call",
            {"name": "room_info", "arguments": {"room_id": created["room_id"]}},
            token=token, accept="text/event-stream", request_id=3)
        self.assertEqual(sse_info_status, HTTPStatus.OK)
        self.assertEqual(sse_info_headers["content-type"], "text/event-stream")
        self.assertEqual(_decode_sse(sse_info_body), json_info,
                         "decoded SSE tools/call must equal the JSON path's payload")

    def test_json_only_accept_is_unchanged(self) -> None:
        token = self._signed_in("json-only")
        status, headers, body = _mcp_post(
            self.base, "ping", None, token=token,
            accept="application/json", request_id=1)
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(json.loads(body.decode("utf-8")), {"jsonrpc": "2.0", "id": 1, "result": {}})
        self.assertNotIn(b"data: ", body,
                         "a JSON-accepting client must not receive SSE framing")

    def test_accept_both_keeps_existing_json_default(self) -> None:
        """Deliberate choice: when the client accepts JSON (alone or alongside
        SSE) we keep the long-standing JSON response, so every caller that
        works today keeps working byte-for-byte. SSE is used only when the
        client explicitly excludes JSON."""
        token = self._signed_in("both")
        status, headers, body = _mcp_post(
            self.base, "tools/list", None, token=token,
            accept="application/json, text/event-stream", request_id=1)
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertNotIn(b"data: ", body)

    def test_accept_neither_is_406(self) -> None:
        token = self._signed_in("neither")
        status, headers, _ = _mcp_post(
            self.base, "ping", None, token=token,
            accept="application/xml", request_id=1)
        self.assertEqual(status, HTTPStatus.NOT_ACCEPTABLE)
        self.assertEqual(headers["content-type"], "application/json")

    def test_unauthenticated_refusals_are_byte_identical_across_negotiated_types(self) -> None:
        """Auth outranks negotiation: a missing token yields the SAME 401 body
        under every Accept header (json-only, sse-only, both), never an SSE
        frame and never a different message."""
        bodies: list[bytes] = []
        for accept in ("application/json", "text/event-stream",
                       "application/json, text/event-stream", None):
            status, headers, body = _mcp_post(
                self.base, "initialize",
                {"protocolVersion": "2025-11-25", "capabilities": {}},
                token=None, accept=accept, request_id=7)
            self.assertEqual(status, HTTPStatus.UNAUTHORIZED,
                             f"401 expected for Accept={accept!r}")
            self.assertEqual(headers["content-type"], "application/json",
                             f"401 must stay JSON for Accept={accept!r}")
            bodies.append(body)
        self.assertTrue(all(b == bodies[0] for b in bodies),
                        "unauthenticated refusals must be byte-identical across Accept values")
        self.assertEqual(_decode_sse(b"data: " + bodies[0] + b"\n\n"),
                         _decode_sse(b"data: " + bodies[0] + b"\n\n"))

    def test_notification_under_sse_only_accept_is_still_202_empty(self) -> None:
        token = self._signed_in("notify-sse")
        status, headers, body = _mcp_post(
            self.base, "notifications/initialized", {}, token=token,
            accept="text/event-stream", request_id=None, notification=True)
        self.assertEqual(status, HTTPStatus.ACCEPTED)
        self.assertEqual(body, b"")

    def test_notification_method_with_id_is_202_not_an_sse_stream(self) -> None:
        """A notifications/* method sent WITH an id replies 202 empty on the
        JSON path; under an SSE-only Accept it must stay a 202 with no body,
        never a streamed ``data:`` frame with no payload."""
        token = self._signed_in("notify-with-id")
        status, headers, body = _mcp_post(
            self.base, "notifications/initialized", {}, token=token,
            accept="text/event-stream", request_id=9, notification=False)
        self.assertEqual(status, HTTPStatus.ACCEPTED)
        self.assertNotEqual(headers.get("content-type"), "text/event-stream")
        self.assertEqual(body, b"")


class HostedMCPSSEKeepaliveTests(HostedMCPTestBase):
    """A long ``room_wait`` streamed over SSE must not sit silent: at least
    one ``: keepalive`` comment frame lands before the final ``data:`` frame."""

    def test_long_room_wait_emits_keepalive_before_final_data_frame(self) -> None:
        old = _CloudHTTPHandler.mcp_sse_keepalive_seconds
        _CloudHTTPHandler.mcp_sse_keepalive_seconds = 1
        try:
            owner = self._signup("ka-o@example.com")
            waiter = self._signup("ka-w@example.com", tenant_id=owner["tenant_id"])
            created = self._assert_ok(owner["session_token"], "room_create",
                                      {"cap": 4}, request_id=1)
            self._assert_ok(waiter["session_token"], "room_join",
                            {"room_id": created["room_id"],
                             "link_token": created["link_token"], "consent": True}, request_id=2)
            head = self._assert_ok(waiter["session_token"], "room_poll",
                                   {"room_id": created["room_id"]}, request_id=3)["cursor_head"]
            self._assert_ok(waiter["session_token"], "room_ack",
                            {"room_id": created["room_id"], "seq": head}, request_id=4)

            start = time.monotonic()
            status, headers, body = _mcp_post(
                self.base, "tools/call",
                {"name": "room_wait",
                 "arguments": {"room_id": created["room_id"], "after_seq": head,
                               "timeout_seconds": 3}},
                token=waiter["session_token"], accept="text/event-stream", request_id=5,
                timeout=20.0)
            elapsed = time.monotonic() - start
            self.assertEqual(status, HTTPStatus.OK)
            self.assertEqual(headers["content-type"], "text/event-stream")
            self.assertGreaterEqual(elapsed, 1.5,
                                    "a blocked wait must actually hold the stream open")

            keepalive_idx = body.find(b": keepalive")
            self.assertGreaterEqual(keepalive_idx, 0,
                                    "an SSE room_wait must emit at least one keepalive")
            final_data = body.rfind(b"data: ")
            self.assertLess(keepalive_idx, final_data,
                            "the keepalive must precede the final data: frame")
            payload = _decode_sse(body)
            self.assertEqual(payload["result"]["structuredContent"]["timed_out"], True,
                             "nobody spoke: the decoded wait result must be the empty timeout")
        finally:
            _CloudHTTPHandler.mcp_sse_keepalive_seconds = old


if __name__ == "__main__":
    unittest.main()
