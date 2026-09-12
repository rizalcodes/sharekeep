"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { ApiError, items, members, search } from "@/lib/api";
import { formatBytes, formatRelative } from "@/lib/format";
import type { Member, ScanStatus, SearchHit, SearchParams, TagCount } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";
import { ScanBadge } from "@/components/scan-badge";
import { TagList } from "@/components/tags-dialog";

/**
 * Search (Architecture.md §4.8, Design.md §5).
 *
 * A page of results, not a dropdown: each one carries its folder path, so nothing has to
 * be opened to find out where it lives, and the active filters stay on screen as
 * removable chips so the reader always knows why this list is this list.
 *
 * Two honesty rules from §4.8 are visible here rather than implied. Folders leave the
 * results as soon as a file-only filter is used — a folder has no type, size or scan
 * status to judge — and the page says so instead of letting their absence look like a
 * failure to match. And an empty result names the filters that produced the emptiness,
 * because "no results" without them tells the reader nothing about what to loosen.
 */
const CATEGORIES = [
  "document",
  "spreadsheet",
  "presentation",
  "image",
  "video",
  "audio",
  "archive",
  "other",
] as const;

const SCAN_STATES: ScanStatus[] = [
  "clean",
  "pending",
  "scanning",
  "flagged",
  "scan_failed",
  "scan_skipped",
];

const SIZES = [
  { label: "any size", min: null, max: null },
  { label: "under 1 MB", min: null, max: 1024 * 1024 },
  { label: "1–10 MB", min: 1024 * 1024, max: 10 * 1024 * 1024 },
  { label: "over 10 MB", min: 10 * 1024 * 1024, max: null },
];

type Filters = {
  q: string;
  tags: string[];
  category: string | null;
  size: number;
  modifiedAfter: string;
  uploadedBy: string | null;
  scanStatus: ScanStatus | null;
};

const EMPTY: Filters = {
  q: "",
  tags: [],
  category: null,
  size: 0,
  modifiedAfter: "",
  uploadedBy: null,
  scanStatus: null,
};

export default function SearchPage() {
  const { workspace } = useWorkspace();
  const router = useRouter();

  const [filters, setFilters] = useState<Filters>(EMPTY);
  const [results, setResults] = useState<SearchHit[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [foldersExcluded, setFoldersExcluded] = useState(false);
  const [tags, setTags] = useState<TagCount[]>([]);
  const [people, setPeople] = useState<Member[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Read `?q=` / `?tag=` from the address bar once, so a tag clicked on a file row
  // arrives here as a search. Read from `location` rather than useSearchParams to keep
  // this page out of a Suspense boundary it does not otherwise need.
  useEffect(() => {
    let cancelled = false;
    // Deferred a tick: setting state synchronously in an effect body triggers cascading
    // renders, and reading `location` during render would not match the server's.
    void Promise.resolve().then(() => {
      if (cancelled) return;
      const params = new URLSearchParams(window.location.search);
      const q = params.get("q") ?? "";
      const tag = params.getAll("tag");
      if (q || tag.length) setFilters((current) => ({ ...current, q, tags: tag }));
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [tagList, memberList] = await Promise.all([
          search.tags(workspace.id),
          members.list(workspace.id).catch(() => [] as Member[]),
        ]);
        if (cancelled) return;
        setTags(tagList);
        setPeople(memberList);
      } catch {
        // The filters degrade to the ones that need no lookup; the search still works.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [workspace.id]);

  const asParams = useCallback(
    (extra: Partial<SearchParams> = {}): SearchParams => ({
      q: filters.q.trim() || undefined,
      tag: filters.tags.length ? filters.tags : undefined,
      category: filters.category,
      size_min: SIZES[filters.size].min,
      size_max: SIZES[filters.size].max,
      modified_after: filters.modifiedAfter
        ? new Date(filters.modifiedAfter).toISOString()
        : null,
      uploaded_by: filters.uploadedBy,
      scan_status: filters.scanStatus,
      limit: 50,
      ...extra,
    }),
    [filters],
  );

  // The query runs on every filter change, awaiting before touching state so nothing is
  // set synchronously inside the effect.
  useEffect(() => {
    let cancelled = false;
    const handle = setTimeout(() => {
      void (async () => {
        try {
          const page = await search.run(workspace.id, asParams());
          if (cancelled) return;
          setResults(page.results);
          setCursor(page.next_cursor);
          setFoldersExcluded(page.folders_excluded);
          setError(null);
        } catch (caught) {
          if (cancelled) return;
          setResults([]);
          setCursor(null);
          setError(
            caught instanceof ApiError ? caught.message : "Could not reach the server.",
          );
        } finally {
          if (!cancelled) setLoading(false);
        }
      })();
      // Typing shouldn't fire a request per keystroke.
    }, 200);
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [workspace.id, asParams]);

  async function loadMore() {
    if (!cursor) return;
    setLoadingMore(true);
    try {
      const page = await search.run(workspace.id, asParams({ cursor }));
      setResults((current) => [...current, ...page.results]);
      setCursor(page.next_cursor);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not reach the server.");
    } finally {
      setLoadingMore(false);
    }
  }

  function update(changes: Partial<Filters>) {
    setLoading(true);
    setFilters((current) => ({ ...current, ...changes }));
  }

  async function download(hit: SearchHit) {
    try {
      const { url } = await items.download(workspace.id, hit.id);
      window.location.assign(url);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not start the download.");
    }
  }

  const active: { label: string; clear: () => void }[] = [
    ...filters.tags.map((tag) => ({
      label: `tag: ${tag}`,
      clear: () => update({ tags: filters.tags.filter((t) => t !== tag) }),
    })),
    ...(filters.category
      ? [{ label: `type: ${filters.category}`, clear: () => update({ category: null }) }]
      : []),
    ...(filters.size !== 0
      ? [{ label: SIZES[filters.size].label, clear: () => update({ size: 0 }) }]
      : []),
    ...(filters.modifiedAfter
      ? [
          {
            label: `modified after ${filters.modifiedAfter}`,
            clear: () => update({ modifiedAfter: "" }),
          },
        ]
      : []),
    ...(filters.uploadedBy
      ? [
          {
            label: `uploaded by ${people.find((p) => p.user_id === filters.uploadedBy)?.email ?? "someone"}`,
            clear: () => update({ uploadedBy: null }),
          },
        ]
      : []),
    ...(filters.scanStatus
      ? [{ label: `scan: ${filters.scanStatus}`, clear: () => update({ scanStatus: null }) }]
      : []),
  ];

  return (
    <main className="flex flex-1 flex-col">
      <header>
        <h1 className="font-display text-xl font-semibold tracking-tight text-ink">Search</h1>
        <p className="mt-0.5 text-sm text-ink-muted">
          Names and tags in {workspace.name}. File contents are not searched.
        </p>
      </header>

      <div className="mt-5 flex flex-wrap items-end gap-2">
        <label className="flex min-w-56 flex-1 flex-col gap-1">
          <span className="text-xs font-medium text-ink">Name or tag</span>
          <input
            type="search"
            value={filters.q}
            onChange={(e) => update({ q: e.target.value })}
            placeholder="quarterly, invoice…"
            maxLength={200}
            className="rounded-md border border-border bg-surface px-3 py-2 text-sm text-ink outline-none focus:border-primary"
          />
        </label>

        <label className="flex flex-col gap-1">
          <span className="text-xs font-medium text-ink">Type</span>
          <select
            value={filters.category ?? ""}
            onChange={(e) => update({ category: e.target.value || null })}
            className="rounded-md border border-border bg-surface px-2 py-2 text-sm text-ink"
          >
            <option value="">any type</option>
            {CATEGORIES.map((category) => (
              <option key={category} value={category}>
                {category}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1">
          <span className="text-xs font-medium text-ink">Size</span>
          <select
            value={filters.size}
            onChange={(e) => update({ size: Number(e.target.value) })}
            className="rounded-md border border-border bg-surface px-2 py-2 text-sm text-ink"
          >
            {SIZES.map((size, index) => (
              <option key={size.label} value={index}>
                {size.label}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1">
          <span className="text-xs font-medium text-ink">Modified after</span>
          <input
            type="date"
            value={filters.modifiedAfter}
            onChange={(e) => update({ modifiedAfter: e.target.value })}
            className="rounded-md border border-border bg-surface px-2 py-2 text-sm text-ink"
          />
        </label>

        <label className="flex flex-col gap-1">
          <span className="text-xs font-medium text-ink">Uploaded by</span>
          <select
            value={filters.uploadedBy ?? ""}
            onChange={(e) => update({ uploadedBy: e.target.value || null })}
            className="max-w-48 rounded-md border border-border bg-surface px-2 py-2 text-sm text-ink"
          >
            <option value="">anyone</option>
            {people.map((person) => (
              <option key={person.user_id} value={person.user_id}>
                {person.email}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1">
          <span className="text-xs font-medium text-ink">Scan</span>
          <select
            value={filters.scanStatus ?? ""}
            onChange={(e) => update({ scanStatus: (e.target.value || null) as ScanStatus | null })}
            className="rounded-md border border-border bg-surface px-2 py-2 text-sm text-ink"
          >
            <option value="">any state</option>
            {SCAN_STATES.map((state) => (
              <option key={state} value={state}>
                {state.replace("_", " ")}
              </option>
            ))}
          </select>
        </label>
      </div>

      {tags.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          <span className="text-xs text-ink-subtle">Tags in use:</span>
          {tags.map(({ tag, files }) => {
            const on = filters.tags.includes(tag);
            return (
              <button
                key={tag}
                type="button"
                aria-pressed={on}
                onClick={() =>
                  update({
                    tags: on ? filters.tags.filter((t) => t !== tag) : [...filters.tags, tag],
                  })
                }
                className={`rounded border px-1.5 py-0.5 text-[11px] transition-colors ${
                  on
                    ? "border-primary bg-primary-subtle text-primary"
                    : "border-border text-ink-muted hover:border-border-strong"
                }`}
              >
                {tag} <span className="text-ink-subtle">{files}</span>
              </button>
            );
          })}
        </div>
      )}

      {active.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          <span className="text-xs text-ink-subtle">Filtering by:</span>
          {active.map((chip) => (
            <button
              key={chip.label}
              type="button"
              onClick={chip.clear}
              className="rounded-full border border-border px-2 py-0.5 text-[11px] text-ink-muted transition-colors hover:border-status-flagged hover:text-status-flagged"
              title="Remove this filter"
            >
              {chip.label} ×
            </button>
          ))}
          <button
            type="button"
            onClick={() => update({ ...EMPTY })}
            className="text-[11px] text-primary underline underline-offset-2"
          >
            clear all
          </button>
        </div>
      )}

      {foldersExcluded && (
        <p className="mt-3 text-xs text-ink-subtle">
          Folders are not listed: type, size, scan state and tags apply to files only, so a
          folder can neither match nor fail them.
        </p>
      )}

      {error && (
        <p
          role="alert"
          className="mt-4 rounded-md bg-status-flagged-subtle px-3 py-2 text-sm text-status-flagged"
        >
          {error}
        </p>
      )}

      <section className="mt-4 rounded-lg border border-border bg-surface">
        {loading ? (
          <p className="px-4 py-6 text-sm text-ink-muted">Searching…</p>
        ) : results.length === 0 ? (
          <p className="px-4 py-8 text-center text-sm text-ink-muted">
            {active.length || filters.q.trim()
              ? `Nothing in ${workspace.name} matches ${[
                  filters.q.trim() ? `“${filters.q.trim()}”` : null,
                  ...active.map((chip) => chip.label),
                ]
                  .filter(Boolean)
                  .join(" · ")}.`
              : `${workspace.name} is empty.`}
          </p>
        ) : (
          <ul className="divide-y divide-border">
            {results.map((hit) => (
              <li key={`${hit.kind}-${hit.id}`} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5">
                <span className="min-w-0 flex-1">
                  {hit.kind === "folder" ? (
                    <button
                      type="button"
                      onClick={() => router.push(`/files?folder=${hit.id}`)}
                      className="block max-w-full truncate text-left text-sm text-ink hover:underline"
                    >
                      {hit.name}
                    </button>
                  ) : hit.is_downloadable ? (
                    <button
                      type="button"
                      onClick={() => void download(hit)}
                      title="Download"
                      className="block max-w-full truncate text-left text-sm text-ink hover:underline"
                    >
                      {hit.name}
                    </button>
                  ) : (
                    <span
                      className="block max-w-full truncate text-sm text-ink"
                      title="This file cannot be downloaded in its current scan state."
                    >
                      {hit.name}
                    </span>
                  )}
                  <span className="block truncate text-xs text-ink-subtle">
                    {hit.kind === "folder" ? "folder" : hit.category}
                    {" · "}
                    {hit.path.length === 0
                      ? workspace.name
                      : `${workspace.name} / ${hit.path.map((entry) => entry.name).join(" / ")}`}
                    {hit.size_bytes !== null && ` · ${formatBytes(hit.size_bytes)}`}
                    {` · ${formatRelative(hit.updated_at)}`}
                  </span>
                </span>

                {hit.kind === "file" && (
                  <TagList
                    tags={hit.tags}
                    onSelect={(tag) =>
                      update({ tags: filters.tags.includes(tag) ? filters.tags : [...filters.tags, tag] })
                    }
                  />
                )}
                {hit.scan_status && <ScanBadge status={hit.scan_status} />}
              </li>
            ))}
          </ul>
        )}
      </section>

      {cursor && !loading && (
        <button
          type="button"
          onClick={() => void loadMore()}
          disabled={loadingMore}
          className="mt-4 self-start rounded-md border border-border px-3 py-1.5 text-sm text-ink-muted transition-colors hover:bg-surface-sunken disabled:opacity-60"
        >
          {loadingMore ? "Loading…" : "More results"}
        </button>
      )}
    </main>
  );
}
