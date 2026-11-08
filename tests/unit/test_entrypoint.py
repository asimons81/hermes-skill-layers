"""The public loader supplies a package namespace, not a sys.path entry."""

import importlib.abc
import importlib.util
import sys
from pathlib import Path


def test_entrypoint_registers_without_absolute_self_package(monkeypatch):
    class RefuseAbsoluteSelfImport(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "skill_layers" or fullname.startswith("skill_layers."):
                raise ImportError("The plugin package was not installed into the probe environment")
            return None

    class Context:
        def __init__(self):
            self.tools, self.commands = [], []

        def register_tool(self, **record):
            self.tools.append(record["name"])

        def register_cli_command(self, **record):
            self.commands.append(record["name"])

    for key in list(sys.modules):
        if key == "skill_layers" or key.startswith("skill_layers."):
            monkeypatch.delitem(sys.modules, key)
    name = "fresh_loader_probe"
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        name, root / "__init__.py", submodule_search_locations=[str(root)]
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(sys, "meta_path", [RefuseAbsoluteSelfImport(), *sys.meta_path])
    try:
        spec.loader.exec_module(module)
        context = Context()
        module.register(context)
        assert sorted(context.tools) == [
            "skill_layers_diff",
            "skill_layers_list",
            "skill_layers_status",
        ]
        assert context.commands == ["skill-layers"]
    finally:
        for key in list(sys.modules):
            if key.startswith(name + "."):
                monkeypatch.delitem(sys.modules, key)
