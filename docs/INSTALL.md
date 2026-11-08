# Install Skill Layers

## Requirements

- Hermes v0.23.0 or newer, subject to the version listed in the release being installed.
- A clean released Skill Layers commit from the public repository.
- Git available on `PATH` when a refresh needs text merging.
- Hermes plugin dependency consent for `ruamel.yaml` (declared as `ruamel.yaml>=0.18.6,<0.19`).
- `plugins.isolation: in_process` for the contributed `hermes skill-layers` CLI command.

The project supports Python 3.11–3.14 according to `pyproject.toml`; use a Python version supported by your Hermes installation. The recorded development checks include six successful CI jobs in run [42444854073](https://github.com/asimons81/hermes-skill-layers/actions/runs/42444854073), covering Ubuntu 3.11 and 3.14, macOS and Windows 3.13, browser checks, and static checks. Green CI does not establish Desktop acceptance for the release payload. Consult `docs/QA_REPORT.md`, the Actions run for the exact commit under review, and the release handoff for the applicable validation record.

## Install from an immutable release commit

Use Hermes' plugin manager and the release's 40-character Git commit SHA:

```sh
hermes plugins install asimons81/hermes-skill-layers --ref <40-character-release-commit>
```

The plugin manager accepts an owner/repository or Git URL and the `--ref` option pins one commit. Do not use a moving branch or tag for a reviewed deployment. If the plugin is installed disabled, enable it with:

```sh
hermes plugins enable skill-layers
```

During install, Hermes handles the declared Python dependency through its normal dependency consent flow. Do not use `--no-deps` if you expect to enable the plugin; Hermes documents that this option leaves the plugin disabled. For an unattended install, `--yes-deps` answers the dependency consent prompt, and `--enable` enables after install; only use those flags when you intend to approve both actions.

To validate a clean local checkout before installing, use Hermes' path-based validation command:

```sh
hermes plugins validate /absolute/path/to/clean/hermes-skill-layers --install-deps
```

Use a clean checkout with only tracked release files. Do not symlink a development project root containing `work/`, host checkouts, environment state, or generated caches into a profile: Hermes' plugin payload snapshot may include files that are not in the distributable package.

Use a physical plugin installation in each intended Hermes profile. The source package uses a virtual dependency member and a normal Python package API entry, so profile copies share the declared third-party dependencies without installing duplicate self-distributions. The final QA receipt records the physical two-profile install and lifecycle checks.

## Hermes install trust review

Hermes' community-source scan can flag the controlled local Git process, byte constants used by the UTF-8 parser, and intentionally unsafe strings and binary files in the test fixtures. The normal installer may stop with a caution verdict. Review the warnings and the exact pinned source before deciding whether to trust it. If you accept those reviewed operations, Hermes provides a one-install override:

```sh
hermes plugins install asimons81/hermes-skill-layers --ref <reviewed-40-character-commit> --force
```

Keep dependency consent explicit. For an intentionally unattended test install, `--yes-deps --enable` approves the declared dependency and enables this install. An install trust decision does not approve native skill edits; those keep Hermes' normal locks and approval controls. Follow managed policy if it prohibits an override.

## Check installation

Run the contributed command:

```sh
hermes skill-layers doctor --json
hermes skill-layers sources --json
```

`doctor` reports whether local Git is available, the publication boundary, and that the CLI requires `plugins.isolation: in_process`. `sources` lists eligible on-disk skills discovered in the active profile. It does not initialize plugin state or change skills.

If `hermes skill-layers` is not registered, first check that the plugin is enabled and that the active isolation mode permits CLI contributions. A managed `plugins.isolation: host` setting prevents this plugin's contributed CLI command. Skill Layers does not change a managed setting; follow your administrator's policy. Do not edit managed configuration just to expose the command.

## First derivative

Use the source ID returned by `sources`, not a path or URL:

```sh
hermes skill-layers derive <source-id> --label "My working copy" --json
hermes skill-layers diff <candidate-id> --json
hermes skill-layers publish <candidate-id> --revision <revision> --review-hash <review-hash> --json
```

`derive` prepares a candidate; it does not create the skill. Inspect the diff and verify `complete_review` is true. Use the candidate's returned revision, review hash, and exact `publish_command`. Publishing creates a fresh skill selector. Select that exact selector in Hermes when you want to use the derivative; the current conversation does not switch automatically.

For the full refresh and rollback workflow, see [README.md](../README.md). For interrupted or stale operations, see [RECOVERY.md](RECOVERY.md).

## Plugin dependency and runtime behavior

The repository's `package.json` is for contributor browser tests. If Hermes offers a Node sidecar during installation, choose No for normal use: the Desktop file is plain ESM and uses Hermes' existing SDK/React runtime. Run `npm ci` only when developing or running the browser harness.

The runtime Python dependency is `ruamel.yaml`, used for bounded validation of SKILL.md frontmatter. Git is an external local executable used for `git merge-file`; it is not installed automatically by Skill Layers. Merge runs do not fetch repositories or access the network. Skill Layers does not install dependencies listed inside a skill, execute skill scripts, or make plugin-owned model/API calls.

The agent-facing tools are read-only and do not create snapshots, initialize state, resolve conflicts, or publish.
