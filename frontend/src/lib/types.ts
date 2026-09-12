export type WorkspaceRole = "owner" | "editor" | "viewer";

export type ScanStatus =
  | "pending"
  | "scanning"
  | "clean"
  | "flagged"
  | "scan_skipped"
  | "scan_failed";

export type User = {
  id: string;
  email: string;
  full_name: string;
  email_verified_at: string | null;
  created_at: string;
};

export type WorkspaceSummary = {
  id: string;
  name: string;
  is_personal: boolean;
  role: WorkspaceRole;
};

export type Session = {
  user: User;
  workspaces: WorkspaceSummary[];
  access_token_expires_at: string | null;
};

export type FolderOut = {
  id: string;
  workspace_id: string;
  parent_folder_id: string | null;
  name: string;
  created_by: string;
  created_at: string;
  /** The viewer's own role on this folder, resolved server-side (Design.md §5). */
  my_role?: WorkspaceRole | null;
  explicit_grants?: number;
};

export type FileOut = {
  id: string;
  workspace_id: string;
  folder_id: string | null;
  name: string;
  mime_type: string;
  size_bytes: number;
  scan_status: ScanStatus;
  /**
   * Resolved by the server (Architecture.md §3). The client deliberately does not
   * re-derive the access table: two implementations of one security rule drift, and
   * the one in the browser is the one that cannot be trusted anyway.
   */
  is_downloadable: boolean;
  /** Why a "not scanned" badge says what it says (Design.md §5). */
  scan_reason: string | null;
  /** Present when a workspace owner released the file despite its scan status. */
  scan_override_at: string | null;
  scan_override_by: string | null;
  scan_override_reason: string | null;
  tags: string[];
  created_by: string;
  created_at: string;
  updated_at: string;
  /** Permission indicator data (Design.md §5, M7), present in a listing. */
  my_role?: WorkspaceRole | null;
  active_share_links?: number;
  explicit_grants?: number;
};

export type FolderListing = {
  folder: FolderOut | null;
  breadcrumb: { id: string; name: string }[];
  /** The viewer's role in this folder (M7); gates upload and new folder. */
  my_role?: WorkspaceRole | null;
  folders: FolderOut[];
  files: FileOut[];
};

export type Quota = {
  storage_quota_bytes: number;
  storage_used_bytes: number;
  storage_available_bytes: number;
  max_file_size_bytes: number;
};

export type UploadPart = {
  part_number: number;
  size_bytes: number;
  url: string;
};

export type UploadInitResponse = {
  file_id: string;
  version_id: string;
  version_number: number;
  parts: UploadPart[];
  is_new_file: boolean;
};

export type CompletedPart = { part_number: number; etag: string };

export type InFlightUpload = {
  version_id: string;
  file_id: string;
  file_name: string;
  folder_id: string | null;
  total_bytes: number;
  uploaded_bytes: number;
  parts_completed: number;
  parts_total: number;
  started_at: string;
  version_number: number;
};

export type PartLayout = {
  part_number: number;
  offset: number;
  size_bytes: number;
};

export type ResumeState = {
  version_id: string;
  file_id: string;
  file_name: string;
  total_bytes: number;
  completed_parts: CompletedPart[];
  remaining_parts: UploadPart[];
  /** Byte offset and size of EVERY part. Never infer these client-side. */
  layout: PartLayout[];
  uploaded_bytes: number;
};

// --- Share links (Architecture.md §4.2, added at M5) ------------------------

export type ShareLinkType =
  | "public"
  | "private"
  | "password_protected"
  | "expiring";

/**
 * A link as its owner sees it. Note the absence of a token: it is stored hashed, so
 * there is nothing the server could send back here even if the UI asked.
 */
export type ShareLinkOut = {
  id: string;
  file_id: string;
  link_type: ShareLinkType;
  has_password: boolean;
  allowed_user_id: string | null;
  expires_at: string | null;
  max_downloads: number | null;
  /** Redemptions, not completed downloads — the API never sees the bytes. */
  uses: number;
  revoked_at: string | null;
  is_active: boolean;
  created_by: string;
  created_at: string;
};

/** The only response that carries a raw token, and only once. */
export type ShareLinkCreated = ShareLinkOut & {
  token: string;
  url_path: string;
};

export type SharePreview = {
  requires_password: boolean;
  requires_sign_in: boolean;
  expires_at: string | null;
  /** Withheld while a password gate remains, deliberately. */
  file_name: string | null;
  size_bytes: number | null;
};

export type ShareDownload = {
  url: string;
  expires_in_seconds: number;
  file_name: string;
  size_bytes: number;
  uses_remaining: number | null;
};

// --- Activity log (Architecture.md §4.7) ------------------------------------

export type AuditAction =
  | "upload"
  | "download"
  | "share_created"
  | "share_revoked"
  | "share_accessed"
  | "share_access_denied"
  | "delete"
  | "permission_changed"
  | "version_restored"
  | "version_rolled_back"
  | "folder_created"
  | "folder_deleted"
  | "login"
  | "login_failed"
  | "logout"
  | "member_added"
  | "member_removed"
  | "member_role_changed"
  | "invitation_sent"
  | "invitation_accepted"
  | "invitation_revoked"
  | "scan_flagged"
  | "scan_override"
  | "quota_exceeded";

export type AuditEntry = {
  id: string;
  action: AuditAction;
  target_type: string;
  target_id: string | null;
  /**
   * The target's name **as recorded at the time**, not as it is now. Null for rows
   * written before M6 established the convention — the log is append-only, so those
   * cannot be backfilled and the interface must say so rather than invent a name.
   */
  label: string | null;
  actor_id: string | null;
  actor_email: string | null;
  actor_name: string | null;
  ip_address: string | null;
  user_agent: string | null;
  metadata: Record<string, unknown> | null;
  created_at: string;
};

export type AuditPage = {
  entries: AuditEntry[];
  next_cursor: string | null;
  /** Sign-ins belong to the account, not the workspace, so they are never here. */
  excludes_account_events: boolean;
};

// --- Invitations (Architecture.md §4.6) ---------------------------------------

/** What a token holder may learn before signing in: nothing about members or files. */
export type InvitationPreview = {
  workspace_name: string;
  email: string;
  role: WorkspaceRole;
  expires_at: string;
  requires_signup: boolean;
};

// --- M7a: the rest of the interface ---------------------------------------------

export type WorkspaceOut = {
  id: string;
  name: string;
  owner_id: string;
  is_personal: boolean;
  storage_quota_bytes: number;
  storage_used_bytes: number;
  created_at: string;
  role: WorkspaceRole;
};

export type VersionOut = {
  id: string;
  version_number: number;
  size_bytes: number;
  checksum_sha256: string | null;
  scan_status: ScanStatus;
  scan_override_at: string | null;
  scan_override_by: string | null;
  scan_override_reason: string | null;
  is_downloadable: boolean;
  uploaded_by: string;
  uploaded_by_email: string | null;
  uploaded_by_name: string | null;
  /** Set when this version was created by rolling back (Architecture.md §4.9). */
  restored_from_version_number: number | null;
  created_at: string;
  is_current: boolean;
};

export type DownloadResponse = {
  url: string;
  expires_in_seconds: number;
  scan_status: ScanStatus;
};

export type DeletionReceipt = {
  deletion_batch_id: string;
  items_deleted: number;
  bytes_released: number;
};

export type Grant = {
  id: string;
  file_id: string | null;
  folder_id: string | null;
  user_id: string;
  email: string | null;
  role: WorkspaceRole;
  granted_by: string;
  created_at: string;
};

export type Member = {
  id: string;
  user_id: string;
  email: string;
  full_name: string;
  role: WorkspaceRole;
  created_at: string;
};

export type InvitationStatus = "pending" | "accepted" | "revoked" | "expired";

export type Invitation = {
  id: string;
  workspace_id: string;
  email: string;
  role: WorkspaceRole;
  status: InvitationStatus;
  invited_by: string;
  expires_at: string;
  created_at: string;
};

export type TrashEntry = {
  deletion_batch_id: string;
  /** Also names the restore endpoint: folders/restore or files/restore. */
  kind: "folder" | "file";
  name: string;
  folders: number;
  files: number;
  size_bytes: number;
  deleted_at: string;
  deleted_by_email: string | null;
  purge_after: string;
  restorable: boolean;
  blocked_reason: string | null;
};

export type TrashOut = { entries: TrashEntry[]; retention_days: number };

// --- M7b: search (Architecture.md §4.8) ----------------------------------------

export type FileCategory =
  | "image"
  | "video"
  | "audio"
  | "spreadsheet"
  | "presentation"
  | "archive"
  | "document"
  | "other";

export type SearchHit = {
  kind: "file" | "folder";
  id: string;
  name: string;
  /** Where it lives, so a result never has to be opened to find out. */
  path: { id: string; name: string }[];
  updated_at: string;
  size_bytes: number | null;
  mime_type: string | null;
  category: FileCategory | null;
  scan_status: ScanStatus | null;
  is_downloadable: boolean;
  tags: string[];
  folder_id: string | null;
};

export type SearchOut = {
  results: SearchHit[];
  next_cursor: string | null;
  /** True when a file-only filter is active, so folders could not be judged. */
  folders_excluded: boolean;
  categories: FileCategory[];
};

export type TagCount = { tag: string; files: number };

export type SearchParams = {
  q?: string;
  tag?: string[];
  category?: string | null;
  size_min?: number | null;
  size_max?: number | null;
  modified_after?: string | null;
  modified_before?: string | null;
  uploaded_by?: string | null;
  scan_status?: ScanStatus | null;
  limit?: number;
  cursor?: string | null;
};
