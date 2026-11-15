# The trading workstation — what changed, and how it was checked

This note records the second redesign pass, the LIVE workflow it exposes, the
checks that were actually run, and the gates that are still open. It replaces
the earlier note of the same name: that build was rejected by the operator as
*"blindingly light, shifting the layout, and not a trading workstation"*, and
this one starts from that verdict rather than defending it.

## 1. Why the previous build was rejected, in its own screenshots

The measurements below come from the running workstation (`--simulate`) in a
headless Chromium, at 1440×900 and 390×844, before this pass:

| What the operator saw | What the screenshot showed |
| --- | --- |
| "blindingly light" | body copy at `#e9eff7` on `#0a0f15` — a 16.6:1 glare surface, near-white text across every paragraph, plus a large light-on-dark label layer |
| "shifts the layout" | the run pages changed shape between routes: `/live` was a two-column document, `/autopilot` a long single column, and the aside moved and re-flowed per page |
| "not a trading workstation" | no command surface: `/live` opened with a full-width red card, then prose; `/autopilot` was 5 201 px of stacked cards at 1440 px, with the run controls buried above a record nobody could reach |
| "LIVE not clear" | the continuous session was one card among twelve, the answer to *"will the next signal send a real order?"* sat in a nested red block, and the one-shot bridge read like a second autopilot |

## 2. What changed

### Theme (`axiom_forex/workstation/ui/theme.py`)

* Charcoal-navy planes: page `#080d13`, panel `#0d151d`, raised `#121c26`,
  rail `#050a0f`. `color-scheme:dark` on `:root`, in the document `<meta>`, and
  on the browser's own widgets.
* **Glare is capped, not maximised.** Headlines are the brightest thing on the
  screen (`--ink #dde7f1`, ≈0.77 relative luminance); body copy is deliberately
  dimmer (`--ink-body #b3c2d1`, ≈0.51) and still clears AAA (≥7:1) against the
  page. Labels step down again (`--ink-3`, `--ink-4`).
* Terminal density: 13.5 px base, 30 px table rows, 30 px buttons, 48 px rail,
  30 px status strip, 5 px/8 px spacing steps.
* One accent grammar: red is LIVE, blue is PAPER, amber is attention, green is a
  passing check. Each tone colour is checked against its own tint for AA.
* Print remains the one light surface; reduced motion is honoured.

### Chrome (`shell.py`)

* The status strip now **opens with the lane** — `SIMULATED DATA`, `LIVE · REAL
  BROKER` or `PAPER · SIMULATED FILLS` — derived from the session, so a simulator
  is never labelled LIVE and a LIVE lane is read before any figure.
* The LIVE entry in the rail carries `data-lane='live'` and its current-page
  marker is drawn in the LIVE colour.
* Below 860 px the rail is one scrolling row instead of four wrapped ones, and
  the strip scrolls rather than stacking: on a 390 px screen the chrome no longer
  eats 300 px before the page starts.

### The command deck (`components.deck`, `readout`, `kpi`, `workgrid`, `jump_nav_bar`)

Both run pages open with the same object in the same place: a lane-marked deck
whose head carries the title and the run control, whose read-out strip carries
the measured figures, and whose body carries the facts. It is a region named by
its own heading, its controls are real buttons with accessible names, and it
never states a value the caller did not pass in.

### Live execution (`pages/live.py`)

* **The continuous guarded session is the deck.** Session state, whether it is
  sending real orders, orders recorded/unresolved, broker-reported positions and
  quote health are the read-outs; the pass/fail facts (strategy, account and
  fingerprint, mode, pair and broker symbol, timeframe, plan match, armed, feed
  detail) sit under them; start/stop are the bridge's own actions in the deck
  head — one control, one location, one order route.
* The *answer* banner is one row: **"The next strategy signal WILL send a real
  order"** / *"Armed, but no session is running"* / *"No real order can be sent
  right now"*, with the state chip at the right edge.
* Blockers stay inline with the control that clears each one (`mode_not_live` →
  *Switch the mode to LIVE*; `not_armed` → *Arm the plan with the phrase*; …), and
  a start button appears only when the bridge reports no blockers.
* The one-shot AX-SCALPER intent is a separate section, labelled `ONE-SHOT`, whose
  four typed steps (record → review → approve → arm) are laid out as steps and
  which says *"not continuous"* and *"Nothing here loops"*. It never borrows the
  deck, and it stays unavailable while no candidate has passed independent
  admission.
* The page is indexed at the top (session · plan · one-shot · account · broker
  stop), every anchor lands below the sticky chrome, and long ledgers read as two
  pairs per row above 1 200 px.

### The PAPER command centre (`pages/autopilot.py`)

* The deck carries loop state, open simulated positions, net P&L after costs,
  quote freshness and the execution mode, with **one** start/stop control and the
  paper-only boundaries in its foot.
* Paper P&L follows the controls. The AX-SCALPER paper lane, the LIVE handoff, the
  paper runner, Kronos and the latency record fold as reference, with their ids,
  headings and tables intact.
* Desktop height at 1440 px: **5 201 px → 1 714 px**; the whole run surface now
  fits in one screen above the fold.

### Every other route

Same rail, same strip, same page head, same `main` + `aside` grid; nested regions
no longer draw a second frame; anchored sections clear the sticky chrome. Long
research pages keep their sticky index and their folds, and now carry the same
sticky band of anchors as the run pages (`/strategies`, `/kronos`, `/mode`,
`/quick-setup`, `/provider-setup`), built from the target list their in-page index
already used.

## 3. Tests

| Check | Result |
| --- | --- |
| `tests/test_workstation_command_deck.py` (new, 22 tests) | passed |
| workstation/LIVE/paper/monitoring/redesign/revamp/symbols/autopilot UI files | 335 passed |
| `pytest -q` (whole suite) | **2 363 passed in 637.50 s** (2 341 before this pass + 22 new) |
| `python -m compileall axiom_forex tests` | clean |
| `git diff --check` | clean |

The new file asserts, in its own words: the glare cap and AAA body contrast, the
lane band on every route and its derivation from the session, the deck anatomy on
both run pages, the LIVE deck's five read-outs, start/stop being the bridge's own
registered actions, a blocked deck offering no start, every blocker carrying its
fix, no submit control without an admitted candidate, the one-shot labelling, no
LIVE action on any PAPER page, no paper action on the LIVE page, the deck's
handlers (start → RUNNING → stop), the honest "nothing changed" answer for a
missing bridge, the chrome/deck state agreement, landmarks/aside/labels, and the
narrow rules that keep the deck usable.

## 4. Visual checks (real browser, real screenshots)

A headless Chromium (`@sparticuz/chromium` 131 + `puppeteer-core`, run with stub
NSS libraries because this sandbox has none) was pointed at the workstation
running `--simulate` on `0.0.0.0:8765`, and at static renders of the *wired* LIVE
states produced from the test suite's own stand-ins (no MT5, no arming, no order).

* **All 16 routes at 1440 and 390 px**: 0 elements with a near-white background,
  0 page-level horizontal overflows, 0 tables escaping their scroller.
* **Document sweep on all 16 routes**: no duplicate `id`, no form control
  without a label, no button or link without an accessible name, exactly one
  `h1`, no `href="#"` placeholder.
* **Keyboard sweep on `/live`**: the first nine tab stops are the skip link, the
  brand, seven rail items and the `Setup & tools` disclosure, each with a 2 px
  visible outline; no trap and no skipped landmark.
* Contrast, measured from the tokens: page `#080d13` at 0.004 relative
  luminance; headline `#dde7f1` at 0.789 (15.6:1); body `#b3c2d1` at 0.528
  (10.7:1, AAA); secondary 9.2:1; meta 7.0:1; the dimmest label 5.6:1 (AA).
* Heights at 1440 px: `/autopilot` 5 201 → 1 714; `/live` 3 272 → 2 269;
  `/` 2 748 → 2 210; `/performance` 4 160 → 3 153; `/positions` 2 766 → 2 056;
  `/activity` 2 174 → 1 618; `/health` 1 950 → 1 437; `/account` 1 611 → 1 111.
* The wired LIVE page was inspected in three states — ready (start offered),
  running (stop offered, *SENDING REAL ORDERS*), blocked (no start, fix inline),
  at 1440 px and at 390 px.
* Two fixes came from looking at the screenshots rather than from a test: the
  page-head row had lost its flex container (primary actions sat under the
  title), and the one-shot steps were four stacked forms 1 200 px tall.

## 5. Gates that are still open

* **No strategy candidate has passed independent admission.** AX-SCALPER live
  submission stays blocked on `/live`; with no typed-intent bridge the page says
  so and offers no submit control at all. Nothing here weakens that admission.
* **LIVE was never armed, started or connected in this environment.** Every LIVE
  state in the screenshots came from the test suite's in-memory stand-ins. The
  ready/running/blocked deck states are exercised by tests, not by a broker.
* **Windows/MT5 were not available here.** The Windows launcher, the terminal
  adapter and the broker path were not executed; `/live` was only rendered.
* Table behaviour at 360–420 px is unchanged: tables marked `table--stack` become
  labelled blocks, and any table not so marked scrolls inside its own box.
