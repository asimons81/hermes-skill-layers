# Recovery and data preservation

Skill Layers stores profile-scoped snapshots, candidates, checkpoints, and an operation journal in Hermes' plugin data directory. It creates new export directories; it does not replace an earlier export. If a command stops or reports an error, preserve the relevant profile and files until you understand the result.

## Start with read-only inspection

These commands inspect the active profile and do not repair, migrate, or publish anything:

```sh
hermes skill-layers doctor --json
hermes skill-layers list --json
hermes skill-layers status <layer-id> --json
hermes skill-layers diff <candidate-id> --json
```

Check the profile label, layer ID, preferred export, candidate ID/state/revision, source selector, and any error code. The preferred export is a plugin preference, not a session alias. Existing conversations do not change skill versions automatically.

## A candidate is blocked or stale

- **Recorded conflicts:** publication is rejected until all recorded conflicts are resolved. Resolve only a known relative path using an available whole-file side, explicit delete, or replacement file. Then run `diff` again and review the new revision and review hash before publishing.
- **Stale revision or review hash:** fetch `diff` again. If the source, local export, layer revision, or profile changed, prepare a new refresh/rollback candidate from the current state instead of reusing an old review hash.
- **Source missing or moved:** current exports remain usable. Refresh fails rather than rebinding to a same-named skill elsewhere. Restore the original skill at the recorded location through its normal installation workflow, or start a new layer from a newly discovered source. Do not edit database source paths to force a match.
- **Changed generated identity:** if the generated name in a managed export was changed, the plugin cannot safely assume it is the recorded export. Preserve the skill and its marker; do not hand-edit the marker or database to claim ownership.

## An operation was interrupted

The operation journal lets the operator reconcile a verified publication that stopped around the directory rename/database commit boundary. Run recovery in the same Hermes profile where the operation began:

```sh
hermes skill-layers recover --json
```

To inspect/recover one operation, pass its operation ID:

```sh
hermes skill-layers recover <operation-id> --json
```

Recovery may finish a publication only when the journal, profile, ownership marker, destination, and complete tree match. It does not delete an unknown path or adopt a directory based on a marker alone. Review the returned operation states, then inspect `list`, `status`, and the exact export directory. If recovery reports `RECOVERY_REQUIRED`, stop and preserve the stage, destination, and plugin data for investigation; do not delete either path or try a manual rename.

If an operation was interrupted in `intended` before its staging tree was committed and the destination does not exist, recovery aborts that operation, preserves any partial stage for inspection, and returns the candidate to `ready`. Review the candidate again and publish with a new request ID; the interrupted request is not resumed. Do not clean up the preserved stage by hand. An interrupted `preparing` operation may be marked failed and require a new request. Repeating a request with the same request ID and same input is safe when the request is still valid; reusing the ID with different input is rejected. When retrying from a CLI error response, inspect `recover` first and use a new request ID when recovery reports an aborted operation.

## Native edit lock or approval response

Native skill editing uses Hermes' native locks and approvals. Leave them in place. Wait for or resolve the native approval through Hermes' own supported control. A denied or pending native edit must not be rerouted through Skill Layers publication. Plugin publication is a separate explicit operator action and does not enter the native skill approval queue.

## Preserve user data

- Do not delete older exports; later edits to an older export are not transferred automatically.
- Do not remove `.skill-layers-export.json`, edit it manually, or treat it as standalone proof of ownership.
- Do not manually alter SQLite records or content objects.
- Disabling or removing the plugin leaves exported skill directories intact. Keep the plugin's profile data if you need layer history, checkpoints, or reconciliation later. V1 has no automatic history cleanup.
- If using Hermes profile backups, include the profile's plugin data and ordinary skill directories together. Restore them into the same profile context; a copied database in a different profile fails closed.

If you cannot establish which files belong to a journaled operation, leave them untouched and provide the operation ID, error code, plugin version, and sanitized logs to the maintainer. Do not include skill contents, credentials, or full private paths unless separately requested through an appropriate private support channel.
