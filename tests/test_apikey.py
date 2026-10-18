from __future__ import annotations

import stat

import anthropic
import pytest

from chttr import apikey
from chttr import config as C

GOOD = "sk-ant-api03-" + "x" * 40


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = C.Config()
    cfg.polish.api_key_file = str(tmp_path / "conf" / "anthropic-api-key")
    return cfg


class FakeHTTPResponse:
    def __init__(self, status):
        self.status_code, self.request, self.headers = status, None, {}


def _error(cls, status):
    return cls("nope", response=FakeHTTPResponse(status), body=None)


def _offline(cls=anthropic.APIConnectionError):
    return cls(request=None)


class FakeModels:
    def __init__(self, exc=None):
        self.exc, self.asked = exc, []

    def retrieve(self, model):
        self.asked.append(model)
        if self.exc:
            raise self.exc
        return {"id": model}


def fake_client(monkeypatch, exc=None):
    models = FakeModels(exc)
    made = []

    def factory(**kw):
        made.append(kw)
        return type("Client", (), {"models": models})()

    monkeypatch.setattr(apikey, "_anthropic", factory)
    return models, made


def test_save_writes_the_key_readable_only_by_you(cfg):
    path = apikey.save(f"  {GOOD}\n", cfg)
    assert path == apikey.path(cfg)
    assert path.read_text() == GOOD + "\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_save_tightens_an_existing_open_file(cfg):
    path = apikey.path(cfg)
    path.parent.mkdir(parents=True)
    path.write_text("old")
    path.chmod(0o644)
    assert apikey.status(cfg).startswith("key file (readable by others")
    apikey.save(GOOD, cfg)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text() == GOOD + "\n"


@pytest.mark.parametrize("bad", ["", "hello", "sk-ant-short", "sk-proj-" + "x" * 40, "sk-ant-api03 " + "x" * 30])
def test_save_rejects_things_that_are_not_keys(cfg, bad):
    with pytest.raises(ValueError, match="does not look like an Anthropic API key"):
        apikey.save(bad, cfg)
    assert not apikey.path(cfg).exists()


def test_status_and_remove(cfg, monkeypatch):
    assert apikey.status(cfg) == "none"
    apikey.save(GOOD, cfg)
    assert apikey.status(cfg) == "key file (0600)"
    monkeypatch.setenv("ANTHROPIC_API_KEY", GOOD)
    assert apikey.status(cfg) == "ANTHROPIC_API_KEY in environment"
    assert apikey.remove(cfg) is True
    assert apikey.remove(cfg) is False


def test_verify_accepts_a_working_key(monkeypatch):
    models, made = fake_client(monkeypatch)
    ok, message = apikey.verify(GOOD, "claude-haiku-5-5")
    assert ok and message.startswith("key accepted (claude-haiku-5-5 reachable, ") and message.endswith(" ms)")
    assert models.asked == ["claude-haiku-5-5"]
    assert made == [{"api_key": GOOD, "max_retries": 0, "timeout": 10}]


@pytest.mark.parametrize(("exc", "ok", "words"), [
    (_error(anthropic.AuthenticationError, 401), False, "that key was rejected"),
    (_error(anthropic.PermissionDeniedError, 403), False, "permission denied"),
    (_error(anthropic.NotFoundError, 404), True, "the model claude-haiku-5-5 is not available"),
    (_offline(), True, "no network"),
    (_offline(anthropic.APITimeoutError), True, "no network"),
    (_error(anthropic.InternalServerError, 529), True, "HTTP 529"),
])
def test_verify_maps_sdk_errors(monkeypatch, exc, ok, words):
    fake_client(monkeypatch, exc)
    got_ok, message = apikey.verify(GOOD, "claude-haiku-5-5")
    assert got_ok is ok and words in message


def test_connect_interactive_checks_then_saves_and_reloads(cfg, monkeypatch, capsys):
    monkeypatch.setattr(apikey.getpass, "getpass", lambda prompt: GOOD)
    fake_client(monkeypatch)
    sent = []
    monkeypatch.setattr("chttr.client.send", lambda cmd, timeout=5.0: sent.append(cmd) or {"ok": True})
    monkeypatch.setattr(apikey, "_dictate_key", lambda cfg: "F9")
    assert apikey.connect_interactive(cfg) is True
    assert apikey.path(cfg).read_text().strip() == GOOD
    assert sent == ["reload"]
    out = capsys.readouterr().out
    assert "key accepted" in out and 'Try it: hold F9 and say "um send the contract Tuesday, no, Friday".' in out


def test_connect_interactive_keeps_a_rejected_key_out(cfg, monkeypatch, capsys):
    monkeypatch.setattr(apikey.getpass, "getpass", lambda prompt: GOOD)
    fake_client(monkeypatch, _error(anthropic.AuthenticationError, 401))
    assert apikey.connect_interactive(cfg) is False
    assert not apikey.path(cfg).exists()
    assert "that key was rejected; nothing saved" in capsys.readouterr().out


def test_connect_interactive_saves_offline_and_tolerates_no_daemon(cfg, monkeypatch, capsys):
    from chttr.client import DaemonNotRunning

    monkeypatch.setattr(apikey.getpass, "getpass", lambda prompt: GOOD)
    fake_client(monkeypatch, _offline())

    def down(cmd, timeout=5.0):
        raise DaemonNotRunning("down")

    monkeypatch.setattr("chttr.client.send", down)
    monkeypatch.setattr(apikey, "_dictate_key", lambda cfg: "Insert")
    assert apikey.connect_interactive(cfg) is True
    assert apikey.path(cfg).exists()
    assert "saved anyway" in capsys.readouterr().out


@pytest.mark.parametrize("entered", ["", "not a key"])
def test_connect_interactive_with_nothing_useful(cfg, monkeypatch, entered):
    monkeypatch.setattr(apikey.getpass, "getpass", lambda prompt: entered)
    monkeypatch.setattr(apikey, "_anthropic", lambda **kw: pytest.fail("nothing to check"))
    assert apikey.connect_interactive(cfg) is False
    assert not apikey.path(cfg).exists()


def test_connect_interactive_on_end_of_input(cfg, monkeypatch):
    def eof(prompt):
        raise EOFError

    monkeypatch.setattr(apikey.getpass, "getpass", eof)
    assert apikey.connect_interactive(cfg) is False
