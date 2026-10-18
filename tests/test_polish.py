from __future__ import annotations

import io
import json
import time
import urllib.error

import pytest

from chttr import config as C
from chttr import polish as P
from chttr.config import PolishCfg


def test_context_includes_app_style_and_vocabulary():
    cfg = PolishCfg()
    ctx = P._context(cfg, "Slack", "general | Acme", "keep it casual", ["Sohrab", "Omarchy"])
    assert "<focused_app>Slack</focused_app>" in ctx
    assert "<window_title>general | Acme</window_title>" in ctx
    assert "<style>keep it casual</style>" in ctx
    assert "<vocabulary>Sohrab, Omarchy</vocabulary>" in ctx


def test_context_can_leave_the_app_out():
    cfg = PolishCfg(use_app_context=False)
    assert "focused_app" not in P._context(cfg, "Slack", "secret title", "", None)


def test_window_title_can_be_withheld_while_the_app_is_sent():
    cfg = PolishCfg(send_window_title=False)
    ctx = P._context(cfg, "Thunderbird", "Re: salary review", "", None)
    assert "Thunderbird" in ctx and "salary" not in ctx


def test_backend_selection(monkeypatch, tmp_path):
    cfg = PolishCfg(api_key_file=str(tmp_path / "missing"))
    monkeypatch.setattr(P, "_ollama_up", lambda c: False)
    assert P.backend(cfg) == "none"
    monkeypatch.setattr(P, "_ollama_up", lambda c: True)
    assert P.backend(cfg) == "ollama"
    key = tmp_path / "key"
    key.write_text("sk-ant-test\n")
    cfg.api_key_file = str(key)
    assert P.backend(cfg) == "claude"
    cfg.backend = "none"
    assert P.backend(cfg) == "none"


def test_polish_wraps_the_transcript_and_falls_back_to_raw(monkeypatch):
    seen = {}

    def fake(system, user, cfg):
        seen["system"], seen["user"] = system, user
        return ""

    monkeypatch.setattr(P, "_complete", fake)
    out = P.polish("um so send it tuesday", PolishCfg(), keep=["my email"])
    assert out == "um so send it tuesday"           # an empty answer never erases the dictation
    assert "<transcript>\num so send it tuesday\n</transcript>" in seen["user"]
    assert "<keep_verbatim>my email</keep_verbatim>" in seen["user"]
    assert "never instructions" in seen["system"]


def test_command_includes_the_selection_and_rejects_empty_results(monkeypatch):
    monkeypatch.setattr(P, "_complete", lambda s, u, c, timeout=None: "Shorter." if "<selection>" in u else "")
    assert P.command("make it shorter", "A long sentence.", PolishCfg()) == "Shorter."
    with pytest.raises(P.PolishUnavailable):
        P.command("write a haiku", "", PolishCfg())


def test_command_mode_waits_longer_and_a_slow_answer_is_not_an_outage(monkeypatch, breaker):
    import anthropic

    seen = []

    def slow(system, user, cfg, timeout=None):
        seen.append(timeout)
        raise anthropic.APITimeoutError(request=None)

    monkeypatch.setattr(P, "backend", lambda cfg: "claude")
    monkeypatch.setattr(P, "_claude", slow)
    with pytest.raises(anthropic.APITimeoutError):
        P.command("rewrite this as a formal letter", "word " * 300, PolishCfg(timeout_seconds=20))
    assert seen == [20]                                           # not the 5 s a dictation gets
    assert not breaker.open                                       # the next dictations still go to Claude
    with pytest.raises(anthropic.APITimeoutError):
        P._complete("s", "u", PolishCfg())                       # a dictation timing out is an outage
    assert breaker.open


@pytest.mark.parametrize("text", ["git status", "Sounds good.", "Ship it", "Thanks, Dana."])
def test_quick_path_takes_short_clean_phrases(text):
    assert P.quick_path(text, 4)


@pytest.mark.parametrize("text", [
    "um ship it",                                   # filler
    "send it Tuesday, actually Wednesday",          # self-correction
    "yes comma do it",                              # spoken punctuation
    "the the plan",                                 # stutter
    "insert my email",                              # snippet trigger
    "this one is clearly longer than four words",
    "",
])
def test_quick_path_sends_everything_else_to_the_model(text):
    assert not P.quick_path(text, 4)


def test_quick_path_can_be_turned_off():
    assert not P.quick_path("ok", 0)


def test_haiku_cleanup_turns_thinking_off_and_skips_fallbacks():
    params = P.claude_request("claude-haiku-5-5", "low", "SYSTEM", "USER")
    assert params["thinking"] == {"type": "disabled"}
    assert "betas" not in params and "fallbacks" not in params
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert params["output_config"] == {"effort": "low"}


def test_opus_keeps_server_side_fallbacks_and_adaptive_thinking():
    params = P.claude_request("claude-opus-5-5", "low", "SYSTEM", "USER")
    assert params["betas"] == ["server-side-fallback-2026-07-01"]
    assert params["fallbacks"] == "default"
    assert "thinking" not in params


def test_clients_are_reused(monkeypatch, tmp_path):
    import anthropic

    made = []
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: made.append(kw) or object())
    P._clients.clear()
    cfg = PolishCfg(api_key_file=str(tmp_path / "none"))
    assert P.client(cfg) is P.client(cfg)
    assert P.client(cfg, timeout=600) is not P.client(cfg)
    assert len(made) == 2


def test_no_backend_raises(monkeypatch):
    monkeypatch.setattr(P, "backend", lambda cfg: "none")
    with pytest.raises(P.PolishUnavailable):
        P._complete("s", "u", PolishCfg())


# ---- breaker -----------------------------------------------------------------

class FakeStatusError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status_code = status


@pytest.fixture
def breaker(monkeypatch):
    """A fresh breaker, and no real network from the background probe."""
    b = P.Breaker()
    monkeypatch.setattr(P, "breaker", b)
    monkeypatch.setattr(P, "_ping", lambda which, cfg: (_ for _ in ()).throw(TimeoutError("still down")))
    yield b
    b.on_recover, b._opened_at = None, None             # a sleeping probe thread ends when it wakes


def _connection_error():
    import anthropic

    return anthropic.APIConnectionError(request=None)


def test_a_connection_failure_opens_the_breaker_and_skips_the_backend(monkeypatch, breaker):
    calls = []

    def dead(system, user, cfg, timeout=None):
        calls.append(user)
        raise _connection_error()

    monkeypatch.setattr(P, "backend", lambda cfg: "claude")
    monkeypatch.setattr(P, "_claude", dead)
    breaker.cooldown = 30
    import anthropic

    with pytest.raises(anthropic.APIConnectionError):
        P._complete("s", "u", PolishCfg())
    assert breaker.open and not breaker.allow()
    t0 = time.monotonic()
    with pytest.raises(P.PolishUnavailable, match="cleanup offline"):
        P._complete("s", "u", PolishCfg())
    assert time.monotonic() - t0 < 0.05
    assert len(calls) == 1                                        # the second dictation never waited on it


def test_the_breaker_tries_again_after_the_cooldown(monkeypatch, breaker):
    now = [1000.0]
    monkeypatch.setattr(P.time, "monotonic", lambda: now[0])
    recovered = []
    breaker.on_recover = lambda: recovered.append(1)
    breaker.failed(_connection_error())
    assert not breaker.allow()
    now[0] += 29
    assert not breaker.allow()
    now[0] += 2
    assert breaker.allow() and breaker.open                       # one trial request may go
    assert not breaker.allow()                                    # ... and only one
    breaker.failed(_connection_error())                           # it failed: another cooldown
    assert not breaker.allow()
    now[0] += 31
    monkeypatch.setattr(P, "backend", lambda cfg: "claude")
    monkeypatch.setattr(P, "_claude", lambda s, u, c, timeout=None: "Cleaned.")
    assert P._complete("s", "u", PolishCfg()) == "Cleaned."
    assert not breaker.open and recovered == [1]
    breaker.succeeded()
    assert recovered == [1]                                       # once per outage


@pytest.mark.parametrize("exc", [FakeStatusError(401), FakeStatusError(400), FakeStatusError(404), ValueError("x")])
def test_client_errors_do_not_open_the_breaker(monkeypatch, breaker, exc):
    monkeypatch.setattr(P, "backend", lambda cfg: "claude")
    monkeypatch.setattr(P, "_claude", lambda s, u, c, timeout=None: (_ for _ in ()).throw(exc))
    with pytest.raises(type(exc)):
        P._complete("s", "u", PolishCfg())
    assert not breaker.open and breaker.allow()


def test_an_error_answer_during_a_trial_ends_the_outage(monkeypatch, breaker):
    # A 401 on the trial: the API is reachable again; the new problem gets its own notice.
    recovered = []
    breaker.on_recover = lambda: recovered.append(1)
    breaker.failed(_connection_error())
    breaker._ready = True
    monkeypatch.setattr(P, "backend", lambda cfg: "claude")
    monkeypatch.setattr(P, "_claude", lambda s, u, c, timeout=None: (_ for _ in ()).throw(FakeStatusError(401)))
    with pytest.raises(FakeStatusError):
        P._complete("s", "u", PolishCfg())
    assert not breaker.open and recovered == [1]


@pytest.mark.parametrize(("exc", "outage"), [
    (FakeStatusError(500), True), (FakeStatusError(529), True), (FakeStatusError(429), True),
    (FakeStatusError(401), False), (FakeStatusError(403), False),
    (TimeoutError("timed out"), True), (ConnectionRefusedError(), True),
    (urllib.error.URLError("no route"), True),
    (urllib.error.HTTPError("http://x", 503, "busy", {}, None), True),
    (urllib.error.HTTPError("http://x", 404, "no model", {}, None), False),
    (ValueError("bad json"), False),
])
def test_what_counts_as_an_outage(exc, outage):
    assert P.is_outage(exc) is outage


def test_api_connection_and_timeout_errors_are_outages():
    import anthropic

    assert P.is_outage(_connection_error())
    assert P.is_outage(anthropic.APITimeoutError(request=None))


def test_when_the_probe_gets_an_answer_the_next_request_is_a_trial(breaker):
    breaker.cooldown = 0.01
    answers = [TimeoutError("down"), None]
    recovered = []
    breaker.on_recover = lambda: recovered.append(1)
    pings = []

    def ping():
        pings.append(1)
        a = answers.pop(0) if answers else None
        if a:
            raise a

    breaker.failed(TimeoutError("down"))
    breaker.probe(ping)
    breaker.probe(ping)                                           # one probe at a time
    deadline = time.monotonic() + 2
    while not breaker._ready and time.monotonic() < deadline:
        time.sleep(0.005)
    assert breaker._ready and answers == [] and len(pings) == 2
    assert breaker.open and recovered == []                       # an answer to a ping is not a cleanup
    breaker.cooldown = 30
    assert breaker.allow()                                        # the next dictation tries the model at once
    assert not breaker.allow()                                    # (one of them)
    breaker.succeeded()
    assert not breaker.open and recovered == [1]


def test_an_overload_that_answers_the_probe_costs_one_notice(monkeypatch, breaker):
    """The API answers models.retrieve while completions keep coming back 529: the
    breaker lets a trial through after each probe, but never closes (or announces
    a recovery) until a completion works."""
    monkeypatch.setattr(P, "_ping", lambda which, cfg: None)       # the probe always gets an answer
    monkeypatch.setattr(P, "backend", lambda cfg: "claude")
    tries = []

    def overloaded(system, user, cfg, timeout=None):
        tries.append(1)
        raise FakeStatusError(529)

    monkeypatch.setattr(P, "_claude", overloaded)
    breaker.cooldown = 0.01
    notes, warned = [], [False]
    breaker.on_recover = lambda: warned.__setitem__(0, False)

    def dictate():                                                # what the daemon does with a failure
        try:
            P._complete("s", "u", PolishCfg())
        except Exception:
            if not warned[0]:
                warned[0] = True
                notes.append("Cleanup offline")

    for _ in range(6):
        dictate()
        time.sleep(0.05)                                          # a probe cycle or more
    assert len(tries) >= 3                                         # trials did go through
    assert notes.count("Cleanup offline") == 1


def test_a_closed_breaker_does_not_probe(breaker):
    called = []
    breaker.probe(lambda: called.append(1))
    time.sleep(0.05)
    assert called == []


# ---- timeouts ---------------------------------------------------------------------

def test_dictation_cleanup_uses_the_short_timeout_and_no_retries(monkeypatch):
    seen = {}

    class Messages:
        def create(self, **params):
            seen["params"] = params
            block = type("B", (), {"type": "text", "text": "Here is the cleaned text: Send it Friday."})()
            usage = type("U", (), {"input_tokens": 1, "output_tokens": 1})()
            return type("R", (), {"stop_reason": "end_turn", "content": [block], "usage": usage})()

    fake = type("C", (), {"messages": Messages()})()
    monkeypatch.setattr(P, "client", lambda cfg, timeout=None, max_retries=1: seen.update(
        timeout=timeout, retries=max_retries) or fake)
    cfg = PolishCfg(cleanup_timeout_seconds=4)
    assert P._claude("s", "send it friday", cfg) == "Send it Friday."    # the preamble is stripped
    assert seen["timeout"] == 4 and seen["retries"] == 0
    assert seen["params"]["timeout"] == 4


def test_long_dictations_get_a_little_longer():
    cfg = PolishCfg(cleanup_timeout_seconds=5)
    assert P._budget(cfg, "word " * 50) == 5
    assert P._budget(cfg, "word " * 400) == 8


def test_notes_keep_their_own_timeout(monkeypatch):
    import anthropic

    made = []
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: made.append(kw) or object())
    P._clients.clear()
    P.client(PolishCfg(api_key_file=""))
    assert made[0]["timeout"] == 20 and made[0]["max_retries"] == 1


# ---- Ollama -------------------------------------------------------------------

class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def ollama(monkeypatch):
    sent = []

    def urlopen(req, timeout=None):
        sent.append({"url": req.full_url, "body": json.loads(req.data or b"{}"), "timeout": timeout})
        return FakeResponse(json.dumps({"message": {"content": "```\nSend it Friday.\n```"}}).encode())

    monkeypatch.setattr(P.urllib.request, "urlopen", urlopen)
    return sent


def test_ollama_keeps_the_model_loaded_and_gets_the_short_prompt(ollama):
    out = P._ollama(P.SYSTEM_PROMPT, "<transcript>\nsend it friday\n</transcript>", PolishCfg(ollama_model="qwen2.5:1.5b"))
    assert out == "Send it Friday."
    body = ollama[0]["body"]
    assert ollama[0]["url"].endswith("/api/chat")
    assert body["model"] == "qwen2.5:1.5b" and body["keep_alive"] == "30m"
    assert body["options"] == {"temperature": 0.2, "num_predict": 400, "num_ctx": 2048}
    assert body["messages"][0]["content"] == P.SYSTEM_PROMPT_SMALL
    assert ollama[0]["timeout"] == 10                      # twice the cleanup budget: it runs on this CPU


def test_long_dictations_get_room_for_the_whole_answer(ollama):
    user = "<transcript>\n" + "word " * 1500 + "\n</transcript>"
    P._ollama(P.SYSTEM_PROMPT, user, PolishCfg())
    options = ollama[0]["body"]["options"]
    assert options["num_predict"] >= 3000                          # never cut at 400 tokens
    prompt_tokens = (len(P.SYSTEM_PROMPT_SMALL) + len(user)) // 3
    assert options["num_ctx"] >= prompt_tokens + options["num_predict"]
    assert options["num_ctx"] in (4096, 8192, 16384)               # grows in steps, so the model rarely reloads


def test_an_answer_ollama_cut_short_is_not_typed(monkeypatch, breaker):
    monkeypatch.setattr(P, "backend", lambda cfg: "ollama")
    monkeypatch.setattr(P.urllib.request, "urlopen", lambda req, timeout=None: FakeResponse(json.dumps(
        {"message": {"content": "Send it"}, "done_reason": "length"}).encode()))
    with pytest.raises(P.PolishUnavailable, match="stopped"):
        P._complete(P.SYSTEM_PROMPT, "u", PolishCfg())
    assert not breaker.open


def test_an_answer_claude_cut_short_is_not_typed(monkeypatch):
    class Messages:
        def create(self, **params):
            block = type("B", (), {"type": "text", "text": "Send it"})()
            return type("R", (), {"stop_reason": "max_tokens", "content": [block]})()

    monkeypatch.setattr(P, "client", lambda cfg, timeout=None, max_retries=1: type("C", (), {"messages": Messages()})())
    with pytest.raises(P.PolishUnavailable, match="stopped"):
        P._claude("s", "send it friday", PolishCfg())


def test_the_short_prompt_is_short():
    assert len(P.SYSTEM_PROMPT_SMALL.split()) < 160 < len(P.SYSTEM_PROMPT.split())


def test_command_mode_keeps_its_own_prompt_on_ollama(ollama):
    P._ollama(P.COMMAND_PROMPT, "<instruction>\nshorter\n</instruction>", PolishCfg())
    assert ollama[0]["body"]["messages"][0]["content"] == P.COMMAND_PROMPT


def test_a_slow_ollama_is_not_an_outage(monkeypatch, breaker):
    monkeypatch.setattr(P, "backend", lambda cfg: "ollama")
    monkeypatch.setattr(P.urllib.request, "urlopen", lambda req, timeout=None: (_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(P.PolishUnavailable, match=r"qwen2\.5:1\.5b"):
        P._complete(P.SYSTEM_PROMPT, "u", PolishCfg())
    assert not breaker.open


def test_ollama_down_opens_the_breaker(monkeypatch, breaker):
    monkeypatch.setattr(P, "backend", lambda cfg: "ollama")
    monkeypatch.setattr(P.urllib.request, "urlopen",
                        lambda req, timeout=None: (_ for _ in ()).throw(urllib.error.URLError(ConnectionRefusedError())))
    with pytest.raises(urllib.error.URLError):
        P._complete(P.SYSTEM_PROMPT, "u", PolishCfg())
    assert breaker.open


def test_warm_loads_the_ollama_model(monkeypatch, ollama):
    monkeypatch.setattr(P, "backend", lambda cfg: "ollama")
    P.warm(PolishCfg(ollama_model="llama3.2"))
    assert ollama[0]["url"].endswith("/api/generate")
    assert ollama[0]["body"] == {"model": "llama3.2", "prompt": "", "keep_alive": "30m",
                                 "options": {"num_ctx": 2048}}                # what dictations use: no reload


def test_warm_does_nothing_without_a_backend(monkeypatch, ollama):
    monkeypatch.setattr(P, "backend", lambda cfg: "none")
    P.warm(PolishCfg())
    assert ollama == []


def test_warm_opens_the_connection_dictation_will_use(monkeypatch):
    used = []
    fake = type("C", (), {"models": type("M", (), {"retrieve": lambda self, m: used.append(m)})()})()
    monkeypatch.setattr(P, "backend", lambda cfg: "claude")
    monkeypatch.setattr(P, "client", lambda cfg, timeout=None, max_retries=1: used.append((timeout, max_retries))
                        or fake)
    P.warm(PolishCfg())
    assert used == [(5, 0), "claude-haiku-5-5"]


# ---- strip_chatter -------------------------------------------------------------

@pytest.mark.parametrize(("model_said", "clean"), [
    ("Here is the cleaned text: Send it Friday.", "Send it Friday."),
    ("Here's the cleaned-up version:\n\nSend it Friday.", "Send it Friday."),
    ("Sure! Here you go:\n\nSend it Friday.", "Send it Friday."),
    ("Cleaned text: hi there", "hi there"),
    ('"Send it Friday."', "Send it Friday."),
    ("“Send it Friday.”", "Send it Friday."),
    ("```\ngit status\n```", "git status"),
    ("```text\ngit status\n```", "git status"),
    ("Send it.\n\nLet me know if you'd like any changes.", "Send it."),
    # what people dictate stays
    ("Here's the plan: ship Friday.", "Here's the plan: ship Friday."),
    ("Here's what I need:\n- milk\n- eggs", "Here's what I need:\n- milk\n- eggs"),
    ("Thanks.\n\nLet me know if you need anything.", "Thanks.\n\nLet me know if you need anything."),
    ('He said "yes" and "no".', 'He said "yes" and "no".'),
    ("Sure, that works.", "Sure, that works."),
    ("Here's the updated version:", ""),
    ("Here is the revised text:\nWe ship Friday.", "We ship Friday."),
])
def test_strip_chatter(model_said, clean):
    assert P.strip_chatter(model_said) == clean


@pytest.mark.parametrize(("dictated", "model_said"), [
    ("here's the updated version colon we ship friday", "Here's the updated version: we ship Friday."),
    ("here is the result colon the tests passed", "Here is the result: the tests passed."),
    ("here's what I edited colon the intro", "Here's what I edited: the intro."),
    ("sure here are the times that work for me colon bullet point monday bullet point tuesday",
     "Sure, here are the times that work for me:\n- Monday\n- Tuesday"),
    ("here is the cleaned text colon all of it", "Here is the cleaned text: all of it."),
    ("send it let me know if you'd like any changes", "Send it.\n\nLet me know if you'd like any changes."),
])
def test_strip_chatter_keeps_what_you_dictated(dictated, model_said):
    assert P.strip_chatter(model_said, f"<transcript>\n{dictated}\n</transcript>") == model_said


def test_a_one_line_preamble_needs_to_name_the_cleanup():
    # without the line break, only "cleaned/corrected/polished/transcript" marks the model's own words
    assert P.strip_chatter("Here's the updated version: we ship Friday.") == "Here's the updated version: we ship Friday."
    assert P.strip_chatter("Here's the corrected transcript: we ship Friday.") == "we ship Friday."


def test_strip_chatter_keeps_quotes_the_user_dictated():
    assert P.strip_chatter('"Hello"', "<transcript>\nopen quote hello close quote\n</transcript>") == '"Hello"'


# ---- chttr polish -----------------------------------------------------------------

def _run_polish(monkeypatch, capsys, stdin, *argv):
    from chttr import cli

    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(stdin))
    monkeypatch.setattr(cli.C, "load_config", lambda *a, **k: C.Config())
    with pytest.raises(SystemExit) as exit_:
        cli.main(["polish", *argv])
    out = capsys.readouterr()
    return exit_.value.code, out.out, out.err


def test_chttr_polish_without_a_backend_cleans_locally(monkeypatch, capsys, breaker):
    monkeypatch.setattr(P, "backend", lambda cfg: "none")
    code, out, err = _run_polish(monkeypatch, capsys, "um send it friday period\n")
    assert code == 0
    assert out == "Send it friday.\n"
    assert "cleaned locally" in err and "chttr connect claude" in err


def test_chttr_polish_local_never_calls_a_model(monkeypatch, capsys):
    monkeypatch.setattr(P, "polish", lambda *a, **k: pytest.fail("called the model"))
    code, out, err = _run_polish(monkeypatch, capsys, "the the plan comma okay", "--local")
    assert (code, out, err) == (0, "The plan, okay.\n", "")


def test_chttr_polish_falls_back_when_the_network_is_down(monkeypatch, capsys, breaker):
    monkeypatch.setattr(P, "backend", lambda cfg: "claude")
    monkeypatch.setattr(P, "_claude", lambda s, u, c, timeout=None: (_ for _ in ()).throw(_connection_error()))
    code, out, err = _run_polish(monkeypatch, capsys, "um ship it today")
    assert code == 0 and out == "Ship it today.\n" and "cleanup failed" in err


def test_a_blackholed_connection_costs_one_short_timeout(monkeypatch, breaker):
    """A real client against a server that accepts the connection and never answers
    (a VPN that drops packets): the dictation waits cleanup_timeout_seconds once, with
    no retry, and the next one doesn't wait at all."""
    import socket

    import anthropic

    hole = socket.socket()
    hole.bind(("127.0.0.1", 0))
    hole.listen(8)                                                # the kernel accepts; nobody reads
    try:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{hole.getsockname()[1]}")
        monkeypatch.setattr(P, "_clients", {})
        cfg = PolishCfg(backend="claude", cleanup_timeout_seconds=0.3)
        t0 = time.monotonic()
        with pytest.raises(anthropic.APITimeoutError):
            P.polish("um send it friday", cfg)
        assert time.monotonic() - t0 < 1.5                         # one 0.3 s timeout, no retries
        assert breaker.open
        t0 = time.monotonic()
        with pytest.raises(P.PolishUnavailable, match="offline"):
            P.polish("um and the next one", cfg)
        assert time.monotonic() - t0 < 0.05
    finally:
        hole.close()
