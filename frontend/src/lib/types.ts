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
};

export type FolderListing = {
  folder: FolderOut | null;
  breadcrumb: { id: string; name: string }[];
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
