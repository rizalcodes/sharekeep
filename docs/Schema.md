# Schema: Secure File Sharing Platform — Data Entities

**Scope note:** this document covers **data schema only** — field names, types, required/optional, and relations. Naming/credential conventions live in `Rules.md`; system/workflow design lives in `Architecture.md`.

**Version:** v1.7 — amended 12 September 2026 at the start of M7. Previously v1.6 (start of M6), v1.5 (start of M5), v1.4 (start of M4), v1.3 (start of M2), v1.2 (start of M1) and v1.1 (kickoff, before M0). See §15 for the amendment log.

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
| `tags` | text[] | required, default `{}` | Backs the tag half of PRD §4's "filename/metadata/tag search". **Constrained v1.7:** at most 25 per file, each lowercased, trimmed and 1–40 characters of letters, digits, spaces, `-`, `_` or `.`. A tag is matched exactly by the tag filter and by substring by the typed query (Architecture §4.8). Before v1.7 only the count was limited, so one tag could be arbitrarily long |
| `created_by` | UUID | required | → `users.id` |
| `deleted_at` | datetime | optional | Soft delete |
| `deletion_batch_id` | UUID | optional | **Added v1.3.** See `folders`. |
| `created_at` / `updated_at` | datetime | required | — |

**Constraints:** UNIQUE (`workspace_id`, `folder_id`, `name`) WHERE `deleted_at IS NULL` — this is what makes "re-upload with the same name creates a new version" well-defined. Same NULL-handling caveat as `folders`. GIN index on `tags`. **Trigram GIN index on `name`** (v1.7, `pg_trgm`) — search matches names by substring, which a B-tree cannot serve (Architecture §4.8).

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
| `checksum_sha256` | string | optional | SHA-256, computed by `content_tasks.compute_checksum` on every completed upload — **independently of whether scanning is enabled** (v1.4; it was previously computed inside the scan task, so with scanning off no checksum existed at all). Serves double duty: integrity verification, and the VirusTotal hash lookup (Architecture §3) |
| `uploaded_by` | UUID | required | → `users.id` |
| `scan_status` | enum(`pending`, `scanning`, `clean`, `flagged`, `scan_skipped`, `scan_failed`) | required, default `pending` | Per-version scan result |
| `scan_detail` | jsonb | optional | Scanner response summary (engine hit counts, VT analysis id, or the reason for `scan_skipped`) |
| `scan_override_at` | datetime | optional | **Added v1.4.** When an admin released this version despite its scan status. NULL = no override. |
| `scan_override_by` | UUID | optional | **Added v1.4.** → `users.id`; the admin who released it. |
| `scan_override_reason` | text | optional | **Added v1.4.** The stated justification, required when an override is recorded. |
| `deleted_at` | datetime | optional | Soft delete; set when the parent file is deleted or a single version is pruned |
| `deletion_batch_id` | UUID | optional | **Added v1.3.** See `folders`. |
| `purged_at` | datetime | optional | **Added v1.7.** Set when the retention sweep deleted this version's object from storage, 30 days after `deleted_at`. The row stays for audit; the bytes are gone, and restore refuses any batch containing a purged version (Architecture §6b) |
| `restored_from_version_id` | UUID | optional | **Added v1.7.** → `file_versions.id`. Set when this version was created by rolling back to an earlier one (Architecture §4.9). The content is a server-side copy of that version, so the UI can say so rather than presenting it as a new upload |
| `created_at` | datetime | required | — |

**Constraints:** UNIQUE (`file_id`, `version_number`). An override is either fully present or fully absent: `scan_override_at`, `scan_override_by` and `scan_override_reason` are all NULL, or all non-NULL.

**Scan override (v1.4):** `flagged` and an exhausted-retry `scan_failed` would otherwise be permanent dead ends — the version unreachable by everyone, its owner included, forever (Architecture §3d). A workspace owner or admin can release one specific version, and the release is recorded in these three fields **rather than by changing `scan_status`**. Marking it `clean` would assert a re-scan that never happened; the record has to keep saying "this was flagged, and a named person chose to release it anyway". Downloadability is therefore a function of the version — status *and* override — not a property of the status enum alone.

The override lives only on the version, not mirrored onto `files`. A listing that needs to show "flagged, released by X" joins the current version rather than duplicating the fields, so there is one source of truth for whether access was granted.

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

Explicit permission grants for a file or folder to a specific workspace member, overriding the workspace-level default role where present. **"Member" is enforced from v1.7**: until then the grant route accepted any user. Grants on files and on folders can each be listed and revoked — before v1.7 a folder grant, once created, could not be removed at all. Resolution order and inheritance are defined in Architecture §4.

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
| `token_hash` | string | required, unique | SHA-256 of a cryptographically random token, never derived from `file_id`. **Changed from a raw `token` column in v1.5.** The raw value is returned once at creation and never stored, exactly as M1 already does for refresh tokens and invitations — a leaked database or backup then yields no usable share link. The consequence is deliberate and surfaced in the UI: a link cannot be copied again later; recovering it means issuing a new one and revoking the old. |
| `link_type` | enum(`public`, `private`, `password_protected`, `expiring`) | required | Primary access rule |
| `hashed_password` | string | optional | **Required** if `link_type = password_protected`; **allowed** on any other type as a modifier |
| `allowed_user_id` | UUID | optional | → `users.id`; required if `link_type = private`. v1 supports a single allowed user per link — create multiple links for multiple recipients |
| `expires_at` | datetime | optional | **Required** if `link_type = expiring`; **allowed** on any other type as a modifier |
| `max_downloads` | integer | optional | Modifier, valid on any type |
| `download_count` | integer | required, default 0 | Counts **link resolutions**, not completed downloads — the bytes are served straight from storage, so a finished download is not observable here (Architecture §4.2). Incremented under a row lock. |
| `created_by` | UUID | required | → `users.id` |
| `revoked_at` | datetime | optional | Manual revocation before expiry |
| `created_at` / `updated_at` | datetime | required | — |

**Constraints:** CHECK enforcing the three type requirements above (`password_protected` ⇒ `hashed_password IS NOT NULL`; `expiring` ⇒ `expires_at IS NOT NULL`; `private` ⇒ `allowed_user_id IS NOT NULL`), UNIQUE (`token_hash`), and CHECK (`max_downloads IS NULL OR max_downloads > 0`) — a link created with a zero limit would be dead on arrival, which is a mistake worth refusing rather than storing.

The CHECK enforces the type *requirements* only. It must not forbid the modifiers on other types: `public` with `expires_at`, or `expiring` with a password, are both valid and are the combinations the v1.1 clarification above exists to permit.

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

**`action` enum (expanded in v1.1):** `upload`, `download`, `share_created`, `share_revoked`, `share_accessed`, `share_access_denied`, `delete`, `permission_changed`, `version_restored`, `folder_created`, `folder_deleted`, `login`, `login_failed`, `member_added`, `member_removed`, `member_role_changed`, `invitation_sent`, `invitation_accepted`, `invitation_revoked`, `logout`, `scan_flagged`, `scan_override`, `quota_exceeded`.

The `share_accessed` / `share_access_denied` pair exists because Architecture §4.5 requires logging every resolution attempt, success **and** failure — the original enum had no way to express a failed attempt, which is precisely the event a security audit log exists to capture.

Audit logs are append-only: no update or delete path in the application.

**`version_restored` means restored from the trash**, despite its name — it is what M2's trash restore has always written. Rolling a file back to an earlier version is a different act and, from v1.7, writes `version_rolled_back`. The old name stays because the log is append-only and existing rows cannot be relabelled; the reader phrases it as "restored from the trash".
**`metadata` carries a human-readable label for the target (convention added v1.6).**
Every write denormalises the target's name — `label` for the thing acted on, plus whatever
else the action needs. Resolving `target_id` to a name at read time instead would make an
append-only table mutable in effect: renaming a file would rewrite what this log says was
downloaded last week. Rows written in M1–M5 are inconsistent here — `download` stored
`file_name`, `delete` stored `name`, `upload` stored neither — and being append-only, they
cannot be corrected. The reader states that the name was not recorded rather than
inventing one or looking it up live (Architecture §4.7).

**Who may read it:** workspace owners only, and account-level events (`workspace_id =
NULL`) appear in no workspace log at all. Specified in Architecture §4.7 at M6, since §4.6
covered administration and never mentioned reading.

## 11. `notifications`

Designed to be channel-agnostic (WebSocket-ready for v1.1) even though v1 only sends
email. Built at M6; the delivery rules are Architecture §5b.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | UUID | required | Primary key |
| `user_id` | UUID | optional | → `users.id`, the recipient. **Nullable since v1.6** — a workspace invitation is addressed to someone who may have no account yet, which is the whole point of inviting them |
| `email` | string | required | **Added v1.6.** The address actually addressed. Resolved from `user_id` when there is one, and the invited address when there is not. Stored rather than derived so the log records where the mail was *sent*, not where it would be sent today — the same append-only reasoning as `audit_logs` labels (§10) |
| `workspace_id` | UUID | optional | → `workspaces.id`; context for rendering and future per-workspace filtering |
| `type` | enum(`file_shared`, `permission_granted`, `storage_quota_warning`, `scan_flagged`, `workspace_invitation`) | required | Recipients per event: Architecture §5a |
| `payload` | jsonb | required | Structured data for rendering the notification (e.g. file name, actor). Denormalised at queue time for the same reason audit labels are |
| `channel` | enum(`email`) | required, default `email` | Extensible for `websocket` in v1.1 without a schema change |
| `status` | enum(`pending`, `sent`, `failed`) | required, default `pending` | `sent` means **the provider accepted it** — not that it was delivered. The restricted send-only API key cannot read a message's status back at all (Architecture §5b) |
| `provider_message_id` | string | optional | **Added v1.6.** Resend's id for the accepted message. Exists precisely because our own record cannot answer a delivery question — a human can, from the provider's dashboard, with this |
| `attempts` | integer | required, default 0 | **Added v1.6.** Incremented on every delivery attempt. On the row rather than only inside the Celery retry, so a worker restart does not lose the count and the sweep can still find the row |
| `last_error` | string | optional | **Added v1.6.** The provider's own wording for the most recent failure, not a paraphrase. A permanent refusal is recorded and never retried |
| `sent_at` | datetime | optional | **Added v1.6.** Set when the provider accepts. Also the **idempotency guard**: a row that has one is never sent again, claimed under a row lock. Without it, a task that succeeded and then died before committing would send a duplicate — and an email cannot be recalled |
| `redirected_to` | string | optional | **Added v1.6.** Set when the sandbox redirect (Architecture §5c) diverted this message, recording where it actually went. A redirected send must never be mistaken for a real one |
| `read_at` | datetime | optional | For future in-app read-state tracking |
| `created_at` / `updated_at` | datetime | required | — |

**Constraints:** index on (`status`, `created_at`) — the delivery task's only query is
"oldest pending rows first". Index on (`user_id`, `created_at`) for a future in-app list.
`user_id` is `ON DELETE CASCADE`: a deleted user's pending mail should not be sent.

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

## 12a. `scan_results` (added v1.4)

Verdict cache keyed on file content, so a hash VirusTotal has already answered for costs
zero further requests (Architecture §3c).

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | UUID | required | Primary key |
| `checksum_sha256` | string | required | The file content hash this verdict is about |
| `verdict` | enum(`clean`, `flagged`) | required | Only settled verdicts are cached; `pending`/`scanning`/`scan_failed` are transient and caching them would serve a non-answer |
| `detail` | jsonb | optional | Engine hit summary and the VT analysis id, copied to each version that uses this verdict |
| `checked_at` | datetime | required | When VirusTotal was asked. An entry older than `scan_cache_ttl_days` (default 30) is treated as absent and re-looked-up — a file clean today may be flagged next month |
| `created_at` | datetime | required | — |
| `updated_at` | datetime | required | Refreshed when a re-lookup replaces a stale verdict |

**Constraints:** UNIQUE (`checksum_sha256`) — one row per hash, updated in place on
re-lookup rather than accumulating history.

**Why a dedicated table rather than querying `file_versions.checksum_sha256`:** version
rows are pruned by the 10-version retention cap and hard-deleted along with their files,
so they disappear exactly when the cache would be most useful. A cache also needs
`checked_at` to expire on its own terms, which a version row has no place to store.

This table holds **no** workspace or user reference. A hash verdict is a fact about file
content, not about who uploaded it, and scoping it per workspace would multiply requests
against a 500/day budget for no benefit. It is therefore not workspace-isolated data and
must never be exposed through a workspace-scoped API — it is worker-internal.

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
users (1) ──< notifications          (user_id nullable: an invitation precedes the account)
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

### v1.4 (10 Sep 2026, start of M4)

1. **`scan_results` added (§12a).** A verdict cache keyed on SHA-256. VirusTotal's free
   tier allows ~500 requests/day, so re-asking about content we have already asked about
   is the easiest budget to waste: two users uploading the same PDF should cost one
   request, not two. Not derivable from `file_versions.checksum_sha256`, because those
   rows are pruned by the 10-version cap and hard-deleted with their files — they vanish
   exactly when the cache would pay off — and because a cache needs its own `checked_at`
   to expire on.
2. **`scan_override_at` / `scan_override_by` / `scan_override_reason` added to
   `file_versions` (§7).** `flagged` and an exhausted-retry `scan_failed` had no exit in
   the design: the version was unreachable by everyone including its owner, permanently.
   The release is recorded in these fields instead of by moving `scan_status` to `clean`,
   because nothing re-scanned the file — claiming otherwise in the record would be a
   fabricated verdict.
3. **`scan_override` added to the `audit_logs` action enum (§10).** Releasing a file the
   scanner flagged is exactly the kind of act an audit log exists for.
4. **`checksum_sha256` decoupled from scanning (§7).** It was described as computed by the
   worker on upload finalize, and Architecture §3 put that work inside the scan task —
   so with `scanning_enabled=false` the integrity checksum was never computed at all.
   Found by reading the two documents against each other at the start of M4, not by
   running anything.

### v1.5 (11 Sep 2026, start of M5)

1. **`share_links.token` became `token_hash` (§9).** The column was specified to hold the
   raw token. Every other bearer token in this system — refresh tokens since M1,
   invitation tokens since M1 — is stored as SHA-256 with the raw value shown once, and
   there was no reason for the most widely distributed token of the three to be the
   exception. A leaked database or backup would have handed over a working link to every
   file ever shared. The trade-off it forces (no re-copying a link later) is recorded in
   §9 and in Architecture §4.2 rather than left for a user to discover.
2. **`download_count` documented as counting resolutions, not downloads (§9).** The API
   never proxies file bytes, so a completed download is not an event this system can see.
   Naming it accurately in the schema keeps the UI from implying a guarantee the
   architecture cannot make.
3. **Constraints tightened and clarified (§9).** UNIQUE on `token_hash`, a CHECK
   rejecting `max_downloads = 0` (a link dead on creation is a mistake, not a state), and
   an explicit note that the type CHECK must not forbid modifiers on other types — the
   exact reading the v1.1 clarification was written to prevent.

### v1.6 (11 Sep 2026, start of M6)

1. **`notifications` gained delivery state: `attempts`, `last_error`, `sent_at`,
   `provider_message_id`, `redirected_to`.** The original table could record *that* a
   notification existed and a three-value status, but nothing about the attempt — so a
   transient failure was indistinguishable from a permanent one, a worker restart lost the
   retry count, and there was no way to tell a duplicate send from a first one. `sent_at`
   doubles as the idempotency guard, which matters more here than elsewhere because an
   email that has gone out cannot be recalled.
2. **`status = sent` was defined down to what is knowable.** The Resend key in use is
   restricted to sending: `GET /emails/{id}` returns 401. We cannot observe delivery, so
   `sent` means the provider accepted the message and `provider_message_id` is stored for
   a human to follow up with. The original field implied a delivery guarantee the
   architecture cannot provide — the same overstatement §9's "uses, not downloads" was
   written to avoid.
3. **`notifications.user_id` became optional, and `email` was added.** A workspace
   invitation is addressed to someone who by definition may not have an account yet, so a
   required `user_id` made the pipeline unable to carry the one notification that M1 had
   explicitly deferred to M6. The address is stored rather than derived, so the row records
   where the mail was sent rather than where it would be sent today.
4. **`audit_logs.metadata` must carry a `label` for its target (§10).** The log is
   append-only, so resolving `target_id` to a name at read time would let a rename rewrite
   history. M1–M5 wrote inconsistent metadata (`file_name`, `name`, or nothing) and cannot
   be corrected in place; the convention applies going forward and the reader is honest
   about older rows.

No table was altered destructively: every added column is nullable or defaulted, so
migration 0006 creates `notifications` and touches nothing that exists.

### v1.7 (12 Sep 2026, start of M7)

1. **`file_versions.restored_from_version_id`** (nullable, → `file_versions.id`). Set
   on a version created by rolling back to an earlier one (Architecture §4.9), so the
   history can say "restored from version 3" instead of presenting an identical upload.
2. **`audit_action` gains `version_rolled_back`.** The existing `version_restored` has
   always meant restore-from-trash; reusing it for rollback would make one action mean
   two things in a log whose value is being unambiguous.
3. **Tags are constrained per tag**, not only by count.
4. **Trigram index on `files.name`** for substring search.
5. **Grants are member-only in fact, not just in this document.**
6. **`file_versions.purged_at`**, for the retention sweep that Architecture §6a
   described and nothing implemented.
