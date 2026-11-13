"""The redesign's design system: tokens, layout and components, in one stylesheet.

This is a deliberate replacement, not a reskin.  The old UI was a dark sidebar
shell built out of rounded ``card``/``kpi`` tiles with a coloured chip for every
fact.  This one is a light, editorial console:

* a dark **command rail** across the top (brand, primary navigation, session
  state) instead of a permanent left sidebar, so the reading width is not
  narrowed on a laptop;
* a one-line **status strip** that carries the facts every page shares (loop,
  mode, terminal, capital, read time) once, instead of repeating them per card;
* **facts as ledger rows** and **numbers in real tables** instead of a grid of
  tiles, so a figure always sits beside the label and provenance that qualify it;
* a single accent grammar: blue is PAPER, red is LIVE, amber is attention, green
  is a passing check.  Colour is never the only carrier of meaning.

Everything is plain CSS: no scripts, no fonts to fetch, no images.  The page must
survive a strict ``script-src 'none'`` policy, which is what makes it safe to
render whatever a terminal said.
"""

from __future__ import annotations

STYLES = """
:root{
  --ink:#111a2b; --ink-2:#3d4b63; --ink-3:#5d6b83; --ink-4:#7d8aa1;
  --paper:#f4f6fa; --surface:#ffffff; --surface-2:#eef1f6; --surface-3:#e4e9f1;
  --line:#d3dae5; --line-2:#bcc6d6; --line-strong:#8e9cb2;
  --paper-accent:#1b4dd8; --paper-accent-ink:#12379c; --paper-tint:#e8eefe;
  --live-accent:#b3261e; --live-tint:#fdecea;
  --ok:#0a6a45; --ok-tint:#e6f4ec;
  --warn:#8a4b08; --warn-tint:#fdf1e0;
  --info:#0b5fa5; --info-tint:#e7f1fa;
  --focus:#0b5fa5;
  --r-xs:3px; --r-sm:5px; --r-md:8px;
  --s1:4px; --s2:8px; --s3:12px; --s4:16px; --s5:24px; --s6:32px; --s7:48px;
  --font:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
  --rail:#0d1728; --rail-2:#152238; --max:1240px;
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
.skip{position:absolute;left:var(--s4);top:-60px;z-index:50;background:var(--ink);color:#fff;
  padding:10px 14px;border-radius:var(--r-sm);text-decoration:none;transition:top .12s}
.skip:focus{top:var(--s3)}

/* ------------------------------------------------------------------ rail */
.rail{background:var(--rail);color:#e9eef8;border-bottom:1px solid #000}
.rail-in{max-width:var(--max);margin:0 auto;padding:0 var(--s5);display:flex;
  align-items:center;gap:var(--s5);min-height:58px;flex-wrap:wrap}
.brand{display:flex;align-items:baseline;gap:10px;color:#fff;text-decoration:none;
  padding:6px 0;flex:0 0 auto}
.brand:hover{text-decoration:none}
.brand-mark{font-weight:700;letter-spacing:.14em;font-size:13px;color:#fff}
.brand-name{font-size:13px;color:#9fb0cc;letter-spacing:.02em}
.rail-nav{display:flex;gap:2px;flex-wrap:wrap;min-width:0}
.rail-nav a{display:inline-flex;align-items:center;min-height:36px;padding:6px 11px;
  border-radius:var(--r-sm);color:#c3cfe4;text-decoration:none;font-size:13.5px;
  font-weight:550;letter-spacing:.01em;white-space:nowrap}
.rail-nav a:hover{background:var(--rail-2);color:#fff;text-decoration:none}
.rail-nav a[aria-current="page"]{background:#fff;color:var(--rail);font-weight:650}
.rail-nav .rail-group{display:flex;align-items:center;gap:2px}
.rail-sep{width:1px;height:24px;background:#26344d;margin:0 var(--s2);flex:0 0 auto}
.rail-end{margin-left:auto;display:flex;align-items:center;gap:var(--s2);flex-wrap:wrap}
.rail-end form{margin:0}

/* ---------------------------------------------------------- status strip */
.strip{background:var(--rail-2);color:#dbe4f3;border-bottom:1px solid #000}
.strip-in{max-width:var(--max);margin:0 auto;padding:7px var(--s5);display:flex;
  align-items:center;gap:var(--s2) var(--s5);flex-wrap:wrap;font-size:12.5px}
.strip-fact{display:inline-flex;align-items:baseline;gap:6px;white-space:nowrap}
.strip-fact .k{color:#8b9cba;text-transform:uppercase;letter-spacing:.1em;font-size:10.5px;
  font-weight:650}
.strip-fact .v{color:#f2f6fd;font-weight:600}
.strip a{color:#bfd2ff}

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
tbody tr:nth-child(even) td{background:#fbfcfe}
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
  border-radius:var(--r-sm);background:#fffdf9}
.blockers li.is-danger{border-left-color:var(--live-accent);background:#fffafa}
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
.chip--paper{border-color:#b9c8f2;background:var(--paper-tint);color:var(--paper-accent-ink)}
.chip--live{border-color:#f0c3bf;background:var(--live-tint);color:var(--live-accent)}
.chip--ok{border-color:#b8ddc8;background:var(--ok-tint);color:var(--ok)}
.chip--warn{border-color:#efd3ad;background:var(--warn-tint);color:var(--warn)}
.chip--danger{border-color:#f0c3bf;background:var(--live-tint);color:var(--live-accent)}
.chip--info{border-color:#bcd6ee;background:var(--info-tint);color:var(--info)}
.chip--fixture{border-color:#cdc6e6;background:#f2effb;color:#4b3f8f}
.chip--simulated{border-color:#c9d3e8;background:#eef2fa;color:#3b4a67}
.chip--accent{border-color:#b9c8f2;background:var(--paper-tint);color:var(--paper-accent-ink)}
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
.btn--primary{background:var(--paper-accent);border-color:var(--paper-accent);color:#fff}
.btn--primary:hover{background:var(--paper-accent-ink);border-color:var(--paper-accent-ink)}
.btn--danger{background:var(--live-accent);border-color:var(--live-accent);color:#fff}
.btn--danger:hover{background:#8f1d17;border-color:#8f1d17}
.btn--ghost{background:transparent}
.btn--sm{min-height:30px;padding:4px 10px;font-size:12.5px}
.btn[aria-disabled="true"],.btn:disabled{opacity:.55;cursor:not-allowed}
.rail .btn{background:transparent;border-color:#33455f;color:#dce6f7}
.rail .btn:hover{background:#1d2c44;border-color:#48597a}
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
  border-radius:var(--r-sm);background:#fff;color:var(--ink);font:inherit;font-size:14px}
.field textarea{min-height:84px;resize:vertical}
.field input:focus,.field select:focus,.field textarea:focus{
  border-color:var(--focus);outline:2px solid rgba(11,95,165,.25);outline-offset:0}
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
  border-radius:var(--r-md);background:#fcfdff}
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

/* -------------------------------------------------------------- responsive */
@media (max-width:1080px){
  .layout{grid-template-columns:minmax(0,1fr)}
  .col--aside{grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
}
@media (max-width:860px){
  .rail-in{padding:0 var(--s4);gap:var(--s3);min-height:52px}
  .rail-nav{order:3;width:100%;overflow-x:auto;flex-wrap:nowrap;padding-bottom:6px;
    scrollbar-width:thin}
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
  .page-actions{width:100%}
  .page-actions .btn,.actions .btn{flex:1 1 auto}
  .field,fieldset{max-width:none}
}
@media (prefers-reduced-motion:reduce){
  *{transition-duration:.01ms!important;animation-duration:.01ms!important}
}
@media print{
  .rail,.strip,.page-actions,.skip{display:none}
  body{background:#fff}
  .section{border-color:#bbb;break-inside:avoid}
}
"""

#: Breakpoints the layout is built and tested against, narrow to wide.
BREAKPOINTS: tuple[int, ...] = (620, 860, 1080)
