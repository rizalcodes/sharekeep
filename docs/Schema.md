# Schema: Secure File Sharing Platform — Data Entities

**Scope note:** this document covers **data schema only** — field names, types, required/optional, and relations. Naming/credential conventions live in `Rules.md`; system/workflow design lives in `Architecture.md`.

**Version:** v1.3 — amended 8 September 2026 at the start of M2. Previously v1.2 (start of M1) and v1.1 (kickoff, before M0). See §15 for the amendment log.

**Conventions:**
- snake_case for all field and table names, plural table names.
- `id` = UUID primary key on every table.
- `created_at` / `updated_at` on every table (plus entity-specific timestamps where relevant).
- Foreign keys named `<referenced_entity_singular>_id`.
- Soft delete: entities that can be deleted by a user carry `deleted_at` (nullable). A non-NULL `deleted_at` means the row is hidden from all normal queries but retained so `audit_logs` entries still resolve to a real row.

---

## 1. `users`

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | UUID | required | Primary key |
| `email` | string | required, unique | — |
| `hashed_password` | string | required | Never store plaintext (Argon2) |
| `full_name` | string | required | — |
| `email_verified_at` | datetime | optional | **Added v1.2.** No verification flow is built in v1 (needs Resend, which arrives at M6) — the column exists so M6 can fill it without a migration. Note that accepting an invitation inherently proves control of the address, so invited members are implicitly verified; self-signup users are not. |
| `created_at` / `updated_at` | datetime | required | — |

## 2. `workspaces`

Represents a team/collaboration space. A user's personal space is also a workspace (with only them as Owner), created automatically on signup.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | UUID | required | Primary key |
| `name` | string | required | — |
| `owner_id` | UUID | required | → `users.id`, the **current primary owner**. **v1.2 clarification:** `workspace_members` is authoritative for every permission decision; this column is a denormalised pointer for display and must always reference a user who holds an `owner` row in `workspace_members`. It moves when ownership is transferred. It is never read to decide access. |
| `is_personal` | boolean | required, default false | True for the auto-created signup workspace; personal workspaces cannot be deleted while the user exists |
| `storage_quota_bytes` | bigint | required, default 5368709120 (5GB) | Configurable per workspace |
| `storage_used_bytes` | bigint | required, default 0 | Running total; updated transactionally on upload/delete, reconciled periodically (Architecture §6) |
| `created_at` / `updated_at` | datetime | required | — |

## 3. `workspace_members`

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | UUID | required | Primary key |
| `workspace_id` | UUID | required | → `workspaces.id` |
| `user_id` | UUID | required | → `users.id` |
| `role` | enum(`owner`, `editor`, `viewer`) | required | Workspace-level default role |
| `created_at` / `updated_at` | datetime | required | `updated_at` matters here — role changes are audited |

**Constraints:** UNIQUE (`workspace_id`, `user_id`) — a user holds exactly one workspace-level role per workspace.

## 4. `workspace_invitations`

Added in v1.1. Without this, there is no path for adding a collaborator who is not already a registered user, and no pending state between "invited" and "member".

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | UUID | required | Primary key |
| `workspace_id` | UUID | required | → `workspaces.id` |
| `email` | string | required | Invitee's email; may or may not correspond to an existing `users` row |
| `role` | enum(`owner`, `editor`, `viewer`) | required | Role granted on acceptance |
| `token` | string | required, unique | Cryptographically random, unguessable — the invite link identifier |
| `status` | enum(`pending`, `accepted`, `revoked`, `expired`) | required, default `pending` | **v1.2:** `expires_at` is authoritative when validating a token — always check `status = 'pending' AND expires_at > now()`. The `expired` value exists only for display and housekeeping, set lazily by a sweep. Trusting `status` alone would accept an expired invitation that the sweep had not reached yet. |
| `invited_by` | UUID | required | → `users.id` |
| `accepted_by` | UUID | optional | → `users.id`; set on acceptance |
| `expires_at` | datetime | required | Invitations are always time-bounded (default 7 days) |
| `created_at` / `updated_at` | datetime | required | — |

**Constraints:** UNIQUE (`workspace_id`, `email`) WHERE `status = 'pending'` — one live invitation per email per workspace.

## 5. `folders`

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | UUID | required | Primary key |
| `workspace_id` | UUID | required | → `workspaces.id` |
| `parent_folder_id` | UUID | optional | → `folders.id`; `NULL` = root of workspace |
| `name` | string | required | — |
| `created_by` | UUID | required | → `users.id` |
| `deleted_at` | datetime | optional | Soft delete; deleting a folder soft-deletes its descendants |
| `deletion_batch_id` | UUID | optional | **Added v1.3.** Set on every row soft-deleted by one delete operation. Restore only revives rows sharing the batch id, so restoring a folder does not resurrect items the user had deleted separately beforehand. |
| `created_at` / `updated_at` | datetime | required | — |

**Constraints:** UNIQUE (`workspace_id`, `parent_folder_id`, `name`) WHERE `deleted_at IS NULL`. Because Postgres treats NULLs as distinct in unique indexes, root-level folders (`parent_folder_id IS NULL`) need `NULLS NOT DISTINCT` (PG15+) or a partial index on a COALESCE'd expression — pick one at implementation time and state it in the migration.

## 6. `files`

Represents the current/latest state of a file. Version history is in `file_versions`.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | UUID | required | Primary key |
| `workspace_id` | UUID | required | → `workspaces.id` |
| `folder_id` | UUID | optional | → `folders.id`; `NULL` = workspace root |
| `name` | string | required | — |
| `mime_type` | string | required | — |
| `size_bytes` | bigint | required | Size of the **current** version. Note this is *not* what the file consumes: quota counts every retained version (see §7 and Architecture §6). |
| `current_version_id` | UUID | **optional** | → `file_versions.id`. Nullable by necessity: `file_versions.file_id` is required, so the file row must exist before its first version row. Set immediately after the first version is created, and non-NULL for every file that has completed an upload. |
| `scan_status` | enum(`pending`, `scanning`, `clean`, `flagged`, `scan_skipped`, `scan_failed`) | required, default `pending` | Malware scan state of the current version (mirrors `file_versions.scan_status`) |
| `tags` | text[] | required, default `{}` | Backs the tag half of PRD §4's "filename/metadata/tag search" |
| `created_by` | UUID | required | → `users.id` |
| `deleted_at` | datetime | optional | Soft delete |
| `deletion_batch_id` | UUID | optional | **Added v1.3.** See `folders`. |
| `created_at` / `updated_at` | datetime | required | — |

**Constraints:** UNIQUE (`workspace_id`, `folder_id`, `name`) WHERE `deleted_at IS NULL` — this is what makes "re-upload with the same name creates a new version" well-defined. Same NULL-handling caveat as `folders`. GIN index on `tags`.

## 7. `file_versions`

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | UUID | required | Primary key |
| `file_id` | UUID | required | → `files.id` |
| `version_number` | integer | required | Increments per file, starting at 1 |
| `storage_key` | string | required | Object key: `workspace_id/file_id/version_id/filename` (provider-agnostic; see Architecture §8) |
| `upload_id` | string | optional | **Added v1.3.** The provider's multipart upload id, held between `upload-init` and `upload-complete` and cleared on completion. Architecture §2 step 3 creates the version row before the bytes exist, so the in-flight upload needs somewhere to live; it is also what the abandoned-upload sweep aborts. |
| `upload_started_at` | datetime | optional | **Added v1.3.** When the multipart session opened, so the sweep can find sessions older than the TTL. |
| `size_bytes` | bigint | required | — |
| `checksum_sha256` | string | optional | SHA-256, computed by the worker on upload finalize. Serves double duty: integrity verification, and the VirusTotal hash lookup (Architecture §3) |
| `uploaded_by` | UUID | required | → `users.id` |
| `scan_status` | enum(`pending`, `scanning`, `clean`, `flagged`, `scan_skipped`, `scan_failed`) | required, default `pending` | Per-version scan result |
| `scan_detail` | jsonb | optional | Scanner response summary (engine hit counts, VT analysis id, or the reason for `scan_skipped`) |
| `deleted_at` | datetime | optional | Soft delete; set when the parent file is deleted or a single version is pruned |
| `deletion_batch_id` | UUID | optional | **Added v1.3.** See `folders`. |
| `created_at` | datetime | required | — |

**Constraints:** UNIQUE (`file_id`, `version_number`).

**Retention (v1.3):** at most **10 retained versions per file**. On upload of an 11th,
the oldest surviving version is pruned — its row soft-deleted and its object removed by
the retention sweep. Without a cap, re-uploading one file repeatedly can exhaust a
workspace's quota with no way to reclaim the space short of deleting the file.

**Quota (v1.3):** a workspace's `storage_used_bytes` is the sum of `size_bytes` across
all **non-deleted** `file_versions` rows in that workspace — not just current versions.
This is the only reading consistent with Architecture §6's reconciliation task, which
recomputes real usage from the workspace's storage prefix; that prefix physically holds
every retained version. Counting current versions only would make reconciliation report
drift permanently, turning the safety check into a standing false alarm.

## 8. `file_permissions`

Explicit permission grants for a file or folder to a specific workspace member, overriding the workspace-level default role where present. Resolution order and inheritance are defined in Architecture §4.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | UUID | required | Primary key |
| `file_id` | UUID | optional | → `files.id` (mutually exclusive with `folder_id`) |
| `folder_id` | UUID | optional | → `folders.id` (mutually exclusive with `file_id`) |
| `user_id` | UUID | required | → `users.id` |
| `role` | enum(`owner`, `editor`, `viewer`) | required | — |
| `granted_by` | UUID | required | → `users.id` |
| `created_at` / `updated_at` | datetime | required | — |

**Constraints:**
- CHECK (`num_nonnulls(file_id, folder_id) = 1`) — exactly one target, enforced in the database, not just in application code.
- UNIQUE (`file_id`, `user_id`) and UNIQUE (`folder_id`, `user_id`) — one explicit grant per user per target.

## 9. `share_links`

**v1 scope:** share links target files only, not folders. Recorded here as a deliberate decision rather than an omission — folder sharing would require the link's access check to walk the folder tree on every request, which is meaningful extra surface area on the most security-sensitive path in the system.

**Type vs. modifiers (v1.1 clarification):** `link_type` is the link's *primary* access rule. `expires_at`, `max_downloads`, and `hashed_password` are **independent modifiers** that may be combined with any type. This is what makes "password-protected AND expiring" — the most common real-world combination — representable; the earlier single-enum reading made it impossible.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | UUID | required | Primary key |
| `file_id` | UUID | required | → `files.id` |
| `token` | string | required, unique | Cryptographically random, unguessable — never derived from `file_id` |
| `link_type` | enum(`public`, `private`, `password_protected`, `expiring`) | required | Primary access rule |
| `hashed_password` | string | optional | **Required** if `link_type = password_protected`; **allowed** on any other type as a modifier |
| `allowed_user_id` | UUID | optional | → `users.id`; required if `link_type = private`. v1 supports a single allowed user per link — create multiple links for multiple recipients |
| `expires_at` | datetime | optional | **Required** if `link_type = expiring`; **allowed** on any other type as a modifier |
| `max_downloads` | integer | optional | Modifier, valid on any type |
| `download_count` | integer | required, default 0 | — |
| `created_by` | UUID | required | → `users.id` |
| `revoked_at` | datetime | optional | Manual revocation before expiry |
| `created_at` / `updated_at` | datetime | required | — |

**Constraints:** CHECK enforcing the three type requirements above (`password_protected` ⇒ `hashed_password IS NOT NULL`; `expiring` ⇒ `expires_at IS NOT NULL`; `private` ⇒ `allowed_user_id IS NOT NULL`).

## 10. `audit_logs`

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | UUID | required | Primary key |
| `workspace_id` | UUID | required | → `workspaces.id` |
| `actor_id` | UUID | optional | → `users.id`; `NULL` if the action was via an anonymous share link |
| `action` | enum — see below | required | — |
| `target_type` | enum(`file`, `folder`, `share_link`, `workspace_member`, `workspace_invitation`, `workspace`) | required | — |
| `target_id` | UUID | required | ID of the affected row (soft delete guarantees it still resolves) |
| `ip_address` | inet | optional | **Added v1.1.** Without this, an anonymous share-link download logs `actor_id = NULL` and nothing identifying — forensically useless for a security product |
| `user_agent` | string | optional | **Added v1.1.** Same reasoning |
| `metadata` | jsonb | optional | Action-specific detail (e.g. which permission changed to what, or the denial reason) |
| `created_at` | datetime | required | — |

**`action` enum (expanded in v1.1):** `upload`, `download`, `share_created`, `share_revoked`, `share_accessed`, `share_access_denied`, `delete`, `permission_changed`, `version_restored`, `folder_created`, `folder_deleted`, `login`, `login_failed`, `member_added`, `member_removed`, `member_role_changed`, `invitation_sent`, `invitation_accepted`, `invitation_revoked`, `logout`, `scan_flagged`, `quota_exceeded`.

The `share_accessed` / `share_access_denied` pair exists because Architecture §4.5 requires logging every resolution attempt, success **and** failure — the original enum had no way to express a failed attempt, which is precisely the event a security audit log exists to capture.

Audit logs are append-only: no update or delete path in the application.

## 11. `notifications`

Designed to be channel-agnostic (WebSocket-ready for v1.1) even though v1 only sends email.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | UUID | required | Primary key |
| `user_id` | UUID | required | → `users.id`, the recipient |
| `workspace_id` | UUID | optional | → `workspaces.id`; context for rendering and future per-workspace filtering |
| `type` | enum(`file_shared`, `permission_granted`, `storage_quota_warning`, `scan_flagged`, `workspace_invitation`) | required | — |
| `payload` | jsonb | required | Structured data for rendering the notification (e.g. file name, actor) |
| `channel` | enum(`email`) | required, default `email` | Extensible for `websocket` in v1.1 without a schema change |
| `status` | enum(`pending`, `sent`, `failed`) | required, default `pending` | — |
| `read_at` | datetime | optional | For future in-app read-state tracking |
| `created_at` / `updated_at` | datetime | required | — |

## 12. `refresh_tokens`

Added in v1.2. Without it, logout cannot revoke anything: the cookie is cleared but the
token stays valid for its full 14-day TTL. For a product positioned on security, a
logout that revokes nothing is not a logout.

Appended as §12 rather than inserted near `users` so existing §-references in
`Architecture.md` keep pointing at the same entities.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | UUID | required | Primary key |
| `user_id` | UUID | required | → `users.id` |
| `token_hash` | string | required, unique | SHA-256 of the token. The raw token is never stored — a database leak must not yield usable sessions |
| `expires_at` | datetime | required | Authoritative, same rule as invitations |
| `revoked_at` | datetime | optional | Set on logout, on rotation, and on "log out everywhere" |
| `rotated_to_id` | UUID | optional | → `refresh_tokens.id`; set when this token is exchanged, so reuse of an already-rotated token is detectable |
| `user_agent` | string | optional | For a future session list |
| `ip_address` | inet | optional | For a future session list |
| `created_at` | datetime | required | — |

**Constraints:** index on (`user_id`, `revoked_at`). Refresh tokens rotate on every use:
the old row is revoked and points at its replacement. Presenting an already-rotated
token is treated as theft — the whole chain for that user is revoked.

---

## 13. Relation Summary

```
users (1) ──< workspace_members >── (1) workspaces
workspaces (1) ──< workspace_invitations
workspaces (1) ──< folders
workspaces (1) ──< files
folders (1) ──< folders                (self-referencing, nested)
folders (1) ──< files
files (1) ──< file_versions            (files.current_version_id → file_versions.id, nullable)
files (1) ──< file_permissions
folders (1) ──< file_permissions
files (1) ──< share_links
workspaces (1) ──< audit_logs
users (1) ──< notifications
users (1) ──< refresh_tokens
```

## 14. Future Considerations (deliberately out of v1)

- **Full-text content indexing** for search — would need a separate `file_content_index` entity if added later.
- **`notifications.channel = 'websocket'`** — the enum is already extensible for this; no schema change needed, just a new delivery path in v1.1.
- **Folder-level share links** — see §9; deferred on security-surface grounds, not cost.
- **Multi-recipient private links** — would need a `share_link_recipients` join table.

## 15. Amendment Log

### v1.1 (7 Sep 2026, pre-M0)

Issues found reading v1.0 against `Architecture.md` and `PRD.md`, fixed before M0:

1. **Circular FK made inserts impossible.** `files.current_version_id` was required while `file_versions.file_id` is also required — neither row could be created first. `current_version_id` is now nullable.
2. **`audit_logs.action` could not express a failed access attempt**, which Architecture §4.5 explicitly requires. Enum expanded; `ip_address` / `user_agent` added.
3. **Tags were in the PRD but not the schema.** `files.tags` added.
4. **No invitation path existed** — `workspace_members` assumed the invitee was already a registered user. `workspace_invitations` added.
5. **No soft delete**, so `audit_logs.target_id` would dangle after any deletion. `deleted_at` added to folders, files, file_versions.
6. **Share links could not combine password + expiry.** Type/modifier split documented.
7. **Missing constraints** that the design depends on: file/folder mutual exclusivity, name uniqueness for versioning, one role per user per workspace.
8. **`updated_at` missing** on several tables despite the stated convention.
9. **`scan_skipped` added** to the scan status enums — see Architecture §3 for the VirusTotal size ceiling that requires it.

### v1.2 (8 Sep 2026, start of M1)

Found reading v1.1 against what M1 actually has to build:

1. **Refresh tokens had nowhere to live.** `config.py` defined a 14-day refresh TTL but
   no entity held one, so logout could not revoke. `refresh_tokens` added (§12), with
   rotation and reuse detection.
2. **`workspaces.owner_id` and `workspace_members.role='owner'` were two sources of
   truth** with no stated precedence — and the "cannot remove the last owner" rule only
   makes sense if multiple owners are possible. `workspace_members` is now explicitly
   authoritative; `owner_id` is a maintained pointer, never an access decision.
3. **`workspace_invitations.status` vs `expires_at` was the same double-source problem,
   but security-relevant**: an expired invitation the sweep had not yet reached would
   have been accepted. `expires_at` is now authoritative at validation time.
4. **`users` had no `email_verified_at`.** Column added now so M6 needs no migration.
5. **`audit_logs` (§10) is built in M1, not M6.** The events it exists to record —
   `login`, `login_failed`, `member_added`, `member_role_changed`, `invitation_sent`,
   `invitation_accepted` — are all produced by M1. Deferring meant reopening every M1
   endpoint later. M6 now covers the audit *UI* and notifications, not the table.
6. **`logout` added to the `audit_logs.action` enum** — §10 could record a login but
   not its end, which makes a session timeline impossible to reconstruct.

### v1.3 (8 Sep 2026, start of M2)

Found reading v1.2 against what M2 has to build:

1. **Quota accounting contradicted itself.** `files.size_bytes` is "size of the current
   version", but Architecture §6's reconciliation recomputes usage from the workspace
   storage prefix, which holds *every* retained version. Counting current versions only
   would have made the reconciliation task report drift forever. Quota now explicitly
   counts all retained versions (§7).
2. **Version history was unbounded**, so one file re-uploaded repeatedly could consume a
   whole workspace with no way to reclaim the space. Capped at 10 retained versions per
   file, oldest pruned on overflow (§7).
3. **The upload flow had nowhere to keep its in-flight state.** Architecture §2 creates
   the `file_versions` row before the bytes exist, but nothing held the provider's
   multipart upload id, so `upload-complete` could not finalise and the abandoned-upload
   sweep had nothing to abort. `upload_id` / `upload_started_at` added.
4. **Soft delete had no restore semantics.** `deletion_batch_id` added to `folders`,
   `files` and `file_versions` so restoring a folder revives exactly what that delete
   removed — and not items the user had already thrown away on purpose.
5. **`file_permissions` (§8) had no milestone.** PRD §4 promises Owner/Editor/Viewer
   *per file or folder* and Architecture §4.1 steps 1-2 depend on it, but M1 was
   workspace-level only. Built in M2 alongside the folder tree, since the ancestor walk
   it needs is the same traversal the tree code already performs.
