"""Argparse contribution and JSON operator interface for Hermes Skill Layers."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from typing import Any

from .contracts import MAX_FILE_BYTES, Code, LayerError
from .service import Service


def _request_id(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--request-id", help="UUID used to safely retry this mutation.")


def setup_cli(parser: argparse.ArgumentParser) -> None:
    """Register every supported subcommand on Hermes' contributed `skill-layers` parser."""
    parser.add_argument("--json", action="store_true", help="Print the response as JSON.")
    commands = parser.add_subparsers(dest="action", required=True)

    for name in ("doctor", "sources", "list"):
        child = commands.add_parser(name)
        child.add_argument("--json", action="store_true", help="Print the response as JSON.")

    status = commands.add_parser("status")
    status.add_argument("layer_id")
    status.add_argument("--json", action="store_true", help="Print the response as JSON.")

    derive = commands.add_parser("derive")
    derive.add_argument("source_id")
    derive.add_argument("--label", required=True)
    derive.add_argument("--json", action="store_true", help="Print the response as JSON.")
    _request_id(derive)

    for name in ("checkpoint", "refresh"):
        child = commands.add_parser(name)
        child.add_argument("layer_id")
        child.add_argument("--json", action="store_true", help="Print the response as JSON.")
        _request_id(child)

    diff = commands.add_parser("diff")
    diff.add_argument("candidate_id")
    diff.add_argument("--json", action="store_true", help="Print the response as JSON.")

    resolve = commands.add_parser("resolve")
    resolve.add_argument("candidate_id")
    resolve.add_argument("--revision", type=int, required=True)
    resolve.add_argument("--path", required=True)
    choice = resolve.add_mutually_exclusive_group(required=True)
    choice.add_argument("--take", choices=("local", "source", "delete"))
    choice.add_argument(
        "--file", dest="edited_file", help="File containing the whole-file replacement."
    )
    resolve.add_argument("--json", action="store_true", help="Print the response as JSON.")
    _request_id(resolve)

    publish = commands.add_parser("publish")
    publish.add_argument("candidate_id")
    publish.add_argument("--revision", type=int, required=True)
    publish.add_argument("--review-hash", required=True)
    publish.add_argument("--json", action="store_true", help="Print the response as JSON.")
    _request_id(publish)

    rollback = commands.add_parser("rollback")
    rollback.add_argument("layer_id")
    rollback.add_argument("--checkpoint", dest="checkpoint_id", required=True)
    rollback.add_argument("--json", action="store_true", help="Print the response as JSON.")
    _request_id(rollback)

    recover = commands.add_parser("recover")
    recover.add_argument("operation_id", nargs="?")
    recover.add_argument("--json", action="store_true", help="Print the response as JSON.")
    _request_id(recover)

    parser.set_defaults(func=handle_cli)


def _edited_bytes(path: str) -> bytes:
    try:
        with Path(path).open("rb") as stream:
            content = stream.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise LayerError(Code.INVALID_STATE, "Cannot read the edited text file.") from exc
    if len(content) > MAX_FILE_BYTES:
        raise LayerError(Code.LIMIT_EXCEEDED, "The edited text file exceeds the file limit.")
    return content


def _request(action: str, args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    if action == "derive":
        return action, {"source_id": args.source_id, "label": args.label}
    if action in {"checkpoint", "refresh"}:
        return action, {"layer_id": args.layer_id}
    if action == "rollback":
        return action, {"layer_id": args.layer_id, "checkpoint_id": args.checkpoint_id}
    if action == "resolve":
        parameters: dict[str, Any] = {
            "candidate_id": args.candidate_id,
            "revision": args.revision,
            "path": args.path,
        }
        if args.take is not None:
            parameters["take"] = args.take
        else:
            parameters["edited_base64"] = base64.b64encode(_edited_bytes(args.edited_file)).decode(
                "ascii"
            )
        return action, parameters
    if action == "publish":
        return action, {
            "candidate_id": args.candidate_id,
            "revision": args.revision,
            "review_hash": args.review_hash,
        }
    if action == "recover":
        return action, {"operation_id": args.operation_id}
    raise LayerError(Code.INVALID_STATE, "Unknown CLI mutation.")


def _response(service: Service, args: argparse.Namespace) -> dict[str, Any]:
    action = args.action
    if action == "status":
        return service.read(action, args.layer_id)
    if action == "diff":
        return service.read(action, args.candidate_id)
    if action in {"doctor", "sources", "list"}:
        return service.read(action)
    operation, parameters = _request(action, args)
    return service.mutate(
        operation,
        parameters,
        expected_profile=service.scope.id,
        request_id=args.request_id,
    )


def handle_cli(args: argparse.Namespace) -> None:
    """Execute the CLI action, print its JSON envelope, and exit nonzero on service errors."""
    service = Service()
    try:
        try:
            response = _response(service, args)
        except LayerError as exc:
            response = {
                "ok": False,
                "profile_id": service.scope.id,
                "profile": service.scope.public(),
                "error": exc.to_dict(),
            }
        print(json.dumps(response, ensure_ascii=False, indent=2))
        if not response.get("ok", False):
            raise SystemExit(1)
    finally:
        service.close()
