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
- **Status colors:** Green (clean/success), Amber (scanning), Red (flagged/error), Amber-red (scan failed), Neutral grey (queued, not scanned) — used sparingly, only for scan status and permission-critical states, not decoratively. *(Corrected at M7: this line said "Amber (scanning/pending)", contradicting §5's own "queued (neutral)", and the implementation had followed this line. Scan failed had no colour of its own and borrowed flagged red.)*

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
- **The modal must show that type and options are independent (added at M5):** password, expiry and a use limit are checkboxes that apply to *any* type, not alternatives to it. Presenting them as four mutually exclusive choices would hide the combination most people actually want — a password-protected link that also expires — which is the exact misreading Schema §9 v1.1 was written to correct.
- **The link is shown once, and the interface says so before it disappears (added at M5):** tokens are stored hashed, so the raw link genuinely cannot be recovered afterwards. The warning belongs next to the copy button at creation time, not in a tooltip discovered later, and the recovery path ("issue a new link, revoke this one") is offered in the same place.
- **A use limit is labelled as uses, not downloads (added at M5):** the API never sees the bytes, so what is counted is how many times the link was redeemed for a URL. Calling it "downloads" would promise an accounting the architecture cannot deliver — and a visitor who abandons the transfer has still spent one.
- **A shared file that the scanner flagged is not shareable, even after an owner released it internally (added at M5):** the share dialog says so plainly rather than silently creating a link that will refuse every visitor. The internal release stands; it just does not travel.
- **Upload progress:** chunked upload progress must be visibly real (per-chunk or percentage-accurate), not a fake/indeterminate spinner, given large-file support is a core feature.
- **Upload queue (added at M3):** each queued file shows its own row with real per-chunk percentage, and a per-file terminal state. Failures are separated by kind, because the two demand different things from the user: a transient network failure retries itself and says so, while a permanent refusal (over quota, file above the provider ceiling, permission revoked, part rejected) stops immediately and states the reason. An upload that will never succeed must never present as one still trying.
- **Quota is checked before the first byte moves (added at M3):** the remaining workspace quota and the provider's per-file ceiling are both evaluated the moment a file is chosen, and a file that cannot fit is marked in the queue with the specific reason. Discovering this three chunks in is a worse experience than not starting.
- **Interrupted uploads (added at M3):** an unfinished upload is surfaced on return with its real progress and an explicit invitation to re-select the same file. It is never presented as resuming by itself — the browser cannot do that, and implying otherwise would be a lie in the interface.
- **Activity log (added at M6):** the audit log is a *reading* surface, not a dashboard — no counters, no charts, no "insights". A dense, scannable, reverse-chronological list of what happened, with the actor, the action in plain language, the target's name, the time, and the IP address where one was recorded. It is the surface where the product's security claim is either legible or it is not, so density beats decoration: an owner scanning for something that looks wrong needs many rows visible at once.
- **The activity log never invents a name (added at M6):** the log is append-only, so a target's name is whatever was recorded at the time, not what the row is called today. Rows written before M6 often recorded no name at all, and those say so — "name not recorded" — rather than showing a bare UUID or quietly resolving it live. Showing today's name for yesterday's event would make the log a rewriting of history dressed as a record of it.
- **Failed actions are visually distinct from successful ones (added at M6):** `login_failed` and `share_access_denied` are the rows an audit log exists for, and a list that renders them identically to a successful download buries exactly what the reader came for. They carry the flagged treatment, not a decorative one.
- **The log says what it cannot show (added at M6):** account-level events — sign-ins, sign-outs — are deliberately absent from a workspace's log, because they belong to the account rather than the workspace. The empty space is explained in the interface rather than left to look like a gap in the record.
- **Only owners see it, and the refusal says why (added at M6):** an editor or viewer reaching the activity log is told it is owner-only, not shown a blank page or a generic error. The log holds visitors' IP addresses and colleagues' activity patterns, and the reason it is restricted is worth stating plainly to someone who expected access.
- **Email is plain, textual and states its own trigger (added at M6):** notifications look like a security product writing to you, not a marketing send — no hero image, no button-shaped call to action stretched across the width, no unsubscribe theatre for transactional mail. Each one says which event caused it and what, if anything, the reader should do. A file was flagged: here is what is withheld and who can release it. A link was shared with you: here is who shared it.
- **An email never carries a token or a signed URL (added at M6):** it links to the application and lets the app authenticate the reader. The single exception is a workspace invitation, where the token *is* the invitation and no other channel exists. Everything else — share links included — is fetched by someone already signed in.
- **Accepting an invitation (added after M6 was first marked done):** the link in the email opens one page that says which workspace, which role, and which address the invitation is for, before anything happens. Joining is one explicit click, not automatic on page load — joining a workspace is a grant of access and should be a decision. Someone signed in as a different address is told plainly that the invitation is for another address, rather than shown an error after clicking. Someone with no account goes to sign-up with the invited address already filled in and **locked**, because the server uses the invitation's address regardless of what is typed. An expired or revoked invitation says so, and says to ask the sender for a new one.
- **A redirected email says so at the top (added at M6):** while the sending domain is unverified, the provider permits only one recipient, so development mail is redirected. The banner names who the message was actually for. A test email that looks exactly like a real one is how a developer ends up believing a delivery path works when it has never been exercised.
- **One shell for every signed-in page (added at M7):** the same header everywhere — workspace switcher, primary navigation, account menu — so moving between Files, Activity, Members and Trash never changes where things are. The chosen workspace is part of the shell, not of a page: opening Activity must show the workspace the user was just looking at, not the first one in their list.
- **File and folder actions live in one row menu (added at M7):** download, versions, rename, move, delete, share, permissions and tags, each shown only when the viewer's role permits it rather than shown and then refused. A viewer sees Download and Versions; an editor sees the rest; permissions are an owner's. Destructive actions say what they will do and whether it can be undone — delete goes to the trash and says for how long.
- **The permission indicator (clarified at M7):** each row shows the viewer's own role on that item only when it differs from their workspace role — that difference is the thing worth noticing, and showing "editor" on every row of an editor's view is noise. A separate mark shows that an item has active share links or explicit grants, with a count on hover. Both are quiet: text and a small glyph, never a coloured status chip, because the status colours are reserved for scan state.
- **Versions show what each one is (added at M7):** version number, size, who uploaded it, when, and its scan badge. A version created by rollback says "restored from version *k*" rather than looking like a fresh upload, and the restore action says plainly that it creates a new version and counts toward the quota.
- **Search is a page of results, not a dropdown (added at M7):** each result shows its location as a breadcrumb, its type, size, modified date and scan badge, and the active filters stay visible as removable chips so the reader always knows why they are seeing this list. An empty result says which filters produced it.
- **The trash lists deletions, not rows (added at M7):** one entry per thing the user chose to delete, with how many items it holds, who deleted it, when, and how long until it is gone for good. An entry that can no longer be restored says why.
- **Authentication surfaces (added at M3):** sign in, sign up, and workspace selection. Deliberately plain — a security product's login screen should look unremarkable and legible, not designed. Full visual pass is M7.

## 6. Motion

Subtle only — upload progress animation, hover states, smooth permission-change transitions. No parallax, no floating decorative elements, consistent with the anti-slop direction used in `ecommerce-ai-platform`.

## 7. Notes for Build

- Design tokens, exact hex values, and font pairing should be finalized/locked during the M0 scaffold milestone (same pattern as `ecommerce-ai-platform`'s Design.md → M1 lock), not treated as final in this draft.
- Full design/anti-slop audit pass planned as its own milestone near the end (mirroring `ecommerce-ai-platform`'s M6), not skipped for this project despite its backend-heavy focus.
