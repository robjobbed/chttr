from __future__ import annotations

import io
import json
import urllib.error

import pytest

from chttr import notes as N
from chttr.config import PolishCfg


def test_builtin_styles_all_parse_with_sections():
    styles = N.load_styles()
    assert len(styles) >= 18
    assert "general" in styles
    for s in styles.values():
        assert s.sections, s.id
        assert len({sec.key for sec in s.sections}) == len(s.sections), f"{s.id}: duplicate section keys"
        for sec in s.sections:
            assert sec.kind in ("bullets", "paragraph", "quotes"), (s.id, sec.title)


@pytest.mark.parametrize("style_id", sorted(N.load_styles()))
def test_schema_requires_every_section_of_the_style(style_id):
    style = N.load_styles()[style_id]
    schema = N._schema(style)
    sections = schema["properties"]["sections"]
    assert sections["required"] == [s.key for s in style.sections]
    assert sections["additionalProperties"] is False
    assert set(schema["required"]) == {"title", "sections", "action_items", "speakers"}


def test_custom_style_schema_lets_the_model_choose_sections():
    schema = N._schema(N.custom_style("focus on risks"))
    assert schema["properties"]["sections"]["type"] == "array"


def test_style_prompt_lists_sections_and_extra_request():
    style = N.load_styles()["bant"]
    prompt = N._style_prompt(style, "mention pricing twice")
    assert "Budget" in prompt and "Timeline" in prompt
    assert "<additional_request>\nmention pricing twice" in prompt


def test_user_styles_override_builtins(tmp_path, monkeypatch):
    user = tmp_path / "note-styles.toml"
    user.write_text('[[style]]\nid = "general"\nname = "Mine"\nsections = [{ title = "Only" }]\n'
                    '[[style]]\nid = "broken"\n')
    monkeypatch.setattr(N, "USER_STYLES", user)
    styles = N.load_styles()
    assert styles["general"].name == "Mine" and not styles["general"].builtin
    assert "broken" not in styles


def test_write_notes_maps_sections_back_in_order(monkeypatch):
    style = N.load_styles()["general"]
    raw = {"title": "T", "sections": {s.key: [f"item for {s.title}"] for s in reversed(style.sections)},
           "action_items": [{"owner": "Rob", "task": "x", "due": ""}], "speakers": []}
    captured = {}

    def fake_call(cfg, effort, schema, content, **kw):
        captured["content"] = content
        return raw

    monkeypatch.setattr(N, "_call", fake_call)
    out = N.write_notes(None, "medium", style, "meeting", "invited", "speakers", ["Speaker 1"], "transcript")
    assert [s["title"] for s in out["sections"]] == [s.title for s in style.sections]
    assert out["action_items"][0]["owner"] == "Rob"
    assert captured["content"][0]["cache_control"] == {"type": "ephemeral"}     # the transcript block is cached
    assert "<unidentified>Speaker 1</unidentified>" in captured["content"][1]["text"]


# ---- Ollama -----------------------------------------------------------------

class Reply(io.BytesIO):
    status = 200


def fake_ollama(monkeypatch, replies: list[dict]):
    """urlopen that records each request body and answers with the next reply's JSON."""
    sent: list[dict] = []

    def urlopen(req, timeout=None):
        sent.append({"url": req.full_url, "timeout": timeout, **json.loads(req.data)})
        content = replies.pop(0)
        text = content if isinstance(content, str) else json.dumps(content)
        return Reply(json.dumps({"message": {"role": "assistant", "content": text}, "done_reason": "stop",
                                 "prompt_eval_count": 100, "eval_count": 20}).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return sent


def ollama_cfg() -> PolishCfg:
    return PolishCfg(backend="ollama", ollama_model="qwen2.5:7b", ollama_url="http://127.0.0.1:11434/")


def style_reply(style: N.Style) -> dict:
    return {"title": "Launch sync", "sections": {s.key: [f"about {s.title}"] for s in reversed(style.sections)},
            "action_items": [{"owner": "Dana", "task": "Send the deck", "due": "Friday"}], "speakers": []}


def test_ollama_notes_send_the_schema_as_format_and_keep_section_order(monkeypatch):
    style = N.load_styles()["general"]
    sent = fake_ollama(monkeypatch, [style_reply(style)])
    out = N.write_notes(ollama_cfg(), "medium", style, "Calendar title: Launch", "", "- Dana: named by Meet's captions",
                        [], "[00:00:04] Dana: We ship on Friday.")
    assert [s["title"] for s in out["sections"]] == [s.title for s in style.sections]
    assert out["sections"][0]["items"] == [f"about {style.sections[0].title}"]
    assert out["action_items"][0]["owner"] == "Dana" and "chunked" not in out
    body = sent[0]
    assert body["url"] == "http://127.0.0.1:11434/api/chat" and body["timeout"] == N.OLLAMA_TIMEOUT
    assert body["model"] == "qwen2.5:7b" and body["stream"] is False and body["keep_alive"] == "30m"
    assert body["format"] == N._schema(style)
    assert body["options"] == {"temperature": 0.2, "num_ctx": 32768}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["messages"][0]["content"] == N.SYSTEM_PROMPT
    assert "[00:00:04] Dana: We ship on Friday." in body["messages"][1]["content"]
    assert "Sections, in order:" in body["messages"][1]["content"]     # the style prompt follows the transcript


def test_ollama_reply_in_the_wrong_shape_is_retried_once(monkeypatch):
    style = N.load_styles()["general"]
    sent = fake_ollama(monkeypatch, ["{not json", style_reply(style)])
    out = N.write_notes(ollama_cfg(), "medium", style, "m", "", "", [], "[00:00:01] Rob: hi")
    assert out["title"] == "Launch sync" and len(sent) == 2
    assert sent[1]["messages"][-1] == {"role": "user", "content": "Return valid JSON matching the schema."}
    assert sent[1]["messages"][-2] == {"role": "assistant", "content": "{not json"}

    fake_ollama(monkeypatch, [{"title": "T"}, {"title": "T", "sections": {}}])     # missing keys, twice
    with pytest.raises(RuntimeError, match="expected format"):
        N.write_notes(ollama_cfg(), "medium", style, "m", "", "", [], "[00:00:01] Rob: hi")


def test_ollama_without_the_model_says_how_to_get_it(monkeypatch):
    def urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {},  # pyright: ignore[reportArgumentType]
                                     io.BytesIO(b'{"error":"model \\"qwen2.5:7b\\" not found"}'))

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(RuntimeError, match=r"ollama pull qwen2\.5:7b"):
        N.pick_style(ollama_cfg(), N.load_styles(), "Weekly", "", "[00:00:01] Rob: hi")


def test_pick_style_works_through_ollama(monkeypatch):
    sent = fake_ollama(monkeypatch, [{"style": "standup"}])
    styles = N.load_styles()
    assert N.pick_style(ollama_cfg(), styles, "Daily", "", "[00:00:01] Rob: yesterday I") == "standup"
    assert sent[0]["format"]["properties"]["style"]["enum"] == list(styles)


def test_long_transcripts_on_ollama_are_read_in_parts(monkeypatch):
    style = N.load_styles()["general"]
    lines = [f"[{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}] Dana: " + f"point number {t} " * 10
             for t in range(0, 1800, 10)]                                  # 30 minutes, ~30 000 characters
    transcript = "\n".join(lines)
    assert len(transcript) > N.CHUNK_CHARS
    sent = fake_ollama(monkeypatch, [{"facts": ["[00:01:00] Dana: budget is $50k"]},
                                     {"facts": ["[00:20:00] Dana: launch moves to May 3"]}, style_reply(style)])
    out = N.write_notes(ollama_cfg(), "medium", style, "m", "", "- Dana", [], transcript)
    assert len(sent) == 3
    assert sent[0]["format"] == sent[1]["format"] == N.FACTS
    assert sent[2]["format"] == N._schema(style)
    assert "[00:00:00] Dana" in sent[0]["messages"][1]["content"] and "[00:15:00]" not in sent[0]["messages"][1]["content"]
    assert "[00:15:00] Dana" in sent[1]["messages"][1]["content"]
    final = sent[2]["messages"][1]["content"]
    assert "Part 1 (00:00:00 to 00:14:50):\n- [00:01:00] Dana: budget is $50k" in final
    assert "Part 2 (00:15:00 to 00:29:50):\n- [00:20:00] Dana: launch moves to May 3" in final
    assert "point number" not in final
    assert out["chunked"] is True


def test_claude_reads_long_transcripts_whole(monkeypatch):
    style = N.load_styles()["general"]
    calls = []
    monkeypatch.setattr(N, "_call", lambda cfg, effort, schema, content, **kw: calls.append(schema) or style_reply(style))
    monkeypatch.setattr(N.P, "backend", lambda cfg: "claude")
    N.write_notes(PolishCfg(), "medium", style, "m", "", "", [], "[00:00:00] Dana: x\n" * 3000)
    assert calls == [N._schema(style)]


@pytest.mark.parametrize(("backend", "key", "expected"), [
    ("claude", True, True), ("claude", False, False), ("ollama", False, True), ("none", True, False),
])
def test_notes_are_available_with_claude_or_ollama(monkeypatch, backend, key, expected):
    monkeypatch.setattr(N.P, "backend", lambda cfg: backend)
    monkeypatch.setattr(N.P, "_claude_configured", lambda cfg: key)
    assert N.available(PolishCfg()) is expected


def test_explicit_backend_is_honoured(monkeypatch):
    monkeypatch.setattr(N.P, "_claude_configured", lambda cfg: True)
    monkeypatch.setattr(N.P, "_ollama_up", lambda cfg: False)
    assert N.available(PolishCfg(backend="ollama"))           # chosen in the config: no probe
    assert not N.available(PolishCfg(backend="none"))
    assert N.available(PolishCfg(backend="auto"))
