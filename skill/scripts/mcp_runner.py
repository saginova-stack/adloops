"""Minimal synchronous JSON-RPC stdio client for MCP servers.

Used by the Phase 2 executors to talk to the vendored kLOsk/adloop MCP
(uv-managed Python) and danielpopamd/linkedin-ads-mcp (Node). Both speak
the standard MCP wire protocol over stdin/stdout: line-delimited JSON-RPC
2.0 with one initialize handshake, then any number of `tools/call`
requests.

Why hand-rolled instead of the `mcp` PyPI package?
  - We need a single synchronous call from a cron-driven script — no
    benefit from the async client.
  - The protocol surface we actually exercise (initialize → call tool →
    teardown) is ~70 lines. Pulling the full async SDK doubles the
    dependency footprint for no behavioural gain.
  - IO is injectable, so tests don't spawn subprocesses — they hand the
    session BytesIO buffers with canned bytes.

Production callers use `call_tool(spec, tool_name, args)` — it spawns the
server, runs one tool, and tears down. For multiple tools per audit run
use the `MCPSession` context manager directly, or call `MCPClient(spec)`
and reuse it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, BinaryIO


PROTOCOL_VERSION = "2024-11-05"   # current MCP spec at time of writing
CLIENT_INFO = {"name": "adloops", "version": "0.1.0"}


class MCPProtocolError(RuntimeError):
    """Wire-level error: bad JSON, missing fields, transport closed early."""


class MCPToolError(RuntimeError):
    """The server returned a JSON-RPC error or `isError: true` from a tool."""


# ---------------------------------------------------------------------------

@dataclass
class ServerSpec:
    """How to spawn an MCP server."""
    command: str
    args: list[str] = field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] | None = None


# ---------------------------------------------------------------------------

class MCPSession:
    """Speaks MCP over a pair of byte-oriented streams.

    The streams are abstracted from subprocess management so tests can
    drive the session with in-memory buffers. Use the `spawn()` classmethod
    or the module-level `call_tool()` helper for real-world use.
    """

    def __init__(self, stdin: BinaryIO, stdout: BinaryIO):
        self._stdin = stdin
        self._stdout = stdout
        self._next_id = 0
        self._initialized = False

    # -- public API ----------------------------------------------------------

    def initialize(self) -> None:
        """Run the MCP initialize handshake. Idempotent."""
        if self._initialized:
            return
        resp = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        })
        if "protocolVersion" not in resp:
            raise MCPProtocolError(
                f"Server did not negotiate protocolVersion: {resp}"
            )
        self._notify("notifications/initialized", {})
        self._initialized = True

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call a server tool. Returns the parsed body of the first text
        content item (JSON if parseable, str otherwise), or the raw content
        list if the response is not text-only.

        Raises MCPToolError on tool failure.
        """
        if not self._initialized:
            raise MCPProtocolError("Call initialize() before tools/call.")
        resp = self._request("tools/call", {
            "name": name,
            "arguments": arguments or {},
        })
        if resp.get("isError"):
            raise MCPToolError(
                f"Tool {name!r} returned isError=true: {_summarize_content(resp.get('content'))}"
            )
        return _unwrap_content(resp.get("content"))

    # -- wire protocol ------------------------------------------------------

    def _next_request_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        rid = self._next_request_id()
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            frame = self._read()
            if frame.get("method") and "id" not in frame:
                # notification from the server (e.g. progress) — ignore for now
                continue
            if frame.get("id") != rid:
                # response to a different request id — protocol violation
                raise MCPProtocolError(
                    f"Received response for id {frame.get('id')!r}; expected {rid!r}"
                )
            if "error" in frame:
                err = frame["error"]
                raise MCPToolError(
                    f"JSON-RPC error from {method!r}: {err.get('code')} {err.get('message')}"
                )
            return frame.get("result") or {}

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        # notifications carry no id — the server does not respond
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, payload: dict[str, Any]) -> None:
        line = (json.dumps(payload) + "\n").encode("utf-8")
        self._stdin.write(line)
        self._stdin.flush()

    def _read(self) -> dict[str, Any]:
        line = self._stdout.readline()
        if not line:
            raise MCPProtocolError("Server closed the stream before responding.")
        try:
            return json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise MCPProtocolError(f"Server emitted non-JSON line: {line!r}") from e


# ---------------------------------------------------------------------------

def _unwrap_content(content: Any) -> Any:
    """Tools return a `content` list of {type, text, ...} items. The common
    case for our executors is a single text item whose body is JSON — try
    to parse, else return the text. Multi-item or non-text content is
    returned verbatim so the caller can deal with it.
    """
    if not isinstance(content, list) or not content:
        return content
    if len(content) == 1 and content[0].get("type") == "text":
        text = content[0].get("text", "")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return content


def _summarize_content(content: Any) -> str:
    """Best-effort one-line summary for error messages."""
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return " | ".join(parts) or repr(content)
    return repr(content)


# ---------------------------------------------------------------------------

def call_tool(
    spec: ServerSpec,
    tool: str,
    arguments: dict[str, Any] | None = None,
    *,
    timeout_s: float = 60.0,
) -> Any:
    """Spawn an MCP server, run a single tool, tear down.

    Use this for one-shot calls. For multiple calls per audit run, spawn
    the server once via `spawn()` and call its session repeatedly.
    """
    proc = _spawn(spec)
    try:
        deadline = time.monotonic() + timeout_s
        session = MCPSession(proc.stdin, proc.stdout)
        session.initialize()
        if time.monotonic() > deadline:
            raise MCPProtocolError("Timed out during initialize handshake.")
        return session.call_tool(tool, arguments)
    finally:
        _terminate(proc)


def spawn(spec: ServerSpec) -> tuple[subprocess.Popen, MCPSession]:
    """Spawn an MCP server and return the process + an initialized session.

    The caller is responsible for `proc.terminate()` (or use the context
    manager `spawn_session(spec)` for automatic teardown).
    """
    proc = _spawn(spec)
    try:
        session = MCPSession(proc.stdin, proc.stdout)
        session.initialize()
        return proc, session
    except Exception:
        _terminate(proc)
        raise


def _spawn(spec: ServerSpec) -> subprocess.Popen:
    return subprocess.Popen(
        [spec.command, *spec.args],
        cwd=spec.cwd,
        env=spec.env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,   # let server stderr land in the audit log
        bufsize=0,           # no Python-side buffering on stdin
    )


def _terminate(proc: subprocess.Popen, grace_s: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
