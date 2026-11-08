import sys
import types

import pytest

from skill_layers.contracts import LayerError, ProfileScope
from skill_layers.sources import Discovery, current_scope, source_from_record, source_to_record


def _host(monkeypatch, root, selector_paths):
    def find_all_skills():
        return [{"name": name, "description": name, "category": None} for name in selector_paths]

    def locate_skill(selector, category, roots):
        assert category is None
        assert all(isinstance(item, tuple) and len(item) == 2 for item in roots)
        path = selector_paths.get(selector)
        if path is None:
            path = root / selector
            if not (path / "SKILL.md").is_file():
                path = next(
                    (
                        search_root / selector
                        for _tier, search_root in roots
                        if (search_root / selector / "SKILL.md").is_file()
                    ),
                    None,
                )
        if path is None:
            return ("not found", None, None)
        return None, path, path / "SKILL.md"

    monkeypatch.setattr(
        Discovery, "_host_helpers", staticmethod(lambda: (find_all_skills, locate_skill))
    )
    return find_all_skills, locate_skill


def _scope(home, skills, roots, context="ctx"):
    return ProfileScope(
        "profile-test",
        home,
        skills,
        tuple((index, root) for index, root in enumerate(roots)),
        context,
    )


def _skill(path, body=b"one"):
    path.mkdir(parents=True)
    (path / "SKILL.md").write_bytes(b"---\nname: test\n---\n" + body)


def test_current_scope_uses_live_public_hermes_helpers(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = tmp_path / "skills-root"
    skills = home / "skills"
    root.mkdir()
    home.mkdir()
    skills.mkdir()
    helpers = types.ModuleType("tools.skills_tool")
    helpers.skill_search_dirs = lambda: ([(1, root)], skills)
    tools_module = types.ModuleType("tools")
    tools_module.__path__ = []
    hermes_constants = types.ModuleType("hermes_constants")
    hermes_constants.get_hermes_home = lambda: home
    monkeypatch.setitem(sys.modules, "hermes_constants", hermes_constants)
    monkeypatch.setitem(sys.modules, "tools", tools_module)
    monkeypatch.setitem(sys.modules, "tools.skills_tool", helpers)
    scope = current_scope()
    assert scope.home == home.resolve()
    assert scope.skills_dir == skills.resolve()
    assert scope.roots == ((1, root.resolve()),)
    assert scope.id.startswith("profile-")
    assert scope.discovery_context.startswith("discovery-")
    assert current_scope().id == scope.id


def test_discovery_context_survives_creation_of_optional_active_root(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    active = home / "skills"
    external = tmp_path / "external"
    external.mkdir()
    roots = [(3, external)]
    helpers = types.ModuleType("tools.skills_tool")
    helpers.skill_search_dirs = lambda: (roots + ([(1, active)] if active.exists() else []), active)
    tools_module = types.ModuleType("tools")
    tools_module.__path__ = []
    hermes_constants = types.ModuleType("hermes_constants")
    hermes_constants.get_hermes_home = lambda: home
    monkeypatch.setitem(sys.modules, "hermes_constants", hermes_constants)
    monkeypatch.setitem(sys.modules, "tools", tools_module)
    monkeypatch.setitem(sys.modules, "tools.skills_tool", helpers)
    before = current_scope()
    active.mkdir()
    after = current_scope()
    assert after.id == before.id
    assert after.discovery_context == before.discovery_context


def test_sources_are_only_host_discovered_selectors_and_serialize(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = tmp_path / "root"
    skill = root / "category" / "skill-a"
    _skill(skill)
    scope = _scope(home, home / "skills", [root])
    _host(monkeypatch, root, {"category/skill-a": skill})
    discovery = Discovery(scope)
    sources = discovery.sources()
    assert len(sources) == 1
    source = sources[0]
    assert source.selector == "category/skill-a"
    assert source.root == root.resolve()
    assert source.relative == "category/skill-a"
    assert discovery.lookup(source.id) == source
    assert discovery.resolve(source) == skill.resolve()
    assert source_from_record(source_to_record(source)) == source


def test_lookup_rejects_path_or_unknown_source_id(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    scope = _scope(tmp_path / "home", tmp_path / "skills", [root])
    _host(monkeypatch, root, {})
    discovery = Discovery(scope)
    for value in (str(root), "https://example.test/skill", "source-not-discovered"):
        with pytest.raises(LayerError) as error:
            discovery.lookup(value)
        assert error.value.code == "INVALID_SOURCE"


def test_resolve_fails_closed_when_selector_moves_roots(tmp_path, monkeypatch):
    root1, root2 = tmp_path / "root1", tmp_path / "root2"
    original, moved = root1 / "skill", root2 / "skill"
    _skill(original, b"original")
    _skill(moved, b"moved")
    selector_paths = {"skill": original}
    scope = _scope(tmp_path / "home", tmp_path / "skills", [root1, root2])
    _host(monkeypatch, root1, selector_paths)
    discovery = Discovery(scope)
    source = discovery.sources()[0]
    selector_paths["skill"] = moved
    original.rename(root1 / "removed")
    with pytest.raises(LayerError) as error:
        discovery.resolve(source)
    assert error.value.code == "SOURCE_MOVED"
    assert error.value.details["current_relative"] == "skill"


def test_changed_frontmatter_name_keeps_recorded_location_identity(tmp_path, monkeypatch):
    root = tmp_path / "root"
    skill = root / "skill"
    _skill(skill, b"original")
    scope = _scope(tmp_path / "home", tmp_path / "skills", [root])
    selector_paths = {"old-name": skill}
    _host(monkeypatch, root, selector_paths)
    discovery = Discovery(scope)
    source = discovery.sources()[0]
    (skill / "SKILL.md").write_bytes(b"---\nname: new-name\n---\nupdated\n")
    selector_paths.clear()
    selector_paths["new-name"] = skill
    assert discovery.resolve(source) == skill.resolve()


def test_verify_export_checks_fresh_active_root_and_exact_selector(tmp_path, monkeypatch):
    home = tmp_path / "home"
    active = home / "skills"
    active.mkdir(parents=True)
    export = active / "sl-demo-abc123"
    _skill(export)
    selector_paths = {"sl-demo-abc123": export}
    find, locate = _host(monkeypatch, active, selector_paths)
    tools = types.ModuleType("tools")
    tools.__path__ = []
    helpers = types.ModuleType("tools.skills_tool")
    helpers.find_all_skills = find
    helpers.locate_skill = locate
    helpers.skill_search_dirs = lambda: ([(1, active)], active)
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.skills_tool", helpers)
    stale_scope = _scope(home, active, [])
    Discovery(stale_scope).verify_export("sl-demo-abc123", export)
    with pytest.raises(LayerError) as error:
        Discovery(stale_scope).verify_export("wrong-selector", export)
    assert error.value.code == "RECOVERY_REQUIRED"


def test_resolve_rejects_different_discovery_context(tmp_path, monkeypatch):
    root = tmp_path / "root"
    skill = root / "skill"
    _skill(skill)
    _host(monkeypatch, root, {"skill": skill})
    source = Discovery(_scope(tmp_path / "home", tmp_path / "skills", [root], "ctx-a")).sources()[0]
    with pytest.raises(LayerError) as error:
        Discovery(_scope(tmp_path / "home", tmp_path / "skills", [root], "ctx-b")).resolve(source)
    assert error.value.code == "SOURCE_MOVED"


def test_resolve_refuses_host_returned_symlink(tmp_path, monkeypatch):
    root = tmp_path / "root"
    real = root / "real"
    _skill(real)
    link = root / "linked"
    link.symlink_to(real, target_is_directory=True)
    scope = _scope(tmp_path / "home", tmp_path / "skills", [root])
    selector_paths = {"linked": real}
    _host(monkeypatch, root, selector_paths)
    discovery = Discovery(scope)
    source = discovery.sources()[0]
    selector_paths["linked"] = link
    with pytest.raises(LayerError) as error:
        discovery.resolve(source)
    assert error.value.code == "PATH_UNSAFE"


def test_intermediate_symlink_beneath_discovery_root_is_not_followed(tmp_path, monkeypatch):
    root = tmp_path / "root"
    real_skill = tmp_path / "outside" / "skill"
    _skill(real_skill)
    root.mkdir()
    (root / "linked-category").symlink_to(real_skill.parent, target_is_directory=True)
    scope = _scope(tmp_path / "home", tmp_path / "skills", [root])
    _host(monkeypatch, root, {"linked-category/skill": root / "linked-category" / "skill"})
    assert Discovery(scope).sources() == []


def test_managed_marker_tree_is_excluded_from_sources(tmp_path, monkeypatch):
    root = tmp_path / "root"
    skill = root / "export"
    _skill(skill)
    (skill / ".skill-layers-export.json").write_text('{"id":"synthetic"}', encoding="utf-8")
    scope = _scope(tmp_path / "home", tmp_path / "skills", [root])
    _host(monkeypatch, root, {"export": skill})
    assert Discovery(scope).sources() == []
