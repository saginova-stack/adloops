from __future__ import annotations

import io
import json

import pytest

from scripts.mcp_runner import (
    MCPProtocolError,
    MCPSession,
    MCPToolError,
)


class FakeStream:
    """Synchronous bidirectional MCP transport for tests.

    Outgoing writes go to `outbox`; responses are pre-queued by the test
    in `inbox` (list of dicts). Each `readline` pops one queued frame.
    """

    def __init__(self, inbox: list[dict] | None = None):
        self.outbox: list[dict] = []
        self.inbox = inbox or []
        # The dual-stream API of MCPSession wants two file-likes. We expose
        # a single object that satisfies the .write/.flush/.readline shape
        # for both directions because tests don't care about separation.

    # -- stdin (we write to it) --
    def write(self, b: bytes) -> int:
        s = b.decode("utf-8")
        for line in s.splitlines():
            if line.strip():
                self.outbox.append(json.loads(line))
        return len(b)

    def flush(self) -> None:
        pass

    # -- stdout (we read from it) --
    def readline(self) -> bytes:
        if not self.inbox:
            return b""  # signals closed stream
        frame = self.inbox.pop(0)
        return (json.dumps(frame) + "\n").encode("utf-8")


def make_session(inbox: list[dict]) -> tuple[MCPSession, FakeStream]:
    stream = FakeStream(inbox)
    sess = MCPSession(stdin=stream, stdout=stream)
    return sess, stream


# ---- initialize handshake -----------------------------------------------

def test_initialize_sends_handshake_and_initialized_notification():
    sess, stream = make_session([
        {"jsonrpc": "2.0", "id": 1, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "serverInfo": {"name": "fake", "version": "1.0"},
        }},
    ])
    sess.initialize()
    methods = [m["method"] for m in stream.outbox]
    assert methods == ["initialize", "notifications/initialized"]
    # Notifications have no id; requests do.
    assert "id" in stream.outbox[0]
    assert "id" not in stream.outbox[1]


def test_initialize_is_idempotent():
    sess, stream = make_session([
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}},
    ])
    sess.initialize()
    sess.initialize()  # should not send a second handshake
    methods = [m["method"] for m in stream.outbox]
    assert methods.count("initialize") == 1


def test_initialize_rejects_missing_protocol_version():
    sess, _ = make_session([
        {"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}},  # no protocolVersion
    ])
    with pytest.raises(MCPProtocolError, match="protocolVersion"):
        sess.initialize()


# ---- tool calls ----------------------------------------------------------

def test_call_tool_returns_parsed_json_text():
    sess, stream = make_session([
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "id": 2, "result": {
            "content": [{"type": "text", "text": '{"status": "ok", "id": "42"}'}],
            "isError": False,
        }},
    ])
    sess.initialize()
    result = sess.call_tool("pause_entity", {"entity_id": "42"})
    assert result == {"status": "ok", "id": "42"}
    # confirm the call payload looks right
    call = stream.outbox[-1]
    assert call["method"] == "tools/call"
    assert call["params"] == {"name": "pause_entity", "arguments": {"entity_id": "42"}}


def test_call_tool_returns_plain_text_when_not_json():
    sess, _ = make_session([
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "id": 2, "result": {
            "content": [{"type": "text", "text": "OK"}],
            "isError": False,
        }},
    ])
    sess.initialize()
    assert sess.call_tool("ping") == "OK"


def test_call_tool_raises_on_is_error():
    sess, _ = make_session([
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "id": 2, "result": {
            "content": [{"type": "text", "text": "campaign 42 not found"}],
            "isError": True,
        }},
    ])
    sess.initialize()
    with pytest.raises(MCPToolError, match="not found"):
        sess.call_tool("pause_entity", {"entity_id": "42"})


def test_call_tool_raises_on_jsonrpc_error():
    sess, _ = make_session([
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "id": 2, "error": {"code": -32601, "message": "Method not found"}},
    ])
    sess.initialize()
    with pytest.raises(MCPToolError, match="Method not found"):
        sess.call_tool("nope")


def test_call_tool_requires_initialize():
    sess, _ = make_session([])
    with pytest.raises(MCPProtocolError, match="initialize"):
        sess.call_tool("anything")


def test_session_skips_unsolicited_notifications_from_server():
    # Real MCP servers can emit progress notifications mid-flight. The
    # session must ignore them while waiting for the matching response.
    sess, _ = make_session([
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 0.5}},
        {"jsonrpc": "2.0", "id": 2, "result": {
            "content": [{"type": "text", "text": "done"}],
            "isError": False,
        }},
    ])
    sess.initialize()
    assert sess.call_tool("slow_thing") == "done"


def test_session_raises_when_stream_closes_mid_request():
    sess, _ = make_session([
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}},
        # no response for the call → readline returns b"" → MCPProtocolError
    ])
    sess.initialize()
    with pytest.raises(MCPProtocolError, match="closed"):
        sess.call_tool("anything")


def test_session_rejects_response_with_wrong_id():
    sess, _ = make_session([
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "id": 99, "result": {"content": []}},  # mismatched id
    ])
    sess.initialize()
    with pytest.raises(MCPProtocolError, match="id"):
        sess.call_tool("anything")
