# Dashboard audit

Date: 2026-10-04. Scope: standalone `dashboard.html`, after applying the user's updated `DESIGN.md`.

## Implementation integrity verdict

**SHIP at the finish review's visual scope.** The dashboard now uses black-and-white surfaces, Sunlight Yellow primary actions with black text, flat square panels, 2px buttons, and typography weights 400/700. The request ledger and selected-request journey remain the composition. The prior dashboard/design-document mismatch is resolved; this is not a claim of full design or accessibility compliance.

NouvelR is proprietary and unavailable. Manrope is the substitute explicitly permitted by the guide's prose. Variable Latin, Latin-ext, and Vietnamese WOFF2 subsets are embedded in the page, with the OFL license in `docs/fonts/Manrope-OFL.txt`. No runtime font download is required. Tokens are at `dashboard.html:14`, typography at `dashboard.html:31`, buttons at `dashboard.html:80`, and prompt presets at `dashboard.html:199`.

The finish reviewer inspected all eight desktop/phone tab captures. Its sole material finding was pill-shaped prompt presets; the radius was corrected to 2px and final desktop/phone Playground captures were reviewed before the SHIP verdict. No material styling findings remained within that review's scope.

## Executive summary

**Health score: 17/20 — Good.** Remaining findings: P0: 0, P1: 1, P2: 3, P3: 0.

| Dimension | Score | Evidence / remaining gap |
|---|---:|---|
| Accessibility | 3/4 | Automated checks pass; screen-reader coverage unverified |
| Performance | 3/4 | Successful polls replace activity table rows |
| Responsive design | 4/4 | Four tested widths fit; no tested controls below 44px height |
| Theming | 4/4 | Authorized palette, font substitute, and geometry applied |
| Implementation integrity | 3/4 | Settings draft retention and validation gaps remain |
| **Total** | **17/20** | **Good** |

## Remaining findings

### [P1] Settings drafts are silently overwritten

- **Location:** `dashboard.html:1072` (`switchTab`), `dashboard.html:1748` (`loadSettings`).
- Entering or refreshing Settings reloads saved values. Editing a model, visiting another tab, and returning loses the unsaved draft without a dirty-state guard.
- Retain drafts until saved/discarded, or confirm before replacing dirty values; expose an unsaved state. No specific WCAG failure is claimed.
- **Follow-up:** `$impeccable harden`.

### [P2] Saving bypasses declared input constraints

- **Location:** `dashboard.html:779` (numeric constraint example), `dashboard.html:1811` (`saveSettings`).
- Save uses a JavaScript handler without `reportValidity`; numeric bounds/steps and URL validity do not prevent submission. Several blank/zero numeric inputs silently become defaults.
- Validate before POST against backend constraints, retain values, and associate actionable errors with invalid fields. Any WCAG error-identification failure depends on the server response.
- **Follow-up:** `$impeccable harden`.

### [P2] Unchanged polls rebuild the activity ledger

- **Location:** `dashboard.html:1184` (`renderTable`), `dashboard.html:1963` (poll timer).
- Successful three-second polls replace the table body's `innerHTML` even when records are unchanged. Focus restoration exists, but node replacement can interrupt text selection and adds DOM work. No measured latency regression is claimed.
- Skip rendering when records and filters are unchanged; preserve the hidden-tab pause and overlap guard.
- **Follow-up:** `$impeccable optimize`.

### [P2] Pre-existing design sidecar metadata is stale

- **Location:** `.impeccable/design.json:354`; authority: `DESIGN.md:1` and `.impeccable/surfaces/dashboard-html.md:1`.
- The sidecar still describes the earlier teal dashboard world, so metadata consumers may see outdated styling. The root guide and current surface brief describe the authorized direction.
- This is documentation/tooling drift, not a remaining rendered-page mismatch. Refresh the sidecar in a separately scoped documentation pass, preserving the user's guide.
- **Follow-up:** `$impeccable document`.

## Detector interpretation

The Impeccable detector ran once, before final corrections. Its Manrope warning is a false positive against the guide's explicit substitute allowance. Six legacy semantic-color advisories were corrected to source tokens; the switch radius now uses the documented full-radius value (9999px). Em-dash empty-value placeholders are legitimate. There was no second detector run, so the original report is not a clean post-fix detector result.

## Verification and limits

- Chromium/Playwright used synthetic intercepted local API fixtures: no secrets, production prompts, or live configuration writes.
- All four tabs fit at 320, 390, 900, and 1440px with no page-level horizontal overflow, zero tested controls below 44px height, and no browser errors. The wide audit table scrolls within its named keyboard-focusable region. Manrope loaded successfully.
- Axe-core 4.10.3 reported **zero violations** in eight desktop (1440px) / phone (390px) tab checks using WCAG 2 A/AA and 2.1 AA tags. This is automated evidence, not accessibility certification.
- Passed interactions: request selection/details, filters and no-match clearing, dialog Tab trap/inert background/Escape/focus restoration, empty Playground validation, pipeline failure feedback, retained last activity while offline, and disabled Settings after loading failure.
- Inline JavaScript syntax verification passes: `python scripts/check_dashboard.py`.
- Final browser report: `%TEMP%/jev-dashboard-review/design-final-report.json`; screenshots: `design-final-{1440|390}-{dashboard|diffs|settings|playground}.png` in the same directory.
- Safari/Firefox, physical touch devices, screen readers, real upstream success responses, and full browser zoom coverage were not tested. The embedded Python fallback UI is outside this standalone-page pass.

## Recommended actions

1. **[P1/P2] `$impeccable harden`:** Preserve Settings drafts and validate before saving.
2. **[P2] `$impeccable optimize`:** Avoid replacing unchanged activity rows.
3. **[P2] `$impeccable document`:** Refresh the pre-existing sidecar metadata without replacing the user's guide.

Re-run `$impeccable audit` after those fixes to update the score.
