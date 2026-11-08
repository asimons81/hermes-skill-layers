# Architecture

The Python service is shared by the operator CLI and Desktop REST routes. Agent tools expose only bounded reads. A layer binds one installed source location to snapshots, candidates, checkpoints, and ordinary, uniquely named native skill exports. Its preferred export is a plugin preference; it never changes a conversation's selection.

## Host boundaries

Discovery uses the public Hermes home, skill-search, discovery, and selector-resolution helpers. Each service binds canonical profile home, skill destination, source location, and discovery context. Capture and publication recheck those bindings. A similarly named source elsewhere is not a replacement for the recorded source.

Storage uses Hermes' public `plugin_data_dir("skill-layers")` only during explicit initialization. Read-only services compute its expected location from the public home API, open existing SQLite state read-only, and never initialize, migrate, capture, or recover. SQLite uses rollback journaling rather than WAL so reads do not create WAL/SHM sidecars.

The buildable Python package installs `skill_layers` into Hermes' managed environment. The root wrapper uses the package namespace supplied by Hermes' loader, so a fresh capability probe does not require a preinstalled self-package. Internal imports are relative. The standalone Dashboard API imports the installed package; both adapters use the same domain implementation. CLI contributions require the host's `in_process` plugin mode. Dashboard routes are mounted by Hermes under `/api/plugins/skill-layers`, behind its normal authentication and origin controls. Request bodies carry an expected profile and idempotency request ID; paths and action fields are allowlisted. Blocking work runs in the host thread pool. Desktop uses only React and the public `@hermes/plugin-sdk`; it opens no server of its own.

The Desktop half is opt-in. Every asynchronous UI request captures connection, profile, and gateway activation epoch; responses from an obsolete context are discarded. Skill content is rendered as text, including conflict proposals. Agent tools cannot derive, resolve, publish, recover, or change preferences.

## Snapshot identities

Raw snapshots preserve relative paths, bytes, and portable executable masks. Canonical SHA-256 manifests include representation/version, identity metadata, and sorted file hashes, sizes, and execute masks. Every content object is verified when loaded. Objects are persisted with an atomic no-replace move; a crash cannot leave a second hardlink that invalidates the object.

Normalized trees replace only the accepted single-line YAML frontmatter `name` scalar with `skill-layers-identity`. Parsing is bounded and rejects duplicate keys, aliases, custom tags, and malformed identity. Bytes outside that scalar, including line endings and other metadata, survive. A managed export's identity must match its recorded generated name before normalization.

Export trees replace the neutral name with a fresh native selector. `.skill-layers-export.json` contains ownership metadata outside the content manifest. Source trees reject that reserved name. Ownership requires the journal and full matching content, not just a marker.

Capture rejects symlinks, reparse points, hardlinked files, nonregular entries, unsafe portable paths, case aliases, and tree limits. POSIX reads use no-follow directory descriptors. Windows uses verified native handles before reading. Staging uses equivalent exclusive, bounded writes. Unsupported executable masks fail at the filesystem boundary rather than being silently discarded.

## Three-way reconciliation

Refresh compares normalized B (recorded source base), L (current preferred export), and U (current source). Per-path equality rules precede `git merge-file --stdout --diff3`. Git receives fixed labels, controlled temporary files, a sanitized environment, timeout, and output limits. Exit status determines conflicts; marker-looking file content does not. Renames are deletion plus addition.

Incompatible additions, delete/modify, binary double edits, and incompatible execute masks produce explicit conflicts. Review preserves the captured B/L/U sides and Git proposal separately, with hashes, sizes, masks, text previews or binary flags. An omitted conflicted result is not presented as a proposed deletion. Whole-file local/source/delete/edited decisions clear only the chosen conflict and change revision and review hash. SKILL.md and the complete tree must validate before publication. A clean textual merge does not prove correct behavior.

Refresh associates U as the next source base. Rollback uses checkpoint content and its associated base without fetching a new source. Both prepare candidates and require separate publication.

## Publication and recovery

Publication verifies profile, revision, review hash, unresolved conflicts, and the complete result. It journals intent before writing a same-filesystem stage outside all discovery roots, then durably records the staged phase. Immediately before rename it checks candidate/layer preference revisions, source and local captures, marker, and staged tree.

Publication holds verified source and destination parent directories. macOS uses descriptor-relative `renameatx_np(RENAME_EXCL)`; Linux uses `renameat2(RENAME_NOREPLACE)`. Windows holds ancestor handles without delete sharing and uses its no-replace rename behavior. Missing platform primitives fail closed. An existing destination is never replaced, even if empty. There is no discoverable file-by-file copy fallback.

After rename, native selector discovery must resolve the exact export, then one SQLite transaction commits its preference, candidate, operation, and idempotent result. A crash between filesystem and database commits is reconciled from the journal and full expected tree. An intended operation with no target can be aborted before stage commit: partial files stay untouched and the candidate may be reviewed and published with a new request. Staged/renamed ambiguity remains an explicit recovery error; unrelated paths are never deleted or adopted.

Plugin publications serialize through profile storage. They do not coordinate with native writers: a native edit arriving after the final capture stays on the older export and is absent from the new one. Every review states this limitation. Old exports and history are never automatically pruned.

Operator publication is separate from Hermes' native `skill_manage` approval queue. Native locks and approvals remain in force for native edits. Publication must never be a fallback for a denied native edit.
