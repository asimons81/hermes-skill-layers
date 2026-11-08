# Security and trust boundaries

Skill Layers handles skill files as untrusted data. It is a local preservation and review tool, not a sandbox for running skills and not a global monitor of Hermes writes.

## Local data and profile scope

- Sources are selected from Hermes' current supported on-disk skill discovery. V1 does not accept arbitrary source paths or URLs and does not select plugin-registered skill bundles.
- State is stored under Hermes' profile-scoped plugin data for `skill-layers`. Reads are bounded and read-only; they do not initialize the database, migrate it, recover journaled operations, or capture new snapshots.
- Profile context is checked on reads and mutations. A copied database under a different canonical profile context fails closed.
- Native exports are ordinary profile skills. They remain usable if Skill Layers is disabled or removed, but their history and reconciliation features require plugin data.

## Untrusted skill content

Skill files are never executed, imported as code, rendered as executable HTML, or sent to a remote Skill Layers service. Skill instructions are not trusted as directions to the operator or this tool. Declared skill dependencies are not installed. The plugin reads only the selected skill subtree and applies bounded parsing to SKILL.md frontmatter.

The capture layer rejects unsafe relative paths, absolute or traversing names, NULs, unsupported Windows names, case/Unicode collisions, VCS metadata, symlinks and reparse points, hard links where detectable, special filesystem entries, oversized files/trees, and files that change during capture. A reserved ownership-marker filename cannot be source content. Windows file capture uses verified handles and fails closed if required filesystem metadata is unavailable. The recorded CI evidence and its exact tested commit are listed in `docs/QA_REPORT.md`; a Windows pass for one commit does not establish validation of a different commit or every filesystem implementation. Consult the Actions run for the exact commit under review.

Initial content limits are 256 files, 2 MiB per file, and 16 MiB per tree. SKILL.md must be UTF-8 with supported YAML frontmatter: a single-line scalar `name` with bounded, duplicate-safe parsing. The identity adapter changes only that scalar; raw snapshots preserve original bytes.

## Merge and publication

Git's `merge-file` is invoked locally with fixed arguments, a sanitized subprocess environment, no shell, bounded runtime, and bounded output. It does not fetch a source repository or contact a remote service. Missing Git blocks merge preparation. Textual merge success does not establish that the resulting skill behaves correctly.

The plugin persists immutable inputs and a publication journal, reviews a candidate revision/hash, and creates a uniquely named new export. Existing destinations are never overwritten. Captured data is point-in-time: edits to an old export after a new capture stay on that old export. Do not treat a clean merge as a behavior or security review.

The `.skill-layers-export.json` file is metadata, not proof of ownership by itself. Ownership is checked against the plugin's profile data and journal. Do not edit or copy it to claim another directory.

## Hermes native controls

Existing Hermes locks and approvals remain in force for later native skill edits. Skill Layers does not unlock sources, change managed configuration, approve pending native edits, or expose agent mutation tools. The three agent tools (`skill_layers_list`, `skill_layers_status`, and `skill_layers_diff`) are bounded reads only.

Operator publication is a distinct explicit action after candidate review. It does not enter the native `skill_manage` approval queue. Never use publication as a fallback for a denied or pending native edit. A source remaining unchanged during Skill Layers operations is not a guarantee that other Hermes tools or unrelated local processes cannot edit it; use the existing native lock controls where needed.

## Network and dependencies

Runtime dependencies are the declared `ruamel.yaml` package and the local Git executable for merges. The Skill Layers workflow does not make model calls, fetch skill sources, or send skill contents to telemetry or a remote service. Desktop requests use Hermes' normal authenticated plugin transport; this is the host-to-plugin interface, not a separate Skill Layers server.

## Reports

When reporting a problem, include the plugin version, Hermes version, platform, sanitized error code, candidate/operation ID, and whether the error occurred during capture, review, publication, or recovery. Do not include source text, private skill names, credentials, environment dumps, or complete private paths in public issues. Review diffs locally before sharing them.

## Installation scan

The repository includes the bounded Git subprocess and negative test fixtures, including non-executable binary samples. Hermes' static community-source scan may flag these and stop installation. Review the exact commit and findings before using its documented one-install `--force` trust option; the plugin does not alter the host's scan configuration. See [installation](INSTALL.md#hermes-install-trust-review). Native skill-edit approvals remain separate.
