"""The design system: tokens, chrome and components, in one stylesheet.

This is a trading workstation, not a document that happens to be dark.  Three
rules decide everything below.

**Low glare, dark first.**  The page is the darkest plane and panels sit a step
above it, so the eye is led by structure rather than by a white sheet.  Text is
never pure white: ``--ink`` tops out at about 0.77 relative luminance for the few
headline figures, body copy sits at ``--ink-body`` (about 0.51), and labels are
deliberately quieter again.  Nothing on screen is brighter than a headline.

**Density where the operator looks.**  A session is read in one glance: a compact
command deck, a ledger of facts, and real tables with 30 px rows and a sticky
header.  Prose is one line, not a paragraph; history folds away behind
``<details>`` so the controls stay above the record.

**One stable frame.**  Every route is drawn inside the same rail, the same status
strip, the same page head and the same ``main`` + ``aside`` grid, so moving
between pages does not move the furniture.  The aside is the page's own compass
and status; it never holds a control that belongs to another lane.

Colour is a second carrier, never the only one: every tone has a word beside it,
every tone colour is checked against its own tint for AA contrast, and the lanes
are named in the strip (LIVE = red, PAPER = blue, sample data = amber).

Everything is plain CSS: no scripts, no fonts to fetch, no images.  The page
survives a strict ``script-src 'none'`` policy, which is what makes it safe to
render whatever a terminal said.
"""

from __future__ import annotations

STYLES = """
:root{
  color-scheme:dark;
  /* Ink: headline figures, body copy, then three quieter steps for labels. */
  --ink:#dde7f1; --ink-body:#b3c2d1; --ink-2:#a3b4c6; --ink-3:#8b9db0; --ink-4:#788b9e;
  /* Planes: charcoal-navy, darker as they go up (page, panel, raised, rail). */
  --paper:#080d13; --surface:#0d151d; --surface-2:#121c26; --surface-3:#182430;
  --line:#1c2833; --line-2:#283949; --line-strong:#3f566c;
  /* Lanes: blue is PAPER, red is LIVE, amber is attention, green passes. */
  --paper-accent:#4f83e8; --paper-accent-ink:#a9c6ff; --paper-accent-hover:#6f9cf5;
  --paper-tint:#0d1b2e;
  --live-accent:#f06a72; --live-accent-ink:#ffb9bc; --live-tint:#2a1216;
  --ok:#4ad295; --ok-ink:#8fe9c3; --ok-tint:#09241a;
  --warn:#e6b656; --warn-ink:#f6d99f; --warn-tint:#241a0c;
  --info:#6fa8f5; --info-ink:#b6d1fb; --info-tint:#0d1c30;
  --focus:#8fb4ff; --ring:rgba(143,180,255,.32); --accent-ink:#06101c;
  --stripe:rgba(255,255,255,.016); --hover:rgba(255,255,255,.03);
  --r-xs:3px; --r-sm:5px; --r-md:7px;
  --s1:4px; --s2:8px; --s3:12px; --s4:16px; --s5:22px; --s6:30px; --s7:44px;
  --font:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
  --rail:#050a0f; --rail-2:#0a1218; --max:1560px; --aside:340px;
  --shadow:0 1px 0 rgba(255,255,255,.025) inset;
}

*,*::before,*::after{box-sizing:border-box}
/* Anchors land below the sticky rail and strip, never underneath them. */
section[id],h2[id],details[id]{scroll-margin-top:96px}
html{-webkit-text-size-adjust:100%;background:var(--rail);scrollbar-color:var(--line-strong) transparent}
body{margin:0;background:var(--paper);color:var(--ink-body);
  font:13.5px/1.5 var(--font);font-variant-numeric:tabular-nums;text-rendering:optimizeLegibility}
h1,h2,h3,h4{margin:0;font-weight:600;letter-spacing:-.01em;line-height:1.25;color:var(--ink)}
h1{font-size:22px;letter-spacing:-.02em}
h2{font-size:15px}
h3{font-size:13.5px;font-weight:600}
p{margin:0}
a{color:var(--paper-accent-ink);text-decoration:none;text-underline-offset:2px}
a:hover{color:#c9dcff;text-decoration:underline;text-decoration-thickness:1px}
code,.mono,.num{font-family:var(--mono);font-size:.94em}
strong{font-weight:600;color:var(--ink)}
small,.small{font-size:12px;color:var(--ink-2)}
hr{border:0;border-top:1px solid var(--line);margin:var(--s4) 0}
::selection{background:#24405f;color:#fff}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:var(--r-xs);box-shadow:0 0 0 4px var(--ring)}
.sr,.visually-hidden{position:absolute!important;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;
  clip:rect(0 0 0 0);clip-path:inset(50%);white-space:nowrap;border:0}
.skip{position:absolute;left:var(--s3);top:-60px;z-index:80;background:var(--paper-accent);color:var(--accent-ink);
  padding:8px 12px;border-radius:var(--r-sm);font-weight:600;transition:top .1s}
.skip:focus{top:var(--s2)}
.muted{color:var(--ink-3)}
.stack--sm>*+*{margin-top:6px}
.list-reset{list-style:none;margin:0;padding:0}

/* ---------------------------------------------------------------- chrome */
.rail{position:sticky;top:0;z-index:50;background:var(--rail);border-bottom:1px solid var(--line-2)}
.rail-in{max-width:var(--max);margin:0 auto;padding:0 var(--s5);display:flex;align-items:center;
  gap:var(--s4);min-height:48px;flex-wrap:wrap}
.brand{display:inline-flex;align-items:baseline;gap:8px;flex:0 0 auto;padding:4px 0}
.brand:hover{text-decoration:none}
.brand-mark{font-size:12px;font-weight:700;letter-spacing:.16em;color:var(--ink)}
.brand-name{font-size:11px;color:var(--ink-4);letter-spacing:.06em;text-transform:uppercase}
.rail-nav{display:flex;gap:1px;min-width:0;flex-wrap:wrap;align-items:center}
.rail-nav a{display:inline-flex;align-items:center;min-height:30px;padding:5px 10px;
  border-radius:var(--r-sm);color:var(--ink-2);font-size:13px;font-weight:500;white-space:nowrap}
.rail-nav a:hover{background:var(--surface-2);color:var(--ink);text-decoration:none}
.rail-nav a[aria-current="page"]{background:var(--surface-3);color:var(--ink);font-weight:600;
  box-shadow:inset 0 -2px 0 0 var(--paper-accent)}
.rail-nav a[aria-current="page"][data-lane="live"]{box-shadow:inset 0 -2px 0 0 var(--live-accent)}
.rail-group{display:flex;align-items:center;gap:1px}
.rail-group-label{font-size:10px;font-weight:700;letter-spacing:.13em;text-transform:uppercase;
  color:var(--ink-4);padding:0 8px}
.rail-sep{width:1px;height:20px;background:var(--line-2);margin:0 var(--s2);flex:0 0 auto}
.rail-end{margin-left:auto;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.rail-end form{margin:0}
.rail-more{position:relative}
.rail-more>summary{list-style:none;cursor:pointer;display:inline-flex;align-items:center;gap:5px;
  min-height:30px;padding:5px 10px;border-radius:var(--r-sm);color:var(--ink-2);font-size:13px}
.rail-more>summary::-webkit-details-marker{display:none}
.rail-more>summary::after{content:'▾';font-size:9px;color:var(--ink-4)}
.rail-more>summary:hover{background:var(--surface-2);color:var(--ink)}
.rail-more[open]>summary{background:var(--surface-2);color:var(--ink)}
.rail-more--current>summary{color:var(--ink);box-shadow:inset 0 -2px 0 0 var(--paper-accent)}
.rail-more-current{color:var(--ink-3);font-size:12px;white-space:nowrap}
.rail-more-panel{position:absolute;right:0;left:auto;top:calc(100% + 6px);z-index:70;
  min-width:min(460px,calc(100vw - 32px));display:grid;grid-template-columns:1fr 1fr;
  gap:var(--s2) var(--s3);padding:var(--s3);border:1px solid var(--line-2);
  border-radius:var(--r-md);background:var(--surface);box-shadow:0 20px 44px -20px #000}
.rail-more-panel .rail-group{display:grid;gap:1px;align-content:start}
.rail-more-panel a{min-height:28px;padding:4px 8px;font-size:12.5px}

/* The status strip: the facts every page shares, stated once.  The lane band is
   the first thing in it, so LIVE is never something an operator has to infer. */
.strip{position:sticky;top:48px;z-index:40;background:var(--rail-2);border-bottom:1px solid var(--line);
  font-size:12px}
.strip-in{max-width:var(--max);margin:0 auto;padding:5px var(--s5);display:flex;align-items:center;
  gap:4px var(--s4);flex-wrap:wrap;min-height:30px}
.strip-fact{display:inline-flex;align-items:baseline;gap:5px;white-space:nowrap}
.strip-fact .k{color:var(--ink-4);text-transform:uppercase;letter-spacing:.1em;font-size:9.5px;font-weight:700}
.strip-fact .v{color:var(--ink-body);font-weight:600}
.strip-fact .v.is-live{color:var(--live-accent-ink)}
.strip a{color:var(--paper-accent-ink)}
.strip .spacer{margin-left:auto}
.lane{display:inline-flex;align-items:center;gap:6px;padding:2px 9px;border-radius:999px;
  font-size:10px;font-weight:700;letter-spacing:.11em;text-transform:uppercase;
  border:1px solid var(--line-2);color:var(--ink-2);background:var(--surface-2)}
.lane--live{background:var(--live-tint);border-color:#5c2830;color:var(--live-accent-ink)}
.lane--paper{background:var(--paper-tint);border-color:#22406b;color:var(--paper-accent-ink)}
.lane--sim{background:var(--warn-tint);border-color:#5a4520;color:var(--warn-ink)}
.lane .dot{width:6px;height:6px;border-radius:50%;background:currentColor}

/* ------------------------------------------------------------------ page */
.shell{max-width:var(--max);margin:0 auto;padding:var(--s4) var(--s5) var(--s7)}
.page-head{padding-bottom:var(--s3);margin-bottom:var(--s4);border-bottom:1px solid var(--line)}
.page-head-row{display:flex;gap:var(--s4);align-items:flex-start;justify-content:space-between;
  flex-wrap:wrap}
.page-head-main{min-width:0;display:grid;gap:3px;max-width:92ch;flex:1 1 60%}
.kicker{display:flex;align-items:center;gap:6px;flex-wrap:wrap;font-size:10px;font-weight:700;
  letter-spacing:.13em;text-transform:uppercase;color:var(--ink-3)}
.page-head .lede{max-width:96ch;color:var(--ink-2);font-size:12.5px;line-height:1.45}
.page-actions{display:flex;gap:6px;flex-wrap:wrap;align-items:center;justify-content:flex-end}
.page-body{display:grid;gap:var(--s4)}
.layout{display:grid;grid-template-columns:minmax(0,1fr) var(--aside);gap:var(--s4);align-items:start}
.layout--single{grid-template-columns:minmax(0,1fr)}
.layout--wide{grid-template-columns:minmax(0,1fr);max-width:none}
.col{display:grid;gap:var(--s4);min-width:0;align-content:start}
.col--aside{position:sticky;top:88px;gap:var(--s3);align-self:start}

/* ----------------------------------------------------------- command deck */
.deck{background:linear-gradient(180deg,var(--surface-2),var(--surface));
  border:1px solid var(--line-2);border-top:2px solid var(--line-strong);
  border-radius:var(--r-md);box-shadow:var(--shadow);overflow:hidden}
.deck--live{border-top-color:var(--live-accent)}
.deck--paper{border-top-color:var(--paper-accent)}
.deck--warn{border-top-color:var(--warn)}
.deck--ok{border-top-color:var(--ok)}
.deck-head{display:flex;gap:var(--s4);justify-content:space-between;align-items:center;
  flex-wrap:wrap;padding:10px var(--s4)}
.deck-title{display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-width:0}
.deck-title .t,.deck-title h2{font-size:14px;font-weight:600;color:var(--ink);letter-spacing:0}
.deck-title .sub{font-size:11.5px;color:var(--ink-3)}
.deck-actions{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.deck-actions form{margin:0}
.deck-readouts{display:grid;grid-template-columns:repeat(auto-fit,minmax(146px,1fr));
  border-top:1px solid var(--line-2);background:var(--paper)}
.ro{padding:9px var(--s4);border-right:1px solid var(--line);min-width:0}
.ro:last-child{border-right:0}
.ro .k{display:block;font-size:9.5px;font-weight:700;letter-spacing:.11em;text-transform:uppercase;
  color:var(--ink-4);margin-bottom:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ro .v{display:block;font-size:18px;font-weight:600;line-height:1.2;letter-spacing:-.01em;
  color:var(--ink);overflow-wrap:anywhere}
.ro .v--sm{font-size:13.5px;font-weight:600;line-height:1.3}
.ro .f{display:block;font-size:11px;color:var(--ink-3);margin-top:2px}
.ro--live .v{color:var(--live-accent-ink)}
.ro--paper .v{color:var(--paper-accent-ink)}
.ro--ok .v{color:var(--ok-ink)}
.ro--warn .v{color:var(--warn-ink)}
.deck-body{border-top:1px solid var(--line-2);padding:var(--s3) var(--s4)}
.deck-foot{border-top:1px solid var(--line);padding:7px var(--s4);font-size:11.5px;
  color:var(--ink-3);display:flex;gap:var(--s3);flex-wrap:wrap;justify-content:space-between}

/* --------------------------------------------------------------- section */
.section{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-md);
  overflow:hidden;box-shadow:var(--shadow)}
.section--plain{background:transparent;border:0;border-radius:0;box-shadow:none}
.section--plain>.section-head{padding-left:0;padding-right:0}
.section-head{display:flex;gap:var(--s3);justify-content:space-between;align-items:flex-start;
  padding:9px var(--s4) 8px;border-bottom:1px solid var(--line);background:var(--surface-2);
  flex-wrap:wrap}
.section--plain>.section-head{background:transparent;border-bottom:1px solid var(--line-2)}
.section-title{display:grid;gap:1px;min-width:0}
.section-title .h{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.section-title .h h2{font-size:13.5px;font-weight:600;letter-spacing:.01em}
.section-note{color:var(--ink-3);font-size:11.5px;max-width:92ch;line-height:1.4}
.section-tools{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.section-body{padding:var(--s3) var(--s4)}
.section-body--tight{padding:var(--s2) var(--s4)}
.section-body--flush{padding:0}
.section-foot{padding:6px var(--s4);border-top:1px solid var(--line);color:var(--ink-3);
  font-size:11.5px;display:flex;gap:var(--s3);flex-wrap:wrap;justify-content:space-between}
.section--flush>.section-head+.section-body{padding:0}
/* Nested regions are not a second frame: a section inside a section keeps the
   heading and drops the box, so a page has one border per level of hierarchy. */
.section .section,.section .section--plain{border:0;box-shadow:none;background:transparent;
  border-radius:0}
.section .section>.section-head{background:transparent;padding-left:0;padding-right:0}
.section .section>.section-body{padding-left:0;padding-right:0}
.fold>.fold{border:0;background:transparent}
.fold .section{border:0;box-shadow:none}

/* ------------------------------------------------------------ facts ledger */
.facts{margin:0;display:grid;grid-template-columns:minmax(120px,min(34%,210px)) minmax(0,1fr);
  column-gap:var(--s3)}
.facts dt{padding:6px 0;border-top:1px solid var(--surface-3);color:var(--ink-3);
  font-size:11.5px;font-weight:600;letter-spacing:.01em}
.facts dd{padding:6px 0;border-top:1px solid var(--surface-3);margin:0;min-width:0;
  overflow-wrap:anywhere;color:var(--ink-body);font-size:13px}
.facts>dt:first-of-type,.facts>dt:first-of-type+dd{border-top:0}
.facts .note{display:block;color:var(--ink-3);font-size:11.5px;margin-top:1px}
.facts--loose dt,.facts--loose dd{padding:8px 0}
.facts--inline{display:flex;flex-wrap:wrap;gap:4px var(--s4)}
.facts--inline .pair{display:flex;gap:5px;align-items:baseline;min-width:0;font-size:12px}
.facts--inline .k{color:var(--ink-4);font-weight:600}
.facts--inline .v{font-weight:600;color:var(--ink-body)}
.facts--tight dt,.facts--tight dd{padding:4px 0}
/* A long ledger reads as two columns of pairs on a wide screen. */
@media (min-width:1200px){
  .facts--split{grid-template-columns:repeat(2,minmax(120px,min(28%,190px)) minmax(0,1fr))}
  .facts--split>dt{border-top:1px solid var(--surface-3)}
  .facts--split>dt:nth-of-type(1),.facts--split>dt:nth-of-type(2),
  .facts--split>dt:nth-of-type(1)+dd,.facts--split>dt:nth-of-type(2)+dd{border-top:0}
}

/* ---------------------------------------------------------------- figures */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));
  border:1px solid var(--line);border-radius:var(--r-md);overflow:hidden;background:var(--surface)}
.stat{padding:10px var(--s4);border-right:1px solid var(--line);min-width:0}
.stat .k{font-size:9.5px;font-weight:700;letter-spacing:.11em;text-transform:uppercase;
  color:var(--ink-4);display:block;margin-bottom:2px}
.stat .v{font-size:20px;font-weight:600;letter-spacing:-.02em;line-height:1.2;display:block;
  color:var(--ink);overflow-wrap:anywhere}
.stat .v--sm{font-size:14px}
.stat .f{display:block;color:var(--ink-3);font-size:11px;margin-top:3px}
.stat--paper .v{color:var(--paper-accent-ink)}
.stat--live .v{color:var(--live-accent-ink)}
.stat--ok .v{color:var(--ok-ink)}
.stat--warn .v{color:var(--warn-ink)}
.stat--danger .v{color:var(--live-accent-ink)}
/* A KPI strip reads as one instrument row, not a wall of tiles. */
.kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:0;
  border:1px solid var(--line);border-radius:var(--r-md);background:var(--surface);overflow:hidden}
.kpi>div{border-right:1px solid var(--line);padding:8px var(--s3);min-width:0}
.kpi>div:last-child{border-right:0}
.kpi .k{display:block;font-size:9.5px;font-weight:700;letter-spacing:.11em;text-transform:uppercase;
  color:var(--ink-4);margin-bottom:2px}
.kpi .v{display:block;font-size:15px;font-weight:600;color:var(--ink);overflow-wrap:anywhere}
.kpi .f{display:block;font-size:11px;color:var(--ink-3);margin-top:1px}

/* ----------------------------------------------------------------- tables */
.table-wrap{overflow-x:auto;border:0;background:transparent;scrollbar-width:thin}
.table{width:100%;border-collapse:collapse;font-size:12.5px}
.table caption{text-align:left;color:var(--ink-3);font-size:11.5px;padding:6px var(--s3);
  border-bottom:1px solid var(--line)}
.table th,.table td{padding:6px 9px;text-align:left;border-bottom:1px solid var(--line);
  vertical-align:top}
.table thead th{position:sticky;top:0;z-index:1;background:var(--surface-3);color:var(--ink-3);
  font-size:9.5px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;white-space:nowrap;
  border-bottom:1px solid var(--line-2)}
.table th.num,.table td.num{text-align:right;font-family:var(--mono);font-size:12px;color:var(--ink)}
.table tbody tr:last-child td{border-bottom:0}
tbody tr:hover td{background:var(--hover)}
.table-empty{color:var(--ink-3);font-size:12.5px;padding:10px var(--s3)}
.table--dense th,.table--dense td{padding:4px 8px}
.table--stack{min-width:100%}

/* --------------------------------------------------------------- blockers */
.blockers{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));
  gap:1px;background:var(--line);border-radius:var(--r-sm);overflow:hidden}
.blockers li{display:grid;grid-template-columns:18px minmax(0,1fr);gap:8px;padding:8px var(--s3);
  background:var(--warn-tint);align-items:start}
.blockers li.is-danger{background:var(--live-tint)}
.blockers .glyph{width:16px;height:16px;border-radius:50%;display:inline-flex;align-items:center;
  justify-content:center;font-size:10px;font-weight:700;background:var(--warn);color:#1a1204;
  margin-top:1px}
.blockers li.is-danger .glyph{background:var(--live-accent);color:#20090b}
.blockers .b-title{display:block;font-weight:600;font-size:12.5px;color:var(--ink)}
.blockers .b-note{display:block;color:var(--warn-ink);font-size:11.5px;margin-top:1px;max-width:90ch}
.blockers li.is-danger .b-note{color:var(--live-accent-ink)}
.blockers .b-code{display:inline-block;font-family:var(--mono);font-size:10.5px;color:var(--ink-4);
  margin-top:2px}
.blockers .do{margin-top:5px;display:flex;gap:6px;flex-wrap:wrap}
/* The same statement rendered as a plain notice list, for older callers. */
.gates{display:grid;gap:1px;background:var(--line);border-radius:var(--r-sm);overflow:hidden}

/* -------------------------------------------------------------- workflow */
.step-list{list-style:none;margin:0;padding:0;display:grid;gap:6px;counter-reset:step}
.step,.guide-step{display:grid;grid-template-columns:auto minmax(0,1fr);gap:6px var(--s3);
  border:1px solid var(--line);border-left:3px solid var(--line-strong);border-radius:var(--r-sm);
  padding:8px var(--s3);background:var(--surface)}
.guide-step{display:block}
.step--live{border-left-color:var(--live-accent)}
.step--paper{border-left-color:var(--paper-accent)}
.step--blocked{border-left-color:var(--warn)}
.step--ready{border-left-color:var(--ok)}
.step-n,.guide-num{font-family:var(--mono);font-size:11px;font-weight:700;color:var(--ink-4);
  padding-top:2px;min-width:1.3em}
.step-body,.guide-detail{display:grid;gap:2px;min-width:0}
.step-body .h{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.step-body .h strong{font-size:13px;color:var(--ink)}
.step-body .why,.guide-why{color:var(--ink-3);font-size:11.5px;max-width:88ch}
.step-body .do{margin-top:4px;display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.step-title{font-weight:600;color:var(--ink);font-size:13px}
.step-num{font-family:var(--mono);font-size:11px;color:var(--ink-4)}
/* The lifecycle: one row of states, the reached ones filled. */
.lifecycle{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr));
  gap:4px;counter-reset:life}
.lifecycle li{position:relative;border:1px solid var(--line);border-radius:var(--r-xs);
  padding:5px 7px;background:var(--surface-2);font-size:11px;color:var(--ink-3)}
.lifecycle li[data-done="1"]{border-color:var(--line-strong);color:var(--ink)}
.lifecycle li[aria-current="step"]{border-color:var(--paper-accent);background:var(--paper-tint);
  color:var(--paper-accent-ink);font-weight:600}
.lifecycle .lc-k{display:block;font-weight:600}
.lifecycle .lc-v{display:block;font-size:10.5px;color:var(--ink-4)}
.lifecycle li[aria-current="step"] .lc-v{color:var(--paper-accent-ink)}

/* ---------------------------------------------------------------- buttons */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:5px;min-height:30px;
  padding:5px 11px;border-radius:var(--r-sm);border:1px solid var(--line-2);
  background:var(--surface-2);color:var(--ink);font:inherit;font-size:12.5px;font-weight:600;
  text-decoration:none;cursor:pointer;white-space:nowrap}
.btn:hover{background:var(--surface-3);border-color:var(--line-strong);color:var(--ink);
  text-decoration:none}
.btn:active{transform:translateY(1px)}
.btn--sm{min-height:26px;padding:3px 9px;font-size:11.5px}
.btn--primary{background:var(--paper-accent);border-color:var(--paper-accent);color:var(--accent-ink)}
.btn--primary:hover{background:var(--paper-accent-hover);border-color:var(--paper-accent-hover);color:var(--accent-ink)}
.btn--danger{background:var(--live-accent);border-color:var(--live-accent);color:#1c070a}
.btn--danger:hover{background:#ff8b91;border-color:#ff8b91;color:#1c070a}
.btn--warn{background:var(--warn);border-color:var(--warn);color:#1a1204}
.btn--warn:hover{background:#f2c877;border-color:#f2c877;color:#1a1204}
.btn--ok{background:var(--ok);border-color:var(--ok);color:#04150e}
.btn--ghost{background:transparent;border-color:var(--line-2);color:var(--ink-2)}
.btn[disabled],.btn[aria-disabled="true"]{opacity:.55;cursor:not-allowed}
.actions{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.actions form{margin:0}

/* ------------------------------------------------------------------ chips */
.chip{display:inline-flex;align-items:center;gap:5px;padding:1px 7px;border-radius:999px;
  border:1px solid var(--line-2);background:var(--surface-2);color:var(--ink-2);
  font-size:10.5px;font-weight:600;letter-spacing:.02em;white-space:nowrap;line-height:1.7}
.chip .dot{width:6px;height:6px;border-radius:50%;background:currentColor;flex:0 0 auto}
.chip--muted{color:var(--ink-3)}
.chip--ok{background:var(--ok-tint);border-color:#1d5540;color:var(--ok-ink)}
.chip--warn{background:var(--warn-tint);border-color:#5a4520;color:var(--warn-ink)}
.chip--danger,.chip--live{background:var(--live-tint);border-color:#5c2830;color:var(--live-accent-ink)}
.chip--paper{background:var(--paper-tint);border-color:#22406b;color:var(--paper-accent-ink)}
.chip--info{background:var(--info-tint);border-color:#254468;color:var(--info-ink)}
.chip--simulated,.chip--fixture{background:var(--warn-tint);border-color:#4d3a19;color:var(--warn-ink)}
.chip--neutral{color:var(--ink-2)}
.chip-row{display:flex;gap:5px;flex-wrap:wrap;align-items:center}
.chip-link{text-decoration:none}
.chip-link:hover .chip{border-color:var(--line-strong);color:var(--ink)}
.chip--lg{font-size:11.5px;padding:2px 9px}

/* --------------------------------------------------------------- notices */
.notice-stack{display:grid;gap:6px;margin-bottom:var(--s3)}
.notice{display:grid;grid-template-columns:20px minmax(0,1fr);gap:8px;align-items:start;
  padding:8px var(--s3);border:1px solid var(--line-2);border-radius:var(--r-sm);
  background:var(--surface-2);color:var(--ink-body);font-size:12.5px}
.notice .glyph{width:18px;height:18px;border-radius:50%;display:inline-flex;align-items:center;
  justify-content:center;font-size:10px;font-weight:700;background:var(--line-2);color:var(--ink);
  margin-top:1px}
.notice .n-title{display:block;font-weight:600;font-size:12.5px;color:var(--ink)}
.notice .n-body{display:block;color:var(--ink-2);margin-top:1px;max-width:112ch}
.notice--ok{border-color:#1d5540;background:var(--ok-tint)}
.notice--ok .glyph{background:var(--ok);color:#04150e}
.notice--ok .n-title{color:var(--ok-ink)}
.notice--warn{border-color:#5a4520;background:var(--warn-tint)}
.notice--warn .glyph{background:var(--warn);color:#1a1204}
.notice--warn .n-title{color:var(--warn-ink)}
.notice--danger{border-color:#5c2830;background:var(--live-tint)}
.notice--danger .glyph{background:var(--live-accent);color:#20090b}
.notice--danger .n-title{color:var(--live-accent-ink)}
.notice--info{border-color:#254468;background:var(--info-tint)}
.notice--info .glyph{background:var(--info);color:#06101c}
.notice--info .n-title{color:var(--info-ink)}
.notice--muted{background:var(--surface-2)}
/* A qualifier, not a card: one line that colours its own left edge. */
.inline-note{margin:0 0 var(--s2);padding:2px 0 2px 9px;border-left:2px solid var(--warn);
  color:var(--ink-3);font-size:11.5px;line-height:1.45}
.inline-note strong{color:var(--ink-2);font-weight:600}
/* The simulated-data bar: one line, the long sentence behind details. */
.dataflag{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;padding:5px var(--s3);
  border:1px solid #5a4520;border-left:3px solid var(--warn);border-radius:var(--r-xs);
  background:var(--warn-tint);color:var(--warn-ink);font-size:11.5px}
.dataflag strong{color:var(--warn-ink);font-weight:600}
.dataflag .glyph{font-weight:700;color:var(--warn)}
.dataflag-more{color:var(--warn-ink)}
.dataflag-more>summary{cursor:pointer;font-weight:600;font-size:11px;color:var(--ink-2)}
.dataflag-more .n-body{display:inline;color:var(--ink-2);margin-left:6px}

/* ----------------------------------------------------------- folds & jumps */
.fold{border:1px solid var(--line);border-radius:var(--r-md);background:var(--surface);overflow:hidden}
.fold>summary{display:flex;gap:6px var(--s3);align-items:center;flex-wrap:wrap;cursor:pointer;
  padding:8px var(--s4);list-style:none;background:var(--surface-2)}
.fold>summary::-webkit-details-marker{display:none}
.fold>summary::after{content:'▸';color:var(--ink-4);font-size:10px;margin-left:auto}
.fold[open]>summary::after{content:'▾'}
.fold>summary:hover{background:var(--surface-3)}
.fold-h{font-size:13px;font-weight:600;color:var(--ink)}
.fold-n{color:var(--ink-3);font-size:11.5px}
.fold-body{padding:var(--s3) var(--s4);border-top:1px solid var(--line)}
.fold-body>.section,.fold-body>.table-wrap{border:0;border-radius:0;box-shadow:none}
.disclosure{border:1px solid var(--line);border-radius:var(--r-sm);background:var(--surface-2)}
.disclosure>summary{cursor:pointer;padding:6px var(--s3);font-size:12px;color:var(--ink-2);
  font-weight:600}
.disclosure[open]>summary{border-bottom:1px solid var(--line)}
.disclosure>p,.disclosure>div{padding:var(--s3);font-size:12px;color:var(--ink-2)}
.anchor-nav{display:flex;gap:2px;flex-wrap:wrap;align-items:center;position:sticky;top:78px;
  z-index:30;background:var(--paper);padding:5px 0;margin-bottom:2px;
  border-bottom:1px solid var(--line)}
.anchor-nav a{font-size:11.5px;font-weight:600;color:var(--ink-3);text-decoration:none;
  padding:3px 9px;border:1px solid transparent;border-radius:999px;white-space:nowrap}
.anchor-nav a:hover{color:var(--ink);background:var(--surface-2);border-color:var(--line-2)}
.page-jump ol{margin:0;padding-left:16px;font-size:12px}
.page-jump a{color:var(--ink-2)}
.page-jump a:hover{color:var(--ink)}

/* ------------------------------------------------------------- forms etc. */
.field{display:grid;gap:3px;max-width:520px;margin-bottom:var(--s3)}
.field label,.field .label{font-size:11.5px;font-weight:600;color:var(--ink-2)}
.field input,.field select,.field textarea{background:var(--surface-3);border:1px solid var(--line-2);
  border-radius:var(--r-xs);color:var(--ink);font:inherit;font-size:12.5px;padding:6px 8px;min-height:30px;
  width:100%}
.field input:hover,.field select:hover,.field textarea:hover{border-color:var(--line-strong)}
.field input:focus,.field select:focus,.field textarea:focus{border-color:var(--focus);outline:2px solid var(--focus);outline-offset:1px}
.field .hint,.field .err{font-size:11px;color:var(--ink-3)}
.field .err{color:var(--live-accent-ink)}
.field--check{display:flex;gap:7px;align-items:flex-start}
.field--check input{width:16px;min-height:16px;margin-top:2px;flex:0 0 auto}
fieldset.field{border:0;padding:0;margin:0 0 var(--s3);max-width:none}
fieldset.field>legend{font-size:11.5px;font-weight:600;color:var(--ink-2);padding:0 0 4px}
.segmented{display:inline-flex;border:1px solid var(--line-2);border-radius:var(--r-sm);overflow:hidden}
.segmented label{display:inline-flex;align-items:center;gap:5px;padding:5px 10px;font-size:12px;
  background:var(--surface-2);color:var(--ink-2);cursor:pointer;border-right:1px solid var(--line)}
.segmented label:last-child{border-right:0}
.segmented label:has(input:checked){background:var(--paper-tint);color:var(--paper-accent-ink);font-weight:600}
.segmented input{accent-color:var(--paper-accent);margin:0}
.form{display:grid;gap:var(--s2)}
.form-row{display:flex;gap:var(--s3);flex-wrap:wrap;align-items:flex-end}
.form-row>.field{flex:1 1 200px;margin-bottom:0}
/* A sequence of one-shot steps reads as a grid of steps, not a stack of forms. */
.form-steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:6px}
.form-step{position:relative;border:1px solid var(--line);border-left:3px solid var(--live-accent);
  border-radius:var(--r-sm);background:var(--surface-2);padding:8px var(--s3) 4px}
.form-step .fs-n{position:absolute;top:6px;right:8px;font-family:var(--mono);font-size:10px;
  color:var(--ink-4)}
.form-step .field{margin-bottom:6px;max-width:none}
.form-step .form-row>.field{flex:1 1 140px}
.form-step .btn{width:100%}
.template{background:var(--rail-2);border:1px solid var(--line);border-radius:var(--r-sm);
  color:var(--ink-2);font-family:var(--mono);font-size:11.5px;padding:8px 10px;overflow:auto;
  white-space:pre;max-height:340px}
.check-list{list-style:none;margin:0;padding:0;display:grid;gap:3px;font-size:12.5px}
.check-list li{display:flex;gap:7px;align-items:flex-start}
.check-list li::before{content:'·';color:var(--ink-4);font-weight:700}
.meter{height:4px;border-radius:999px;background:var(--surface-3);overflow:hidden;margin-top:5px}
.meter>span{display:block;height:100%;background:var(--paper-accent)}
.meter--ok>span{background:var(--ok)}
.meter--warn>span{background:var(--warn)}
.progress{height:6px;border-radius:999px;background:var(--surface-3);overflow:hidden}
.progress>span{display:block;height:100%;background:var(--paper-accent)}

/* ------------------------------------------------------------- empty & misc */
.empty{display:grid;gap:3px;justify-items:start;padding:var(--s3) 0;color:var(--ink-2)}
.e-title{font-weight:600;color:var(--ink);font-size:13px}
.e-body{font-size:12.5px;color:var(--ink-3);max-width:88ch}
.e-actions{margin-top:5px;display:flex;gap:6px;flex-wrap:wrap}
.events{list-style:none;margin:0;padding:0;display:grid;gap:0}
.events li{display:grid;grid-template-columns:118px minmax(0,1fr);gap:var(--s3);
  padding:5px 0;border-bottom:1px solid var(--line);font-size:12.5px}
.events li:last-child{border-bottom:0}
.events .when{font-family:var(--mono);font-size:11px;color:var(--ink-4);white-space:nowrap}
.events .what{min-width:0;color:var(--ink-body)}
.events .meta{display:block;color:var(--ink-3);font-size:11px}
.events--attention li{background:var(--warn-tint);padding-left:6px;padding-right:6px}
.footer{margin-top:var(--s6);padding-top:var(--s3);border-top:1px solid var(--line);
  display:flex;gap:var(--s3) var(--s5);flex-wrap:wrap;justify-content:space-between;
  font-size:11px;color:var(--ink-4)}
.footer p{font-size:11px;color:var(--ink-4)}
.labels{display:flex;gap:5px;flex-wrap:wrap;align-items:center;font-size:10.5px;color:var(--ink-4)}
.spacer{flex:1 1 auto}
.pager{display:flex;gap:8px;align-items:center;font-size:12px;padding-top:var(--s2)}
.spinner{width:14px;height:14px;border-radius:50%;border:2px solid var(--line-strong);
  border-top-color:var(--paper-accent);animation:spin 1s linear infinite;display:inline-block}
@keyframes spin{to{transform:rotate(360deg)}}
.scroll-list{max-height:220px;overflow:auto}
.pick-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:5px;max-height:360px;overflow:auto}
.pick{display:flex;gap:7px;align-items:center;padding:4px 7px;border:1px solid var(--line);
  border-radius:var(--r-xs);background:var(--surface-2);font-size:12px}
.candidate-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:6px}
.candidate{border:1px solid var(--line);border-radius:var(--r-sm);padding:7px 9px;background:var(--surface-2)}
.cand-meta{color:var(--ink-3);font-size:11px}
.split-callout{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:var(--s3)}
.callout-tag{font-size:10px;font-weight:700;letter-spacing:.11em;text-transform:uppercase;color:var(--ink-4)}
.callout-text{font-size:12.5px;color:var(--ink-2)}
.chart{overflow-x:auto;border:1px solid var(--line);border-radius:var(--r-sm);background:var(--surface-2);
  padding:var(--s2)}
.chart svg{display:block;min-width:420px;max-width:100%;height:auto}
.job{display:grid;gap:5px;border:1px solid var(--line);border-radius:var(--r-sm);padding:8px var(--s3);
  background:var(--surface-2)}
.job-head{display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:space-between}
.split{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:var(--s4)}
.advanced{margin-top:var(--s5);border:1px solid var(--line);border-radius:var(--r-sm);
  background:var(--surface)}
.advanced>summary{cursor:pointer;padding:7px var(--s3);font-size:12px;color:var(--ink-2);font-weight:600}
.advanced>div{padding:var(--s3);border-top:1px solid var(--line);display:flex;gap:6px;flex-wrap:wrap}
.page-scope{display:none}
.guide-head{display:flex;gap:var(--s3);align-items:center;flex-wrap:wrap}
.guide-status{margin-left:auto}
.guide-blockers{display:grid;gap:6px}
.guide-steps{display:grid;gap:6px;list-style:none;margin:0;padding:0}
.live-banner,.mode-banner{display:flex;align-items:center;gap:4px 10px;flex-wrap:wrap;
  padding:7px var(--s3);border-radius:var(--r-sm);border:1px solid var(--line-2);
  background:var(--surface-2);font-size:12.5px;border-left:3px solid var(--line-strong)}
.live-banner .lb-title{font-weight:600;color:var(--ink);font-size:12.5px}
.live-banner .lb-note{color:var(--ink-3);font-size:11.5px;max-width:92ch}
.live-banner--live-off{border-left-color:var(--ok);background:var(--surface-2)}
.live-banner--live-armed{border-left-color:var(--warn);background:var(--warn-tint)}
.live-banner--live-armed .lb-title{color:var(--warn-ink)}
.live-banner--live-on{border-left-color:var(--live-accent);background:var(--live-tint)}
.live-banner--live-on .lb-title{color:var(--live-accent-ink)}
.mode-banner{border-left-color:var(--paper-accent);background:var(--paper-tint);display:grid;gap:4px;
  padding:var(--s3) var(--s4)}
.mode-actions{display:flex;gap:6px;flex-wrap:wrap}
.h{display:flex;gap:6px;align-items:center;flex-wrap:wrap}

/* --------------------------------------------------------- dark work plane */
.section--plain>.section-head>.section-title>.h>h2{color:var(--ink)}
.notice .n-body a{color:var(--paper-accent-ink)}
.section-note a{color:var(--paper-accent-ink)}

/* -------------------------------------------------------------- responsive */
@media (max-width:1080px){
  .layout{grid-template-columns:minmax(0,1fr)}
  .col--aside{position:static;grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
  .split,.split-callout{grid-template-columns:minmax(0,1fr)}
}
@media (max-width:860px){
  .rail-in{padding:0 var(--s3);gap:var(--s2);min-height:44px}
  /* One scrolling row of links, not four wrapped ones: the chrome must not eat
     the viewport before the page starts.  The menu stays a real disclosure. */
  .rail-nav{order:3;width:100%;flex-wrap:nowrap;gap:1px;padding-bottom:3px;
    overflow-x:auto;overflow-y:hidden;scrollbar-width:none;-webkit-overflow-scrolling:touch}
  .rail-nav::-webkit-scrollbar{display:none}
  .rail-group{flex-wrap:nowrap}
  .rail-nav a{min-height:28px;padding:4px 8px;font-size:12.5px}
  .rail-more{flex:0 0 auto}
  .rail-more-panel{position:static;min-width:0;grid-template-columns:minmax(0,1fr);
    margin-top:3px;box-shadow:none;width:min(420px,78vw)}
  .rail-sep{display:none}
  .strip{position:static;top:auto}
  .strip-in{padding:4px var(--s3);gap:3px var(--s3);flex-wrap:nowrap;overflow-x:auto;
    scrollbar-width:none}
  .strip-in::-webkit-scrollbar{display:none}
  .strip-fact{flex:0 0 auto}
  .anchor-nav{position:static}
  .shell{padding:var(--s3) var(--s3) var(--s6)}
  h1{font-size:20px}
  .events li{grid-template-columns:minmax(0,1fr)}
  .facts{grid-template-columns:minmax(0,1fr);column-gap:0}
  .facts dt{border-top:0;padding-bottom:0}
  .facts dd{padding-top:0;border-top:0}
  .facts>dt{padding-top:10px}
  .facts>dt:first-of-type{padding-top:0}
  .blockers{grid-template-columns:minmax(0,1fr)}
}
@media (max-width:620px){
  body{font-size:13px}
  h1{font-size:19px}
  .shell{padding:var(--s3)}
  .shell{padding:var(--s3) var(--s3) var(--s6)}
  .strip-fact{font-size:11.5px}
  .brand-name{display:none}
  .anchor-nav{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none}
  .anchor-nav::-webkit-scrollbar{display:none}
  .anchor-nav a{flex:0 0 auto}
  .ro{padding:7px var(--s3)}
  .ro .v{font-size:16px}
  .deck-head{padding:8px var(--s3)}
  .section-head{padding:7px var(--s3) 6px}
  .section-body{padding:var(--s2) var(--s3)}
  .section-body--flush{padding:0}
  /* Tables become cards: each row is a labelled block, nothing scrolls sideways. */
  .table-wrap--stack{overflow:visible;border:0;background:transparent}
  .table--stack thead{position:absolute;width:1px;height:1px;overflow:hidden;
    clip:rect(0 0 0 0);clip-path:inset(50%);white-space:nowrap}
  .table--stack tr{display:block;border:1px solid var(--line);border-radius:var(--r-sm);
    margin-bottom:6px;padding:5px var(--s3);background:var(--surface)}
  .table--stack tbody tr:nth-child(even) td{background:transparent}
  .table--stack td{display:grid;grid-template-columns:minmax(78px,36%) minmax(0,1fr);
    gap:var(--s2);padding:3px 0;border:0;text-align:left}
  .table--stack td::before{content:attr(data-label);color:var(--ink-4);font-size:10.5px;
    font-weight:700;letter-spacing:.06em;text-transform:uppercase}
  .table--stack td.num{text-align:left}
  .table--stack td:not([data-label]){grid-template-columns:minmax(0,1fr)}
  .table--stack td:not([data-label])::before{content:none}
  .page-head-row{gap:var(--s2)}
  .page-actions{width:100%;justify-content:flex-start}
  .page-actions .btn,.actions .btn{flex:1 1 auto}
  .kpi{grid-template-columns:repeat(auto-fit,minmax(120px,1fr))}
  .kpi>div{border-right:0;border-bottom:1px solid var(--line)}
  .step,.guide-step{grid-template-columns:minmax(0,1fr);gap:2px}
  .step-n{padding-top:0}
  .pick-grid{grid-template-columns:minmax(0,1fr);max-height:none}
  .split-callout{grid-template-columns:minmax(0,1fr)}
  .chart{padding:var(--s1)}
  .chart svg{min-width:0}
  .lifecycle li[aria-current="step"]::after{content:none}
  .candidate-list{grid-template-columns:minmax(0,1fr)}
  .guide-head{flex-wrap:wrap}
  .guide-status{margin-left:0}
  .field,fieldset{max-width:none}
  .col--aside{grid-template-columns:minmax(0,1fr)}
}
@media (prefers-reduced-motion:reduce){
  *{transition-duration:.01ms!important;animation-duration:.01ms!important}
  .spinner{animation:none;border-top-color:var(--line-strong)}
}
@media print{
  .rail,.strip,.page-actions,.skip,.anchor-nav{display:none}
  body{background:#fff;color:#111}
  .section{border-color:#bbb;break-inside:avoid}
}
"""

#: Breakpoints the layout is built and tested against, narrow to wide.
BREAKPOINTS: tuple[int, ...] = (620, 860, 1080)
