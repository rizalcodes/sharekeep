# Design: Secure File Sharing Platform — Design System

**Scope note:** visual/UX direction for the frontend. Not data structure, conventions, or backend architecture.

---

## 1. Positioning

"A secure, no-nonsense file sharing platform built for teams who actually care where their files go." Trustworthy, professional, unmistakably a *security-conscious* product — not a flashy consumer app. Priority: Trust > Clarity of file/folder state > Collaboration clarity (who can do what) > Visual personality.

## 2. Anti-Slop Guidance (carried over from prior projects)

Avoid: gradient-heavy hero sections, glassmorphism, 3D illustrations/blobs, generic AI-startup look, default shadcn/Recharts blue, pill-shaped everything.

## 3. Color Palette (proposed — confirm/lock at build kickoff)

Given the security-first positioning, a cooler, more neutral palette than the warm e-commerce project:

- **Background:** Off-white / near-white (e.g. `#FAFAFA`)
- **Primary accent:** Deep blue or slate (e.g. `#1E3A5F` or `#2C5282`) — conveys trust/security without being generic "tech blue"
- **Text:** Dark slate/charcoal, not pure black
- **Status colors:** Green (clean/success), Amber (scanning/pending), Red (flagged/error) — used sparingly, only for scan status and permission-critical states, not decoratively

## 4. Typography

- Heading: a clean, slightly technical sans-serif (e.g. IBM Plex Sans, Inter Tight) — readable, not overly personality-driven given the security positioning.
- Body: Inter or IBM Plex Sans, consistent with prior projects' body font choices.

## 5. Key UI Surfaces

- **File/folder browser:** table or grid view (user-toggleable), clear iconography per file type, folder breadcrumb navigation reflecting the nested structure.
- **Scan status indicator:** visible per-file badge — this is a core trust signal and should never be hidden or an afterthought. **Six states (v1.1, M4)**, per Architecture §3: **queued for scanning** (neutral — waiting for scanner budget, see below), scanning (amber), clean (green), flagged (red), **not scanned** (neutral/grey), scan failed (amber-red). The "not scanned" state is deliberately visible rather than quietly rendered as clean: a trust product that overstates what it verified is worse than one that admits a gap.
- **"Not scanned" must say which kind (added at M4):** the badge is shared by three different situations — scanning is switched off, the file is above the scanner's size ceiling, or the scanner had never seen this file and its content was deliberately not uploaded (Architecture §3b, the common case). The badge stays neutral in all three, but the tooltip states the actual reason. "Not scanned" without a why invites the reader to assume the most reassuring explanation.
- **"Queued for scanning" is distinct from "scanning" (added at M4):** VirusTotal's free tier allows ~500 requests/day, and a file uploaded after that runs out genuinely waits for the next window rather than being scanned right now. Showing it as "scanning" would be an indeterminate spinner that never moves — the same dishonesty the upload progress rules exist to prevent. The state says it is waiting, and the file is not downloadable while it does.
- **A released file still shows it was flagged (added at M4):** when a workspace owner overrides a flagged or failed scan (Architecture §3d), the badge does **not** become green. It keeps the flagged treatment and adds that a named person released it, with their stated reason. The override grants access; it does not create a clean verdict, and the interface must not invent one.
- **Permission indicator:** clear visual distinction between Owner/Editor/Viewer access on shared items, and whether an item is shared at all.
- **Share link modal:** clearly distinguishes the 4 link types at creation time, with plain-language explanation of each (not just technical labels) — mirrors the "explain like the user has never seen this" discipline used in the n8n project's buyer docs.
- **Upload progress:** chunked upload progress must be visibly real (per-chunk or percentage-accurate), not a fake/indeterminate spinner, given large-file support is a core feature.
- **Upload queue (added at M3):** each queued file shows its own row with real per-chunk percentage, and a per-file terminal state. Failures are separated by kind, because the two demand different things from the user: a transient network failure retries itself and says so, while a permanent refusal (over quota, file above the provider ceiling, permission revoked, part rejected) stops immediately and states the reason. An upload that will never succeed must never present as one still trying.
- **Quota is checked before the first byte moves (added at M3):** the remaining workspace quota and the provider's per-file ceiling are both evaluated the moment a file is chosen, and a file that cannot fit is marked in the queue with the specific reason. Discovering this three chunks in is a worse experience than not starting.
- **Interrupted uploads (added at M3):** an unfinished upload is surfaced on return with its real progress and an explicit invitation to re-select the same file. It is never presented as resuming by itself — the browser cannot do that, and implying otherwise would be a lie in the interface.
- **Authentication surfaces (added at M3):** sign in, sign up, and workspace selection. Deliberately plain — a security product's login screen should look unremarkable and legible, not designed. Full visual pass is M7.

## 6. Motion

Subtle only — upload progress animation, hover states, smooth permission-change transitions. No parallax, no floating decorative elements, consistent with the anti-slop direction used in `ecommerce-ai-platform`.

## 7. Notes for Build

- Design tokens, exact hex values, and font pairing should be finalized/locked during the M0 scaffold milestone (same pattern as `ecommerce-ai-platform`'s Design.md → M1 lock), not treated as final in this draft.
- Full design/anti-slop audit pass planned as its own milestone near the end (mirroring `ecommerce-ai-platform`'s M6), not skipped for this project despite its backend-heavy focus.
