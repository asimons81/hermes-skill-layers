# Hermes Skill Layers

Skill Layers preserves an installed skill as a read-only source reference and lets you work in ordinary, writable copies. It can compare a copy with a later version of the installed source, show a reviewable candidate, and publish a new copy after you approve the exact revision and review hash.

## Validation status

For the current validation record, see `docs/QA_REPORT.md`, the Actions run for the exact commit under review, and the release handoff. A green CI run alone does not establish Hermes Desktop acceptance for the release payload.

Profile testing covered two named profiles sharing one Hermes root and runtime, with the same canonical finite plugin payload symlinked into both; profile B independently derived a skill. Separate duplicated plugin clones enabled at the same time have not been verified and are not a supported topology claim.

## What it does

- Captures selected on-disk skills in the active Hermes profile, including support files, without rewriting the source.
- Creates a distinctly named derivative skill in that profile. Derive, refresh, and rollback prepare candidates; **publish** is the action that creates a new export.
- Compares source base, current derivative, and updated source. Conflicts require an explicit whole-file resolution.
- Keeps old exports and checkpoints. A later edit to an old export stays there; it is not copied automatically into the new export.
- Exposes only bounded, read-only agent tools: `skill_layers_list`, `skill_layers_status`, and `skill_layers_diff`.

The preferred export is a Skill Layers preference. It does not switch a running conversation. Select the exact published skill name when you want Hermes to use that copy.

## Install

Install a reviewed 40-character release commit from the public repository:

```sh
hermes plugins install asimons81/hermes-skill-layers --ref <40-character-release-commit>
```

Replace the placeholder with the commit SHA published for the release. Hermes may ask you to approve the declared Python dependency, `ruamel.yaml`; approve it to enable the plugin. Do not install from a dirty development checkout or symlink the project root into a profile: local `work/` files and host checkouts are not plugin payload. Use a clean repository clone or the immutable Git commit above.

The contributed `hermes skill-layers` command requires Hermes plugin isolation mode `in_process`. Skill Layers reports this in `doctor`; it does not change `plugins.isolation` or managed policy. If your administrator pins `plugins.isolation: host`, the contributed CLI is unavailable. Follow that policy and ask the administrator about supported deployment options instead of changing the setting to enable this plugin.

The tested named-profile arrangement uses a shared Hermes root and runtime, with one canonical finite plugin payload symlinked into both profiles. Profile-scoped state is maintained separately and profile B was able to derive independently. Do not assume that installing independent duplicated plugin clones into both profiles is supported or behaves identically.

`git` must be installed for merge preparation. Hermes plugin installation handles the declared Python dependency through its normal dependency consent flow; Skill Layers does not install dependencies declared by a skill.

## Three-minute workflow

List eligible installed skills, then prepare a derivative:

```sh
hermes skill-layers sources --json
hermes skill-layers derive <source-id> --label "Release notes" --json
```

Derive returns a candidate ID and revision. Review its complete diff and use the exact `publish_command` and `review_hash` returned by `diff`:

```sh
hermes skill-layers diff <candidate-id> --json
hermes skill-layers publish <candidate-id> --revision <revision> --review-hash <review-hash> --json
```

If a candidate has conflicts, publication is rejected until you resolve every conflict. Choose a whole file from a side that exists, delete it explicitly, or provide a whole-file replacement:

```sh
hermes skill-layers resolve <candidate-id> --revision <revision> --path <relative-path> --take local --json
hermes skill-layers diff <candidate-id> --json
```

Resolution changes the candidate revision and review hash. Review the new diff before publishing; do not reuse an earlier review hash.

After publishing, use its exact native selector in Hermes. Native skill edit locks and approvals continue to apply to the derivative. A plugin publication is a separate, explicit operator action; it does not enter Hermes' native `skill_manage` approval queue. Never use plugin publication as a fallback for a denied native edit.

## Update a source and keep local work

Update the original skill using your existing installation workflow. Skill Layers does not fetch or update the source. Then checkpoint the current derivative and prepare a refresh:

```sh
hermes skill-layers checkpoint <layer-id> --json
hermes skill-layers refresh <layer-id> --json
hermes skill-layers diff <candidate-id> --json
```

Review the proposed tree, resolve conflicts if needed, and publish using the returned revision and review hash. This creates a new skill name. Earlier exports remain usable and unchanged, including later edits made to them. A clean text merge does not prove the skill behaves correctly; inspect and test the resulting skill yourself.

To prepare a rollback copy from a checkpoint:

```sh
hermes skill-layers rollback <layer-id> --checkpoint <checkpoint-id> --json
hermes skill-layers diff <candidate-id> --json
```

Review and publish the rollback candidate like any other candidate. Rollback creates another new export; it does not replace an earlier one.

## Limits and data

- Sources must be ordinary on-disk skills discovered in the active profile. V1 does not accept plugin-registered skill bundles, arbitrary paths, or URLs as sources.
- Capture limits are 256 files, 2 MiB per file, and 16 MiB per tree. Unsafe paths, links/reparse points, hard links where detectable, reserved names, and filesystem collisions are rejected.
- A copied skill that relies on paths outside its own directory may need manual adaptation.
- Raw snapshots preserve captured bytes. Normalized merge trees replace only the validated frontmatter `name` scalar; exports receive a new generated name.
- Git's local `merge-file` command is used for text merging. It runs without a shell or network access. Missing Git blocks merge preparation.
- The plugin stores profile-scoped history in Hermes plugin data. Disabling or removing the plugin does not remove exported skills. Removing the plugin also does not intentionally delete plugin history; that history is needed for future review and reconciliation.
- Skill Layers does not guarantee that unrelated Hermes tools or other processes cannot modify a source. Existing native locks are the supported protection mechanism for those writers.

See [installation](docs/INSTALL.md), [recovery](docs/RECOVERY.md), and [security](docs/SECURITY.md) for details.

## Uninstall

Disable or remove the plugin with Hermes:

```sh
hermes plugins disable skill-layers
hermes plugins remove skill-layers
```

Neither action deletes published skill copies. Keep the profile's Skill Layers plugin data if you may want its checkpoints, source history, or reconciliation records later. Do not manually delete that data while operations are pending.
