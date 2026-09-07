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
- **Scan status indicator:** visible per-file badge — this is a core trust signal and should never be hidden or an afterthought. Five states, per Architecture §3: scanning (amber), clean (green), flagged (red), **not scanned** (neutral/grey — file was too large for the scanner to accept), scan failed (amber-red). The "not scanned" state is deliberately visible rather than quietly rendered as clean: a trust product that overstates what it verified is worse than one that admits a gap.
- **Permission indicator:** clear visual distinction between Owner/Editor/Viewer access on shared items, and whether an item is shared at all.
- **Share link modal:** clearly distinguishes the 4 link types at creation time, with plain-language explanation of each (not just technical labels) — mirrors the "explain like the user has never seen this" discipline used in the n8n project's buyer docs.
- **Upload progress:** chunked upload progress must be visibly real (per-chunk or percentage-accurate), not a fake/indeterminate spinner, given large-file support is a core feature.

## 6. Motion

Subtle only — upload progress animation, hover states, smooth permission-change transitions. No parallax, no floating decorative elements, consistent with the anti-slop direction used in `ecommerce-ai-platform`.

## 7. Notes for Build

- Design tokens, exact hex values, and font pairing should be finalized/locked during the M0 scaffold milestone (same pattern as `ecommerce-ai-platform`'s Design.md → M1 lock), not treated as final in this draft.
- Full design/anti-slop audit pass planned as its own milestone near the end (mirroring `ecommerce-ai-platform`'s M6), not skipped for this project despite its backend-heavy focus.
