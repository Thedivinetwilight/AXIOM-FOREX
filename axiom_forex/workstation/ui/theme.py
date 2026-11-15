"""The design system: tokens, layout and components, in one stylesheet.

This is a dark-first trading workstation, not a reskin.  The planes are
charcoal-navy and get darker as they go up (page, panel, rail), so a screen
never glares and the eye is led by contrast rather than by white space:

* a **command rail** across the top, in two tiers: the run/monitor links an
  operator uses on every shift stay on one row, and setup/diagnostic pages sit
  behind one labelled disclosure, so sixteen links never become a wall;
* a one-line **status strip** that carries the facts every page shares (loop,
  mode, terminal, capital, read time) once, instead of repeating them per card;
* **facts as ledger rows** and **numbers in real tables** instead of a grid of
  tiles, so a figure always sits beside the label and provenance that qualify it;
* **folds** for history -- decisions, fills, steps, latency, stored books -- so
  the run controls stay above the record rather than a thousand rows below it;
* a single accent grammar: blue is PAPER, red is LIVE, amber is attention, green
  is a passing check.  Colour is never the only carrier of meaning, every tone
  colour is checked against its own tint for AA contrast, and the browser is
  told ``color-scheme:dark`` so its own widgets match.

Everything is plain CSS: no scripts, no fonts to fetch, no images.  The page must
survive a strict ``script-src 'none'`` policy, which is what makes it safe to
render whatever a terminal said.
"""

from __future__ import annotations

STYLES = """
:root{
  color-scheme:dark;
  /* Charcoal-navy, dark first: the page is the darkest plane, panels sit a step
     above it, and nothing here is white. */
  --ink:#e9eff7; --ink-2:#b8c6d6; --ink-3:#93a4b8; --ink-4:#8296ab;
  --paper:#0a0f15; --surface:#101a24; --surface-2:#15212c; --surface-3:#1d2b38;
  --line:#22303d; --line-2:#2c3c4c; --line-strong:#43586d;
  --paper-accent:#4a86ff; --paper-accent-ink:#9dc0ff; --paper-tint:#122340;
  --paper-accent-hover:#6d9fff;
  --live-accent:#ff7a76; --live-tint:#2d1618;
  --ok:#5cd79c; --ok-tint:#0d2820;
  --warn:#f0bb63; --warn-tint:#2b2113;
  --info:#7cb8ff; --info-tint:#0f2338;
  --focus:#9dc0ff; --ring:rgba(157,192,255,.32); --accent-ink:#07101b;
  --stripe:rgba(255,255,255,.022); --hover:rgba(255,255,255,.035);
  --r-xs:3px; --r-sm:5px; --r-md:8px;
  --s1:4px; --s2:8px; --s3:12px; --s4:16px; --s5:24px; --s6:32px; --s7:48px;
  --font:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
  --rail:#060a0f; --rail-2:#0c141b; --max:1440px;
  --shadow:0 1px 0 rgba(255,255,255,.03) inset,0 10px 30px -22px #000;
}
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 var(--font);
  font-variant-numeric:tabular-nums;text-rendering:optimizeLegibility}
h1,h2,h3,h4{margin:0;font-weight:650;letter-spacing:-.014em;line-height:1.22}
h1{font-size:30px}
h2{font-size:19px}
h3{font-size:15px}
p{margin:0}
a{color:var(--paper-accent-ink);text-decoration:underline;text-underline-offset:2px;
  text-decoration-thickness:1px}
a:hover{text-decoration-thickness:2px}
code,.mono,.num{font-family:var(--mono);font-size:.92em}
strong{font-weight:650}
hr{border:0;border-top:1px solid var(--line);margin:var(--s5) 0}
:focus-visible{outline:3px solid var(--focus);outline-offset:2px;border-radius:2px}
.sr{position:absolute!important;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;
  clip:rect(0 0 0 0);clip-path:inset(50%);white-space:nowrap;border:0}
.skip{position:absolute;left:var(--s4);top:-60px;z-index:50;background:var(--paper-accent);color:var(--accent-ink);
  padding:10px 14px;border-radius:var(--r-sm);text-decoration:none;transition:top .12s}
.skip:focus{top:var(--s3)}

/* ------------------------------------------------------------------ rail */
.rail{background:var(--rail);color:var(--ink);border-bottom:1px solid var(--line)}
.rail-in{max-width:var(--max);margin:0 auto;padding:0 var(--s5);display:flex;
  align-items:center;gap:var(--s5);min-height:58px;flex-wrap:wrap}
.brand{display:flex;align-items:baseline;gap:10px;color:var(--ink);text-decoration:none;
  padding:6px 0;flex:0 0 auto}
.brand:hover{text-decoration:none}
.brand-mark{font-weight:700;letter-spacing:.14em;font-size:13px;color:var(--ink)}
.brand-name{font-size:13px;color:var(--ink-3);letter-spacing:.02em}
.rail-nav{display:flex;gap:2px;flex-wrap:wrap;min-width:0}
.rail-nav a{display:inline-flex;align-items:center;min-height:36px;padding:6px 11px;
  border-radius:var(--r-sm);color:var(--ink-2);text-decoration:none;font-size:13.5px;
  font-weight:550;letter-spacing:.01em;white-space:nowrap}
.rail-nav a:hover{background:var(--surface-2);color:var(--ink);text-decoration:none}
.rail-nav a[aria-current="page"]{background:var(--surface-2);color:var(--ink);
  font-weight:650;box-shadow:inset 0 -2px 0 0 var(--paper-accent)}
.rail-nav .rail-group{display:flex;align-items:center;gap:2px}
.rail-sep{width:1px;height:24px;background:var(--line);margin:0 var(--s2);flex:0 0 auto}
.rail-end{margin-left:auto;display:flex;align-items:center;gap:var(--s2);flex-wrap:wrap}
.rail-end form{margin:0}

/* ---------------------------------------------------------- status strip */
.strip{background:var(--rail-2);color:var(--ink-2);border-bottom:1px solid var(--line)}
.strip-in{max-width:var(--max);margin:0 auto;padding:7px var(--s5);display:flex;
  align-items:center;gap:var(--s2) var(--s5);flex-wrap:wrap;font-size:12.5px}
.strip-fact{display:inline-flex;align-items:baseline;gap:6px;white-space:nowrap}
.strip-fact .k{color:var(--ink-4);text-transform:uppercase;letter-spacing:.1em;font-size:10.5px;
  font-weight:650}
.strip-fact .v{color:var(--ink);font-weight:600}
.strip a{color:var(--paper-accent-ink)}

/* ------------------------------------------------------------------ page */
.shell{max-width:var(--max);margin:0 auto;padding:var(--s5) var(--s5) var(--s7)}
.page-head{display:grid;gap:var(--s3);padding-bottom:var(--s4);
  border-bottom:1px solid var(--line-2);margin-bottom:var(--s5)}
.kicker{display:flex;align-items:center;gap:var(--s2);flex-wrap:wrap;
  font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
  color:var(--ink-3)}
.page-head .lede{max-width:74ch;color:var(--ink-2);font-size:15px}
.page-head-row{display:flex;gap:var(--s5);align-items:flex-start;justify-content:space-between;
  flex-wrap:wrap}
.page-head-main{min-width:0;display:grid;gap:var(--s2)}
.page-actions{display:flex;gap:var(--s2);flex-wrap:wrap;align-items:center}
.layout{display:grid;grid-template-columns:minmax(0,1fr) 336px;gap:var(--s5);align-items:start}
.layout--single{grid-template-columns:minmax(0,1fr)}
.layout--wide{grid-template-columns:minmax(0,1fr);max-width:none}
.col{display:grid;gap:var(--s5);min-width:0}
.col--aside{gap:var(--s4)}

/* --------------------------------------------------------------- section */
.section{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-md);
  overflow:hidden}
.section--plain{background:transparent;border:0;border-radius:0}
.section-head{display:flex;gap:var(--s4);justify-content:space-between;align-items:flex-start;
  padding:var(--s4) var(--s5) var(--s3);border-bottom:1px solid var(--line);
  flex-wrap:wrap}
.section--plain>.section-head{padding-left:0;padding-right:0;border-bottom:1px solid var(--line-2)}
.section-title{display:grid;gap:3px;min-width:0}
.section-title .h{display:flex;align-items:center;gap:var(--s2);flex-wrap:wrap}
.section-note{color:var(--ink-3);font-size:13px;max-width:78ch}
.section-tools{display:flex;gap:var(--s2);align-items:center;flex-wrap:wrap}
.section-body{padding:var(--s4) var(--s5) var(--s5)}
.section-body--tight{padding:var(--s3) var(--s5)}
.section-body--flush{padding:0}
.section-foot{padding:var(--s3) var(--s5);border-top:1px solid var(--line);
  color:var(--ink-3);font-size:12.5px;display:flex;gap:var(--s2) var(--s4);
  flex-wrap:wrap;justify-content:space-between}

/* ----------------------------------------------------------------- facts */
.facts{margin:0;display:grid;grid-template-columns:minmax(120px,min(38%,220px)) minmax(0,1fr);
  column-gap:var(--s4)}
.facts dt{padding:8px 0;border-top:1px solid var(--surface-3);color:var(--ink-3);
  font-size:12.5px;font-weight:600;letter-spacing:.01em}
.facts dd{padding:8px 0;border-top:1px solid var(--surface-3);margin:0;min-width:0;
  overflow-wrap:anywhere}
.facts>dt:first-of-type,.facts>dt:first-of-type+dd{border-top:0}
.facts .note{display:block;color:var(--ink-3);font-size:12px;margin-top:2px}
.facts--loose dt,.facts--loose dd{padding:10px 0}
.facts--inline{display:flex;flex-wrap:wrap;gap:var(--s2) var(--s5)}
.facts--inline .pair{display:flex;gap:6px;align-items:baseline;min-width:0}
.facts--inline .k{color:var(--ink-3);font-size:12px;font-weight:600}
.facts--inline .v{font-weight:600}

/* ----------------------------------------------------------------- stats */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));
  border:1px solid var(--line);border-radius:var(--r-md);overflow:hidden;
  background:var(--surface)}
.stat{padding:var(--s4) var(--s4);border-right:1px solid var(--line);
  border-bottom:1px solid var(--line);min-width:0}
.stat:nth-child(n){border-right:1px solid var(--line)}
.stat .k{font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
  color:var(--ink-3);display:block;margin-bottom:6px}
.stat .v{font-size:23px;font-weight:650;letter-spacing:-.02em;line-height:1.15;
  display:block;overflow-wrap:anywhere}
.stat .v--sm{font-size:16px}
.stat .f{display:block;color:var(--ink-3);font-size:12px;margin-top:6px}
.stat--paper .v{color:var(--paper-accent-ink)}
.stat--live .v{color:var(--live-accent)}
.stat--ok .v{color:var(--ok)}
.stat--warn .v{color:var(--warn)}

/* ---------------------------------------------------------------- tables */
.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:var(--r-md);
  background:var(--surface);-webkit-overflow-scrolling:touch}
.section-body--flush .table-wrap{border:0;border-radius:0}
table{border-collapse:collapse;width:100%;font-size:13.5px}
caption{caption-side:top;text-align:left;padding:0 0 var(--s3);color:var(--ink-3);
  font-size:12.5px}
th,td{text-align:left;padding:9px var(--s4);border-bottom:1px solid var(--surface-3);
  vertical-align:top}
thead th{position:sticky;top:0;z-index:1;background:var(--surface-2);color:var(--ink-2);
  font-size:11.5px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
  border-bottom:1px solid var(--line-2);white-space:nowrap}
tbody tr:last-child td{border-bottom:0}
tbody tr:nth-child(even) td{background:var(--stripe)}
td.num,th.num{text-align:right;font-family:var(--mono);font-size:12.5px;
  font-variant-numeric:tabular-nums}
td.mono{font-family:var(--mono);font-size:12.5px}
.table--dense th,.table--dense td{padding:7px var(--s3)}
.table-empty{padding:var(--s6) var(--s5);text-align:center;color:var(--ink-3)}

/* -------------------------------------------------------------- timeline */
.events{list-style:none;margin:0;padding:0}
.events li{display:grid;grid-template-columns:150px 1fr;gap:var(--s2) var(--s4);
  padding:10px 0;border-top:1px solid var(--surface-3)}
.events li:first-child{border-top:0}
.events .when{font-family:var(--mono);font-size:12px;color:var(--ink-3);white-space:nowrap}
.events .what{min-width:0}
.events .meta{color:var(--ink-3);font-size:12.5px;display:flex;gap:var(--s2) var(--s3);
  flex-wrap:wrap;margin-top:2px}
.events--attention li{background:linear-gradient(90deg,var(--warn-tint),transparent 65%)}

/* -------------------------------------------------------------- blockers */
.blockers{list-style:none;margin:0;padding:0;display:grid;gap:var(--s3)}
.blockers li{display:grid;grid-template-columns:auto minmax(0,1fr);gap:var(--s3);
  padding:var(--s3) var(--s4);border:1px solid var(--line);border-left:3px solid var(--warn);
  border-radius:var(--r-sm);background:var(--warn-tint)}
.blockers li.is-danger{border-left-color:var(--live-accent);background:var(--live-tint)}
.blockers .b-title{font-weight:650;display:block}
.blockers .b-note{color:var(--ink-2);font-size:13px;display:block;margin-top:2px}
.blockers .b-code{font-family:var(--mono);font-size:11.5px;color:var(--ink-3);
  display:block;margin-top:4px}

/* --------------------------------------------------------------- notices */
.notice{display:grid;grid-template-columns:auto minmax(0,1fr);gap:var(--s3);
  padding:var(--s3) var(--s4);border:1px solid var(--line);border-left-width:3px;
  border-radius:var(--r-sm);background:var(--surface-2);font-size:13.5px}
.notice .n-title{font-weight:650;display:block}
.notice .n-body{color:var(--ink-2);display:block}
.notice--info{border-left-color:var(--info);background:var(--info-tint)}
.notice--ok{border-left-color:var(--ok);background:var(--ok-tint)}
.notice--warn{border-left-color:var(--warn);background:var(--warn-tint)}
.notice--danger{border-left-color:var(--live-accent);background:var(--live-tint)}
.notice--muted{border-left-color:var(--line-strong)}
.notice-stack{display:grid;gap:var(--s2)}
.glyph{font-family:var(--mono);font-weight:700;font-size:13px;line-height:1.4;
  color:var(--ink-2);min-width:1.2em;text-align:center}
.notice--ok>.glyph{color:var(--ok)} .notice--warn>.glyph{color:var(--warn)}
.notice--danger>.glyph{color:var(--live-accent)} .notice--info>.glyph{color:var(--info)}

/* ----------------------------------------------------------------- chips */
.chip{display:inline-flex;align-items:center;gap:5px;max-width:100%;
  padding:2px 8px;border:1px solid var(--line-2);border-radius:999px;
  font-size:11.5px;font-weight:650;letter-spacing:.02em;background:var(--surface);
  color:var(--ink-2);white-space:nowrap;vertical-align:baseline}
.chip--paper{border-color:#2b4776;background:var(--paper-tint);color:var(--paper-accent-ink)}
.chip--live{border-color:#5d2b2b;background:var(--live-tint);color:var(--live-accent)}
.chip--ok{border-color:#255a43;background:var(--ok-tint);color:var(--ok)}
.chip--warn{border-color:#5a4520;background:var(--warn-tint);color:var(--warn)}
.chip--danger{border-color:#5d2b2b;background:var(--live-tint);color:var(--live-accent)}
.chip--info{border-color:#284a70;background:var(--info-tint);color:var(--info)}
.chip--fixture{border-color:#3b3466;background:#1b1730;color:#c3b8ff}
.chip--simulated{border-color:#33465c;background:#16222e;color:#b3c4d6}
.chip--accent{border-color:#2b4776;background:var(--paper-tint);color:var(--paper-accent-ink)}
.chip--muted{background:var(--surface-2);color:var(--ink-3)}
.chip-link{text-decoration:none;display:inline-flex}
.chip-link:hover .chip{border-color:var(--line-strong);background:var(--surface-2)}
.chip .dot{width:6px;height:6px;border-radius:50%;background:currentColor;flex:0 0 auto}
.chip-row{display:flex;gap:6px;flex-wrap:wrap;align-items:center}

/* --------------------------------------------------------------- buttons */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;
  min-height:36px;padding:7px 14px;border:1px solid var(--line-2);border-radius:var(--r-sm);
  background:var(--surface);color:var(--ink);font:inherit;font-size:13.5px;font-weight:600;
  cursor:pointer;text-decoration:none}
.btn:hover{background:var(--surface-2);border-color:var(--line-strong);text-decoration:none}
.btn--primary{background:var(--paper-accent);border-color:var(--paper-accent);color:var(--accent-ink)}
.btn--primary:hover{background:var(--paper-accent-hover);border-color:var(--paper-accent-hover)}
.btn--danger{background:var(--live-accent);border-color:var(--live-accent);color:var(--accent-ink)}
.btn--danger:hover{background:#ff9b98;border-color:#ff9b98}
.btn--ghost{background:transparent}
.btn--sm{min-height:30px;padding:4px 10px;font-size:12.5px}
.btn[aria-disabled="true"],.btn:disabled{opacity:.55;cursor:not-allowed}
.rail .btn{background:transparent;border-color:var(--line-2);color:var(--ink-2)}
.rail .btn:hover{background:var(--surface-2);border-color:var(--line-strong)}
.actions{display:flex;gap:var(--s2);flex-wrap:wrap;align-items:center}
.actions form{margin:0}

/* ----------------------------------------------------------------- forms */
.form{display:grid;gap:var(--s4)}
.form-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
  gap:var(--s4);align-items:start}
.field{display:grid;gap:5px;min-width:0;max-width:560px}
.field label,.field .label{font-size:12.5px;font-weight:650;color:var(--ink-2)}
.field input[type=text],.field input[type=number],.field input[type=password],
.field input[type=search],.field select,.field textarea{
  width:100%;min-height:38px;padding:7px 10px;border:1px solid var(--line-2);
  border-radius:var(--r-sm);background:var(--surface-2);color:var(--ink);
  font:inherit;font-size:14px;color-scheme:dark}
.field textarea{min-height:84px;resize:vertical}
.field input:focus,.field select:focus,.field textarea:focus{
  border-color:var(--focus);outline:2px solid var(--ring);outline-offset:0}
.field .hint{color:var(--ink-3);font-size:12px}
.field .err{color:var(--live-accent);font-size:12.5px;font-weight:600}
.field--check{display:flex;grid-template-columns:none;gap:10px;align-items:flex-start;
  max-width:none}
.field--check input{width:18px;height:18px;margin-top:2px;flex:0 0 auto}
.field--check .label{font-weight:550;color:var(--ink)}
.field--check .hint{display:block;font-weight:400}
fieldset{border:1px solid var(--line);border-radius:var(--r-md);padding:var(--s4);
  margin:0;min-width:0}
legend{padding:0 6px;font-size:12.5px;font-weight:700;color:var(--ink-2);
  letter-spacing:.02em}
.segmented{display:flex;flex-wrap:wrap;gap:0;border:1px solid var(--line-2);
  border-radius:var(--r-sm);overflow:hidden}
.segmented label{display:inline-flex;align-items:center;gap:7px;padding:8px 12px;
  font-size:13.5px;font-weight:600;cursor:pointer;background:var(--surface);
  border-right:1px solid var(--line)}
.segmented label:last-child{border-right:0}
.segmented input{accent-color:var(--paper-accent);margin:0}
.segmented label:has(input:checked){background:var(--paper-tint);color:var(--paper-accent-ink)}
.form-note{color:var(--ink-3);font-size:12.5px;max-width:78ch}

/* ------------------------------------------------------------------ misc */
.empty{padding:var(--s6) var(--s4);text-align:center;border:1px dashed var(--line-2);
  border-radius:var(--r-md);background:var(--surface)}
.empty .e-title{font-weight:650;display:block;margin-bottom:4px}
.empty .e-body{color:var(--ink-3);font-size:13.5px;max-width:60ch;margin:0 auto}
.empty .e-actions{margin-top:var(--s4);display:flex;gap:var(--s2);justify-content:center;
  flex-wrap:wrap}
.meter{height:6px;border-radius:999px;background:var(--surface-3);overflow:hidden;
  margin-top:8px}
.meter span{display:block;height:100%;background:var(--paper-accent)}
.meter--ok span{background:var(--ok)} .meter--warn span{background:var(--warn)}
.meter--danger span{background:var(--live-accent)}
.split{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));
  gap:var(--s4)}
.page-jump{font-size:13px;color:var(--ink-2);padding:var(--s3) 0}
.page-jump a{text-decoration:none} .page-jump a:hover{text-decoration:underline}
/* ----------------------------------------------------------------- status */
.spinner{width:14px;height:14px;border-radius:50%;flex:0 0 auto;margin-top:3px;
  border:2px solid var(--line-strong);border-top-color:var(--paper-accent);
  animation:spin 900ms linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
/* ---------------------------------------------------------------- charting */
.chart{border:1px solid var(--line);border-radius:var(--r-md);background:var(--surface);
  padding:var(--s3);overflow-x:auto}
.chart svg{display:block;min-width:520px;max-width:100%;height:auto}
.disclosure{border:1px solid var(--line);border-radius:var(--r-md);background:var(--surface-2);
  padding:var(--s3) var(--s4);margin-top:var(--s3)}
.disclosure>summary{cursor:pointer;font-size:13px;font-weight:650;color:var(--ink-2)}
.disclosure[open]>summary{margin-bottom:var(--s3)}
.progress{border-radius:99px;overflow:hidden;background:var(--surface-3);height:10px}
.progress .meter{margin:0;border-radius:0}
/* ------------------------------------------------------- one file, one job */
.job{border:1px solid var(--line);border-radius:var(--r-md);padding:var(--s3) var(--s4);
  background:var(--surface);margin-top:var(--s3)}
.job:first-child{margin-top:0}
.job-head{display:flex;gap:var(--s2);align-items:center;justify-content:space-between;
  flex-wrap:wrap;margin-bottom:var(--s3)}
.job-head h3{margin:0;font-size:13.5px;font-weight:650}
.job-head code{font-size:13px;overflow-wrap:anywhere}

/* --------------------------------------------------------------- lifecycle */
.lifecycle{list-style:none;margin:0;padding:0;display:grid;gap:2px;
  counter-reset:step}
.lifecycle li{display:flex;gap:var(--s2);align-items:baseline;padding:5px 0;
  border-top:1px solid var(--surface-3);color:var(--ink-2);font-size:13px}
.lifecycle li:first-child{border-top:0}
.lifecycle .glyph{color:var(--ink-4)}
.lifecycle li.is-done .glyph{color:var(--ok)}
.lifecycle li[aria-current="step"]{font-weight:650;color:var(--ink)}
.lifecycle li[aria-current="step"] .glyph{color:var(--paper-accent)}
.lifecycle li[aria-current="step"]::after{content:"current";margin-left:auto;
  font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--paper-accent-ink)}
/* The LIVE status banner: one sentence, one word, one tone. */
.live-banner{display:flex;gap:var(--s3);align-items:flex-start;padding:var(--s3) var(--s4);
  border:1px solid var(--line);border-left-width:3px;border-radius:var(--r-sm);
  background:var(--surface-2);margin-bottom:var(--s4)}
.live-banner--live-on{border-left-color:var(--live-accent);background:var(--live-tint)}
.live-banner--live-armed{border-left-color:var(--warn);background:var(--warn-tint)}
.live-banner--live-off{border-left-color:var(--ok);background:var(--ok-tint)}
.live-banner .spacer{flex:1 1 auto}
.mode-banner{display:flex;gap:var(--s3);align-items:flex-start;padding:var(--s3) var(--s4);
  border:1px solid var(--line);border-left-width:3px;border-radius:var(--r-sm);
  background:var(--surface-2);margin-bottom:var(--s4)}
.mode-banner--paper{border-left-color:var(--paper-accent);background:var(--paper-tint)}
.mode-banner--live{border-left-color:var(--live-accent);background:var(--live-tint)}
.mode-banner .spacer{flex:1 1 auto}
.mode-actions{display:flex;gap:var(--s2);flex-wrap:wrap;align-items:center;
  margin-top:var(--s3)}
.split-callout{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));
  gap:var(--s3);margin-bottom:var(--s4)}
.split-callout>div{display:flex;gap:var(--s2);align-items:baseline;
  padding:var(--s3) var(--s4);border:1px solid var(--line);border-radius:var(--r-sm);
  background:var(--surface-2);font-size:13px}
.callout-tag{font-size:10.5px;font-weight:700;letter-spacing:.1em;white-space:nowrap;
  padding:2px 6px;border-radius:var(--r-xs)}
.callout-tag--live{background:var(--live-tint);color:var(--live-accent);
  border:1px solid #5d2b2b}
.callout-tag--paper{background:var(--paper-tint);color:var(--paper-accent-ink);
  border:1px solid #2b4776}
.callout-text{color:var(--ink-2);min-width:0}
.check-list{margin:0;padding-left:18px;display:grid;gap:4px;font-size:13px;
  color:var(--ink-2)}
.list-reset{list-style:none;margin:0;padding:0}

/* ----------------------------------------------------------- symbol picker */
.pick-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;
  max-height:360px;overflow:auto;padding:2px}
.pick{display:flex;gap:8px;align-items:flex-start;border:1px solid var(--line);
  border-radius:var(--r-sm);padding:7px 10px;background:var(--surface);font-size:13px;
  line-height:1.35;min-width:0}
.pick input{margin:2px 0 0;width:16px;height:16px;flex:0 0 auto;accent-color:var(--paper-accent)}
.pick span{min-width:0;overflow-wrap:anywhere}
.pick .why{display:block;font-size:11.5px;color:var(--ink-3);margin-top:2px}
.scroll-list{max-height:220px;overflow:auto;margin-top:var(--s2)}
.template{font-family:var(--mono);font-size:12px;background:var(--surface-2);
  border:1px solid var(--line);border-radius:var(--r-sm);padding:var(--s3);
  overflow:auto;max-height:280px;margin:0}
.guide-steps{list-style:none;margin:0;padding:0;display:grid;gap:var(--s3)}
.guide-step{border:1px solid var(--line);border-left:3px solid var(--line-strong);
  border-radius:var(--r-sm);padding:var(--s3) var(--s4);background:var(--surface)}
.guide-step--next{border-left-color:var(--paper-accent);background:var(--paper-tint)}
.guide-step--done{border-left-color:var(--ok)}
.guide-step--blocked{border-left-color:var(--warn);background:var(--warn-tint)}
.guide-step--running{border-left-color:var(--paper-accent)}
.guide-step--unavailable{opacity:.75}
.guide-head{display:flex;gap:var(--s3);align-items:flex-start}
.guide-num{font-family:var(--mono);font-size:12px;font-weight:700;color:var(--ink-3);
  min-width:1.6em}
.guide-head h3{margin:0;font-size:15px}
.guide-why{margin:2px 0 0;color:var(--ink-3);font-size:12.5px;max-width:70ch}
.guide-status{margin-left:auto}
.guide-detail{margin:6px 0 0;color:var(--ink-2);font-size:13.5px}
.guide-blockers{margin:6px 0 0;padding-left:18px;color:var(--ink-2);font-size:13px}
.candidate{display:flex;gap:8px;align-items:flex-start;border:1px solid var(--line);
  border-radius:var(--r-sm);padding:7px 10px;font-size:13px;background:var(--surface)}
.candidate-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));
  gap:8px;margin:8px 0}
.candidate .cand-meta{display:block;font-size:11.5px;color:var(--ink-3);margin-top:2px}
.pager{display:flex;gap:var(--s2);align-items:center;flex-wrap:wrap;
  padding:var(--s3) var(--s5);border-top:1px solid var(--line);font-size:13px}
.pager .spacer{flex:1 1 auto}
.muted{color:var(--ink-3)}
.small{font-size:12.5px}
.nowrap{white-space:nowrap}
.stack{display:grid;gap:var(--s4)}
.stack--sm{display:grid;gap:var(--s2)}
.inline{display:inline}
.labels{display:flex;gap:var(--s2) var(--s4);flex-wrap:wrap;align-items:center;
  font-size:12px;color:var(--ink-3);padding-top:var(--s2)}
.footer{max-width:var(--max);margin:0 auto;padding:0 var(--s5) var(--s6);
  color:var(--ink-3);font-size:12px;display:flex;gap:var(--s2) var(--s5);
  flex-wrap:wrap;justify-content:space-between}
.footer p{margin:0;max-width:70ch}

/* --------------------------------------------------------- sample-data bar */
.dataflag{display:flex;gap:var(--s2) var(--s3);align-items:baseline;flex-wrap:wrap;
  padding:6px var(--s4);border:1px solid var(--line);border-left:3px solid var(--warn);
  border-radius:var(--r-sm);background:var(--warn-tint);color:var(--ink-2);
  font-size:12.5px;margin:0 0 var(--s2)}
.dataflag strong{color:var(--warn);font-size:12px;font-weight:700;letter-spacing:.04em}
.dataflag .glyph{color:var(--warn)}
.dataflag-more{display:inline}
.dataflag-more>summary{cursor:pointer;color:var(--ink-3);font-size:12px;
  display:inline-block;padding:0 2px}
.dataflag-more>summary:hover{color:var(--ink)}
.dataflag-more .n-body{display:block;color:var(--ink-2);margin-top:2px;max-width:110ch}
/* Prose is measured in characters, not in the width of the window. */
.notice .n-body,.notice .n-title{max-width:110ch}
.page-head .lede{max-width:82ch}
.blockers .b-note,.section-note{max-width:88ch}

/* ------------------------------------------------------- dark work surface */
html{background:var(--rail);scrollbar-color:var(--line-strong) transparent}
body{background:var(--paper);color:var(--ink)}
::selection{background:#26456f;color:#fff}
a{color:var(--paper-accent-ink)}
a:hover{color:var(--paper-accent-hover)}
.mono,code,.num{color:var(--ink)}
.rail{position:sticky;top:0;z-index:40}
/* The rail's second tier: setup and diagnostic pages live behind one labelled
   disclosure, so the primary run/monitor links keep a single row at every width. */
.rail-more{position:relative}
.rail-more>summary{list-style:none;cursor:pointer;display:inline-flex;align-items:center;
  gap:6px;min-height:36px;padding:6px 11px;border-radius:var(--r-sm);
  color:var(--ink-2);font-size:13.5px;font-weight:550;white-space:nowrap}
.rail-more>summary::-webkit-details-marker{display:none}
.rail-more>summary::after{content:'▾';font-size:10px;color:var(--ink-3)}
.rail-more>summary:hover{background:var(--surface-2);color:var(--ink)}
.rail-more[open]>summary{background:var(--surface-2);color:var(--ink)}
.rail-more--current>summary{color:var(--ink);box-shadow:inset 0 -2px 0 0 var(--paper-accent)}
.rail-more-current{color:var(--ink-3);font-size:12.5px;white-space:nowrap}
.rail-more-panel{position:absolute;right:0;left:auto;top:calc(100% + 6px);z-index:60;
  min-width:min(420px,calc(100vw - 32px));
  display:grid;grid-template-columns:1fr 1fr;gap:var(--s2) var(--s4);
  padding:var(--s4);border:1px solid var(--line-2);border-radius:var(--r-md);
  background:var(--surface);box-shadow:0 18px 40px -18px #000}
.rail-more-panel .rail-group{display:grid;gap:2px;align-content:start}
.rail-more-panel .rail-group-label{font-size:10.5px;font-weight:700;letter-spacing:.12em;
  text-transform:uppercase;color:var(--ink-4);padding:0 10px 4px}
.rail-more-panel a{min-height:30px;padding:5px 10px;font-size:13px}
/* A page is one plane: the panel is raised a step, the head band a step again. */
.section{box-shadow:var(--shadow)}
.section-head{background:var(--surface-2)}
.section--plain>.section-head{background:transparent}
.section-title .h h2{font-size:17px}
.section-foot{background:transparent}
tbody tr:hover td{background:var(--hover)}
td.num,th.num{color:var(--ink)}
.table-empty{color:var(--ink-3)}
.field label,.field .label{color:var(--ink-2)}
.field .hint,.field .err{color:var(--ink-3)}
.field .err{color:var(--live-accent)}
.btn:not(.btn--primary):not(.btn--danger){background:var(--surface-2)}
.btn:not(.btn--primary):not(.btn--danger):hover{background:var(--surface-3)}
.btn--ghost{background:transparent}
.btn--primary{background:var(--paper-accent);border-color:var(--paper-accent);color:var(--accent-ink)}
.btn--primary:hover{background:var(--paper-accent-hover);border-color:var(--paper-accent-hover)}
.btn--danger{background:var(--live-accent);border-color:var(--live-accent);color:var(--accent-ink)}
.btn--danger:hover{background:#ff9b98;border-color:#ff9b98}
.chip{background:var(--surface-2);border-color:var(--line-2);color:var(--ink-2)}
.progress{background:var(--surface-3)}
.template{background:var(--rail-2);border-color:var(--line);color:var(--ink-2)}
.disclosure{background:var(--surface-2)}
/* The live page's own read-out: four figures that a session is judged on. */
.kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:var(--s2)}
.kpi>div{border:1px solid var(--line);border-radius:var(--r-sm);padding:var(--s3) var(--s4);
  background:var(--surface-2);min-width:0}
.kpi .k{display:block;font-size:10.5px;font-weight:700;letter-spacing:.1em;
  text-transform:uppercase;color:var(--ink-4);margin-bottom:4px}
.kpi .v{display:block;font-size:15px;font-weight:650;overflow-wrap:anywhere}
.kpi .f{display:block;font-size:11.5px;color:var(--ink-3);margin-top:3px}
/* A workflow is read top to bottom, so the step number is the first thing seen. */
.step-list{list-style:none;margin:0;padding:0;display:grid;gap:var(--s3);counter-reset:step}
.step{display:grid;grid-template-columns:auto minmax(0,1fr);gap:var(--s3) var(--s4);
  border:1px solid var(--line);border-left:3px solid var(--line-strong);border-radius:var(--r-sm);
  padding:var(--s3) var(--s4);background:var(--surface)}
.step--live{border-left-color:var(--live-accent)}
.step--paper{border-left-color:var(--paper-accent)}
.step--blocked{border-left-color:var(--warn)}
.step--ready{border-left-color:var(--ok)}
.step-n{font-family:var(--mono);font-size:12px;font-weight:700;color:var(--ink-4);
  padding-top:2px;min-width:1.4em}
.step-body{display:grid;gap:4px;min-width:0}
.step-body .h{display:flex;gap:var(--s2);align-items:center;flex-wrap:wrap}
.step-body .h strong{font-size:14.5px}
.step-body .why{color:var(--ink-3);font-size:12.5px;max-width:80ch}
.step-body .do{margin-top:var(--s2);display:flex;gap:var(--s2);flex-wrap:wrap;align-items:center}
/* A folded section: the heading stays, the history waits behind it. */
.fold{border:1px solid var(--line);border-radius:var(--r-md);background:var(--surface)}
.fold>summary{display:flex;gap:var(--s2) var(--s3);align-items:center;flex-wrap:wrap;
  cursor:pointer;padding:var(--s3) var(--s5);list-style:none}
.fold>summary::-webkit-details-marker{display:none}
.fold>summary::after{content:'▸';color:var(--ink-4);font-size:11px;margin-left:auto}
.fold[open]>summary::after{content:'▾'}
.fold>summary:hover{background:var(--surface-2)}
.fold>summary:focus-visible{outline:2px solid var(--focus);outline-offset:-2px}
.fold-h{font-size:15px;font-weight:650;color:var(--ink)}
.fold-n{color:var(--ink-3);font-size:12.5px}
.fold-body{padding:0 var(--s5) var(--s5);border-top:1px solid var(--line);padding-top:var(--s4)}
.fold-body>.section,.fold-body>.table-wrap{border:0;border-radius:0;box-shadow:none}

/* Section-level tabs keep a long page readable without moving it. */
.anchor-nav{display:flex;gap:var(--s1);flex-wrap:wrap;align-items:center;
  padding:var(--s2) 0;margin-bottom:var(--s2)}
.anchor-nav a{font-size:12px;font-weight:600;color:var(--ink-3);text-decoration:none;
  padding:4px 9px;border:1px solid transparent;border-radius:999px}
.anchor-nav a:hover{color:var(--ink);background:var(--surface-2);border-color:var(--line)}

/* -------------------------------------------------------------- responsive */
@media (max-width:1080px){
  .layout{grid-template-columns:minmax(0,1fr)}
  .col--aside{grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
}
@media (max-width:860px){
  .rail-in{padding:0 var(--s4);gap:var(--s3);min-height:52px}
  .rail-nav{order:3;width:100%;overflow:visible;flex-wrap:wrap;gap:2px;
    padding-bottom:4px}
  .rail-group{flex-wrap:wrap}
  .rail-nav a{min-height:32px;padding:5px 9px}
  .rail-more{width:100%}
  .rail-more-panel{position:static;min-width:0;grid-template-columns:minmax(0,1fr);
    margin-top:4px;box-shadow:none}
  .rail-sep{display:none}
  .rail-end{margin-left:auto}
  .strip-in{padding:6px var(--s4);gap:6px var(--s4)}
  .shell{padding:var(--s4) var(--s4) var(--s6)}
  h1{font-size:25px}
  .events li{grid-template-columns:minmax(0,1fr)}
  .events .when{font-size:11.5px}
  .facts{grid-template-columns:minmax(0,1fr);column-gap:0}
  .facts dt{border-top:0;padding-bottom:0}
  .facts dd{padding-top:1px;border-top:0}
  .facts>dt{padding-top:12px}
  .facts>dt:first-of-type{padding-top:0}
}
@media (max-width:620px){
  body{font-size:14.5px}
  h1{font-size:22px}
  .shell{padding:var(--s3) var(--s3) var(--s6)}
  .strip-fact{font-size:12px}
  .stat{padding:var(--s3)}
  .stat .v{font-size:20px}
  .section-head{padding:var(--s3) var(--s4) var(--s2)}
  .section-body{padding:var(--s3) var(--s4) var(--s4)}
  .section-body--flush{padding:0}
  /* Tables become cards: each row is a labelled block, nothing scrolls sideways. */
  .table-wrap--stack{overflow:visible;border:0;background:transparent}
  .table--stack thead{position:absolute;width:1px;height:1px;overflow:hidden;
    clip:rect(0 0 0 0);clip-path:inset(50%);white-space:nowrap}
  .table--stack tr{display:block;border:1px solid var(--line);border-radius:var(--r-sm);
    margin-bottom:var(--s2);padding:var(--s2) var(--s3);background:var(--surface)}
  .table--stack tbody tr:nth-child(even) td{background:transparent}
  .table--stack td{display:grid;grid-template-columns:minmax(84px,38%) minmax(0,1fr);
    gap:var(--s3);padding:5px 0;border:0;text-align:left}
  .table--stack td::before{content:attr(data-label);color:var(--ink-3);font-size:11.5px;
    font-weight:700;letter-spacing:.06em;text-transform:uppercase}
  .table--stack td.num{text-align:left}
  .table--stack td:not([data-label]){grid-template-columns:minmax(0,1fr)}
  .table--stack td:not([data-label])::before{content:none}
  .page-head-row{gap:var(--s4)}
  .kpi{grid-template-columns:repeat(auto-fit,minmax(126px,1fr))}
  .step{grid-template-columns:minmax(0,1fr);gap:var(--s2)}
  .step-n{padding-top:0}
  .page-actions{width:100%}
  .page-actions .btn,.actions .btn{flex:1 1 auto}
  .pick-grid{grid-template-columns:minmax(0,1fr);max-height:none}
  .split-callout{grid-template-columns:minmax(0,1fr)}
  /* A chart scrolls in its own box until the phone breakpoint, then fits. */
  .chart{padding:var(--s2)}
  .chart svg{min-width:0}
  .lifecycle li[aria-current="step"]::after{content:none}
  .candidate-list{grid-template-columns:minmax(0,1fr)}
  .guide-head{flex-wrap:wrap}
  .guide-status{margin-left:0}
  .field,fieldset{max-width:none}
}
@media (prefers-reduced-motion:reduce){
  *{transition-duration:.01ms!important;animation-duration:.01ms!important}
  .spinner{animation:none;border-top-color:var(--line-strong)}
}
@media print{
  .rail,.strip,.page-actions,.skip{display:none}
  body{background:#fff;color:#111}
  .section{border-color:#bbb;break-inside:avoid}
}
"""

#: Breakpoints the layout is built and tested against, narrow to wide.
BREAKPOINTS: tuple[int, ...] = (620, 860, 1080)
