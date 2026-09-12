"""Search over names, tags and metadata (Architecture.md §4.8).

Scoped to one workspace and to members, and that is the whole permission story: roles are
only viewer, editor and owner, so a grant can narrow a member's role on an item but never
hide it — every member resolves to at least viewer on every file (§4.1). If a "no access"
role is ever added, this module is the first thing that must change.

Two rules decided at M7b are implemented here rather than in the router, so a second
caller cannot implement them differently:

- The typed query matches a file's **name or any of its tags**; the tag *filter* stays
  exact. The query is the forgiving way in, the filter the precise one.
- Folders leave the results as soon as a file-only filter is used, because a folder has no
  type, size or scan status and so can neither pass nor fail those filters.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import String, and_, case, cast, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.content import File, Folder
from app.models.enums import ScanStatus
from app.services import scan

MAX_QUERY = 200
MAX_LIMIT = 100

# One mapping, used for both the filter and the label on a result, so the two cannot
# disagree about what a file is (§4.8). Order matters: the first match wins.
CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("image", ("image/",)),
    ("video", ("video/",)),
    ("audio", ("audio/",)),
    (
        "spreadsheet",
        (
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml",
            "application/vnd.oasis.opendocument.spreadsheet",
            "text/csv",
        ),
    ),
    (
        "presentation",
        (
            "application/vnd.ms-powerpoint",
            "application/vnd.openxmlformats-officedocument.presentationml",
            "application/vnd.oasis.opendocument.presentation",
        ),
    ),
    (
        "archive",
        (
            "application/zip",
            "application/x-tar",
            "application/gzip",
            "application/x-7z-compressed",
            "application/vnd.rar",
            "application/x-bzip",
        ),
    ),
    (
        "document",
        (
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml",
            "application/vnd.oasis.opendocument.text",
            "application/rtf",
            "text/",
        ),
    ),
)
CATEGORY_NAMES = tuple(name for name, _ in CATEGORIES) + ("other",)


def category_of(mime_type: str) -> str:
    """The category a mime type belongs to, for labelling a result."""
    value = (mime_type or "").lower()
    for name, prefixes in CATEGORIES:
        if any(value.startswith(prefix) for prefix in prefixes):
            return name
    return "other"


def _category_expression():
    """The same mapping as SQL, so filtering and labelling agree by construction."""
    lowered = func.lower(File.mime_type)
    branches = [
        (or_(*[lowered.startswith(prefix) for prefix in prefixes]), literal(name))
        for name, prefixes in CATEGORIES
    ]
    return case(*branches, else_=literal("other"))


def escape_like(term: str) -> str:
    r"""Escape a user's `%`, `_` and `\` so they are searched for, not interpreted.

    Someone typing `50%` is looking for those characters, not asking the database to
    match everything (§4.8).
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass(frozen=True)
class Filters:
    query: str = ""
    tags: tuple[str, ...] = ()
    category: str | None = None
    size_min: int | None = None
    size_max: int | None = None
    modified_after: datetime | None = None
    modified_before: datetime | None = None
    uploaded_by: uuid.UUID | None = None
    scan_status: ScanStatus | None = None

    @property
    def file_only(self) -> bool:
        """Whether a filter is active that a folder cannot be judged by (§4.8)."""
        return any(
            value is not None
            for value in (
                self.category,
                self.size_min,
                self.size_max,
                self.uploaded_by,
                self.scan_status,
            )
        ) or bool(self.tags)

    @property
    def any_active(self) -> bool:
        return bool(self.query) or self.file_only or bool(
            self.modified_after or self.modified_before
        )


@dataclass
class Hit:
    kind: str  # "file" or "folder"
    id: uuid.UUID
    name: str
    rank: int
    updated_at: datetime
    path: list[dict] = field(default_factory=list)
    size_bytes: int | None = None
    mime_type: str | None = None
    category: str | None = None
    scan_status: ScanStatus | None = None
    is_downloadable: bool = False
    tags: list[str] = field(default_factory=list)
    folder_id: uuid.UUID | None = None


def encode_cursor(hit: Hit) -> str:
    raw = f"{hit.rank}|{hit.updated_at.isoformat()}|{hit.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[int, datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        rank, stamp, row_id = raw.split("|", 2)
        return int(rank), datetime.fromisoformat(stamp), uuid.UUID(row_id)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("That page cursor is not valid.") from exc


def _rank_expression(name_column, query: str, tag_column=None):
    """Relevance: an exact name, then a prefix, then anywhere in the name, then a tag.

    Ranked in SQL rather than in Python because the cursor pages on it: a rank computed
    after the fact could not be part of a keyset condition.
    """
    term = escape_like(query)
    branches = [
        (func.lower(name_column) == query.lower(), literal(0)),
        # ILIKE on the column, not `lower(name) LIKE`: the trigram index (migration
        # 0008) is built on `name`, so the expression has to be one it covers. The
        # first version of this used lower(name) and quietly defeated its own index.
        (name_column.ilike(f"{term}%", escape="\\"), literal(1)),
        (name_column.ilike(f"%{term}%", escape="\\"), literal(2)),
    ]
    if tag_column is not None:
        branches.append(
            (
                func.array_to_string(tag_column, " ").ilike(f"%{term}%", escape="\\"),
                literal(3),
            )
        )
    return case(*branches, else_=literal(9))


def _keyset(rank_expr, updated_column, id_column, cursor: tuple[int, datetime, uuid.UUID]):
    rank, stamp, row_id = cursor
    return or_(
        rank_expr > rank,
        and_(
            rank_expr == rank,
            or_(
                updated_column < stamp,
                and_(updated_column == stamp, id_column < row_id),
            ),
        ),
    )


async def _folder_paths(db: AsyncSession, workspace_id: uuid.UUID) -> dict:
    """Every live folder's (name, parent), so a result can carry its location.

    One query for the workspace's folder tree, then paths are assembled in memory:
    a recursive walk per result would be a query per row on the page a reader scans.
    """
    rows = await db.execute(
        select(Folder.id, Folder.name, Folder.parent_folder_id).where(
            Folder.workspace_id == workspace_id, Folder.deleted_at.is_(None)
        )
    )
    return {row.id: (row.name, row.parent_folder_id) for row in rows.all()}


def _path_for(folder_id: uuid.UUID | None, folders: dict) -> list[dict]:
    trail: list[dict] = []
    seen: set[uuid.UUID] = set()
    current = folder_id
    while current is not None and current in folders and current not in seen:
        seen.add(current)
        name, parent = folders[current]
        trail.append({"id": str(current), "name": name})
        current = parent
    trail.reverse()
    return trail


async def run(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    filters: Filters,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[Hit], str | None, bool]:
    """Returns (hits, next_cursor, folders_excluded)."""
    position = decode_cursor(cursor) if cursor else None
    query = filters.query.strip()[:MAX_QUERY]
    term = escape_like(query)

    # --- files ---
    file_rank = (
        _rank_expression(File.name, query, File.tags) if query else literal(0)
    )
    files_stmt = (
        select(File, file_rank.label("rank"), _category_expression().label("category"))
        .where(
            File.workspace_id == workspace_id,
            File.deleted_at.is_(None),
            # An upload still in flight is not a file yet (§4.8).
            File.current_version_id.is_not(None),
        )
        .order_by(file_rank, File.updated_at.desc(), File.id.desc())
        .limit(limit + 1)
    )
    if query:
        files_stmt = files_stmt.where(
            or_(
                File.name.ilike(f"%{term}%", escape="\\"),
                # Tags have their own GIN index, but it serves exact membership, not a
                # substring — so this part is a scan of the workspace's files. Acceptable
                # at this scale, and the alternative (a second trigram index over a
                # derived string) would need a materialised column to be worth it.
                func.array_to_string(File.tags, " ").ilike(f"%{term}%", escape="\\"),
            )
        )
    for tag in filters.tags:
        # AND across tags: each one narrows (§4.8).
        files_stmt = files_stmt.where(File.tags.any(tag))
    if filters.category:
        files_stmt = files_stmt.where(_category_expression() == filters.category)
    if filters.size_min is not None:
        files_stmt = files_stmt.where(File.size_bytes >= filters.size_min)
    if filters.size_max is not None:
        files_stmt = files_stmt.where(File.size_bytes <= filters.size_max)
    if filters.modified_after is not None:
        files_stmt = files_stmt.where(File.updated_at >= filters.modified_after)
    if filters.modified_before is not None:
        files_stmt = files_stmt.where(File.updated_at <= filters.modified_before)
    if filters.uploaded_by is not None:
        files_stmt = files_stmt.where(File.created_by == filters.uploaded_by)
    if filters.scan_status is not None:
        files_stmt = files_stmt.where(File.scan_status == filters.scan_status)
    if position:
        files_stmt = files_stmt.where(
            _keyset(file_rank, File.updated_at, File.id, position)
        )

    file_rows = (await db.execute(files_stmt)).all()

    # --- folders ---
    folders_excluded = filters.file_only
    folder_rows: list = []
    if not folders_excluded:
        folder_rank = _rank_expression(Folder.name, query) if query else literal(0)
        folders_stmt = (
            select(Folder, folder_rank.label("rank"))
            .where(Folder.workspace_id == workspace_id, Folder.deleted_at.is_(None))
            .order_by(folder_rank, Folder.updated_at.desc(), Folder.id.desc())
            .limit(limit + 1)
        )
        if query:
            folders_stmt = folders_stmt.where(
                Folder.name.ilike(f"%{term}%", escape="\\")
            )
        if filters.modified_after is not None:
            folders_stmt = folders_stmt.where(Folder.updated_at >= filters.modified_after)
        if filters.modified_before is not None:
            folders_stmt = folders_stmt.where(Folder.updated_at <= filters.modified_before)
        if position:
            folders_stmt = folders_stmt.where(
                _keyset(folder_rank, Folder.updated_at, Folder.id, position)
            )
        folder_rows = (await db.execute(folders_stmt)).all()

    folders = await _folder_paths(db, workspace_id)
    versions = await scan.presentation_for(db, [row[0] for row in file_rows])

    hits: list[Hit] = []
    for file, rank, category in file_rows:
        facts = versions.get(file.id, {})
        hits.append(
            Hit(
                kind="file",
                id=file.id,
                name=file.name,
                rank=int(rank),
                updated_at=file.updated_at,
                path=_path_for(file.folder_id, folders),
                size_bytes=file.size_bytes,
                mime_type=file.mime_type,
                category=category,
                scan_status=file.scan_status,
                is_downloadable=bool(facts.get("is_downloadable", False)),
                tags=list(file.tags or []),
                folder_id=file.folder_id,
            )
        )
    for folder, rank in folder_rows:
        hits.append(
            Hit(
                kind="folder",
                id=folder.id,
                name=folder.name,
                rank=int(rank),
                updated_at=folder.updated_at,
                path=_path_for(folder.parent_folder_id, folders),
                folder_id=folder.parent_folder_id,
            )
        )

    # Files and folders are queried separately, so the merged page is ordered here on
    # the same keys the cursor uses.
    hits.sort(key=lambda hit: (hit.rank, -hit.updated_at.timestamp(), str(hit.id)))
    page = hits[:limit]
    next_cursor = encode_cursor(page[-1]) if len(hits) > limit and page else None
    return page, next_cursor, folders_excluded


async def tag_counts(db: AsyncSession, workspace_id: uuid.UUID) -> list[dict]:
    """Every tag in use in the workspace, with how many files carry it.

    The chip list needs this: offering a free-text tag filter over a set nobody can see
    would make the precise filter unusable in practice.
    """
    tag = func.unnest(File.tags).label("tag")
    subquery = (
        select(tag)
        .where(
            File.workspace_id == workspace_id,
            File.deleted_at.is_(None),
            File.current_version_id.is_not(None),
        )
        .subquery()
    )
    rows = await db.execute(
        select(cast(subquery.c.tag, String).label("tag"), func.count().label("files"))
        .group_by(subquery.c.tag)
        .order_by(func.count().desc(), subquery.c.tag)
    )
    return [{"tag": row.tag, "files": int(row.files)} for row in rows.all()]
