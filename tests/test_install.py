from __future__ import annotations

import subprocess

import pytest

from chttr import config as C
from chttr import hotkeys as H
from chttr import install
from chttr import models as M
from chttr import polish as P
from chttr.client import DaemonNotRunning
from chttr.desktop import Desktop

KEYS = {"dictate": "Insert", "command": "Alt+Insert", "hands_free": "Super+Insert",
        "history": "Super+Shift+Insert", "meeting": "Super+Alt+Insert"}


class FakeSystem:
    """subprocess.run stand-in: records argv, answers journalctl and is-active."""

    def __init__(self, restart_code=0, active="activating"):
        self.calls: list[list[str]] = []
        self.restart_code, self.active = restart_code, active

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        out, code = "", 0
        if argv[:1] == ["journalctl"]:
            out = "\n".join(f"line {n}" for n in range(1, 21))
        elif "is-active" in argv:
            out = self.active + "\n"
        elif "restart" in argv:
            code = self.restart_code
        return subprocess.CompletedProcess(argv, code, out, "Failed to restart chttr.service: Unit not found.\n"
                                           if code else "")


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "UNIT", tmp_path / "systemd/chttr.service")
    monkeypatch.setattr(install, "HYPR", tmp_path / "hypr")
    fake = FakeSystem()
    monkeypatch.setattr(install.subprocess, "run", fake)
    sleeps = []
    monkeypatch.setattr(install.time, "sleep", sleeps.append)
    fake.sleeps = sleeps
    return fake


def scripted_send(monkeypatch, replies):
    """install.send returning (or raising) each of `replies` in turn, then the last one forever."""
    asked = []

    def send(command, timeout=5.0):
        asked.append(command)
        reply = replies[min(len(asked), len(replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(install, "send", send)
    return asked


def test_service_waits_until_the_daemon_answers(system, monkeypatch, capsys):
    asked = scripted_send(monkeypatch, [DaemonNotRunning("no"), ConnectionRefusedError(),
                                        {"ok": True, "state": "idle", "model": "loading"},
                                        {"ok": True, "state": "idle", "model": "ready"}])
    assert install.service("/bin/chttr") is True
    out = capsys.readouterr().out
    assert "daemon: starting…" in out
    assert "daemon: loading the speech model…" in out
    assert "daemon: ready (" in out and " s)" in out
    assert out.index("starting") < out.index("loading") < out.index("ready")
    assert asked == ["status"] * 4
    assert system.sleeps == [install.POLL] * 3
    assert ["systemctl", "--user", "restart", "chttr.service"] in system.calls
    assert install.UNIT.read_text().startswith("[Unit]")
    assert not any(c[0] == "journalctl" for c in system.calls)


def test_service_counts_a_daemon_without_a_model_field_as_ready(system, monkeypatch, capsys):
    scripted_send(monkeypatch, [{"ok": True, "state": "idle"}])
    assert install.service("/bin/chttr") is True


def test_service_times_out_with_the_journal_tail(system, monkeypatch, capsys):
    scripted_send(monkeypatch, [DaemonNotRunning("no")])
    assert install.service("/bin/chttr") is False
    out = capsys.readouterr().out
    assert ["journalctl", "--user", "-u", "chttr", "--no-pager", "-n", "8"] in system.calls
    assert "    | line 20" in out and "line 12" not in out
    assert out.rstrip().endswith("daemon: PROBLEM: not answering; see the log above")
    assert len(system.sleeps) == int(install.READY_TIMEOUT / install.POLL)


def test_service_stops_waiting_once_systemd_gives_up(system, monkeypatch, capsys):
    system.active = "failed"
    asked = scripted_send(monkeypatch, [DaemonNotRunning("no")])
    assert install.service("/bin/chttr") is False
    assert len(asked) == 8                          # 2 s, not 45
    assert "not answering" in capsys.readouterr().out


def test_service_reports_a_missing_model(system, monkeypatch, capsys):
    scripted_send(monkeypatch, [{"ok": True, "state": "idle", "model": "missing"}])
    assert install.service("/bin/chttr") is False
    assert "PROBLEM: running, but it has no speech model" in capsys.readouterr().out


def test_service_reports_a_failed_restart(system, monkeypatch, capsys):
    system.restart_code = 5
    scripted_send(monkeypatch, [AssertionError("never asked")])
    assert install.service("/bin/chttr") is False
    assert "PROBLEM: systemctl couldn't start it: Failed to restart chttr.service" in capsys.readouterr().out


def test_models_turns_a_failed_download_into_a_problem_line(monkeypatch, tmp_path, capsys):
    from chttr import diarize, transcriber

    def missing(configured):
        raise FileNotFoundError("no parakeet")

    def offline(spec, dest, progress=None):
        raise OSError("encoder-model.int8.onnx: download failed after 6 tries (Network is unreachable)")

    monkeypatch.setattr(transcriber, "resolve_parakeet", missing)
    monkeypatch.setattr(C, "PARAKEET_SEARCH_DIRS", [tmp_path / "parakeet"])
    monkeypatch.setattr(M, "download", offline)
    monkeypatch.setattr(diarize, "MODELS_DIR", tmp_path / "models")
    assert install.models() is False
    out = capsys.readouterr().out
    assert "  speech model: PROBLEM: encoder-model.int8.onnx: download failed" in out
    assert "  retry with: chttr setup" in out


def test_models_skips_files_that_are_there(monkeypatch, tmp_path, capsys):
    from chttr import diarize, transcriber

    monkeypatch.setattr(transcriber, "resolve_parakeet", lambda configured: tmp_path / "parakeet")
    monkeypatch.setattr(diarize, "MODELS_DIR", tmp_path / "models")
    for rel, spec in diarize.MODELS.items():
        (tmp_path / "models" / rel).parent.mkdir(parents=True, exist_ok=True)
        with (tmp_path / "models" / rel).open("wb") as fh:
            fh.truncate(spec.size)
    monkeypatch.setattr(M, "download", lambda *a, **k: pytest.fail("nothing to download"))
    assert install.models() is True
    assert "meeting models: voice activity + speaker embeddings\n" in capsys.readouterr().out


def test_card_shows_the_bound_keys_and_the_backends(monkeypatch, capsys):
    monkeypatch.setattr(P, "backend", lambda cfg: "none")
    cfg = C.Config()
    cfg.hotkeys.backend = "evdev"
    keys = install.card("/bin/chttr", cfg, Desktop("wayland", "niri"))
    assert keys == KEYS
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip().startswith(("╭", "│", "╰"))]
    assert len(lines) == 7 and len({len(line) for line in lines}) == 1          # a closed box
    assert "│ Dictate (hold, release to type)" in out and "Super+Shift+Insert" in out
    assert "Cleanup: local rules (add a key any time: chttr connect claude)" in out
    assert "Notes: on-device" in out
    assert "Check anything: chttr doctor" in out


def test_card_names_claude_when_a_key_is_set(monkeypatch, capsys):
    monkeypatch.setattr(P, "backend", lambda cfg: "claude")
    install.card("/bin/chttr", C.Config(), Desktop("wayland", "niri"))
    out = capsys.readouterr().out
    assert "Cleanup: Claude\n" in out and "Notes: Claude\n" in out


@pytest.fixture
def setup_steps(monkeypatch):
    """run() with every step faked; returns the notifications it sent."""
    sent = []
    monkeypatch.setattr(install, "models", lambda: True)
    monkeypatch.setattr(install, "keybindings", lambda exe: None)
    monkeypatch.setattr(install, "meet", lambda: None)
    monkeypatch.setattr(install, "service", lambda exe: True)
    monkeypatch.setattr(install, "offer_claude", lambda cfg: None)
    monkeypatch.setattr(install, "notify", lambda *a, **k: sent.append((a, k)))
    monkeypatch.setattr(H, "effective_bindings", lambda cfg, desk, **kw: dict(KEYS))
    monkeypatch.setattr(P, "backend", lambda cfg: "none")
    monkeypatch.setattr("chttr.desktop.detect", lambda env=None: Desktop("wayland", "hyprland"))
    return sent


def test_run_ends_ready_with_the_card_and_a_notification(setup_steps, capsys):
    assert install.run() == 0
    out = capsys.readouterr().out
    assert "╭─ Shortcuts" in out
    assert out.rstrip().endswith("Ready. Hold Insert and talk; let go to type.")
    assert setup_steps == [(("chttr is ready", "Hold Insert and talk; let go to type. "
                             "Alt+Insert: say what to do with selected text."), {"timeout_ms": 12000})]


def test_run_uses_the_real_keys(setup_steps, monkeypatch, capsys):
    monkeypatch.setattr(H, "effective_bindings", lambda cfg, desk, **kw: {"dictate": "F9"})
    assert install.run() == 0
    assert "Ready. Hold F9 and talk" in capsys.readouterr().out
    assert setup_steps[0][0][1] == "Hold F9 and talk; let go to type."


def test_run_returns_1_without_the_banner_when_a_step_failed(setup_steps, monkeypatch, capsys):
    monkeypatch.setattr(install, "service", lambda exe: False)
    assert install.run() == 1
    out = capsys.readouterr().out
    assert "╭─ Shortcuts" in out
    assert "Ready." not in out
    assert "Not quite ready: fix the PROBLEM lines above, then run chttr setup again." in out
    assert setup_steps == []


def test_run_is_not_ready_when_nothing_catches_the_keys(setup_steps, monkeypatch, capsys):
    monkeypatch.setattr(H, "effective_bindings", lambda cfg, desk, **kw: {})
    assert install.run() == 1
    assert "no keys are bound yet" in capsys.readouterr().out


def test_run_without_the_service_does_not_claim_ready(setup_steps, capsys):
    assert install.run(do_service=False) == 0
    out = capsys.readouterr().out
    assert "Ready." not in out and "systemctl --user start chttr" in out
    assert setup_steps == []


def test_offer_claude_only_at_a_terminal_with_no_backend(monkeypatch):
    from chttr import apikey

    asked = []
    monkeypatch.setattr(apikey, "connect_interactive", lambda cfg, prompt="": asked.append(prompt) or True)
    monkeypatch.setattr(P, "backend", lambda cfg: "none")
    cfg = C.Config()
    monkeypatch.setattr(install.sys.stdin, "isatty", lambda: False, raising=False)
    install.offer_claude(cfg)
    assert asked == []
    monkeypatch.setattr(install.sys.stdin, "isatty", lambda: True, raising=False)
    install.offer_claude(cfg)
    assert asked == ["  Paste an Anthropic API key for Claude cleanup and meeting notes (Enter to skip): "]
    cfg.polish.backend = "none"                     # cleanup turned off on purpose: don't ask
    install.offer_claude(cfg)
    assert len(asked) == 1


def test_notify_prefers_omarchy(monkeypatch):
    calls = []
    monkeypatch.setattr(install.subprocess, "run", lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(install.shutil, "which", lambda name: "/usr/bin/" + name)
    install.notify("chttr is ready", "Hold Insert", timeout_ms=12000)
    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    install.notify("chttr is ready", "Hold Insert", timeout_ms=12000)
    assert calls == [
        ["omarchy-notification-send", "--app-name", "chttr", "-g", "\U000f036c", "-u", "normal", "-t", "12000",
         "chttr is ready", "Hold Insert"],
        ["notify-send", "-a", "chttr", "-u", "normal", "-t", "12000", "chttr is ready", "Hold Insert"],
    ]


# ---- bin/setup and install.sh -------------------------------------------------------------

ROOT = install.Path(__file__).resolve().parents[1]
PY_SNIPPET = [
    """py="${CHTTR_PYTHON:-$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)}\"""",
    "[[ $py =~ ^3\\.([0-9]+)(\\.[0-9]+)?$ ]] && ((BASH_REMATCH[1] >= 12)) || py=3.13",
]


def _stubs(tmp_path, version: str | None):
    """A PATH directory with fake python3/uv that log their arguments, and a fake HOME."""
    import os

    bin_ = tmp_path / "stubs"
    bin_.mkdir()
    log = tmp_path / "log"
    if version is not None:
        (bin_ / "python3").write_text(
            "#!/bin/bash\n"
            f'if [[ $* == *print* ]]; then echo {version}; exit 0; fi\n'
            f'[[ $1 == -I ]] && echo "python3 $*" >> {log}\n'
            "exit 0\n")
    (bin_ / "uv").write_text(f'#!/bin/bash\necho "uv $*" >> {log}\n')
    home = tmp_path / "home"
    (home / ".local/bin").mkdir(parents=True)
    (home / ".local/bin/chttr").write_text(f'#!/bin/bash\necho "chttr $*" >> {log}\n')
    for f in [*bin_.iterdir(), home / ".local/bin/chttr"]:
        f.chmod(0o755)
    path = f"{bin_}:/usr/bin:/bin" if version is not None else str(bin_)     # None: no python3 at all
    env = {"PATH": path, "HOME": str(home), "XDG_DATA_HOME": str(home / "data"),
           "XDG_CONFIG_HOME": str(home / "config"), "LANG": os.environ.get("LANG", "C.UTF-8")}
    return env, log


@pytest.mark.parametrize("script", ["bin/setup", "install.sh"])
def test_installer_scripts_parse_and_share_the_python_choice(script):
    subprocess.run(["bash", "-n", str(ROOT / script)], check=True)
    text = (ROOT / script).read_text()
    for line in PY_SNIPPET:
        assert line in text
    assert "CHTTR_PYTHON=3.13" not in text and "3.14}" not in text


@pytest.mark.parametrize(("system", "override", "expected"), [
    ("3.14", "", "3.14"), ("3.12", "", "3.12"), ("3.11", "", "3.13"), (None, "", "3.13"),
    ("3.14", "3.13", "3.13"), ("3.14", "3.10", "3.13"),
])
def test_python_choice(tmp_path, system, override, expected):
    env, _ = _stubs(tmp_path, system)
    if override:
        env["CHTTR_PYTHON"] = override
    out = subprocess.run(["/bin/bash", "-c", "\n".join([*PY_SNIPPET, 'echo "$py"'])], env=env, capture_output=True,
                         text=True, check=True).stdout.strip()
    assert out == expected


def test_bin_setup_downloads_the_model_while_uv_installs(tmp_path):
    env, log = _stubs(tmp_path, "3.14")
    subprocess.run(["bash", str(ROOT / "bin/setup"), "--no-meet"], env=env, check=True, capture_output=True)
    lines = log.read_text().splitlines()
    data = env["XDG_DATA_HOME"]
    assert f"python3 -I {ROOT}/src/chttr/models.py {data}/chttr/models/parakeet-tdt-0.6b-v3-int8 " \
           f"{data}/chttr/models" in lines
    assert any(line.startswith("uv tool install --python 3.14 --editable") for line in lines)
    assert lines[-1] == "chttr setup --no-meet"


def test_bin_setup_skips_the_download_when_the_model_is_there(tmp_path):
    env, log = _stubs(tmp_path, "3.14")
    model = install.Path(env["HOME"]) / ".local/share/voxtype/models/parakeet-tdt-0.6b-v3-int8"
    model.mkdir(parents=True)
    for name in ("encoder-model.int8.onnx", "decoder_joint-model.int8.onnx", "vocab.txt"):
        (model / name).write_text("x")
    subprocess.run(["bash", str(ROOT / "bin/setup")], env=env, check=True, capture_output=True)
    assert not any(line.startswith("python3 -I") for line in log.read_text().splitlines())


def test_bin_setup_stops_when_the_download_fails(tmp_path):
    env, log = _stubs(tmp_path, "3.14")
    stub = install.Path(env["PATH"].split(":")[0]) / "python3"
    stub.write_text(stub.read_text().replace("exit 0\n", "[[ $1 == -I ]] && exit 1\nexit 0\n"))
    done = subprocess.run(["bash", str(ROOT / "bin/setup")], env=env, capture_output=True, text=True)
    assert done.returncode == 1
    assert "continue where it stopped" in done.stdout
    assert not any(line.startswith("chttr setup") for line in log.read_text().splitlines())
