"""M7b: search over names, tags and metadata (Architecture.md §4.8).

The adversarial cases are the point: search is a new way to learn that a file exists, so
what it must never return matters more than what it returns. Everything else here pins
the four decisions taken at M7b, each of which could plausibly have gone the other way.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import pytest

from app.models.content import File, FileVersion
from app.models.enums import ScanStatus, WorkspaceRole
from app.models.identity import WorkspaceMember
from app.services import search as search_service
from tests.conftest import new_user, upload_file

WS = "/api/v1/workspaces"


async def _workspace(make_client, email: str, name: str = "Searchable"):
    client, session = await new_user(make_client, email)
    ws = (await client.post(WS, json={"name": name})).json()["id"]
    return client, ws, session


async def _search(client, ws: str, **params) -> dict:
    """Build the query string the way a browser would.

    Values are percent-encoded, which the first version of this helper skipped: an ISO
    timestamp's `+00:00` arrived as a space and the endpoint rejected it, which looked
    like a product fault and was this helper's.
    """
    pairs: list[str] = []
    for key, value in params.items():
        if value is None:
            continue
        for item in value if isinstance(value, list) else [value]:
            pairs.append(f"{key}={quote(str(item), safe='')}")
    response = await client.get(f"{WS}/{ws}/search?{'&'.join(pairs)}")
    assert response.status_code == 200, response.text
    return response.json()


def _names(page: dict) -> list[str]:
    return [hit["name"] for hit in page["results"]]


async def _tag(client, ws: str, file_id: str, *tags: str) -> None:
    response = await client.patch(f"{WS}/{ws}/files/{file_id}", json={"tags": list(tags)})
    assert response.status_code == 200, response.text


# --- What it must never return ----------------------------------------------------


async def test_another_workspaces_files_are_never_returned(make_client):
    client, mine, _ = await _workspace(make_client, "s-iso@example.com")
    theirs = (await client.post(WS, json={"name": "Other"})).json()["id"]
    await upload_file(client, mine, "budget.xlsx")
    await upload_file(client, theirs, "budget.xlsx")

    page = await _search(client, mine, q="budget")
    assert _names(page) == ["budget.xlsx"]
    assert all(hit["id"] for hit in page["results"])


async def test_a_non_member_cannot_search_a_workspace(make_client):
    _, ws, _ = await _workspace(make_client, "s-owner@example.com")
    stranger, _ = await new_user(make_client, "s-stranger@example.com")
    # 404, not 403: a non-member must not learn that the workspace exists.
    assert (await stranger.get(f"{WS}/{ws}/search?q=a")).status_code == 404
    assert (await stranger.get(f"{WS}/{ws}/tags")).status_code == 404


async def test_deleted_and_in_flight_files_are_excluded(make_client, db):
    client, ws, _ = await _workspace(make_client, "s-excl@example.com")
    gone = (await upload_file(client, ws, "gone-report.pdf")).json()["id"]
    await upload_file(client, ws, "kept-report.pdf")
    await client.delete(f"{WS}/{ws}/files/{gone}")

    # An upload that was started and never completed is not a file yet.
    started = await client.post(
        f"{WS}/{ws}/uploads/init",
        json={"name": "half-report.pdf", "size_bytes": 4096, "mime_type": "application/pdf"},
    )
    assert started.status_code == 201

    page = await _search(client, ws, q="report")
    assert _names(page) == ["kept-report.pdf"]


async def test_a_file_inside_a_deleted_folder_is_excluded(make_client):
    client, ws, _ = await _workspace(make_client, "s-excl2@example.com")
    folder = (await client.post(f"{WS}/{ws}/folders", json={"name": "Old"})).json()["id"]
    await upload_file(client, ws, "inside.pdf", folder_id=folder)
    await upload_file(client, ws, "outside.pdf")
    await client.delete(f"{WS}/{ws}/folders/{folder}")

    page = await _search(client, ws, q="pdf")
    assert _names(page) == ["outside.pdf"]


# --- The typed query matches names AND tags (decided at M7b) ----------------------


async def test_the_query_finds_a_file_by_its_tag(make_client):
    """§4.8 first restricted the query to names, which would have made a tag
    undiscoverable by typing it — while PRD §4 promises tag search."""
    client, ws, _ = await _workspace(make_client, "s-q1@example.com")
    file_id = (await upload_file(client, ws, "q3.pdf")).json()["id"]
    await _tag(client, ws, file_id, "invoice", "2026")
    await upload_file(client, ws, "unrelated.pdf")

    page = await _search(client, ws, q="invoice")
    assert _names(page) == ["q3.pdf"]


async def test_the_query_is_case_insensitive_and_matches_a_substring(make_client):
    client, ws, _ = await _workspace(make_client, "s-q2@example.com")
    await upload_file(client, ws, "Quarterly-Report.PDF")

    for term in ["quarterly", "REPORT", "ly-re"]:
        page = await _search(client, ws, q=term)
        assert _names(page) == ["Quarterly-Report.PDF"], term


async def test_a_wildcard_in_the_query_is_searched_for_not_interpreted(make_client):
    """Someone typing `50%` wants those characters, not every file (§4.8)."""
    client, ws, _ = await _workspace(make_client, "s-q3@example.com")
    await upload_file(client, ws, "50% off.txt")
    await upload_file(client, ws, "plain.txt")

    # The helper encodes, so these are the characters the user actually typed.
    assert _names(await _search(client, ws, q="50%")) == ["50% off.txt"]
    # A bare "%" matches the one name containing it, not every file.
    assert _names(await _search(client, ws, q="%")) == ["50% off.txt"]
    # And "_" is likewise a character, not "any single character".
    assert _names(await _search(client, ws, q="_")) == []


async def test_relevance_puts_an_exact_name_first_then_prefix_then_tag(make_client):
    client, ws, _ = await _workspace(make_client, "s-rank@example.com")
    tagged = (await upload_file(client, ws, "zzz.txt")).json()["id"]
    await _tag(client, ws, tagged, "report")
    await upload_file(client, ws, "report")  # exact
    await upload_file(client, ws, "report-draft.txt")  # prefix
    await upload_file(client, ws, "the report final.txt")  # substring

    page = await _search(client, ws, q="report")
    assert _names(page) == ["report", "report-draft.txt", "the report final.txt", "zzz.txt"]


# --- Tag filtering is exact, and several tags mean AND ----------------------------


async def test_the_tag_filter_is_exact_where_the_query_is_not(make_client):
    client, ws, _ = await _workspace(make_client, "s-t1@example.com")
    file_id = (await upload_file(client, ws, "a.txt")).json()["id"]
    await _tag(client, ws, file_id, "invoice")

    assert _names(await _search(client, ws, tag=["invoice"])) == ["a.txt"]
    # A partial tag matches nothing through the filter — that is what the query is for.
    assert _names(await _search(client, ws, tag=["invoi"])) == []
    assert _names(await _search(client, ws, q="invoi")) == ["a.txt"]


async def test_two_tags_narrow_rather_than_widen(make_client):
    client, ws, _ = await _workspace(make_client, "s-t2@example.com")
    both = (await upload_file(client, ws, "both.txt")).json()["id"]
    one = (await upload_file(client, ws, "one.txt")).json()["id"]
    await _tag(client, ws, both, "invoice", "2026")
    await _tag(client, ws, one, "invoice")

    assert set(_names(await _search(client, ws, tag=["invoice"]))) == {"both.txt", "one.txt"}
    # Adding a tag narrows; it never widens.
    assert _names(await _search(client, ws, tag=["invoice", "2026"])) == ["both.txt"]


async def test_tags_are_normalised_and_bounded(make_client):
    client, ws, _ = await _workspace(make_client, "s-t3@example.com")
    file_id = (await upload_file(client, ws, "a.txt")).json()["id"]

    ok = await client.patch(
        f"{WS}/{ws}/files/{file_id}", json={"tags": ["  Invoice ", "INVOICE", "2026"]}
    )
    assert ok.status_code == 200
    # Lowercased, trimmed, de-duplicated, sorted.
    assert ok.json()["tags"] == ["2026", "invoice"]

    for bad in [["a" * 41], ["bad/slash"], ["-leading"], [str(n) for n in range(26)]]:
        refused = await client.patch(f"{WS}/{ws}/files/{file_id}", json={"tags": bad})
        assert refused.status_code == 422, bad


async def test_the_tag_list_counts_only_live_files(make_client):
    client, ws, _ = await _workspace(make_client, "s-t4@example.com")
    first = (await upload_file(client, ws, "a.txt")).json()["id"]
    second = (await upload_file(client, ws, "b.txt")).json()["id"]
    await _tag(client, ws, first, "shared", "alpha")
    await _tag(client, ws, second, "shared")

    tags = {row["tag"]: row["files"] for row in (await client.get(f"{WS}/{ws}/tags")).json()}
    assert tags == {"shared": 2, "alpha": 1}

    await client.delete(f"{WS}/{ws}/files/{second}")
    tags = {row["tag"]: row["files"] for row in (await client.get(f"{WS}/{ws}/tags")).json()}
    assert tags == {"shared": 1, "alpha": 1}


# --- The five metadata filters ----------------------------------------------------


@pytest.mark.parametrize(
    "mime,category",
    [
        ("application/pdf", "document"),
        ("text/plain", "document"),
        ("image/png", "image"),
        ("video/mp4", "video"),
        ("audio/mpeg", "audio"),
        ("text/csv", "spreadsheet"),
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "spreadsheet"),
        ("application/vnd.ms-powerpoint", "presentation"),
        ("application/zip", "archive"),
        ("application/octet-stream", "other"),
    ],
)
async def test_the_type_filter_and_the_label_agree(make_client, mime, category):
    """One mapping serves both, so a result cannot be labelled one thing and filtered
    as another (§4.8)."""
    client, ws, _ = await _workspace(make_client, f"s-cat-{category}-{mime[:6]}@example.com")
    await upload_file(client, ws, f"file-{category}.bin", mime_type=mime)

    page = await _search(client, ws, category=category)
    assert _names(page) == [f"file-{category}.bin"]
    assert page["results"][0]["category"] == category
    assert search_service.category_of(mime) == category


async def test_an_unknown_type_is_refused_with_the_choices(make_client):
    client, ws, _ = await _workspace(make_client, "s-cat-bad@example.com")
    refused = await client.get(f"{WS}/{ws}/search?category=spreadhseet")
    assert refused.status_code == 422
    assert "spreadsheet" in refused.json()["detail"]


async def test_size_and_date_and_uploader_and_scan_status_filter(make_client, db):
    owner, ws, _ = await _workspace(make_client, "s-meta@example.com")
    editor, editor_session = await new_user(make_client, "s-meta-e@example.com")
    db.add(
        WorkspaceMember(
            workspace_id=uuid.UUID(ws),
            user_id=uuid.UUID(editor_session["user"]["id"]),
            role=WorkspaceRole.EDITOR,
        )
    )
    await db.commit()

    small = (await upload_file(owner, ws, "small.bin", size_bytes=1000)).json()["id"]
    await upload_file(owner, ws, "large.bin", size_bytes=9000)
    await upload_file(editor, ws, "theirs.bin", size_bytes=5000)

    assert _names(await _search(owner, ws, size_max=2000)) == ["small.bin"]
    assert _names(await _search(owner, ws, size_min=8000)) == ["large.bin"]
    assert _names(await _search(owner, ws, uploaded_by=editor_session["user"]["id"])) == [
        "theirs.bin"
    ]

    # Scan status: flag one directly, the way the scanner would.
    file = await db.get(File, uuid.UUID(small))
    version = await db.get(FileVersion, file.current_version_id)
    version.scan_status = ScanStatus.FLAGGED
    file.scan_status = ScanStatus.FLAGGED
    await db.commit()
    assert _names(await _search(owner, ws, scan_status="flagged")) == ["small.bin"]

    tomorrow = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    assert _names(await _search(owner, ws, modified_after=tomorrow)) == []


# --- Folders ----------------------------------------------------------------------


async def test_a_folder_matches_by_name_and_carries_its_path(make_client):
    client, ws, _ = await _workspace(make_client, "s-f1@example.com")
    top = (await client.post(f"{WS}/{ws}/folders", json={"name": "Invoices"})).json()["id"]
    sub = (
        await client.post(f"{WS}/{ws}/folders", json={"name": "2026", "parent_folder_id": top})
    ).json()["id"]
    await upload_file(client, ws, "invoice-jan.pdf", folder_id=sub)

    page = await _search(client, ws, q="invoice")
    by_name = {hit["name"]: hit for hit in page["results"]}
    assert set(by_name) == {"Invoices", "invoice-jan.pdf"}
    assert by_name["Invoices"]["kind"] == "folder"
    # The file says where it lives, so nobody has to open it to find out.
    assert [entry["name"] for entry in by_name["invoice-jan.pdf"]["path"]] == [
        "Invoices",
        "2026",
    ]
    assert page["folders_excluded"] is False


async def test_folders_leave_the_results_when_a_file_only_filter_is_used(make_client):
    """A folder has no type, size or scan status, so it can neither pass nor fail those
    filters; one surviving a "flagged only" search would read as a claim about it."""
    client, ws, _ = await _workspace(make_client, "s-f2@example.com")
    await client.post(f"{WS}/{ws}/folders", json={"name": "Reports"})
    await upload_file(client, ws, "reports.pdf", mime_type="application/pdf")

    page = await _search(client, ws, q="report", category="document")
    assert _names(page) == ["reports.pdf"]
    assert page["folders_excluded"] is True

    unfiltered = await _search(client, ws, q="report")
    assert "Reports" in _names(unfiltered)
    assert unfiltered["folders_excluded"] is False


# --- Paging -----------------------------------------------------------------------


async def test_paging_does_not_repeat_or_skip_across_pages(make_client):
    client, ws, _ = await _workspace(make_client, "s-page@example.com")
    for index in range(7):
        await upload_file(client, ws, f"paper-{index}.txt")
    await client.post(f"{WS}/{ws}/folders", json={"name": "paper-folder"})

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        page = await _search(client, ws, q="paper", limit=3, cursor=cursor)
        seen.extend(hit["id"] for hit in page["results"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert cursor is None, "paging did not terminate"
    assert len(seen) == len(set(seen)), "a result appeared on two pages"
    everything = await _search(client, ws, q="paper", limit=100)
    assert len(seen) == len(everything["results"]) == 8


async def test_a_malformed_cursor_is_refused(make_client):
    client, ws, _ = await _workspace(make_client, "s-cursor@example.com")
    refused = await client.get(f"{WS}/{ws}/search?q=a&cursor=not-a-cursor")
    assert refused.status_code == 422


async def test_an_over_long_query_is_refused(make_client):
    client, ws, _ = await _workspace(make_client, "s-long@example.com")
    refused = await client.get(f"{WS}/{ws}/search?q={'a' * 201}")
    assert refused.status_code == 422


async def test_no_query_and_no_filters_lists_the_workspace(make_client):
    """An empty search is a browse: it should not be an error, and it should not be
    empty either."""
    client, ws, _ = await _workspace(make_client, "s-empty@example.com")
    await upload_file(client, ws, "a.txt")
    await client.post(f"{WS}/{ws}/folders", json={"name": "F"})

    page = await _search(client, ws)
    assert set(_names(page)) == {"a.txt", "F"}


# --- The permission argument §4.8 rests on --------------------------------------


async def test_a_narrowed_grant_does_not_hide_a_file_from_search(make_client, db):
    """§4.8's scoping argument: a grant can narrow a member's role but never hide an
    item, so search needs no per-row permission join. If that ever stops being true,
    this test should fail rather than the omission going unnoticed."""
    owner, ws, _ = await _workspace(make_client, "s-perm@example.com")
    member, member_session = await new_user(make_client, "s-perm-m@example.com")
    db.add(
        WorkspaceMember(
            workspace_id=uuid.UUID(ws),
            user_id=uuid.UUID(member_session["user"]["id"]),
            role=WorkspaceRole.EDITOR,
        )
    )
    await db.commit()

    folder = (await owner.post(f"{WS}/{ws}/folders", json={"name": "Narrow"})).json()["id"]
    await upload_file(owner, ws, "inside.txt", folder_id=folder)
    await owner.post(
        f"{WS}/{ws}/folders/{folder}/grants",
        json={"user_id": member_session["user"]["id"], "role": "viewer"},
    )

    page = await _search(member, ws, q="inside")
    assert _names(page) == ["inside.txt"]


async def test_search_reaches_files_the_member_has_not_opened(make_client, db):
    owner, ws, _ = await _workspace(make_client, "s-perm2@example.com")
    viewer, viewer_session = await new_user(make_client, "s-perm2-v@example.com")
    db.add(
        WorkspaceMember(
            workspace_id=uuid.UUID(ws),
            user_id=uuid.UUID(viewer_session["user"]["id"]),
            role=WorkspaceRole.VIEWER,
        )
    )
    await db.commit()
    await upload_file(owner, ws, "owner-only.txt")

    assert _names(await _search(viewer, ws, q="owner-only")) == ["owner-only.txt"]


# --- The trigram index is actually used -----------------------------------------


async def test_the_name_search_uses_the_trigram_index(db):
    """A substring search that falls back to a sequential scan would still pass every
    test above. This asserts the index exists and that the planner can use it, which is
    the only reason migration 0008 exists."""
    from sqlalchemy import text

    rows = await db.execute(
        text("SELECT indexname FROM pg_indexes WHERE tablename = 'files'")
    )
    assert "ix_files_name_trgm" in {row[0] for row in rows.all()}

    # Sequential scans are penalised so the planner reaches for an index if it has one.
    # It only has one here because the query is `name ILIKE …` — the expression the
    # trigram index covers. The first implementation used `lower(name) LIKE …`, which
    # returns the same rows and cannot use the index at all, so this is the check that
    # would have caught it.
    await db.execute(text("SET LOCAL enable_seqscan = off"))
    plan = await db.execute(
        text("EXPLAIN SELECT id FROM files WHERE name ILIKE '%report%'")
    )
    rendered = " ".join(row[0] for row in plan.all())
    assert "ix_files_name_trgm" in rendered, rendered

    # The contrast, so this test carries its own evidence: the expression the first
    # implementation used returns the same rows and cannot use the index at all.
    defeated = await db.execute(
        text("EXPLAIN SELECT id FROM files WHERE lower(name) LIKE '%report%'")
    )
    rendered_defeated = " ".join(row[0] for row in defeated.all())
    assert "ix_files_name_trgm" not in rendered_defeated, rendered_defeated
