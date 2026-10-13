from __future__ import annotations

import csv
import io
import json

from chttr import cli
from tests.test_library import lib  # noqa: F401


def test_export_json_all(lib, capsys):  # noqa: F811
    parser = cli.build_parser()
    args = parser.parse_args(["export"])
    rc = args.func(args)
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert len(data) == 2
    titles = {m["title"] for m in data}
    assert "Pricing review" in titles
    assert "Launch sync" in titles

    pricing = next(m for m in data if m["title"] == "Pricing review")
    assert pricing["date"].startswith("2026-10-01")
    assert "Dana" in pricing["speakers"]
    assert "Ali" in pricing["speakers"]
    assert len(pricing["action_items"]) == 1
    assert pricing["action_items"][0]["owner"] == "Ali"
    assert len(pricing["transcript"]) == 2


def test_export_json_since(lib, capsys):  # noqa: F811
    parser = cli.build_parser()
    args = parser.parse_args(["export", "--since", "2026-10-05"])
    rc = args.func(args)
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert len(data) == 1
    assert data[0]["title"] == "Launch sync"


def test_export_json_specific_meeting(lib, capsys):  # noqa: F811
    parser = cli.build_parser()
    args = parser.parse_args(["export", "2026-10-01-140000"])
    rc = args.func(args)
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert len(data) == 1
    assert data[0]["title"] == "Pricing review"


def test_export_csv(lib, capsys):  # noqa: F811
    parser = cli.build_parser()
    args = parser.parse_args(["export", "--format", "csv"])
    rc = args.func(args)
    assert rc == 0
    captured = capsys.readouterr()
    reader = csv.DictReader(io.StringIO(captured.out))
    rows = list(reader)
    assert len(rows) == 2
    pricing = next(r for r in rows if r["title"] == "Pricing review")
    assert "Dana" in pricing["speakers"]
    assert "Ali" in pricing["action_items"]
    assert "Dana: We agreed the budget" in pricing["transcript"]

def test_export_missing_meeting(lib, capsys):  # noqa: F811
    parser = cli.build_parser()
    args = parser.parse_args(["export", "non-existent-id"])
    rc = args.func(args)
    assert rc == 0
    captured = capsys.readouterr()
    assert "warning: meeting 'non-existent-id' not found" in captured.err
    data = json.loads(captured.out)
    assert data == []
