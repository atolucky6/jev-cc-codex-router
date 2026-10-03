---
name: Jev Router
description: A debugger inspection bench for Codex routing.
colors:
  bg-primary: "#f5f7f8"
  bg-secondary: "#ffffff"
  bg-card: "#eef2f3"
  border: "#dce3e6"
  text-main: "#203039"
  text-muted: "#566972"
  accent: "#067368"
  accent-soft: "#e4f3ef"
  accent-purple: "#6556a6"
  luna: "#087c70"
  terra: "#2865a2"
  sol: "#796125"
  astra: "#8259a3"
  danger: "#b63737"
  sidebar: "#18272e"
typography:
  headline:
    fontFamily: '"Segoe UI", -apple-system, BlinkMacSystemFont, sans-serif'
    fontSize: "28px"
    fontWeight: 650
    lineHeight: 1.25
    letterSpacing: "-0.025em"
  title:
    fontFamily: '"Segoe UI", -apple-system, BlinkMacSystemFont, sans-serif'
    fontSize: "16px"
    fontWeight: 650
    lineHeight: 1.5
    letterSpacing: "-0.015em"
  body:
    fontFamily: '"Segoe UI", -apple-system, BlinkMacSystemFont, sans-serif'
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: '"Segoe UI", -apple-system, BlinkMacSystemFont, sans-serif'
    fontSize: "12px"
    fontWeight: 600
    lineHeight: 1.5
  mono:
    fontFamily: 'Consolas, "SFMono-Regular", monospace'
    fontSize: "12px"
    lineHeight: 1.5
  metric:
    fontSize: "25px"
    fontWeight: 600
    lineHeight: 1.5
    letterSpacing: "-0.03em"
rounded:
  badge: "4px"
  field: "6px"
  control: "7px"
  panel: "12px"
  modal: "14px"
spacing:
  tight: "7px"
  controls: "9px"
  compact: "12px"
  small: "16px"
  mobile: "18px"
  section: "22px"
  form: "24px"
  frame: "34px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.bg-secondary}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "9px 13px"
  button-secondary:
    backgroundColor: "{colors.bg-secondary}"
    rounded: "{rounded.control}"
    padding: "9px 13px"
    typography: "{typography.label}"
  button-quiet:
    backgroundColor: "transparent"
    textColor: "{colors.text-muted}"
    rounded: "{rounded.control}"
    padding: "5px 9px"
  input:
    textColor: "{colors.text-main}"
    rounded: "{rounded.field}"
    typography: "{typography.mono}"
    padding: "9px 11px"
  panel:
    backgroundColor: "{colors.bg-secondary}"
    rounded: "{rounded.panel}"
  tier-chip:
    typography: "{typography.label}"
    rounded: "{rounded.badge}"
    padding: "3px 7px"
  navigation:
    rounded: "{rounded.control}"
    padding: "11px 12px"
  request-ledger:
    backgroundColor: "{colors.bg-secondary}"
    rounded: "{rounded.panel}"
  request-journey:
    backgroundColor: "{colors.bg-secondary}"
    rounded: "{rounded.panel}"
    padding: "18px 22px 24px"
---

# Design System: Jev Router

## Overview

**Creative North Star: "The Debugger Inspection Bench"**

A cool, light work surface sits beside dark ink navigation. Compact system typography, monospaced identifiers, and ruled records make routing decisions easy to inspect. Teal identifies deliberate actions and selection; tier colors retain their semantic roles.

The system serves a local developer tool: readable evidence, explicit connection states, and practical controls take precedence over decoration. The implementation is standalone HTML with inline CSS and SVG; it ships no raster assets.

**Key Characteristics:**
- Dark navigation beside a cool light workspace.
- Ruled data surfaces and restrained panel borders.
- Teal actions and four named semantic tiers.
- System sans text with monospaced technical values.

## Colors

The palette combines cool neutral surfaces with a restrained teal action color and distinct tier hues. Frontmatter values match the dashboard's root custom properties.

### Primary
- **Inspection Teal** (`accent`): primary buttons, live status, selected journey nodes, and interactive text.
- **Soft Teal** (`accent-soft`): selected comparison tabs.

### Secondary
- **Luna Teal**, **Terra Blue**, **Sol Ochre**, and **Astra Violet**: tier distribution marks and bars. Tier chips use separate pale backgrounds and darker foregrounds captured in the sidecar snippets.
- **Auxiliary Violet** (`accent-purple`): incumbent root token; retain it without expanding its role beyond existing violet treatments.
- **Danger Red** (`danger`): disconnected and error states.

### Neutral
- **Cool Workspace** (`bg-primary`), **White Surface** (`bg-secondary`), and **Quiet Surface** (`bg-card`): canvas, panels, and small neutral tags.
- **Ruled Border** (`border`): structural separators.
- **Main Ink** (`text-main`) and **Muted Ink** (`text-muted`): primary content and secondary descriptions.
- **Navigation Ink** (`sidebar`): persistent navigation background.

**The Semantic Tier Rule.** Keep Luna teal, Terra blue, Sol ochre, and Astra violet consistent across tier marks, distributions, and badges; retain text labels alongside color.

## Typography

**Body Font:** Segoe UI with platform sans fallbacks. **Label/Mono Font:** Consolas with SFMono-Regular and monospace fallbacks.

The hierarchy is compact and functional. Headings use the same system family as controls; technical identifiers use monospace. There is no separate display face.

### Hierarchy
- **Headline:** page title; becomes 25px at the mobile breakpoint.
- **Title:** panel and settings headings.
- **Body:** general interface copy; descriptions also use 13px and compact metadata uses the label size.
- **Label:** semibold field labels and buttons; secondary metadata uses normal weight.
- **Mono:** model identifiers, connection values, and configuration inputs.
- **Metric:** summary values with tabular numerals; becomes 24px on mobile.

**The Technical Value Rule.** Reserve monospace for technical values and retain tabular numerals for measurements.

## Layout

The desktop sidebar is fixed at 208px; the application offsets by the same width. The topbar is 64px high. Main content is centered with a 1600px maximum width and 30px 34px 22px padding. Panels and form grids repeatedly use 22px gaps and interior spacing.

The activity workspace uses a flexible ledger and a 272px aside; the aside becomes 310px from 1500px. At 1180px and below the sidebar is 184px, content gutters are 24px, and the aside is 240px. At 980px and below the ledger and aside stack; the aside temporarily uses two columns.

At 700px and below navigation moves into normal flow above the workspace, with horizontally arranged tabs and no application offset. Main gutters become 16px; panels use 18px interior gutters. The journey becomes two columns, summary values become two columns, and form grids become one column. Request ledger rows become two-column grids with a full-width request title; audit tables keep their scrollable region.

## Elevation & Depth

Panels use tonal separation and fine borders without shadows. Only the prompt comparison modal is lifted: its shadow is `0 18px 60px #00000030`, behind a `#142a36a6` overlay.

**The Flat Work Surface Rule.** Keep ordinary panels flat; reserve lifted depth for the modal.

## Shapes

Panels have gentle corners; controls are tighter and tags tighter still. The frontmatter records the reused corner sizes. Journey nodes and status dots are circular; tier distribution marks use small square corners. Borders are generally one pixel. Icons are inline stroked SVG, usually 18px, rather than font glyphs or image assets.

## Components

### Buttons
Compact semibold controls have a 38px minimum height; small variants use 30px. Teal primary actions darken on hover. Secondary controls use white surfaces and subdued borders; quiet actions remove the border. Focus remains a visible 3px teal outline with a 3px offset. Disabled controls use half opacity and a waiting cursor.

### Chips
Tier chips pair named text with distinct pale semantic backgrounds. Neutral tags use the quiet surface. The exact chip foreground/background pairs belong to component snippets, rather than redefining the root tier colors.

### Cards / Containers
White bordered panels use compact heading regions and ruled subdivisions. Settings panels use 24px padding; request detail panels use 22px. Keep data regions dense enough to compare neighboring records.

### Inputs / Fields
Light fields use one-pixel borders and compact corners. Technical fields are monospaced; search and select controls use sans text. Carets use the action accent. Preserve visible labels, global focus outlines, and disabled settings until configuration loads.

### Navigation
Dark navigation uses muted light text, a darker blue-green hover surface, and a green selected surface with a small trailing marker. Mobile navigation becomes a horizontal tab strip; the marker disappears while the selected background remains.

### Request ledger and journey
Ledger rows use horizontal rules, subtle hover tint, and green selection. Request titles act as buttons and update the inline journey and details. Four numbered nodes use ruled connectors; the last node is solid teal. Mobile rows wrap request text and expose measurements without requiring horizontal scrolling.

### Motion
Button color transitions last 160ms. Distribution fills transition over 200ms with ease-out. Reduced-motion preference disables transitions and animations.

## Do's and Don'ts

### Do:
- **Do** preserve readable labels alongside status and tier colors.
- **Do** use ruled rows and flat bordered panels for inspectable data.
- **Do** retain visible keyboard focus and reduced-motion behavior.
- **Do** stack request rows and forms at the mobile breakpoint.

### Don't:
- **Don't** add ornamental raster imagery to the inspection workspace.
- **Don't** apply modal shadows to ordinary panels.
- **Don't** use technical monospace for ordinary headings and navigation.

Not canonized: the incumbent system-family page heading is documented as shipped functional UI, not promoted into a system display-face rule; no display typography token is introduced.
