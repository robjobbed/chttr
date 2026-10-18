from __future__ import annotations

import io
import json

from chttr import ask as A
from chttr import mcp_server as M
from tests.test_library import lib  # noqa: F401 - the fixture


def tools():
    return {fn.__name__: fn for fn in A.tool_functions()}


def test_tools_search_read_and_notes(lib):  # noqa: F811
    t = tools()
    hits = json.loads(t["search_meetings"]("contract"))
    assert hits[0]["meeting"] == "Pricing review" and hits[0]["at"] == "00:00:40"
    lines = json.loads(t["read_transcript"](hits[0]["meeting_id"], around="00:00:40", seconds=60))
    assert [x["speaker"] for x in lines] == ["Dana", "Ali"]
    notes = json.loads(t["get_notes"]("2026-10-01-140000"))
    assert notes["notes"]["action_items"][0]["owner"] == "Ali"
    assert json.loads(t["get_notes"]("nope"))["error"]
    assert [m["title"] for m in json.loads(t["list_meetings"](after="2026-10-05"))] == ["Launch sync"]
    assert json.loads(t["list_action_items"](owner="ali"))[0]["due"] == "Friday"


def test_read_transcript_reads_mm_ss_as_minutes_and_seconds(lib, monkeypatch):  # noqa: F811
    seen = []
    monkeypatch.setattr(A.L, "context_around", lambda mid, at, seconds: seen.append(at) or [])
    t = tools()
    t["read_transcript"]("2026-10-01-140000", around="12:40")
    t["read_transcript"]("2026-10-01-140000", around="01:02:03")
    t["read_transcript"]("2026-10-01-140000", around="40")
    assert seen == [12 * 60 + 40, 3723, 40]
    assert "error" in json.loads(t["read_transcript"]("2026-10-01-140000", around="soon"))


def rpc(lines: list[dict]) -> list[dict]:
    out = io.StringIO()
    M.run(io.StringIO("\n".join(json.dumps(x) for x in lines) + "\nnot json\n"), out)
    return [json.loads(x) for x in out.getvalue().splitlines()]


def test_mcp_handshake_list_and_call(lib):  # noqa: F811
    replies = rpc([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "search_meetings", "arguments": {"query": "launch"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "resources/list"},
    ])
    by_id = {r.get("id"): r for r in replies}
    assert by_id[1]["result"]["protocolVersion"] == "2025-06-18"
    assert by_id[1]["result"]["serverInfo"]["name"] == "chttr"
    names = {t["name"] for t in by_id[2]["result"]["tools"]}
    assert names == {"search_meetings", "read_transcript", "get_notes", "list_meetings", "list_action_items"}
    search = next(t for t in by_id[2]["result"]["tools"] if t["name"] == "search_meetings")
    assert search["inputSchema"]["required"] == ["query"]
    assert "YYYY-MM-DD" in search["inputSchema"]["properties"]["after"]["description"]
    assert "Launch sync" in by_id[3]["result"]["content"][0]["text"]
    assert by_id[4]["result"]["isError"] is True
    assert by_id[5]["error"]["code"] == -32601
    assert by_id[None]["error"]["code"] == -32700          # the "not json" line
    assert len(replies) == 6                                 # no reply to the notification


def test_ask_runs_the_tool_runner(lib, monkeypatch):  # noqa: F811
    from chttr import polish
    from chttr.config import Config

    seen = {}

    class Block:
        type = "text"
        text = "Ali committed to sending the contract by Friday [Pricing review, 2026-10-01, 00:00:40]."

    class Msg:
        stop_reason = "end_turn"
        content = (Block(),)

    class Runner:
        def __init__(self, **params):
            seen.update(params)
            # call a tool the way the SDK would, through the decorated wrapper
            search = next(t for t in params["tools"] if t.name == "search_meetings")
            seen["tool_result"] = search.call({"query": "contract"})

        def __iter__(self):
            return iter([Msg()])

    class Client:
        class beta:
            class messages:
                tool_runner = Runner

    monkeypatch.setattr(polish, "client", lambda *a, **k: Client())
    calls = []
    answer = A.ask("what did Ali commit to?", Config(), on_search=lambda name, args: calls.append(name))
    assert "Friday" in answer
    assert seen["model"] == "claude-opus-5-5" and seen["fallbacks"] == "default"
    assert "Pricing review" in str(seen["tool_result"])
    assert calls == ["search_meetings"]
