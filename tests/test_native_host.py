from __future__ import annotations

import io
import json
import struct
import sys

from chttr import native_host as NH


def frame(obj: dict) -> bytes:
    data = json.dumps(obj).encode()
    return struct.pack("<I", len(data)) + data


def unframe(buf: bytes) -> list[dict]:
    out = []
    while buf:
        (n,) = struct.unpack("<I", buf[:4])
        out.append(json.loads(buf[4:4 + n]))
        buf = buf[4 + n:]
    return out


def test_extension_id_is_derived_from_the_manifest_key():
    ext = NH.extension_id()
    assert len(ext) == 32 and set(ext) <= set("abcdefghijklmnop")
    assert ext == NH.extension_id()


def test_native_host_relays_messages_and_echoes_the_tab(monkeypatch):
    sent = []

    def fake_send(command, timeout=5.0):
        sent.append(command)
        return {"ok": True, "recording": True}

    monkeypatch.setattr(NH, "send", fake_send)
    stdin = io.BytesIO(frame({"type": "joined", "code": "abc-defg-hij", "tab": 7}) + frame({"type": "heartbeat"}))
    stdout = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": stdin})())
    monkeypatch.setattr(sys, "stdout", type("S", (), {"buffer": stdout})())
    assert NH.run() == 0
    assert sent[0].startswith("meet {") and '"code":"abc-defg-hij"' in sent[0]
    replies = unframe(stdout.getvalue())
    assert replies[0] == {"ok": True, "recording": True, "tab": 7}
    assert "tab" not in replies[1]


def test_native_host_survives_a_missing_daemon(monkeypatch):
    def down(command, timeout=5.0):
        raise NH.DaemonNotRunning("not running")

    monkeypatch.setattr(NH, "send", down)
    stdin = io.BytesIO(frame({"type": "heartbeat", "tab": 1}))
    stdout = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": stdin})())
    monkeypatch.setattr(sys, "stdout", type("S", (), {"buffer": stdout})())
    NH.run()
    assert unframe(stdout.getvalue())[0]["ok"] is False


def test_load_extension_flag_is_added_once_and_replaces_old_copies(tmp_path):
    flags = tmp_path / "chromium-flags.conf"
    flags.write_text("--ozone-platform=wayland\n--load-extension=/opt/other\n")
    ext = str(NH.EXTENSION_DIR)
    assert NH._add_load_extension(flags, ext).startswith("added")
    assert NH._add_load_extension(flags, ext) == "already loaded"
    text = flags.read_text(encoding="utf-8")
    assert f"--load-extension=/opt/other,{ext}" in text
    assert (tmp_path / "chromium-flags.conf.bak-chttr").exists()


def test_add_flag_adds_a_bare_flag_once_and_keeps_the_others(tmp_path):
    flags = tmp_path / "chromium-flags.conf"
    flags.write_text("--ozone-platform=wayland\n--load-extension=/opt/other\n")
    assert NH._add_flag(flags, "--force-renderer-accessibility").startswith("added")
    assert NH._add_flag(flags, "--force-renderer-accessibility") == "already set"
    assert flags.read_text() == "--ozone-platform=wayland\n--load-extension=/opt/other\n--force-renderer-accessibility\n"
    assert (tmp_path / "chromium-flags.conf.bak-chttr").read_text() == "--ozone-platform=wayland\n--load-extension=/opt/other\n"


def test_add_flag_creates_a_missing_flags_file(tmp_path):
    flags = tmp_path / "chrome-flags.conf"
    assert NH._add_flag(flags, "--load-extension", "/x/extension").startswith("added")
    assert flags.read_text() == "--load-extension=/x/extension\n"
    assert not (tmp_path / "chrome-flags.conf.bak-chttr").exists()       # nothing to back up


def test_ensure_accessibility_flag_only_touches_browsers_that_have_a_profile(tmp_path, monkeypatch):
    used, unused = tmp_path / "chromium", tmp_path / "brave"
    used.mkdir()
    monkeypatch.setattr(NH, "BROWSER_DIRS", [(used, tmp_path / "chromium-flags.conf"), (unused, tmp_path / "brave-flags.conf")])
    assert NH.missing_accessibility_flag() == [tmp_path / "chromium-flags.conf"]
    done = NH.ensure_accessibility_flag()
    assert len(done) == 1 and "restart chromium" in done[0]
    assert (tmp_path / "chromium-flags.conf").read_text() == "--force-renderer-accessibility\n"
    assert not (tmp_path / "brave-flags.conf").exists()
    assert NH.ensure_accessibility_flag() == [] and NH.missing_accessibility_flag() == []


def test_uninstall_takes_out_only_chttr(tmp_path, monkeypatch):
    profile, flags = tmp_path / "chromium", tmp_path / "chromium-flags.conf"
    profile.mkdir()
    original = "--ozone-platform=wayland\n--load-extension=/opt/other\n"
    flags.write_text(original)
    monkeypatch.setattr(NH, "BROWSERS", [(("chromium",), profile, flags)])
    monkeypatch.setattr(NH, "BROWSER_DIRS", [(profile, flags)])
    monkeypatch.setattr(NH.C, "DATA_DIR", tmp_path / "data")
    NH.install()
    assert (profile / "NativeMessagingHosts" / f"{NH.HOST_NAME}.json").exists()
    done = NH.uninstall()
    assert not (profile / "NativeMessagingHosts" / f"{NH.HOST_NAME}.json").exists()
    assert flags.read_text() == original and not (tmp_path / "chromium-flags.conf.bak-chttr").exists()
    assert not (tmp_path / "data" / "native-host").exists()
    assert any("native host removed" in line for line in done)
    assert NH.uninstall() == []                                           # nothing left to do


def test_uninstall_deletes_a_flags_file_chttr_created(tmp_path, monkeypatch):
    profile, flags = tmp_path / "chromium", tmp_path / "chromium-flags.conf"
    profile.mkdir()
    monkeypatch.setattr(NH, "BROWSERS", [(("chromium",), profile, flags)])
    monkeypatch.setattr(NH, "BROWSER_DIRS", [(profile, flags)])
    monkeypatch.setattr(NH.C, "DATA_DIR", tmp_path / "data")
    NH.install()
    assert flags.exists()
    NH.uninstall()
    assert not flags.exists()


def test_uninstall_keeps_flags_added_since(tmp_path, monkeypatch):
    profile, flags = tmp_path / "chromium", tmp_path / "chromium-flags.conf"
    profile.mkdir()
    flags.write_text(f"--load-extension=/opt/other,{NH.EXTENSION_DIR}\n--enable-features=X\n")
    monkeypatch.setattr(NH, "BROWSERS", [(("chromium",), profile, flags)])
    NH.uninstall()
    assert flags.read_text() == "--load-extension=/opt/other\n--enable-features=X\n"
