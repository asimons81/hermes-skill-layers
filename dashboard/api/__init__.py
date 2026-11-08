"""Load the Dashboard router only when the host requests its web API."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .skill_layers.dashboard_api import router

__all__ = ["router"]


def __getattr__(name: str) -> Any:
    if name == "router":
        from .skill_layers.dashboard_api import router

        return router
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
