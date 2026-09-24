# MGM University Convocation System — UI/UX Audit Report
**Date:** September 24, 2026  
**Auditor:** UI/UX Engineering & Antigravity Agent  
**Reference Document:** MGM University 6th Convocation Invitation (Chhatrapati Sambhajinagar)

---

## Executive Summary
The MGM University Convocation Management System is a robust single-server event operations application handling high-throughput student processing across Registry (Reporting & Robe Allocation / Return), Seating, Queue, Stage, Caller, and Lunch stations, backed by an Admin console for reporting, imports, and system integrity.

While the backend engine, event audit, transaction isolation, and JavaScript controllers are functionally mature with 100% test coverage, the user interface currently presents as a generic, bare utility tool styled with cold blue/gray SaaS defaults (`#1f4e8c`, `#f4f6f9`). It lacks the dignified academic prestige, formal university identity, and ceremonial elegance embodied by the official MGM University Convocation invitation.

This audit covers all 30 functional and aesthetic dimensions required by Phase 1 before applying the redesign.

---

## Comprehensive 30-Point Audit

### 1. Global Layout
- **Current State:** Basic `max-width: 64rem; margin: 0 auto; padding: 1rem;` centered layout.
- **Issues:** Header is a flat blue strip (`.bar`) spanning edge-to-edge; body container feels cramped on large desktop displays and lacks generous margins or academic visual framing.
- **Plan:** Introduce a refined global layout container with institutional header, page breadcrumbs/context ribbon, clean card wrappers, and an official footer.

### 2. Header / Navigation
- **Current State:** Bare blue bar displaying only `settings.event_name` ("Annual Convocation 2026"), operator username with role, and a plain "Sign out" link.
- **Issues:** No university name, no logo emblem, no visual hierarchy, no role badge.
- **Plan:** Build a compact, distinguished global header featuring the MGM University sunburst emblem, "MGM UNIVERSITY", "Convocation Management System", role badge pill, and user actions.

### 3. Footer
- **Current State:** Completely missing from `templates/base.html`.
- **Issues:** Abrupt cutoff at the bottom of every page.
- **Plan:** Implement a clean institutional footer: "MGM University &middot; Convocation Management System &middot; &copy; MGM University".

### 4. Typography
- **Current State:** Generic system font stack `system-ui, "Segoe UI", sans-serif`.
- **Issues:** Lacks academic weight and gravitas. Headings blend into body text without distinction.
- **Plan:** Formal academic serif typography for headings, university titles, and station titles (`Georgia, "Times New Roman", serif`), combined with high-legibility tabular sans-serif for numbers, codes, PRNs, and data tables.

### 5. Color Palette
- **Current State:** Generic SaaS tech blue (`#1f4e8c`), cold background gray (`#f4f6f9`), cold dark slate (`#1b2430`).
- **Issues:** In stark contrast to the warm, formal gold-and-brown Indian university convocation identity shown on the invitation.
- **Plan:** Implement CSS variables derived from the invitation reference:
  - Deep Academic Brown: `--color-primary: #5A321F;` / `--color-primary-dark: #3F2418;`
  - Ceremonial Gold: `--color-gold: #C99545;` / `--color-gold-light: #E4C58F;`
  - Warm Ivory / Parchment: `--color-bg: #FBF7F0;`
  - Crisp Surface White: `--color-card: #FFFFFF;`
  - Deep Espresso Ink: `--color-text: #30251F;`
  - Warm Slate Muted: `--color-muted: #756B63;`
  - Warm Institutional Border: `--color-border: #E4D8C8;`

### 6. Buttons & Interactive Controls
- **Current State:** Ad-hoc button styles (`.big`, `.confirm`, `.big-action`, `.danger`, `button.link`).
- **Issues:** Inconsistent padding, lack of unified hover/active/focus elevations, raw contrast issues.
- **Plan:** Standardize design tokens for Primary (Academic Brown/Gold), Secondary (Parchment/Border), Success (Ceremonial Forest Green `#1B6E38`), Danger (Deep Crimson `#991B1B`), and Ghost links, with gold focus rings.

### 7. Form Controls
- **Current State:** Plain rectangular boxes with default browser outlines.
- **Issues:** Inconsistent borders, inputs feel utilitarian, focus state lacks prominence for barcode operators.
- **Plan:** Refined form inputs with warm borders (`#E4D8C8`), subtle background tint on focus, and ceremonial gold highlight rings (`#C99545`).

### 8. Tables
- **Current State:** Bare unstyled table rows with basic `border-bottom: 1px solid var(--line);`. No row hover.
- **Issues:** Hard to track across rows on large admin reports.
- **Plan:** Institutional table styling with warm parchment headers (`#F7EBDD`), subtle alternating or crisp hover highlights, clean numeric right-alignment (`td.num`), and rounded containment borders.

### 9. Cards & Containers
- **Current State:** Flat 1px gray border with 8px radius.
- **Issues:** Lacks depth and refinement.
- **Plan:** Subtle warm elevation (`box-shadow: 0 1px 3px rgba(90, 50, 31, 0.06);`), refined double-ruled or gold accent headers, consistent padding.

### 10. Alerts, Flash Messages & Status Banners
- **Current State:** `.banner.blue`, `.green`, `.amber`, `.red` used by `station.js` and `stage.js`; `.flash.ok`, `.flash.bad` in `base.html`.
- **Issues:** Loud pastel colors clash with warm university palette.
- **Plan:** Harmonize banners while preserving all exact class names and ARIA live regions:
  - Blue: Informational warm navy/slate on soft cream-blue.
  - Green: Celebratory emerald on soft ivory-green.
  - Amber: Ceremonial gold/ochre on warm cream.
  - Red: Formal crimson on soft warm-rose.

### 11. Empty States
- **Current State:** Flat text: `<td colspan="..." class="muted">Nothing to show.</td>` or `<p class="muted">Nobody matches that.</p>`.
- **Issues:** Feels like broken data or an unhandled exception rather than an intentional zero-state.
- **Plan:** Tasteful empty state presentations with an institutional icon/border and clear explanatory sentence.

### 12. Loading States & Progress Indicators
- **Current State:** Tested and wired through `static/busy.js` (`data-busy`, `data-busy-note`, `.is-busy`, `@keyframes busy-spin`).
- **Issues:** Purely functional; colors use generic blue.
- **Plan:** Retain 100% of existing behavior and test contracts in `test_loading_ui.py` while styling the spinner and notes with the warm gold/brown palette.

### 13. Student Profile & Photo Presentation
- **Current State:**
  - `station.html`: `#card-photo` inside `.student`.
  - `stage.html`: `#current-photo` inside `.slot-current`.
  - `admin_students.html`: inline raw HTML (`width: 40px; height: 40px; background: #ccc; ... "No photo"`).
  - `static/placeholder.svg`: cold slate-gray generic icon (`#E2E8F0` / `#94A3B8`).
- **Issues:** Missing photos show ugly gray boxes or broken image icons if an image 404s.
- **Plan:**
  - Redesign `static/placeholder.svg` into an elegant, warm cream and gold MGM University academic profile silhouette with "MGM UNIVERSITY" and "PHOTO UNAVAILABLE" in formal serif lettering.
  - Standardize photo presentation with an academic gold frame (`border: 2px solid var(--color-gold)`).
  - Add client-side error fallback (`onerror="this.src='/static/placeholder.svg'"`) to guarantee no broken image icon ever appears.

### 14. QR Scanning Station Screens (`station.html`)
- **Current State:** High-speed workflow: Scan QR &rarr; Card pops up &rarr; Operator confirms.
- **Strengths:** Optimized keyboard and barcode gun workflow; Enter confirms; rapid debounce.
- **Plan:** Make the scanned student details instantly legible at a distance; elevate the `.confirm` button to a prominent, satisfying ceremonial green; improve camera preview framing; preserve all IDs and attributes.

### 15. Stage Operator Screen (`stage.html`)
- **Current State:** High-stakes screen with CURRENT student, NEXT button (clicker/Page Down), waiting queue, search, skip, take-over.
- **Issues:** Current student card is cramped; buttons lack clear visual hierarchy.
- **Plan:** Magnify the CURRENT student name in formal serif; create a dominant, unmistakable NEXT action button; style the waiting queue with clear position chips; preserve all clicker shortcuts (Page Down, Arrow Right, Esc).

### 16. Queue Station Screen
- **Current State:** Shares `station.html` configured for activity `QUEUE`.
- **Plan:** Benefits directly from the station improvements.

### 17. Admin Console (`admin_home.html`, `admin_dashboard.html`, `_dashboard_body.html`)
- **Current State:** Plain grid of navigation tiles; live 3-second dashboard with funnel table.
- **Plan:** Elevate dashboard tiles with warm iconography and descriptions; render the journey funnel with polished gold-accented progress bars; structure live reporting stats into executive metric cards.

### 18. Import Screens (`admin_import*.html`)
- **Current State:** Multi-step wizard (Upload &rarr; Columns &rarr; Preview &rarr; Summary).
- **Strengths:** Excellent dry-run checks and duplicate warnings.
- **Plan:** Add an intuitive step progress header (Step 1 of 4); improve column mapping select readability; format preview and error tallies into clear summary cards.

### 19. Reports & Exports (`admin_reports.html`, `admin_passes.html`, `admin_audit.html`)
- **Current State:** Categorized report index, tabular previews, CSV/XLSX export links.
- **Plan:** Organize report categories into distinguished academic sections; add clear badge pills for export actions; refine pagination controls.

### 20. Login Screen (`login.html`)
- **Current State:** Bare `<h1>Sign in</h1>` and a 2-field card.
- **Plan:** Transform into a prestigious university ceremony sign-in page: MGM University emblem, formal bilingual or ceremonial headings, warm cream card on ivory canvas, gold-bordered inputs.

### 21. Responsive & Mobile Behavior
- **Current State:** Some elements break out on narrow phone screens (e.g. wide tables, stage grid).
- **Plan:** Ensure flexible grid columns, responsive table scroll wrappers, and touch-friendly button targets (minimum 44px) for handheld scanners and tablets.

### 22. Accessibility
- **Current State:** Good basic ARIA labels and live regions.
- **Plan:** Boost color contrast ratios to exceed WCAG AAA standards (>7:1 for text); provide explicit gold focus rings with `outline-offset: 2px`; ensure high contrast for screen-reader status announcements.

### 23. Visual Hierarchy
- **Current State:** Headings and text sizes are flat and uniform.
- **Plan:** Establish a clear typographic scale: Display H1 (2.2rem serif), Section H2 (1.5rem serif), Card H3 (1.2rem semi-bold), Body (1rem tabular/sans), Meta (0.85rem muted).

### 24. Spacing Consistency
- **Current State:** Mix of inline styles, `padding: .6rem`, `padding: 1rem`, `margin: 1.2rem`.
- **Plan:** Standardize on an 8pt spacing grid (`--space-1: 0.25rem`, `--space-2: 0.5rem`, `--space-3: 0.75rem`, `--space-4: 1rem`, `--space-6: 1.5rem`, `--space-8: 2rem`).

### 25. Information Density
- **Current State:** Too loose on admin data tables, too cramped on operator screens.
- **Plan:** Balance density: high legibility on operator and stage screens (quick glance from distance), high scannability on admin tables.

### 26. Error Recovery & User Feedback
- **Current State:** Plain one-sentence messages adhering to Golden Rule 11.
- **Plan:** Retain the exact one-sentence plain messaging while giving it high-visibility, calm, dignified framing that reduces operator anxiety.

### 27. Navigation Consistency
- **Current State:** Admin pages have disjointed `← Admin` text links.
- **Plan:** Standardize a breadcrumb bar (`Admin / Reports / Not Attended`) with clear hierarchy.

### 28. Destructive Actions
- **Current State:** `.danger-zone` with red border and confirmation phrase.
- **Plan:** Deep crimson styling with formal institutional caution iconography, maintaining all validation phrases and CSRF/password tokens.

### 29. Long-Running Operations
- **Current State:** Managed by `busy.js` with `Please keep this page open. This may take several minutes.`
- **Plan:** Preserve exact text and attributes required by `test_loading_ui.py` while providing clear visual loading feedback.

### 30. Existing Global Loading/Busy Behavior
- **Current State:** `data-busy`, `data-download`, `@keyframes busy-spin`.
- **Plan:** Strictly preserved.

---

## Architectural & Functional Boundaries: What NOT to Touch
1. **Never alter routes, URL paths, or form POST destinations.**
2. **Never change existing IDs and classes queried by JavaScript:**
   - Station: `#scan`, `#card`, `#card-photo`, `#card-name`, `#card-state`, `#card-fields`, `#markers`, `#confirm`, `#camera-details`, `#camera-video`, `#camera-canvas`, `#camera-status`, `#search-prn`, `#search-btn`, `.banner`, `.banner.blue`, `.green`, `.amber`, `.red`, `.student`.
   - Stage: `#next`, `#show-again`, `#home`, `#previous`, `#skip-reason`, `#skip`, `#search-input`, `#search-btn`, `#results`, `#take-over`, `#led`, `#current-photo`, `#current-name`, `#current-programme`, `#depth`, `#waiting`, `.slot-current`.
   - Admin: `#live`, `[data-busy]`, `[data-busy-note]`, `[data-download]`.
   - Caller & LED: `#holding`, `#stage`, `#holding-title`, `#holding-text`, `#photo`, `#name`, `#programme`, `#school`, `#award`, `#caller-name`, `#caller-programme`, `#caller-status`.
3. **Always use `asset_url()` for all CSS and JS assets in templates** as required by `AGENTS.md` and enforced by `test_static_assets.py`.
4. **No schema, migration, or backend logic modifications.**
