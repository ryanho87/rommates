---
name: ROMmates
description: A quiet, precise workbench for curating ROM libraries and handheld collections.
colors:
  primary: "oklch(55% 0.19 286)"
  primary-hover: "oklch(49% 0.2 286)"
  primary-soft: "oklch(93% 0.045 286)"
  on-primary: "oklch(98% 0.005 285)"
  success: "oklch(46% 0.12 150)"
  success-soft: "oklch(94% 0.035 150)"
  warning: "oklch(52% 0.13 75)"
  warning-soft: "oklch(94% 0.05 75)"
  danger: "oklch(52% 0.18 25)"
  danger-soft: "oklch(94% 0.045 25)"
  canvas-light: "oklch(97.8% 0.006 285)"
  surface-light: "oklch(99.2% 0.004 285)"
  surface-raised-light: "oklch(100% 0.003 285)"
  sidebar-light: "oklch(95.4% 0.008 285)"
  border-light: "oklch(88% 0.012 285)"
  border-strong-light: "oklch(78% 0.018 285)"
  text-light: "oklch(25% 0.018 285)"
  text-muted-light: "oklch(49% 0.018 285)"
  text-faint-light: "oklch(61% 0.014 285)"
  canvas-dark: "oklch(16% 0.012 285)"
  surface-dark: "oklch(18.5% 0.014 285)"
  surface-raised-dark: "oklch(22% 0.016 285)"
  sidebar-dark: "oklch(14.5% 0.014 285)"
  border-dark: "oklch(28% 0.018 285)"
  border-strong-dark: "oklch(38% 0.024 285)"
  text-dark: "oklch(91% 0.012 285)"
  text-muted-dark: "oklch(70% 0.018 285)"
  text-faint-dark: "oklch(57% 0.018 285)"
typography:
  headline:
    fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI, system-ui, sans-serif"
    fontSize: "1.25rem"
    fontWeight: 700
    lineHeight: 1.25
    letterSpacing: "-0.025em"
  title:
    fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 700
    lineHeight: 1.35
    letterSpacing: "-0.01em"
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI, system-ui, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.45
    letterSpacing: "normal"
  label:
    fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI, system-ui, sans-serif"
    fontSize: "0.72rem"
    fontWeight: 620
    lineHeight: 1.35
    letterSpacing: "normal"
rounded:
  xs: "6px"
  sm: "9px"
  md: "13px"
  lg: "17px"
  pill: "999px"
spacing:
  xxs: "4px"
  xs: "8px"
  sm: "12px"
  md: "16px"
  lg: "20px"
  xl: "24px"
  xxl: "28px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: "0 12px"
    height: "34px"
  button-secondary:
    backgroundColor: "{colors.surface-light}"
    textColor: "{colors.text-light}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: "0 12px"
    height: "34px"
  input:
    backgroundColor: "{colors.surface-light}"
    textColor: "{colors.text-light}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: "0 10px"
    height: "34px"
  chip:
    backgroundColor: "{colors.surface-light}"
    textColor: "{colors.text-muted-light}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "0 7px"
    height: "24px"
  panel:
    backgroundColor: "{colors.surface-light}"
    textColor: "{colors.text-light}"
    rounded: "{rounded.md}"
    padding: "16px"
---

# Design System: ROMmates

## Overview

**Creative North Star: "The Curator's Workbench"**

ROMmates is a focused workstation for people managing thousands of files, multiple handhelds, and filesystem operations with real consequences. The interface should feel like a well-organized workbench: dense without crowding, calm without becoming vague, and friendly without turning into a novelty. The name carries the joke. The interface earns trust.

The system is desktop-first because comparison, cleanup, and bulk operations benefit from width and density. The phone experience is not a reduced demo. It preserves every essential task through structural adaptation: the sidebar becomes a drawer, tables become compact records, secondary actions move into menus, and controls remain touch-friendly. Light and dark themes follow the operating system so the same restrained violet-tinted vocabulary works in a bright room or a dim one.

ROMmates explicitly rejects decorative retro-gaming launcher styling, arcade cabinet skins, generic card-heavy admin templates, and Finder's slow spatial file-browser feel. Information hierarchy, filenames, system state, and reversible consequences always outrank ornament.

**Key Characteristics:**

- Quiet, dense, and operational.
- Restrained violet accent with explicit semantic state colors.
- Gently rounded, consistently bordered controls and containers.
- Compact rows on desktop, readable records on mobile.
- Visible consequences before filesystem changes.
- Familiar controls that disappear into the task.

**The Consequence-First Rule.** The visual hierarchy must make pending additions, removals, conflicts, destructive actions, and recovery paths easier to see than decoration.

**The Structural Responsiveness Rule.** At 720px and below, restructure the interaction instead of merely shrinking it. At intermediate widths, wrap toolbars and simplify grids before content collides.

## Colors

The palette is a restrained violet-tinted neutral system. Violet identifies actions and selection; green, amber, and red communicate state. No color exists only to decorate a surface.

### Primary

- **Library Violet** (`primary`): primary actions, selected navigation, links, focus, and intentional emphasis.
- **Pressed Library Violet** (`primary-hover`): hover and pressed states for primary actions.
- **Violet Wash** (`primary-soft`): selected rows, active navigation, and quiet emphasis behind violet text.
- **Clean Cartridge Label** (`on-primary`): high-contrast text on primary actions.

### Secondary

- **Verified Green** (`success`, `success-soft`): completed jobs, online devices, unique ROMs, and safe states.
- **Review Amber** (`warning`, `warning-soft`): pending work, possible duplicates, and situations that require judgment.
- **Recovery Red** (`danger`, `danger-soft`): destructive actions, failed jobs, conflicts, and restore warnings.

### Neutral

- **Archive Paper** (`canvas-light`), **Workbench Surface** (`surface-light`), and **Raised Sheet** (`surface-raised-light`): light-theme canvas and surface hierarchy.
- **Quiet Rail** (`sidebar-light`): the light-theme navigation rail and low-emphasis tracks.
- **Hairline Violet Gray** (`border-light`) and **Structural Violet Gray** (`border-strong-light`): light-theme dividers and interactive boundaries.
- **Catalog Ink** (`text-light`), **Annotation Gray** (`text-muted-light`), and **Index Gray** (`text-faint-light`): light-theme text hierarchy.
- **Night Archive** (`canvas-dark`), **Night Workbench** (`surface-dark`), and **Raised Night Sheet** (`surface-raised-dark`): dark-theme canvas and surface hierarchy.
- **Night Rail** (`sidebar-dark`): the dark-theme navigation rail.
- **Night Hairline** (`border-dark`) and **Night Structure** (`border-strong-dark`): dark-theme dividers and boundaries.
- **Night Catalog Ink** (`text-dark`), **Night Annotation** (`text-muted-dark`), and **Night Index** (`text-faint-dark`): dark-theme text hierarchy.

**The One Accent Rule.** Violet is reserved for primary actions, current selection, focus, and actionable text. It must not become decorative wallpaper.

**The State Has Words Rule.** Success, warning, and danger colors must always be reinforced by text, an icon, or a recognizable state label. Color alone never carries meaning.

**The Theme Parity Rule.** Light and dark themes preserve the same hierarchy and semantics. Dark mode is not a separate neon aesthetic.

## Typography

**Display Font:** None. ROMmates has no marketing display layer inside the product.

**Body Font:** Native system sans (`-apple-system`, `BlinkMacSystemFont`, `Segoe UI`, `system-ui`, sans-serif)

**Label/Mono Font:** Native system sans for labels; the browser monospace stack is reserved for filesystem paths, hashes, and technical identifiers.

**Character:** The single system-sans family is neutral, compact, and immediately familiar across macOS, iOS, Android, Linux, and Windows. Hierarchy comes from weight, size, muted color, and spacing rather than a decorative font pairing.

### Hierarchy

- **Headline** (700, 1.25rem, 1.25): route titles and the highest local page heading.
- **Title** (700, 1rem, 1.35): panel headings, group names, and meaningful empty-state titles.
- **Body** (400, 0.875rem, 1.45): primary product copy and general controls. Prose is capped near 72 characters; data tables may run wider.
- **Label** (620, 0.72rem, 1.35): table headings, field labels, metadata, and dense state descriptions.
- **Eyebrow** (750, approximately 0.68rem, 0.08em tracking): rare category labels such as onboarding or device-group ownership. Uppercase is limited to this role.

**The Filename Wins Rule.** Filenames and game titles receive the strongest weight in collection views. Metadata, paths, platform, and operational state step down in that order.

**The Two-Line Limit Rule.** Mobile game titles may use at most two lines. Supporting metadata stays on one compact wrapping row, and long paths use ellipsis or intentional wrapping according to context.

## Elevation

ROMmates is flat by default. Borders, tinted surface layers, and spacing establish structure. Light mode uses one ambient shadow for temporary or raised surfaces; dark mode removes the ambient shadow because tonal layering already establishes depth. Popovers and assignment sheets may use a stronger temporary shadow so they clearly sit above working content.

### Shadow Vocabulary

- **Ambient Lift** (`0 12px 36px oklch(25% 0.02 285 / 12%)`): dialogs, popovers, creation panels, and temporary raised surfaces in light mode only.
- **Focused Overlay** (`0 24px 70px oklch(8% 0.015 285 / 48%)`): centered assignment and confirmation surfaces over a backdrop.
- **Mobile Menu Lift** (`0 14px 34px oklch(8% 0.01 285 / 42%)`): compact action menus that float above a mobile record.
- **Row Separation** (`0 1px 0 oklch(8% 0.01 285 / 18%)`): the lightest possible reinforcement for mobile library cards.

**The Flat-Until-Temporary Rule.** Persistent page structure uses borders and tonal layers. Shadows are reserved for temporary overlays or a control actively floating above content.

**The Single Surface Rule.** Never nest a decorative card inside another decorative card. Use dividers, rows, and spacing inside a container.

## Components

### Buttons

- **Shape:** compact rounded rectangle (`rounded.sm`, 9px desktop and 10px mobile), never a sharp rectangle.
- **Primary:** Library Violet background, Clean Cartridge Label text, 34px desktop height, and 12px horizontal padding. Mobile touch targets grow to at least 44px.
- **Hover / Focus:** hover deepens the primary tone; keyboard focus uses a 2px violet outline with 2px offset. Active state moves by 1px without bounce.
- **Secondary:** workbench surface, neutral text, and a quiet border. Hover strengthens both border and surface.
- **Danger:** solid Recovery Red for confirmed destructive actions. Danger-subtle uses Recovery Red text on its soft tint for staging or review.
- **Text action:** violet text without a container, reserved for low-emphasis inline actions.

### Chips

- **Style:** pill-shaped, 24px minimum height, compact label type, one-pixel border, and semantic tint when stateful.
- **State:** exact duplicates and failures use danger; possible duplicates and pending work use warning; unique, complete, and online use success; neutral states remain on the surface color.
- **Platform chips:** mobile platform identifiers use stronger per-platform color but remain small, uppercase, and subordinate to the title.

### Cards / Containers

- **Corner Style:** standard panels use `rounded.md` (13px desktop and 15px mobile). Prominent groups and bottom sheets use `rounded.lg` (17px desktop and 20px mobile).
- **Background:** use one of the canvas, surface, raised-surface, or sidebar roles. Do not invent isolated fills.
- **Shadow Strategy:** persistent containers are border-led and flat; only temporary raised surfaces receive elevation.
- **Border:** one pixel, using the normal border for separation and strong border for interactive or high-consequence containers.
- **Internal Padding:** 12px for dense panels, 16px for standard panels, and 20px only for onboarding, creation, or spacious empty states.

### Inputs / Fields

- **Style:** 34px desktop height, surface background, one-pixel border, `rounded.sm`, and 10px horizontal padding. Selects reserve space for the native disclosure indicator and ellipsize when constrained.
- **Focus:** 2px Library Violet outline with 2px offset. Focus must remain visible in both themes.
- **Error / Disabled:** use danger text or border with an explicit message. Disabled controls retain their label and use reduced opacity; never remove the reason an action is unavailable.

### Navigation

- **Desktop:** a 224px sticky rail with 36px compact rows, muted inactive labels, and a Violet Wash active state. Counts align to the trailing edge with tabular numerals.
- **Intermediate:** the rail narrows to 174px below 1020px; optional table columns yield before primary information.
- **Mobile:** at 720px and below, the rail becomes an off-canvas drawer behind a visible menu button and backdrop. The current route remains visible in the top bar.
- **State:** hover changes the surface; active state uses both background and border, never color alone.

### Tables and Responsive Records

- **Desktop:** use compact 36px rows, sticky headings, whitespace-preserving data cells, and an explicit horizontal scrolling container when the data truly requires width.
- **Mobile:** convert general tables to labeled records. Library entries become compact rounded rows with a checkbox, a two-line title, a metadata line, and one three-dot action menu.
- **Overflow:** titles wrap or clamp; paths and device names ellipsize with the full value available through context or title text. Page-level horizontal scrolling is forbidden.

### Device Assignment and Groups

- **Route title:** the top bar always says “Devices.” The selected device or group appears immediately below as a large, title-style dropdown so the page location and managed target never compete.
- **ROM views:** two pill controls—“Add ROMs” and “On Device”—are the only primary workspace choices. They switch the list in place without navigating away or duplicating the current target.
- **Secondary flows:** “Create new device,” “Create device group,” and “Download ROMs” appear as quiet links below the view controls. Each opens a focused dialog; onboarding forms never expand inline and push the ROM list down the page.
- **Current target:** show the selected-ROM count and delivery state once beneath the target selector. Ownership is communicated through the selector’s option groups rather than repeated in the selected label.
- **Assignment:** device choices appear in one focused popover with explicit selected state and a review-and-apply footer. Bulk selection uses the same pattern as single-game assignment.
- **Groups:** owner-scoped groups use one grouped roster and inline management menus behind a “Group members” disclosure. Group rows remain dividers inside one container, not individual cards.
- **Progressive disclosure:** ownership controls, hardlink/copy diagnostics, unmatched files, and Syncthing administration are admin-only technical details. They never interrupt the member’s core device flow.
- **Consequences:** show a review action only when pending filesystem changes exist. The confirmation view carries detailed additions and removals before applying.

### Feedback, Jobs, and Empty States

- **Loading:** use skeleton rows that preserve the expected layout. Avoid blocking central spinners.
- **Jobs:** status is always text plus a semantic chip and, when relevant, numeric progress.
- **Empty states:** teach the next useful action in one sentence and one primary or secondary control.
- **Motion:** state transitions run for 120 to 190ms with an ease-out curve. Reduced-motion preference disables nonessential transitions.

## Do's and Don'ts

### Do:

- **Do** optimize for scanning thousands of filenames: compact rows, sticky headings, indexed search, filters, and bulk actions.
- **Do** use the shared radius scale for every fully bordered control or container.
- **Do** keep the violet accent rare enough that primary actions and current selection remain obvious.
- **Do** preview filesystem consequences before rename, trash, restore, deployment, clone, or group synchronization operations.
- **Do** make destructive operations recoverable and show where the recovery path lives.
- **Do** preserve complete functionality on mobile through drawers, record layouts, action menus, and 44px touch targets.
- **Do** test new pages at 1440px, 1024px, 768px, and 390px before shipping.
- **Do** use semantic controls, visible focus, non-color labels, and WCAG 2.1 AA contrast as the baseline.
- **Do** use the filesystem as operational truth while presenting database-backed intent and ownership clearly.

### Don't:

- **Don't** resemble a decorative retro-gaming launcher or an arcade cabinet skin.
- **Don't** build a generic card-heavy admin template. A row or divider is usually enough.
- **Don't** recreate Finder's slow spatial file browser with folders as the primary discovery model.
- **Don't** use oversized dashboard metrics. Collection health must remain proportional to the work around it.
- **Don't** use ornamental game imagery when it competes with filenames, state, or actions.
- **Don't** hide destructive behavior or collapse consequences into vague confirmation copy.
- **Don't** use playful styling that competes with filenames and operational status. The ROMmates name supplies the personality.
- **Don't** use sharp bordered rectangles beside rounded components. The shared radius tokens are mandatory.
- **Don't** allow text, menus, tables, or toolbars to create page-level horizontal overflow.
- **Don't** use gradient text, glassmorphism, colored side-stripe borders, decorative motion, or bounce easing.
- **Don't** invent a new button, chip, input, or card vocabulary for one screen. Extend the central system instead.
- **Don't** rely on color alone for success, warning, danger, ownership, selection, or connectivity.
