"""CLI contract tests for the Hermes command contribution."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

import pytest

from skill_layers import cli


class FakeService:
    instance: FakeService | None = None

    def __init__(self):
        self.scope = type(
            "Scope", (), {"id": "active-profile", "public": lambda self: {"id": self.id}}
        )()
        self.calls: list[tuple] = []
        self.closed = False
        FakeService.instance = self

    def read(self, action, identifier=None, *, bounded=False):
        self.calls.append(("read", action, identifier, bounded))
        return {"ok": True, "data": {"action": action}}

    def mutate(self, action, parameters, *, expected_profile, request_id=None):
        self.calls.append(("mutate", action, parameters, expected_profile, request_id))
        return {"ok": True, "data": {"action": action}}

    def close(self):
        self.closed = True


def parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    cli.setup_cli(parser)
    return parser.parse_args(argv)


def test_read_commands_and_mutations_emit_json(monkeypatch, capsys):
    monkeypatch.setattr(cli, "Service", FakeService)
    args = parse("status", "layer-1", "--json")
    cli.handle_cli(args)
    assert json.loads(capsys.readouterr().out) == {"ok": True, "data": {"action": "status"}}
    assert FakeService.instance.calls == [("read", "status", "layer-1", False)]
    assert FakeService.instance.closed

    cli.handle_cli(parse("derive", "source-1", "--label", "Docs", "--request-id", "r-1"))
    assert FakeService.instance.calls == [
        ("mutate", "derive", {"source_id": "source-1", "label": "Docs"}, "active-profile", "r-1")
    ]


def test_file_resolution_preserves_bytes_as_base64(monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setattr(cli, "Service", FakeService)
    payload = b"---\r\nname: changed\r\n---\r\n\x00body\xff\r\n"
    replacement = tmp_path / "replacement"
    replacement.write_bytes(payload)
    cli.handle_cli(
        parse(
            "resolve",
            "candidate",
            "--revision",
            "3",
            "--path",
            "SKILL.md",
            "--file",
            str(replacement),
        )
    )
    call = FakeService.instance.calls[0]
    assert call[0:2] == ("mutate", "resolve")
    assert call[2]["edited_base64"] == base64.b64encode(payload).decode("ascii")
    assert "edited_file" not in call[2]
    assert FakeService.instance.closed
    capsys.readouterr()


def test_file_resolution_is_bounded(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(cli, "Service", FakeService)
    replacement = tmp_path / "large"
    replacement.write_bytes(b"x" * (cli.MAX_FILE_BYTES + 1))
    with pytest.raises(SystemExit) as exc:
        cli.handle_cli(
            parse(
                "resolve",
                "candidate",
                "--revision",
                "3",
                "--path",
                "SKILL.md",
                "--file",
                str(replacement),
            )
        )
    assert exc.value.code == 1
    assert FakeService.instance.closed


def test_failed_service_response_exits_nonzero(monkeypatch, capsys):
    class Failed(FakeService):
        def read(self, action, identifier=None, *, bounded=False):
            return {"ok": False, "error": {"code": "NOT_FOUND"}}

    monkeypatch.setattr(cli, "Service", Failed)
    with pytest.raises(SystemExit) as exc:
        cli.handle_cli(parse("list"))
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
