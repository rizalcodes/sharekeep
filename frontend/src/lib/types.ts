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
