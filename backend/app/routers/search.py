"""Search (Architecture.md §4.8).

Members only, scoped to the workspace in the path — the same dependency every workspace
endpoint uses, which §4.8 explains is sufficient here rather than leaving it implied.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from app.core.config import settings
from app.core.dependencies import DbSession, ViewerAccess
from app.models.enums import ScanStatus
from app.services import search

router = APIRouter(prefix=f"{settings.api_v1_prefix}/workspaces", tags=["search"])


class PathEntry(BaseModel):
    id: str
    name: str


class SearchHit(BaseModel):
    kind: str
    id: uuid.UUID
    name: str
    path: list[PathEntry]
    updated_at: datetime
    size_bytes: int | None = None
    mime_type: str | None = None
    category: str | None = None
    scan_status: ScanStatus | None = None
    is_downloadable: bool = False
    tags: list[str] = []
    folder_id: uuid.UUID | None = None


class SearchOut(BaseModel):
    results: list[SearchHit]
    next_cursor: str | None
    folders_excluded: bool
    categories: list[str]


class TagCount(BaseModel):
    tag: str
    files: int


@router.get("/{workspace_id}/search", response_model=SearchOut)
async def search_workspace(
    access: ViewerAccess,
    db: DbSession,
    q: Annotated[str, Query(max_length=search.MAX_QUERY)] = "",
    tag: Annotated[list[str] | None, Query()] = None,
    category: str | None = None,
    size_min: int | None = None,
    size_max: int | None = None,
    modified_after: datetime | None = None,
    modified_before: datetime | None = None,
    uploaded_by: uuid.UUID | None = None,
    scan_status: ScanStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=search.MAX_LIMIT)] = 50,
    cursor: str | None = None,
) -> SearchOut:
    """Names, tags and the five metadata filters, combinable (§4.8)."""
    workspace, _ = access
    if category is not None and category not in search.CATEGORY_NAMES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unknown file type. Choose one of: {', '.join(search.CATEGORY_NAMES)}.",
        )

    filters = search.Filters(
        query=q,
        tags=tuple(t.strip().lower() for t in (tag or []) if t.strip()),
        category=category,
        size_min=size_min,
        size_max=size_max,
        modified_after=modified_after,
        modified_before=modified_before,
        uploaded_by=uploaded_by,
        scan_status=scan_status,
    )
    try:
        hits, next_cursor, folders_excluded = await search.run(
            db, workspace_id=workspace.id, filters=filters, limit=limit, cursor=cursor
        )
    except ValueError as exc:
        # A malformed cursor is the caller's mistake, not an empty page: returning the
        # first page again would hide a paging bug instead of surfacing it.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    return SearchOut(
        results=[
            SearchHit(
                kind=hit.kind,
                id=hit.id,
                name=hit.name,
                path=[PathEntry(**entry) for entry in hit.path],
                updated_at=hit.updated_at,
                size_bytes=hit.size_bytes,
                mime_type=hit.mime_type,
                category=hit.category,
                scan_status=hit.scan_status,
                is_downloadable=hit.is_downloadable,
                tags=hit.tags,
                folder_id=hit.folder_id,
            )
            for hit in hits
        ],
        next_cursor=next_cursor,
        folders_excluded=folders_excluded,
        categories=list(search.CATEGORY_NAMES),
    )


@router.get("/{workspace_id}/tags", response_model=list[TagCount])
async def list_tags(access: ViewerAccess, db: DbSession) -> list[TagCount]:
    """Tags in use here, with counts, so the tag filter can offer what exists."""
    workspace, _ = access
    return [TagCount(**row) for row in await search.tag_counts(db, workspace.id)]
