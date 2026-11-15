# The dark workstation — what changed, and how it was checked

This note records the redesign that replaced the light console, the LIVE
restructure that came with it, the checks that were actually run, and the gates
that are still open. It is deliberately short on adjectives and specific about
evidence.

## 1. What changed

### Theme (`axiom_forex/workstation/ui/theme.py`)

* Dark first, and only dark on screen: page `#0a0f15`, panel `#101a24`, raised
  `#15212c`, rail `#060a0f`. `color-scheme:dark` is declared on `:root`, on form
  controls, and in the document `<meta>`, so the browser paints its own widgets
  dark as well. Print is the one light surface.
* Contrast is a test, not a wish: `--ink` 16.6:1 and `--ink-2` 11.1:1 on the page,
  every tone colour (ok / warn / info / live / paper) at 6.6:1 or better on its
  own tint, faint labels `--ink-4` lifted to 5.4:1.
* One stylesheet, one token block, the same three breakpoints (620 / 860 / 1080),
  reduced-motion and print blocks unchanged. No scripts, no fonts, no images.

### Chrome (`axiom_forex/workstation/ui/shell.py`)

* The sample-data warning is one strip with its full sentence behind *details*;
  a `--simulate` session no longer pushes the page it qualifies below the fold.
* The rail is two tiers from one table: **Run** and **Monitor** stay on the row,
  **Set up** and **Advanced** sit behind a labelled *Setup & tools* disclosure,
  and when the current page is in the second tier the summary names it.
* Every route is still reachable from the rail, exactly one link carries
  `aria-current='page'`, and the skip link is the first thing Tab reaches.
* Prose is measured in characters (`max-width:82ch`), not in the width of the
  window.
* Folds: decisions, simulated fills, run steps, observed latency, the stored book,
  the AX-SCALPER paper lane's review/risk/symbol dumps and every strategy-library
  template but the first are one click away, with their ids, tables and counts
  intact. Page heights at 1440 px: `/autopilot` 6456 → 5201, `/strategies`
  16558 → 7145.

### Live execution (`axiom_forex/workstation/ui/pages/live.py`, `app.py`)

* **The continuous guarded session is the first card on `/live`.** It shows state,
  whether it is sending real orders, orders and refusals, unresolved requests,
  broker positions, the quote feed with its age, measured latency, the strategy
  and exact plan, the account the session would trade with its fingerprint, and
  whether an arm has confirmed that fingerprint.
* Every blocker is inline with the control that clears it (`mode_not_live` →
  *Switch the mode to LIVE*, `not_armed` → *Arm the plan with the phrase*, …).
  An unmapped reason still lands on setup rather than a dead end.
* Start appears only when the bridge reports no blockers; stop replaces it while
  it runs. Both post the bridge's own actions, so there is no second order route:
  every order still goes through the guarded runner and the broker adapter.
* `/autopilot` no longer starts a live session. It reports the state and hands
  over to `/live#continuous`. One control location, one real account.
* The AX-SCALPER bridge is labelled one-shot in its title, its chip and its note,
  and says it is not the continuous session above it. It stays blocked while the
  continuous session runs and while no candidate has passed independent
  admission.
* The page is indexed at the top (session · plan · manual session · one-shot ·
  account · broker stop) and a page that has no adapter says so once, in one
  section, instead of three stacked notices.

## 2. Tests actually run

| Check | Result |
| --- | --- |
| `pytest tests -q` (whole suite, final commit) | **2341 passed in 438.06 s** |
| `tests/test_workstation_dark_workstation.py` (new) | 16 passed |
| `tests/test_autopilot_ui.py` (3 bridge tests moved to `/live`) | 20 passed |
| `tests/test_live_workflow.py` (no-adapter wording) | 45 passed with the dark file |
| `compileall axiom_forex tests` | clean |
| `git diff --check c796afd..HEAD` | clean |
| conflict markers (`git grep`) | none |

The new file guards, in its own words: the dark defaults (page luminance, ink
contrast, no white surface outside print), `data-theme='dark'` and the
`color-scheme` meta on every route, the two-tier rail and second-tier current
page, route reachability and exactly one `aria-current`, the continuous panel in
blocked / ready / running states, inline fixes, the unmapped-fix fallback, the
one-shot wording, single control location, and that the live page never posts the
paper loop's actions or the reverse.

## 3. Visual checks (real browser, real screenshots)

A headless Chromium (`@sparticuz/chromium` 153 + `puppeteer-core`) was installed
in the sandbox and pointed at the workstation running `--simulate` on
`127.0.0.1:8901`. Every route was loaded at **1440, 1024, 860, 420 and 360 px**
and measured, not just looked at: **80 route/width combinations.**

* page-level horizontal overflow: **0 of 80**
* elements escaping the viewport outside a scroll container: **0 of 80**
* worst text contrast with its own background: **5.37:1** (a 10.5 px uppercase
  label), everything else higher
* keyboard: first Tab lands on *Skip to content* with a 3 px focus ring; 23 tab
  stops, no focus trap, no stop on a ring-less control, and closed menus are not
  in the tab order
* the second-tier menu fits the viewport at every width (it overflowed by 47 px
  at 1024 px before the fix), and below 860 px the rail wraps instead of
  scrolling so the panel cannot open off-screen

Screenshots inspected during the work: `/`, `/live`, `/autopilot`,
`/performance` at 1440 px; `/`, `/live`, `/autopilot`, `/performance`,
`/positions`, `/health` at 420 px. Two rounds of fixes came from looking at them:
the triple warning at the top of `/live`, the dark-on-dark primary buttons the
audit caught at 1.17:1, and the second-tier menu escaping at 1024 px. A third
came from measuring the rendered height of every route: `/strategies` was
16558 px and `/autopilot` 6456 px before their long tables were folded.

Tables scroll inside their own box on wide screens by design, and tables marked
`table--stack` become labelled blocks at the phone breakpoint. Tables that are
not marked do still scroll inside their own box at 360–420 px; nothing scrolls
the page itself.

## 4. Gates that are still open

* **No strategy candidate has passed independent admission.** AX-SCALPER execution
  stays blocked on `/live` and `/autopilot`; nothing in this change weakens or
  bypasses that admission.
* **LIVE was never armed, started or connected in this environment.** Every state
  shown here came from the simulator or from stand-ins in tests. The continuous
  card's *ready* and *running* states are exercised by tests, not by a broker.
* **No evidence of profitability is claimed anywhere**, and none was produced.
* The one-off `test_the_close_is_not_offered_while_the_loop_is_running` failure
  from the previous phase remains unreproduced (8 lane runs, 4 full suites); the
  stored-book read path it touches is unchanged by this work.

## 5. Windows / MT5 checks still to run

None of these can run in this sandbox (no Windows, no MetaTrader 5 terminal), so
they are listed rather than claimed:

1. Launch on Windows against a real terminal (`--settings mt5_settings.local.json`,
   no `--simulate`) and confirm the rail shows `MT5 · <mode>` rather than
   *simulator*.
2. **Connect**, then confirm the account section matches the terminal's login
   (masked), server, currency and leverage, and that the continuous card's account
   row shows the same fingerprint.
3. Switch to LIVE, type the phrase and the account signature, arm a plan, and
   confirm the continuous card's *Armed on this account* and the start button
   appear only when the bridge reports no blockers.
4. Start the continuous session and confirm: the state moves to RUNNING, the
   stop replaces the start, the order path records the first attempt with its
   retcode, and an unresolved request blocks the next one.
5. Stop it and confirm the plan is disarmed, new submissions stop, and any open
   position stays open at the broker.
6. With the continuous session running, confirm the one-shot AX-SCALPER section
   reports *blocked*; with it stopped and no admitted candidate, confirm it is
   still blocked for the admission reason.
7. Confirm PAPER never sends: run the paper loop from `/autopilot`, then check the
   terminal's journal shows no order from AXIOM.
