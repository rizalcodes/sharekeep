"use client";

import { useState } from "react";
import { ApiError, items, type ItemRef } from "@/lib/api";
import { Dialog, DialogError, buttonPrimary, buttonQuiet, fieldClass } from "@/components/dialog";

/**
 * Editing a file's tags (Design.md §5, M7b).
 *
 * The rules are stated rather than enforced silently: a tag is lowercased and trimmed
 * for you, and the characters it may contain are written down, because a field that
 * rejects input without saying why teaches nothing.
 *
 * Tags are what the tag filter matches exactly, so normalising here — not at every
 * place that reads them — is what keeps "invoice" and "Invoice " the same tag.
 */
const MAX_TAGS = 25;
const MAX_LENGTH = 40;

export function TagsDialog({
  workspaceId,
  item,
  initial,
  onClose,
  onSaved,
}: {
  workspaceId: string;
  item: ItemRef;
  initial: string[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [tags, setTags] = useState<string[]>(initial);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function add() {
    const value = draft.trim().toLowerCase();
    if (!value) return;
    if (value.length > MAX_LENGTH) {
      setError(`A tag can be at most ${MAX_LENGTH} characters.`);
      return;
    }
    if (tags.length >= MAX_TAGS) {
      setError(`A file can have at most ${MAX_TAGS} tags.`);
      return;
    }
    setError(null);
    setTags((current) => (current.includes(value) ? current : [...current, value].sort()));
    setDraft("");
  }

  async function save() {
    setBusy(true);
    setError(null);
    try {
      await items.updateFile(workspaceId, item.id, { tags });
      onSaved();
    } catch (caught) {
      // The server validates the same rules; its wording is the authority.
      setError(caught instanceof ApiError ? caught.message : "Could not reach the server.");
      setBusy(false);
    }
  }

  return (
    <Dialog title="Tags" subtitle={item.name} onClose={onClose} width="max-w-md">
      <ul className="flex flex-wrap gap-1.5">
        {tags.length === 0 && (
          <li className="text-sm text-ink-subtle">No tags yet.</li>
        )}
        {tags.map((tag) => (
          <li key={tag}>
            <span className="inline-flex items-center gap-1 rounded border border-border bg-surface-sunken px-2 py-0.5 text-xs text-ink">
              {tag}
              <button
                type="button"
                aria-label={`Remove tag ${tag}`}
                onClick={() => setTags((current) => current.filter((t) => t !== tag))}
                className="text-ink-subtle hover:text-status-flagged"
              >
                ×
              </button>
            </span>
          </li>
        ))}
      </ul>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          add();
        }}
        className="mt-4 flex gap-2"
      >
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="invoice"
          maxLength={MAX_LENGTH}
          aria-label="New tag"
          className={fieldClass}
        />
        <button type="submit" disabled={!draft.trim()} className={buttonQuiet}>
          Add
        </button>
      </form>
      <p className="mt-2 text-xs text-ink-subtle">
        Letters, digits, spaces, hyphens, underscores and dots. Up to {MAX_TAGS} tags of{" "}
        {MAX_LENGTH} characters. Tags are lowercased, so &ldquo;Invoice&rdquo; and
        &ldquo;invoice&rdquo; are the same tag.
      </p>

      <DialogError message={error} />
      <div className="mt-4 flex justify-end gap-2">
        <button type="button" onClick={onClose} className={buttonQuiet}>
          Cancel
        </button>
        <button type="button" onClick={() => void save()} disabled={busy} className={buttonPrimary}>
          {busy ? "Saving…" : "Save tags"}
        </button>
      </div>
    </Dialog>
  );
}

/**
 * A file's tags on its row (Design.md §5, M7b): the first few, then a count. Tags kept
 * only inside an editor would be invisible, and an invisible tag is one nobody applies.
 */
export function TagList({
  tags,
  onSelect,
  max = 3,
}: {
  tags: string[];
  onSelect?: (tag: string) => void;
  max?: number;
}) {
  if (tags.length === 0) return null;
  const shown = tags.slice(0, max);
  const hidden = tags.length - shown.length;

  return (
    <span className="flex shrink-0 items-center gap-1">
      {shown.map((tag) =>
        onSelect ? (
          <button
            key={tag}
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              onSelect(tag);
            }}
            title={`Search for files tagged "${tag}"`}
            className="rounded border border-border px-1.5 py-0.5 text-[11px] text-ink-muted transition-colors hover:border-primary hover:text-primary"
          >
            {tag}
          </button>
        ) : (
          <span
            key={tag}
            className="rounded border border-border px-1.5 py-0.5 text-[11px] text-ink-muted"
          >
            {tag}
          </span>
        ),
      )}
      {hidden > 0 && (
        <span className="text-[11px] text-ink-subtle" title={tags.join(", ")}>
          +{hidden}
        </span>
      )}
    </span>
  );
}
