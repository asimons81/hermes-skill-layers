"""Hermes entry point for the Skill Layers plugin."""

from typing import Any


def register(ctx: Any) -> None:
    """Use the package namespace supplied by Hermes' public plugin loader."""
    from .skill_layers.plugin import register as register_plugin

    register_plugin(ctx)


__all__ = ["register"]
