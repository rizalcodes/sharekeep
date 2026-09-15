import type { FileCategory } from "@/lib/types";

/**
 * An icon per file type (Design.md §5, M7c).
 *
 * The category comes from the server — the same mapping the type filter uses
 * (Architecture §4.8) — so an icon and a filter can never disagree about what a file is.
 *
 * Deliberately line drawings in one weight and `currentColor`, not a colourful
 * file-type set: colour in this interface means scan state, and a row of bright icons
 * would shout over the badge that actually matters.
 */
const STROKE = { stroke: "currentColor", strokeWidth: 1.2, fill: "none" } as const;

function Frame({ children }: { children: React.ReactNode }) {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 16 16"
      aria-hidden
      className="shrink-0 text-ink-subtle"
    >
      {children}
    </svg>
  );
}

/** The page outline every file icon shares, so the set reads as one family. */
const page = (
  <path d="M4 2h5l3 3v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1Z" {...STROKE} />
);
const fold = <path d="M9 2v3.5h3.5" {...STROKE} />;

const ICONS: Record<FileCategory, React.ReactNode> = {
  document: (
    <>
      {page}
      {fold}
      <path d="M5 8.5h6M5 11h4" {...STROKE} />
    </>
  ),
  spreadsheet: (
    <>
      {page}
      {fold}
      <rect x="4.7" y="8.3" width="6.6" height="4.4" rx="0.4" {...STROKE} />
      <path d="M4.7 10.5h6.6M8 8.3v4.4" {...STROKE} />
    </>
  ),
  presentation: (
    <>
      {page}
      {fold}
      <rect x="5" y="8" width="6" height="4" rx="0.5" {...STROKE} />
    </>
  ),
  image: (
    <>
      {page}
      {fold}
      <circle cx="5.9" cy="8.7" r="0.8" {...STROKE} />
      <path d="M4.2 12.9l2.3-2.4 1.5 1.5 1.1-1.1 2 2" {...STROKE} />
    </>
  ),
  video: (
    <>
      {page}
      {fold}
      <path d="M6.5 8.6v3.3l3.2-1.65Z" {...STROKE} />
    </>
  ),
  audio: (
    <>
      {page}
      {fold}
      <path d="M9.4 8.2v3.4" {...STROKE} />
      <path d="M9.4 8.2c-1.5.3-2.6.6-2.6.6" {...STROKE} />
      <circle cx="8.1" cy="11.6" r="1.3" {...STROKE} />
    </>
  ),
  archive: (
    <>
      {page}
      {fold}
      <path d="M6.5 4.2v1.5M6.5 6.6v1.5M6.5 8.9v1.2" {...STROKE} />
      <rect x="5.6" y="10.4" width="1.8" height="2.3" rx="0.6" {...STROKE} />
    </>
  ),
  other: (
    <>
      {page}
      {fold}
    </>
  ),
};

export function FileTypeIcon({ category }: { category?: FileCategory | null }) {
  return <Frame>{ICONS[category ?? "other"] ?? ICONS.other}</Frame>;
}

export function FolderTypeIcon() {
  return (
    <Frame>
      <path
        d="M1.5 4.5A1.5 1.5 0 0 1 3 3h3l1.5 1.5H13A1.5 1.5 0 0 1 14.5 6v5.5A1.5 1.5 0 0 1 13 13H3a1.5 1.5 0 0 1-1.5-1.5v-7Z"
        {...STROKE}
      />
    </Frame>
  );
}

/** Plain-language name for a category, for tooltips and the grid's secondary line. */
export const CATEGORY_LABEL: Record<FileCategory, string> = {
  document: "Document",
  spreadsheet: "Spreadsheet",
  presentation: "Presentation",
  image: "Image",
  video: "Video",
  audio: "Audio",
  archive: "Archive",
  other: "File",
};
