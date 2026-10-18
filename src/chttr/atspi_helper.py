#!/usr/bin/python3
"""Read the focused text field through the accessibility bus (AT-SPI).

Runs under the *system* Python (needs PyGObject with Atspi 2.0, which most
desktops ship), like overlay.py. Prints one JSON object and exits:

    {"app": "...", "before": "...", "after": "...", "caret": 123, "count": 456}

or {} when nothing focused is a readable text field. Never writes anything.
With --probe it only says whether screen context can work here (for `chttr doctor`):

    {"gi": true, "bus": true, "apps": 7}

    atspi_helper.py [--timeout SECONDS] [--before N] [--after N] [--probe]
"""

import argparse
import json
import sys
import time


def probe() -> dict:
    """Is PyGObject's Atspi here, does the accessibility bus answer, and how many
    apps are on it. Never raises."""
    out = {"gi": False, "bus": False, "apps": 0}
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        gi.require_version("Gio", "2.0")
        from gi.repository import Atspi, Gio, GLib
    except (ImportError, ValueError):
        return out
    out["gi"] = True
    try:
        # The session bus hands out the accessibility bus's address (starting
        # at-spi-bus-launcher if needed); no address, no screen context.
        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = conn.call_sync("org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress", None,
                               GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NONE, 1500, None)
        out["bus"] = bool(reply.unpack()[0])
    except Exception:  # noqa: BLE001 - no session bus, or no at-spi2-core
        return out
    try:
        out["apps"] = int(Atspi.get_desktop(0).get_child_count())
    except Exception:  # noqa: BLE001
        pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=0.6)
    ap.add_argument("--before", type=int, default=600)
    ap.add_argument("--after", type=int, default=200)
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args()
    if a.probe:
        try:
            print(json.dumps(probe()))
        except Exception:  # noqa: BLE001 - --probe must always answer
            print(json.dumps({"gi": False, "bus": False, "apps": 0}))
        return 0
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except (ImportError, ValueError):
        print("{}")
        return 0

    deadline = time.monotonic() + a.timeout
    S = Atspi.StateType

    def states(obj):
        try:
            return obj.get_state_set()
        except Exception:  # noqa: BLE001 - the app may vanish mid-walk
            return None

    def find(obj, depth):
        if obj is None or depth > 40 or time.monotonic() > deadline:
            return None
        st = states(obj)
        if st is None:
            return None
        if st.contains(S.FOCUSED) and (st.contains(S.EDITABLE) or st.contains(S.MULTI_LINE)):
            return obj
        try:
            n = min(obj.get_child_count(), 400)
        except Exception:  # noqa: BLE001
            return None
        for i in range(n):
            try:
                child = obj.get_child_at_index(i)
            except Exception:  # noqa: BLE001
                continue
            cst = states(child)
            if cst is None or not (cst.contains(S.SHOWING) or cst.contains(S.FOCUSED)):
                continue
            hit = find(child, depth + 1)
            if hit is not None:
                return hit
        return None

    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue
        try:
            windows = [app.get_child_at_index(j) for j in range(app.get_child_count())]
        except Exception:  # noqa: BLE001
            continue
        for win in windows:
            st = states(win)
            if st is None or not st.contains(S.ACTIVE):
                continue
            field = find(win, 0)
            if field is None:
                continue
            try:
                count = field.get_character_count()
                caret = max(0, min(field.get_caret_offset(), count))
                before = field.get_text(max(0, caret - a.before), caret)
                after = field.get_text(caret, min(count, caret + a.after))
            except Exception:  # noqa: BLE001 - not a text field after all
                continue
            print(json.dumps({"app": app.get_name() or "", "before": before, "after": after,
                              "caret": caret, "count": count}))
            return 0
    print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
