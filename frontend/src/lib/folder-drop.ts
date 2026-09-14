/**
 * Reading a dropped folder (Design.md §5, M7c — deferred from M3).
 *
 * The browser will not hand over a directory as a `File`; it hands over an entry tree
 * through `webkitGetAsEntry`, which has to be walked one level at a time. That traversal
 * is the reason M3 deferred this rather than half-doing it.
 *
 * Two limits are enforced here rather than discovered later:
 *
 * - **Depth 32**, matching the server's folder nesting cap (Architecture §4.1). A drop
 *   deeper than that would create folders the permission walk refuses to resolve.
 * - **A file ceiling**, so one careless drop of a home directory cannot queue tens of
 *   thousands of uploads. Hitting it is reported, never silently trimmed.
 */
export type DroppedFile = {
  file: File;
  /** Folder names from the drop target downwards; empty means the current folder. */
  path: string[];
};

export type DropReading = {
  files: DroppedFile[];
  /** Directories seen, including empty ones, so they can be recreated. */
  directories: string[][];
  /** True when the ceiling or the depth cap stopped the walk short. */
  truncated: boolean;
  hadDirectories: boolean;
};

export const MAX_DEPTH = 32;
export const MAX_FILES = 500;

type Entry = {
  isFile: boolean;
  isDirectory: boolean;
  name: string;
  file?: (cb: (file: File) => void, err?: (e: unknown) => void) => void;
  createReader?: () => {
    readEntries: (cb: (entries: Entry[]) => void, err?: (e: unknown) => void) => void;
  };
};

function readFile(entry: Entry): Promise<File | null> {
  return new Promise((resolve) => {
    if (!entry.file) return resolve(null);
    entry.file(
      (file) => resolve(file),
      () => resolve(null),
    );
  });
}

/** One `readEntries` call returns a batch, not the whole directory: keep asking. */
function readBatch(reader: {
  readEntries: (cb: (entries: Entry[]) => void, err?: (e: unknown) => void) => void;
}): Promise<Entry[]> {
  return new Promise((resolve) => {
    reader.readEntries(
      (entries) => resolve(entries),
      () => resolve([]),
    );
  });
}

async function walk(entry: Entry, path: string[], out: DropReading): Promise<void> {
  if (out.files.length >= MAX_FILES) {
    out.truncated = true;
    return;
  }
  if (entry.isFile) {
    const file = await readFile(entry);
    if (file) out.files.push({ file, path });
    return;
  }
  if (!entry.isDirectory || !entry.createReader) return;

  const here = [...path, entry.name];
  if (here.length > MAX_DEPTH) {
    out.truncated = true;
    return;
  }
  out.directories.push(here);

  const reader = entry.createReader();
  for (;;) {
    const batch = await readBatch(reader);
    if (batch.length === 0) break;
    for (const child of batch) {
      await walk(child, here, out);
      if (out.files.length >= MAX_FILES) {
        out.truncated = true;
        return;
      }
    }
  }
}

/**
 * Flatten a drop into files with their folder paths.
 *
 * Entries must be taken from `dataTransfer.items` synchronously in the drop handler —
 * the list is emptied once the event returns — which is why the caller passes them in
 * rather than the event.
 */
export async function readDrop(entries: (FileSystemEntry | null)[]): Promise<DropReading> {
  const out: DropReading = {
    files: [],
    directories: [],
    truncated: false,
    hadDirectories: false,
  };
  for (const raw of entries) {
    if (!raw) continue;
    const entry = raw as unknown as Entry;
    if (entry.isDirectory) out.hadDirectories = true;
    await walk(entry, [], out);
  }
  return out;
}

/** Total bytes in a reading, for the quota check that happens before any byte moves. */
export function totalBytes(reading: DropReading): number {
  return reading.files.reduce((sum, item) => sum + item.file.size, 0);
}

/** Distinct folder paths, shallowest first, so parents are created before children. */
export function foldersToCreate(reading: DropReading): string[][] {
  const seen = new Set<string>();
  const paths: string[][] = [];
  const add = (path: string[]) => {
    for (let depth = 1; depth <= path.length; depth += 1) {
      const slice = path.slice(0, depth);
      const key = slice.join("/");
      if (!seen.has(key)) {
        seen.add(key);
        paths.push(slice);
      }
    }
  };
  reading.directories.forEach(add);
  reading.files.forEach((item) => add(item.path));
  return paths.sort((a, b) => a.length - b.length);
}
