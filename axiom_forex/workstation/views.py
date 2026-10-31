"""Server-rendered workstation UI: one design system, no scripts, no assets.

Choices that matter for safety and for how it feels:

* **No JavaScript, no external files, no web fonts, no cookies.**  Every action is
  a normal form post and every state is a page load, so there is nothing to
  intercept, nothing in browser storage, and nothing that could reconnect in the
  background.  A strict Content-Security-Policy backs that up by forbidding
  scripts outright.
* **One design system.**  Colour, spacing, type, and component classes are
  defined once in :data:`STYLES`; every screen is assembled from the same cards,
  tables, chips, and buttons.
* **Accessible by construction.**  Landmarks, a skip link, labelled controls,
  visible focus rings, ``aria-current`` navigation, tables with captions and
  scoped headers, and AA-contrast tokens (asserted in the test suite).
* **Honest states.**  Empty, loading, disconnected, and error states are first
  class; and every figure carries the data class it came from, so real broker
  data, paper data, and fixtures can never be mistaken for one another.
"""

from __future__ import annotations

from html import escape
from typing import Any, Iterable, Mapping, Sequence

from .context import PageContext, data_class_label

APP_NAME = "AXIOM"
APP_SUBTITLE = "FOREX"

#: Navigation, in order.  ``icon`` is an inline SVG path (no icon font).
NAV: tuple[tuple[str, str, str], ...] = (
    ("overview", "/", "Overview"),
    ("mode", "/mode", "Trading mode"),
    ("live", "/live", "Live execution"),
    ("setup", "/setup", "Setup"),
    ("account", "/account", "Account & symbols"),
    ("positions", "/positions", "Positions"),
    ("data", "/data", "Data & evaluation"),
    ("provider-setup", "/provider-setup", "Provider setup"),
    ("kronos", "/kronos", "Kronos forecast"),
    ("strategies", "/strategies", "Strategy library"),
    ("activity", "/activity", "Activity"),
    ("health", "/health", "System health"),
)

ICONS: dict[str, str] = {
    "overview": "M3 12h4l3 7 4-14 3 7h4",
    "mode": "M4 7h10l-3-3M20 17H10l3 3M14 7h6v6M10 17H4v-6",
    "live": "M12 3v3M12 18v3M3 12h3M18 12h3M6.3 6.3l2.1 2.1M15.6 15.6l2.1 2.1M17.7 6.3l-2.1 2.1M8.4 15.6l-2.1 2.1M12 9a3 3 0 100 6 3 3 0 000-6z",
    "setup": "M4 6h10M4 12h16M4 18h7M18 4v4M16 18h4M20 16v4",
    "account": "M12 12a4 4 0 100-8 4 4 0 000 8zM4 20a8 8 0 0116 0",
    "positions": "M4 18V9M10 18V5M16 18v-6M22 18h-20",
    "data": "M12 3c4.4 0 8 1.3 8 3s-3.6 3-8 3-8-1.3-8-3 3.6-3 8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
    "provider-setup": "M9 3h7l4 4v14H9zM4 7v14h5M12 8h4M12 12h4M12 16h4",
    "activity": "M3 12h3l2-6 4 12 3-7h6",
    "health": "M12 21s-7-4.5-7-10a4 4 0 017-2.5A4 4 0 0119 11c0 5.5-7 10-7 10z",
    "connection": "M5 12a7 7 0 0114 0M8 12a4 4 0 018 0M12 12v0",
    "shield": "M12 3l7 3v6c0 5-3.5 8-7 9-3.5-1-7-4-7-9V6l7-3z",
    "kronos": "M3 17l4-6 3 4 3-8 3 6 2-3 3 7M3 21h18",
    "strategies": "M4 6h16M4 12h10M4 18h7M17 15l2 2 3-4",
}

STYLES = """
:root{
  --bg:#0b0f14; --bg-2:#0d131b; --panel:#111823; --panel-2:#16202c;
  --line:#24313f; --line-soft:#1b2735;
  --text:#e9eff6; --text-2:#b3c1cf; --text-3:#8fa1b2;
  --accent:#5ec8ff; --accent-2:#1ea7e6; --accent-ink:#04121b;
  --ok:#45d99a; --warn:#ffc061; --danger:#ff8a8a;
  --live:#4cd1a0; --paper:#c9a7ff; --fixture:#93aabf; --sim:#ffc061;
  --radius:12px; --radius-sm:8px; --radius-lg:18px;
  --s1:4px; --s2:8px; --s3:12px; --s4:16px; --s5:24px; --s6:32px; --s7:48px;
  --shadow:0 16px 40px rgba(0,0,0,.35);
  --font:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,"Cascadia Mono","Segoe UI Mono",Consolas,monospace;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:
    radial-gradient(1200px 600px at 80% -10%,rgba(94,200,255,.10),transparent 60%),
    radial-gradient(900px 500px at 0% 0%,rgba(201,167,255,.07),transparent 55%),
    var(--bg);
  color:var(--text); font-family:var(--font); font-size:15px; line-height:1.5;
  -webkit-font-smoothing:antialiased; font-variant-numeric:tabular-nums;
}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
.skip{position:absolute;left:-9999px;top:0;background:var(--accent);color:var(--accent-ink);padding:10px 16px;z-index:50;font-weight:600}
.visually-hidden{position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
.skip:focus{left:12px;top:12px}

/* ---------- shell ---------- */
.shell{display:grid;grid-template-columns:252px minmax(0,1fr);min-height:100vh}
.sidebar{
  background:linear-gradient(180deg,var(--bg-2),#0a0f15);
  border-right:1px solid var(--line); padding:var(--s5) var(--s4);
  display:flex;flex-direction:column;gap:var(--s5); position:sticky; top:0; height:100vh;
}
.brand{display:flex;align-items:center;gap:var(--s3)}
.brand-mark{
  width:38px;height:38px;border-radius:11px;flex:0 0 auto;
  background:linear-gradient(145deg,var(--accent),#2b6df6 70%);
  display:grid;place-items:center;color:#04121b;font-weight:800;font-size:15px;
  box-shadow:0 6px 18px rgba(94,200,255,.28);
}
.brand-name{font-size:17px;font-weight:700;letter-spacing:.14em}
.brand-sub{font-size:11px;letter-spacing:.22em;color:var(--text-3);text-transform:uppercase}
.nav{display:flex;flex-direction:column;gap:2px}
.nav-label{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--text-3);padding:0 var(--s2) var(--s2)}
.nav a{
  display:flex;align-items:center;gap:var(--s3);padding:9px 10px;border-radius:10px;
  color:var(--text-2);font-weight:500;border:1px solid transparent;
}
.nav a:hover{background:rgba(255,255,255,.035);color:var(--text);text-decoration:none}
.nav a[aria-current="page"]{
  background:linear-gradient(90deg,rgba(94,200,255,.16),rgba(94,200,255,.04));
  color:#fff;border-color:rgba(94,200,255,.32);
}
.nav a svg{width:17px;height:17px;flex:0 0 auto;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.nav-badge{margin-left:auto;font-size:11px;background:rgba(255,255,255,.08);border-radius:999px;padding:1px 7px;color:var(--text-2)}
.sidebar-foot{margin-top:auto;display:flex;flex-direction:column;gap:var(--s2)}
.side-note{font-size:11.5px;color:var(--text-3);line-height:1.45}

.main{min-width:0;display:flex;flex-direction:column}
.topbar{
  position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:var(--s4);
  padding:var(--s4) var(--s6); border-bottom:1px solid var(--line);
  background:rgba(11,15,20,.86); backdrop-filter:blur(10px);
}
.topbar h1{font-size:19px;margin:0;font-weight:650;letter-spacing:-.01em}
.topbar .sub{color:var(--text-3);font-size:12.5px;margin-top:2px}
.topbar-actions{margin-left:auto;display:flex;align-items:center;gap:var(--s2);flex-wrap:wrap;justify-content:flex-end}
.content{padding:var(--s6);display:flex;flex-direction:column;gap:var(--s5);max-width:1320px;width:100%}

/* ---------- chips & badges ---------- */
.chip{
  display:inline-flex;align-items:center;gap:6px;padding:3px 9px;border-radius:999px;
  font-size:11.5px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;
  border:1px solid var(--line);background:var(--panel);color:var(--text-2);white-space:nowrap;
}
.chip .dot{width:7px;height:7px;border-radius:50%;background:currentColor;flex:0 0 auto}
.chip--live{color:var(--live);border-color:rgba(76,209,160,.4);background:rgba(76,209,160,.1)}
.chip--paper{color:var(--paper);border-color:rgba(201,167,255,.36);background:rgba(201,167,255,.1)}
.chip--fixture{color:var(--fixture);border-color:rgba(147,170,191,.4);background:rgba(147,170,191,.12)}
.chip--simulated{color:var(--sim);border-color:rgba(255,192,97,.4);background:rgba(255,192,97,.12)}
.chip--ok{color:var(--ok);border-color:rgba(69,217,154,.4);background:rgba(69,217,154,.1)}
.chip--warn{color:var(--warn);border-color:rgba(255,192,97,.4);background:rgba(255,192,97,.1)}
.chip--danger{color:var(--danger);border-color:rgba(255,138,138,.42);background:rgba(255,138,138,.1)}
.chip--muted{color:var(--text-3)}

/* ---------- cards ---------- */
.card{background:linear-gradient(180deg,rgba(255,255,255,.02),transparent),var(--panel);border:1px solid var(--line);border-radius:var(--radius-lg);padding:var(--s5)}
.card--flush{padding:0;overflow:hidden}
.card-head{display:flex;align-items:flex-start;gap:var(--s3);margin-bottom:var(--s4)}
.card-head h2{font-size:14px;margin:0;font-weight:650;letter-spacing:.02em}
.card-head .hint{margin:2px 0 0;color:var(--text-3);font-size:12.5px}
.card-head .right{margin-left:auto;display:flex;gap:var(--s2);align-items:center;flex-wrap:wrap}
.grid{display:grid;gap:var(--s5)}
.grid--kpi{grid-template-columns:repeat(auto-fit,minmax(196px,1fr))}
.grid--two{grid-template-columns:minmax(0,1.55fr) minmax(0,1fr)}
.grid--even{grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}

/* ---------- kpi ---------- */
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:var(--s4)}
.kpi .label{font-size:11.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--text-3);display:flex;align-items:center;gap:6px}
.kpi .value{font-size:26px;font-weight:680;letter-spacing:-.02em;margin-top:6px;display:flex;align-items:baseline;gap:6px}
.kpi .value small{font-size:12px;font-weight:500;color:var(--text-3);letter-spacing:0}
.kpi .foot{margin-top:6px;font-size:12px;color:var(--text-3)}
.kpi--accent{border-color:rgba(94,200,255,.3);background:linear-gradient(160deg,rgba(94,200,255,.13),rgba(17,24,35,.9) 55%)}

/* ---------- buttons ---------- */
.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
  padding:9px 15px;border-radius:10px;border:1px solid var(--line);
  background:var(--panel-2);color:var(--text);font:inherit;font-weight:600;font-size:13.5px;
  cursor:pointer;text-decoration:none;transition:background .12s ease,border-color .12s ease,transform .06s ease;
}
.btn:hover{background:#1d2937;border-color:#33465a;text-decoration:none;color:var(--text)}
.btn:active{transform:translateY(1px)}
.btn--primary{background:var(--accent-2);border-color:transparent;color:var(--accent-ink)}
.btn--primary:hover{background:#2bb6f4;color:var(--accent-ink)}
.btn--danger{background:rgba(255,138,138,.12);border-color:rgba(255,138,138,.45);color:var(--danger)}
.btn--danger:hover{background:rgba(255,138,138,.2);color:var(--danger)}
.btn--ghost{background:transparent}
.btn:disabled,.btn[aria-disabled="true"]{opacity:.5;cursor:not-allowed;transform:none}
.btn-sm{padding:6px 11px;font-size:12.5px;border-radius:8px}

/* ---------- tables ---------- */
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13.5px}
caption{text-align:left;color:var(--text-3);font-size:12px;padding:0 var(--s5) var(--s3)}
th,td{padding:10px var(--s4);text-align:left;border-bottom:1px solid var(--line-soft);vertical-align:top}
thead th{font-size:11px;letter-spacing:.09em;text-transform:uppercase;color:var(--text-3);font-weight:600;background:rgba(255,255,255,.02)}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover{background:rgba(255,255,255,.02)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.mono{font-family:var(--mono);font-size:12.5px}

/* ---------- definition lists ---------- */
.dl{display:grid;grid-template-columns:minmax(140px,auto) 1fr;gap:9px var(--s4);margin:0;font-size:13.5px}
.dl dt{color:var(--text-3)}
.dl dd{margin:0;color:var(--text);font-weight:520;word-break:break-word}
.dl--wide{grid-template-columns:minmax(180px,auto) 1fr}

/* ---------- states ---------- */
.empty{
  border:1px dashed var(--line);border-radius:var(--radius);padding:var(--s5);
  text-align:center;color:var(--text-2);background:rgba(255,255,255,.012);
}
.empty strong{display:block;color:var(--text);margin-bottom:4px;font-size:14px}
.empty p{margin:0 auto;max-width:52ch;color:var(--text-3);font-size:13px}
.notice{
  display:flex;gap:var(--s3);align-items:flex-start;border-radius:var(--radius);
  padding:var(--s4);border:1px solid var(--line);background:var(--panel-2);font-size:13.5px;
}
.notice--ok{border-color:rgba(69,217,154,.35);background:rgba(69,217,154,.08)}
.notice--warn{border-color:rgba(255,192,97,.35);background:rgba(255,192,97,.08)}
.notice--error{border-color:rgba(255,138,138,.4);background:rgba(255,138,138,.08)}
.notice strong{display:block;margin-bottom:2px}
.notice p{margin:0;color:var(--text-2)}
.spinner{width:18px;height:18px;border-radius:50%;border:2px solid rgba(255,255,255,.18);border-top-color:var(--accent);animation:spin .8s linear infinite;flex:0 0 auto}
@keyframes spin{to{transform:rotate(360deg)}}
.bar{height:6px;border-radius:999px;background:rgba(255,255,255,.08);overflow:hidden}
.bar > span{display:block;height:100%;background:linear-gradient(90deg,var(--accent),#7c8cff);border-radius:999px}

/* ---------- stepper ---------- */
.steps{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:var(--s3)}
.step{display:grid;grid-template-columns:34px minmax(0,1fr);gap:var(--s3);align-items:start}
.step-num{
  width:30px;height:30px;border-radius:50%;display:grid;place-items:center;font-weight:700;font-size:13px;
  border:1px solid var(--line);background:var(--panel-2);color:var(--text-2)
}
.step--complete .step-num{background:rgba(69,217,154,.16);border-color:rgba(69,217,154,.45);color:var(--ok)}
.step--action_required .step-num{background:rgba(94,200,255,.14);border-color:rgba(94,200,255,.45);color:var(--accent)}
.step--blocked .step-num{background:rgba(255,138,138,.14);border-color:rgba(255,138,138,.45);color:var(--danger)}
.step-body{border:1px solid var(--line);border-radius:var(--radius);padding:var(--s4);background:var(--panel)}
.step--blocked .step-body{border-color:rgba(255,138,138,.3)}
.step-title{display:flex;align-items:center;gap:var(--s2);flex-wrap:wrap;font-weight:600}
.step-title h3{font-size:14px;margin:0;font-weight:650}
.step-body p{margin:6px 0 0;color:var(--text-2);font-size:13px}
.step-body .detail{margin-top:8px;font-size:12.5px;color:var(--text-3)}
.step-actions{margin-top:var(--s3);display:flex;gap:var(--s2);flex-wrap:wrap}

/* ---------- execution mode ---------- */
.mode-grid{display:grid;gap:var(--s3);grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
.mode-option{
  display:flex;flex-direction:column;gap:8px;padding:var(--s4);border-radius:var(--radius);
  border:1px solid var(--line);background:var(--panel-2);
}
.mode-option--active{border-color:rgba(94,200,255,.55);box-shadow:0 0 0 1px rgba(94,200,255,.25) inset}
.mode-option--live.mode-option--active{border-color:rgba(255,138,138,.6);box-shadow:0 0 0 1px rgba(255,138,138,.28) inset}
.mode-option h3{margin:0;font-size:14px;display:flex;align-items:center;gap:8px}
.mode-option p{margin:0;font-size:12.5px;color:var(--text-2);line-height:1.5}
.mode-option .mode-actions{margin-top:auto;padding-top:10px;display:flex;gap:8px;flex-wrap:wrap}
.mode-banner{
  display:flex;align-items:center;gap:var(--s3);padding:14px var(--s4);border-radius:var(--radius);
  border:1px solid var(--line);background:var(--panel-2);flex-wrap:wrap;
}
.mode-banner--live{border-color:rgba(255,138,138,.5);background:linear-gradient(120deg,rgba(255,138,138,.14),rgba(17,24,35,.9))}
.mode-banner--paper{border-color:rgba(201,167,255,.36);background:linear-gradient(120deg,rgba(201,167,255,.12),rgba(17,24,35,.9))}
.mode-banner strong{font-size:14px}
.mode-banner .spacer{margin-left:auto}
.blocker-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:10px}
.blocker{border:1px solid var(--line);border-radius:var(--radius);padding:12px 14px;background:rgba(0,0,0,.18)}
.blocker .why{font-size:13px;margin:4px 0 0;color:var(--text-2)}
.blocker .fix{font-size:12.5px;margin:6px 0 0;color:var(--text);border-left:2px solid var(--accent);padding-left:9px}
.blocker .why strong,.blocker .fix strong{color:var(--text)}

/* ---------- misc ---------- */
.muted{color:var(--text-3)}
.small{font-size:12.5px}
.stack{display:flex;flex-direction:column;gap:var(--s2)}
.row{display:flex;gap:var(--s2);align-items:center;flex-wrap:wrap}
.spread{display:flex;justify-content:space-between;gap:var(--s3);align-items:center}
.divider{height:1px;background:var(--line-soft);margin:var(--s4) 0}
pre.template{
  margin:0;padding:var(--s4);border-radius:var(--radius);border:1px solid var(--line);
  background:#0a1017;color:var(--text-2);font-family:var(--mono);font-size:12.5px;
  overflow-x:auto;line-height:1.55;
}
.list-reset{list-style:none;margin:0;padding:0}
.feed{display:flex;flex-direction:column;gap:var(--s3)}
.feed-item{display:grid;grid-template-columns:8px minmax(0,1fr) auto;gap:var(--s3);align-items:start;font-size:13px}
.feed-dot{width:8px;height:8px;border-radius:50%;background:var(--text-3);margin-top:7px}
.feed-dot--ok{background:var(--ok)}
.feed-dot--warn{background:var(--warn)}
.feed-dot--danger{background:var(--danger)}
.feed-when{color:var(--text-3);font-size:12px;white-space:nowrap}
.check-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px;font-size:13.5px}
.check-list li{display:grid;grid-template-columns:18px minmax(0,1fr);gap:10px;align-items:start}
.check-ico{width:16px;height:16px;border-radius:50%;display:grid;place-items:center;font-size:10px;font-weight:800;margin-top:2px}
.check-ico--ok{background:rgba(69,217,154,.18);color:var(--ok)}
.check-ico--bad{background:rgba(255,138,138,.18);color:var(--danger)}
.symbol-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:6px;max-height:320px;overflow:auto;padding:var(--s2);border:1px solid var(--line);border-radius:var(--radius);background:rgba(0,0,0,.16)}
.symbol{display:flex;gap:8px;align-items:center;padding:6px 8px;border-radius:8px;font-size:13px}
.symbol:hover{background:rgba(255,255,255,.04)}
.symbol input{width:15px;height:15px;accent-color:var(--accent-2)}
.field{display:flex;flex-direction:column;gap:6px;max-width:640px}
.field label{font-size:12.5px;color:var(--text-2);font-weight:600}
.field .help{font-size:12px;color:var(--text-3)}
input[type="text"]{
  width:100%;padding:9px 11px;border-radius:9px;border:1px solid var(--line);
  background:#0a1017;color:var(--text);font:inherit;font-size:13.5px
}
input[type="text"]:focus{border-color:var(--accent);outline:none;box-shadow:0 0 0 3px rgba(94,200,255,.18)}
select,input[type="number"]{
  background:var(--panel-2);border:1px solid var(--line);border-radius:10px;
  color:var(--text);padding:9px 11px;font:inherit;font-size:13.5px;min-width:120px;
}
select:focus,input[type="number"]:focus{border-color:var(--accent);outline:none;box-shadow:0 0 0 3px rgba(94,200,255,.18)}
.data-grid{display:grid;gap:var(--s2);grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.data-field{display:flex;flex-direction:column;gap:6px}
.data-field label{font-size:12px;color:var(--text-2);font-weight:600}
.data-form{border-top:1px solid var(--line-soft);padding-top:12px;margin-top:12px}
.data-form:first-of-type{border-top:0;padding-top:0;margin-top:0}
.legend{display:flex;gap:var(--s4);flex-wrap:wrap;font-size:12px;color:var(--text-3)}

.live-banner{
  display:flex;align-items:center;gap:var(--s3);padding:14px 16px;border-radius:12px;
  border:1px solid var(--line);background:var(--panel);margin-bottom:var(--s3)
}
.live-banner strong{font-size:14.5px}
.live-banner .spacer{margin-left:auto}
.live-banner--live-on{border-color:rgba(255,138,138,.55);background:linear-gradient(120deg,rgba(255,138,138,.18),rgba(17,24,35,.92))}
.live-banner--live-armed{border-color:rgba(255,206,120,.45);background:linear-gradient(120deg,rgba(255,206,120,.12),rgba(17,24,35,.92))}
.live-banner--live-off{border-color:rgba(126,227,167,.35);background:linear-gradient(120deg,rgba(126,227,167,.10),rgba(17,24,35,.92))}
.field--check{flex-direction:row;align-items:center;gap:8px}
.field--check input{width:16px;height:16px;accent-color:var(--accent-2)}
.list{margin:8px 0 0;padding-left:18px;display:flex;flex-direction:column;gap:6px;font-size:13px}

/* ---------- responsive ---------- */
@media (max-width:1120px){
  .grid--two{grid-template-columns:minmax(0,1fr)}
  .content{padding:var(--s5)}
  .topbar{padding:var(--s4) var(--s5)}
}
@media (max-width:860px){
  .shell{grid-template-columns:minmax(0,1fr)}
  .sidebar{position:static;height:auto;flex-direction:row;align-items:center;gap:var(--s4);overflow-x:auto;padding:var(--s3) var(--s4)}
  .sidebar-foot{display:none}
  .nav{flex-direction:row;gap:4px}
  .nav-label{display:none}
  .nav a{white-space:nowrap;padding:7px 10px}
  .content{padding:var(--s4);gap:var(--s4)}
  .dl{grid-template-columns:1fr;gap:2px var(--s3)}
  .dl dt{margin-top:8px}
}
@media (prefers-reduced-motion:reduce){
  *{animation-duration:.01ms !important;transition-duration:.01ms !important}
}
"""


# --------------------------------------------------------------------------
# small builders
# --------------------------------------------------------------------------


def chip(text: str, tone: str = "muted", dot: bool = False) -> str:
    dot_html = "<span class='dot' aria-hidden='true'></span>" if dot else ""
    return f"<span class='chip chip--{escape(tone)}'>{dot_html}{escape(text)}</span>"


def data_chip(label: Mapping[str, str]) -> str:
    return chip(str(label.get("label", "UNKNOWN")), str(label.get("tone", "muted")))


def icon(name: str, *, size: int = 17) -> str:
    path = ICONS.get(name, ICONS["overview"])
    return (
        f"<svg viewBox='0 0 24 24' width='{size}' height='{size}' aria-hidden='true' "
        f"focusable='false'><path d='{path}'/></svg>"
    )


def card(title: str, body: str, *, hint: str = "", right: str = "", flush: bool = False) -> str:
    head = ""
    if title or right:
        hint_html = f"<p class='hint'>{escape(hint)}</p>" if hint else ""
        right_html = f"<div class='right'>{right}</div>" if right else ""
        head = (
            f"<div class='card-head'><div><h2>{escape(title)}</h2>{hint_html}</div>"
            f"{right_html}</div>"
        )
    klass = "card card--flush" if flush else "card"
    return f"<section class='{klass}'>{head}{body}</section>"


def kpi(label: str, value: str, *, foot: str = "", tone: str = "", label_chip: str = "") -> str:
    klass = "kpi kpi--accent" if tone == "accent" else "kpi"
    foot_html = f"<div class='foot'>{foot}</div>" if foot else ""
    chip_html = f" {label_chip}" if label_chip else ""
    return (
        f"<div class='{klass}'><div class='label'>{escape(label)}{chip_html}</div>"
        f"<div class='value'>{value}</div>{foot_html}</div>"
    )


def empty_state(title: str, message: str, action: str = "") -> str:
    action_html = f"<div class='row' style='justify-content:center;margin-top:12px'>{action}</div>" if action else ""
    return (
        f"<div class='empty'><strong>{escape(title)}</strong>"
        f"<p>{escape(message)}</p>{action_html}</div>"
    )


def notice(tone: str, title: str, message: str = "") -> str:
    body = f"<p>{escape(message)}</p>" if message else ""
    return (
        f"<div class='notice notice--{escape(tone)}' role='status'>"
        f"<div><strong>{escape(title)}</strong>{body}</div></div>"
    )


def table(
    caption: str,
    headers: Sequence[tuple[str, str]],
    rows: Iterable[Sequence[Any]],
    *,
    empty: str = "",
) -> str:
    body_rows = list(rows)
    if not body_rows:
        return empty or empty_state("Nothing to show yet", "There are no rows for this section.")
    head = "".join(
        "<th scope='col'{}>{}</th>".format(
            " class='num'" if kind == "num" else "", escape(label)
        )
        for label, kind in headers
    )
    rendered = []
    for row in body_rows:
        cells = []
        for index, cell in enumerate(row):
            kind = headers[index][1]
            cls = " class='num'" if kind == "num" else (" class='mono'" if kind == "mono" else "")
            cells.append(f"<td{cls}>{cell}</td>")
        rendered.append("<tr>" + "".join(cells) + "</tr>")
    return (
        f"<div class='table-wrap'><table><caption>{escape(caption)}</caption>"
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(rendered)}</tbody></table></div>"
    )


def dl(pairs: Iterable[tuple[str, str]], *, wide: bool = False) -> str:
    rows = "".join(
        f"<dt>{escape(label)}</dt><dd>{value}</dd>" for label, value in pairs
    )
    return f"<dl class='dl{' dl--wide' if wide else ''}'>{rows}</dl>"


def action_form(action: str, nonce: str, label: str, *, tone: str = "", extra: str = "", confirm: str = "") -> str:
    klass = "btn" + (f" btn--{tone}" if tone else "")
    # A plain form post: no script, no fetch, no storage. The per-process nonce
    # proves the request came from a page this process rendered.
    return (
        f"<form method='post' action='/actions/{escape(action)}' style='display:inline'>"
        f"<input type='hidden' name='nonce' value='{escape(nonce)}'>{extra}"
        f"<button class='{klass}' type='submit'>{escape(label)}</button></form>"
    )


def connection_chip(ctx: PageContext) -> str:
    """The one place connection state is worded.

    A simulator run is never described as "connected": no terminal answered, and
    the wording has to make that impossible to misread.
    """

    terminal = ctx.terminal
    if terminal.state == "CONNECTING":
        return chip("Connecting", "warn", dot=True)
    if terminal.state == "ERROR":
        return chip("Connection error", "danger", dot=True)
    if terminal.simulated:
        if terminal.connected:
            return chip("Simulator · not MT5", "simulated", dot=True)
        return chip("Simulator · not started", "simulated", dot=True)
    if not terminal.connected:
        return chip("Disconnected", "muted", dot=True)
    mode = terminal.mode if terminal.mode_confirmed else f"{terminal.mode}?"
    return chip(f"Connected · {mode}", "live", dot=True)


def mode_chip(ctx: PageContext) -> str:
    """Account mode, always attributed to its source."""

    terminal = ctx.terminal
    if not terminal.connected:
        return chip("No account mode", "muted")
    if terminal.simulated:
        return chip(f"Sample mode: {terminal.mode}", "simulated")
    if not terminal.mode_confirmed:
        return chip(f"Account mode: {terminal.mode} (unconfirmed)", "warn")
    tone = "ok" if terminal.mode == "DEMO" else ("warn" if terminal.mode == "REAL" else "warn")
    return chip(f"Account mode: {terminal.mode}", tone)


def execution_mode_chip(ctx: PageContext) -> str:
    """The persistent PAPER / LIVE indicator.

    It is on every page, in the top bar and the sidebar, and it says which of the
    two it is *and* what that means right now: paper fills are simulated, and a
    live mode that this process has not confirmed routes nothing.
    """

    view = ctx.execution_mode or {}
    mode = str(view.get("mode", "PAPER")).upper()
    if mode == "LIVE":
        if ctx.live_effective:
            return chip("LIVE · MT5 execution", "danger", dot=True)
        return chip("LIVE · not confirmed", "warn", dot=True)
    return chip("PAPER · simulated fills", "paper", dot=True)


def connect_label(ctx: PageContext, *, long: bool = True) -> str:
    """Button text for the connect action, matched to what it actually does."""

    if ctx.terminal.simulated:
        return "Start simulator preview (not MT5)" if long else "Start preview"
    return "Connect (observe only)" if long else "Connect"


# --------------------------------------------------------------------------
# shell
# --------------------------------------------------------------------------


def _nav(ctx: PageContext) -> str:
    items = []
    for key, href, label in NAV:
        current = " aria-current='page'" if key == ctx.page else ""
        badge = ctx.nav_badge(key)
        badge_html = f"<span class='nav-badge'>{escape(badge)}</span>" if badge else ""
        items.append(
            f"<a href='{href}'{current}>{icon(key)}<span>{escape(label)}</span>{badge_html}</a>"
        )
    return f"<nav class='nav' aria-label='Main'><span class='nav-label'>Workspace</span>{''.join(items)}</nav>"


def _sidebar(ctx: PageContext) -> str:
    state_note = (
        "Observing a terminal session. Read-only."
        if ctx.terminal.connected
        else "Not connected. AXIOM connects only when you ask."
    )
    return (
        "<aside class='sidebar'>"
        "<div class='brand'><div class='brand-mark' aria-hidden='true'>AX</div>"
        f"<div><div class='brand-name'>{APP_NAME}</div>"
        f"<div class='brand-sub'>{APP_SUBTITLE}</div></div></div>"
        f"{_nav(ctx)}"
        "<div class='sidebar-foot'>"
        f"<div class='row'>{connection_chip(ctx)}</div>"
        f"<div class='row'>{execution_mode_chip(ctx)}</div>"
        "<p class='side-note'>Paper or live execution, chosen by you. No order entry "
        "here, no arming here, no account switching, no automatic reconnection.</p>"
        f"<p class='side-note muted'>v{escape(ctx.platform_version or 'dev')}</p>"
        "</div></aside>"
    )


def _topbar(ctx: PageContext) -> str:
    actions = [execution_mode_chip(ctx), connection_chip(ctx), mode_chip(ctx)]
    if ctx.terminal.simulated and ctx.terminal.connected:
        actions.append(
            action_form("disconnect", ctx.action_nonce, "Stop simulator preview", tone="danger")
        )
    elif ctx.terminal.connected:
        actions.append(
            f"<a class='btn btn-sm' href='/{escape(ctx.page) if ctx.page != 'overview' else ''}'>Refresh</a>"
        )
        actions.append(
            action_form("disconnect", ctx.action_nonce, "Disconnect", tone="danger")
        )
    else:
        actions.append("<a class='btn btn-sm' href='/mode'>Trading mode</a>")
        actions.append("<a class='btn btn-sm' href='/setup'>Open setup</a>")
    return (
        "<header class='topbar'>"
        f"<div><h1>{escape(ctx.title)}</h1>"
        f"<div class='sub'>AXIOM-FOREX · trading workstation</div></div>"
        f"<div class='topbar-actions'>{''.join(actions)}</div></header>"
    )


def render_shell(ctx: PageContext, body: str) -> str:
    banner = ""
    if ctx.terminal.simulated:
        if ctx.terminal.connected:
            banner = notice(
                "warn",
                "SIMULATED / SAMPLE DATA — no MetaTrader 5 terminal is attached",
                "The offline simulator is running because the launcher was started with "
                "--simulate. Nothing on this page was read from a broker or an account, and "
                "this preview can never reach observation mode.",
            )
        else:
            banner = notice(
                "warn",
                "SIMULATED / SAMPLE DATA — offline simulator enabled",
                "The launcher was started with --simulate. The preview is not started, no "
                "terminal is attached, and nothing here was read from a broker or account.",
            )
    elif ctx.notice:
        banner = notice("ok", ctx.notice)
    legend = (
        "<div class='legend' aria-label='Data legend'>"
        "<span>Data labels:</span>"
        f"{chip('Real broker', 'live', dot=True)}"
        f"{chip('Paper', 'paper', dot=True)}"
        f"{chip('Sample fixture', 'fixture', dot=True)}"
        f"{chip('Simulated', 'simulated', dot=True)}"
        "</div>"
    )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(ctx.title)} · {APP_NAME} {APP_SUBTITLE}</title>"
        "<meta name='color-scheme' content='dark'>"
        f"<style>{STYLES}</style></head><body>"
        "<a class='skip' href='#main'>Skip to content</a>"
        "<div class='shell'>"
        f"{_sidebar(ctx)}"
        "<main class='main' id='main' tabindex='-1'>"
        f"{_topbar(ctx)}"
        f"<div class='content'>{banner}{body}"
        f"<footer class='row small muted' style='justify-content:space-between;padding-top:8px'>"
        f"<span>{legend}</span>"
        f"<span>Generated {escape(ctx.generated_at.strftime('%Y-%m-%d %H:%M:%S UTC'))}</span>"
        "</footer></div></main></div></body></html>"
    )


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------


def _session_kpis(ctx: PageContext) -> str:
    terminal = ctx.terminal
    if terminal.simulated:
        # Deliberately not the word "Connected": no terminal was contacted.
        state_value = "Preview running" if terminal.connected else "Preview stopped"
        state_foot = "Offline simulator — not MetaTrader 5"
    else:
        state_value = (
            "Connected"
            if terminal.connected
            else ("Connecting" if terminal.state == "CONNECTING" else ("Error" if terminal.state == "ERROR" else "Disconnected"))
        )
        state_foot = (
            f"Source: {escape(terminal.source_label)}"
            if terminal.source_label
            else "Press Connect on the setup screen."
        )
    mode_value = terminal.mode if terminal.connected else "—"
    if terminal.simulated:
        mode_foot = "Sample value — no account was read"
    elif terminal.mode_confirmed:
        mode_foot = "Reported by the connected terminal"
    elif terminal.connected:
        mode_foot = "The terminal did not state a mode"
    else:
        mode_foot = "No terminal attached"
    symbol_value = str(len(terminal.symbols)) if terminal.connected else "—"
    selected_count = len(ctx.wizard.choices.selected_symbols)
    if not terminal.connected:
        symbol_foot = "Available after connecting"
    elif terminal.simulated:
        symbol_foot = (
            f"{selected_count} sample symbol(s) selected" if selected_count else "Sample symbols only"
        )
    elif selected_count:
        symbol_foot = f"{selected_count} selected for observation"
    else:
        symbol_foot = "None selected yet — choose on Account & symbols"
    paper_label = data_chip(ctx.paper_label) if ctx.paper.available else ""
    if ctx.paper.available:
        paper_value = escape(ctx.paper.equity or "—")
        paper_foot = f"{escape(ctx.paper.account_currency or 'fixture')} · paper only"
    else:
        paper_value = "—"
        paper_foot = "Paper platform unavailable"
    return (
        "<div class='grid grid--kpi'>"
        + kpi("Terminal session", escape(state_value), foot=escape(state_foot), tone="accent")
        + kpi("Selected account mode", escape(mode_value), foot=escape(mode_foot))
        + kpi(
            "Sample symbols" if terminal.simulated else "Symbols on the terminal",
            symbol_value,
            foot=escape(symbol_foot),
        )
        + kpi(
            "Paper account equity",
            paper_value,
            foot=escape(paper_foot),
            label_chip=paper_label,
        )
        + "</div>"
    )


def _terminal_card(ctx: PageContext) -> str:
    terminal = ctx.terminal
    if terminal.state == "CONNECTING":
        return card(
            "Terminal session",
            "<div class='notice' role='status'><span class='spinner' aria-hidden='true'></span>"
            "<div><strong>Connecting to the terminal…</strong>"
            "<p>Waiting for MetaTrader 5 to answer. This page does not poll or retry on its own.</p>"
            "</div></div>",
        )
    if terminal.state == "ERROR" and terminal.error:
        error = terminal.error
        return card(
            "Terminal session",
            notice(
                "error",
                f"Connection failed: {error.get('reason', 'unknown')}",
                str(error.get("hint") or error.get("detail") or ""),
            )
            + f"<div class='step-actions'>{action_form('connect', ctx.action_nonce, 'Try again', tone='primary')}"
            "<a class='btn' href='/setup'>Setup steps</a></div>",
        )
    if not terminal.connected:
        return card(
            "Terminal session",
            empty_state(
                "No terminal connected",
                "AXIOM has not opened a terminal session. It connects only when you press "
                "Connect, using the MetaTrader 5 terminal you are already signed in to.",
                action_form("connect", ctx.action_nonce, connect_label(ctx), tone="primary")
                + "<a class='btn' href='/setup'>Review setup</a>",
            ),
        )
    sample = terminal.simulated
    prefix = "Sample " if sample else ""
    rows = [
        (
            "Connection",
            f"{chip('Simulator', 'simulated', dot=True)} {data_chip(ctx.terminal_label)}"
            if sample
            else f"{chip('Connected', 'ok', dot=True)} {data_chip(ctx.terminal_label)}",
        ),
        (f"{prefix}Terminal", escape(terminal.terminal_name or "MetaTrader 5")),
        (f"{prefix}Build", escape(terminal.build or "unknown")),
        (f"{prefix}Broker / server", escape(terminal.server or "unknown")),
        (
            f"{prefix}Account",
            f"<span class='mono'>…{escape(terminal.fingerprint)}</span> {chip('identifier redacted', 'muted')}",
        ),
        (
            f"{prefix}Account mode",
            escape(terminal.mode)
            + ("" if terminal.mode_confirmed or sample else " (unconfirmed)"),
        ),
        (
            f"{prefix}Currency / leverage",
            escape(f"{terminal.currency or '—'} · {terminal.leverage or '—'}"),
        ),
        (f"{prefix}Margin accounting", escape(terminal.margin_mode or "unknown")),
        (
            f"{prefix}Terminal trading permission",
            escape("allowed" if terminal.trade_allowed else "not allowed"),
        ),
        ("Connected at", escape(str(terminal.connected_at or "—"))),
        (
            "Balances",
            f"{chip('withheld', 'muted')} this interface never shows balances or equity",
        ),
    ]
    if sample:
        body = (
            notice(
                "warn",
                "Sample account — no terminal was contacted",
                "Every value below comes from the offline simulator's fixture, not from "
                "MetaTrader 5 or a broker.",
            )
            + dl(rows)
        )
    else:
        body = (
            dl(rows)
            + "<p class='small muted' style='margin-top:12px'>Observation only: this session has "
            "no order, arming, or account-switching capability attached to it.</p>"
        )
    return card("Terminal session", body)


def _market_card(ctx: PageContext) -> str:
    paper = ctx.paper
    session_chip = chip(paper.calendar_status or "UNKNOWN", "ok" if paper.calendar_status == "OPEN" else "warn", dot=True)
    return card(
        "Market & session",
        dl(
            [
                ("Session state", f"{session_chip} <span class='muted small'>window {escape(paper.calendar_window or 'n/a')}</span>"),
                ("Next open", escape(paper.calendar_next_open or "—")),
                ("Calendar source", "Declared calendar on the paper platform"),
                ("Configured server", escape(str(ctx.wizard.environment.settings_server or "not set"))),
            ]
        )
        + notice(
            "warn",
            "Session times come from the declared calendar, not from your broker",
            "Until your broker's sessions, holidays, and DST rules are confirmed, treat "
            "market-open values as indicative only.",
        ),
        hint="Calendar state is the paper platform's declared calendar.",
        right=data_chip(ctx.paper_label),
    )


def _paper_card(ctx: PageContext) -> str:
    paper = ctx.paper
    if not paper.available:
        return card("Paper account", empty_state("Paper platform unavailable", paper.error or "No status could be read."))
    return card(
        "Paper account",
        dl(
            [
                ("Equity", escape(paper.equity)),
                ("Gross exposure", escape(paper.gross_exposure)),
                ("Margin used", escape(paper.margin)),
                ("Trades today", escape(paper.trades_today)),
                ("Realised P&L today", escape(paper.realized_pnl_today)),
                ("Currency", escape(paper.account_currency or "—")),
                ("Data class", f"{data_chip(ctx.paper_label)} <span class='muted small'>source {escape(paper.source or 'unknown')}</span>"),
            ]
        ),
        hint="Simulated paper ledger. Not broker positions and not a live account.",
        right=data_chip(ctx.paper_label),
    )


def _runner_state_chip(ctx: PageContext) -> str:
    """The runner's own word for its state, never a green "OK" for a stopped one."""

    runner = ctx.runner
    if not runner or not runner.get("available", True):
        return chip("RUNNER UNAVAILABLE", "muted")
    state = str(runner.get("state", "")).upper()
    if state == "RUNNING":
        return chip("RUNNING (PAPER)", "paper", dot=True)
    if state == "BLOCKED":
        return chip("BLOCKED", "danger", dot=True)
    if state == "STOPPED":
        return chip("STOPPED", "muted")
    return chip(state or "UNKNOWN", "muted")


def _blocker_list(blockers: Sequence[Mapping[str, Any]]) -> str:
    """Blocker entries as a list: a code, what it means, and what to do."""

    items = list(blockers or ())
    if not items:
        return ""
    rows = "".join(
        f"<li><strong>{escape(str(item.get('reason', '')))}</strong> "
        f"<span class='muted small'>{escape(str(item.get('message', '')))}</span>"
        + (
            f"<br><span class='small'>{escape(str(item.get('hint', '')))}</span>"
            if item.get("hint")
            else ""
        )
        + "</li>"
        for item in items
    )
    return f"<ul class='list-reset small' style='margin:12px 0 0'>{rows}</ul>"


def _runner_card(ctx: PageContext) -> str:
    """Everything the operator needs to see why the runner is or is not trading."""

    runner = ctx.runner
    if not runner:
        return card(
            "Paper strategy runner",
            empty_state(
                "No runner configured",
                "This workstation was started without a paper strategy runner. "
                "Nothing is trading and nothing can be started from here.",
            ),
            hint="A runner needs imported history, a passed evaluation, and paper mode.",
        )
    if not runner.get("available", True):
        first = next(iter(runner.get("blockers") or ()), None)
        message = (
            str(first.get("message"))
            if isinstance(first, Mapping) and first.get("message")
            else "The runner could not be reached. Nothing is trading."
        )
        return card(
            "Paper strategy runner",
            empty_state("Runner unavailable", escape(message)),
            right=_runner_state_chip(ctx),
        )

    if runner.get("pending"):
        # Alive, but nothing is built yet: a provider platform waits for the
        # operator to connect a terminal.  Offering Start is the point.
        pending_blockers = list(runner.get("setup_blockers") or ())
        return card(
            "Paper strategy runner",
            empty_state(
                "Waiting for the terminal",
                escape(
                    str(
                        runner.get("reason_detail")
                        or "Connect in the browser, then press Start."
                    )
                ),
            )
            + _blocker_list(pending_blockers)
            + "<div style='margin-top:12px'>"
            + action_form("runner-start", ctx.action_nonce, "Start runner", tone="paper")
            + "</div>",
            hint=(
                "The symbol, the price grid, and the contract terms come from your "
                "connected terminal. Nothing is substituted while it is absent."
            ),
            right=_runner_state_chip(ctx),
        )

    strategy = runner.get("strategy") or {}
    feed = runner.get("data") or {}
    counters = runner.get("counters") or {}
    blockers = list(runner.get("blockers") or ())
    status = str(strategy.get("status", ""))
    status_tone = "ok" if status == "SELECTED_FOR_PAPER" else "warn"
    reason = str(runner.get("reason", ""))
    detail = str(runner.get("reason_detail", ""))
    live = runner.get("live") or {}
    live_ready = bool(live.get("ready"))
    live_reasons = [
        str(item.get("reason", "")) for item in (live.get("blockers") or ())
    ]
    actions = ""
    if ctx.live_effective and not runner.get("live_configured", False):
        actions = chip(
            "Live mode is active but no live path is configured: the runner refuses to start",
            "danger",
        )
    elif ctx.live_effective and not live_ready:
        actions = chip(
            "Live mode is active: starting is refused until the live checks pass ("
            + escape(", ".join(live_reasons[:3]) or "not ready")
            + ")",
            "danger",
        )
    elif ctx.live_effective and runner.get("running"):
        actions = action_form("runner-stop", ctx.action_nonce, "Stop live runner", tone="danger")
    elif ctx.live_effective:
        actions = action_form("runner-start", ctx.action_nonce, "Start LIVE runner", tone="danger")
    elif runner.get("running"):
        actions = action_form("runner-stop", ctx.action_nonce, "Stop runner", tone="warn")
    else:
        actions = action_form("runner-start", ctx.action_nonce, "Start runner", tone="paper")

    if not runner.get("running"):
        seen = {str(item.get("reason", "")) for item in blockers if isinstance(item, Mapping)}
        for item in runner.get("setup_blockers") or ():
            if not isinstance(item, Mapping):
                continue
            if str(item.get("reason", "")) in seen:
                continue
            seen.add(str(item.get("reason", "")))
            blockers.append(dict(item))

    blocker_html = _blocker_list(blockers)

    recent = list(runner.get("recent") or ())[:5]
    recent_rows = [
        (
            escape(str(item.get("at", ""))[:19]),
            escape(str(item.get("reason", ""))),
            escape(str(item.get("code", "") or "—")),
            escape(str(item.get("detail", ""))[:80]),
        )
        for item in recent
    ]

    body = (
        dl(
            [
                ("State", f"{_runner_state_chip(ctx)} <span class='muted small'>{escape(reason)}</span>"),
                ("Last tick", escape(str(runner.get("last_tick_at", "") or "—"))),
                ("Last signal", f"{escape(str(runner.get('last_signal', '') or '—'))} <span class='muted small'>{escape(str(runner.get('last_signal_at', '') or ''))}</span>"),
                ("Last fill", escape(str(runner.get("last_fill_at", "") or "—"))),
                ("Strategy", f"{escape(str(strategy.get('candidate_id', '')))} <span class='muted small'>{escape(str(strategy.get('parameters', '')))}</span>"),
                ("Candidate status", f"<span class='chip chip--{status_tone}'>{escape(status)}</span>"),
                ("Symbol", f"{escape(str(runner.get('symbol', '')))} <span class='muted small'>qty {escape(str(runner.get('quantity', '')))}</span>"),
                (
                    "Feed",
                    f"{escape(str(feed.get('kind', '')))} "
                    f"<span class='muted small'>{escape(str(feed.get('data_class', '')))} "
                    f"· every {escape(str((feed.get('policy') or {}).get('interval_seconds', '—')))}s "
                    f"· max quote age {escape(str((feed.get('policy') or {}).get('max_quote_age_seconds', '—')))}s "
                    f"— {escape(str(feed.get('label', '')))}</span>",
                ),
                (
                    "Mode",
                    f"<span class='chip chip--{'danger' if str(runner.get('execution_mode', '')).upper() == 'LIVE' else 'paper'}'>"
                    f"{escape(str(runner.get('execution_mode', 'PAPER')))}</span> "
                    f"<span class='muted small'>order sending: "
                    f"{'yes' if runner.get('order_sending') else 'no'} · "
                    f"{escape(str(runner.get('trading_capability', '')))}</span>",
                ),
                (
                    "Live path",
                    (
                        f"<span class='chip chip--{'ok' if live_ready else 'danger'}'>"
                        f"{'ready' if live_ready else 'blocked'}</span> "
                        f"<span class='muted small'>"
                        f"{escape(', '.join(live_reasons[:4]) or 'every live check passes')}"
                        f" · orders sent {escape(str(runner.get('live_orders', 0)))}</span>"
                    )
                    if runner.get("live_configured")
                    else (
                        "<span class='chip chip--warn'>not configured</span> "
                        "<span class='muted small'>this process has no MT5 trading adapter; "
                        "live execution runs from the operator's own `axiom live-run` session, "
                        "never from the browser</span>"
                    ),
                ),
                ("Cycles / bars", f"{escape(str(counters.get('cycles', 0)))} / {escape(str(counters.get('bars', 0)))}"),
                ("Signals / orders / fills", f"{escape(str(counters.get('signals', 0)))} / {escape(str(counters.get('orders', 0)))} / {escape(str(counters.get('fills', 0)))}"),
                ("Refusals", escape(str(counters.get("refusals", 0)))),
                ("Why", f"{escape(reason)} <span class='muted small'>{escape(detail[:120])}</span>"),
                ("Restored", "yes" if runner.get("restored") else "no (fresh state)"),
            ]
        )
        + blocker_html
        + "<div style='margin-top:12px'>"
        + actions
        + "</div>"
        + (
            table(
                "Last decisions",
                (("When", "text"), ("Reason", "text"), ("Code", "text"), ("Detail", "text")),
                recent_rows,
                empty=empty_state("No decisions yet", "Press Start; the first cycle records its reason here."),
            )
            if True
            else ""
        )
        + "<p class='small muted' style='margin:12px 0 0'>"
        + (
            "Live mode: a signal that passes the same pre-trade gate is sent to MT5 and the "
            "broker's own fill is booked back into the ledger. The runner never arms the "
            "terminal, never retries an order, and stops on an unknown outcome."
            if runner.get("live_configured")
            else "Paper only: signals are simulated against the local ledger. The runner "
            "cannot send an order, arm live execution, or change the execution mode; "
            "without a live path it refuses to run in LIVE mode."
        )
        + "</p>"
    )

    positions = list(runner.get("positions") or ())
    fills = list(runner.get("fills") or ())
    position_rows = [
        (
            escape(str(item.get("pair", ""))),
            escape(str(item.get("side", ""))),
            escape(str(item.get("remaining_quantity", item.get("quantity", "")))),
            escape(str(item.get("entry_price", ""))),
            escape(str(item.get("entry_timestamp", ""))[:19]),
        )
        for item in positions
    ]
    fill_rows = [
        (
            escape(str(item.get("filled_at", ""))[:19]),
            escape(str(item.get("action", ""))),
            escape(str(item.get("side", ""))),
            escape(str(item.get("quantity", ""))),
            escape(str(item.get("price", ""))),
        )
        for item in reversed(fills[-8:])
    ]

    return (
        card(
            "Paper strategy runner",
            body,
            hint="Simulated fills only. This is not a live trading system and makes no promise of profit.",
            right=_runner_state_chip(ctx),
        )
        + card(
            "Runner positions and fills",
            table(
                "Positions opened by the runner (simulated ledger)",
                (("Pair", "text"), ("Side", "text"), ("Qty", "num"), ("Entry", "num"), ("Opened", "text")),
                position_rows,
                empty=empty_state("No open runner positions", "Flat until the strategy signals an entry."),
            )
            + table(
                "Recent simulated fills",
                (("Filled", "text"), ("Action", "text"), ("Side", "text"), ("Qty", "num"), ("Price", "num")),
                fill_rows,
                empty=empty_state("No fills yet", "A fill appears only after a signal passes every gate."),
            ),
            flush=True,
        )
    )


def _positions_card(ctx: PageContext) -> str:
    paper = ctx.paper
    if not paper.available:
        return card("Open positions", empty_state("Positions unavailable", paper.error or ""))
    rows = []
    for position in paper.open_positions:
        rows.append(
            (
                escape(str(position.get("pair", ""))),
                escape(str(position.get("side", ""))),
                escape(str(position.get("quantity", ""))),
                escape(str(position.get("entry_price", ""))),
                escape(str(position.get("entry_timestamp", ""))),
            )
        )
    body = table(
        "Open paper positions (simulated ledger)",
        (("Pair", "text"), ("Side", "text"), ("Quantity", "num"), ("Entry", "num"), ("Opened", "text")),
        rows,
        empty=empty_state(
            "No open paper positions",
            "The paper ledger is flat. Positions appear here only after paper fills; "
            "this interface cannot open one.",
        ),
    )
    return card("Open positions", body, right=data_chip(ctx.paper_label), flush=True)


def _activity_card(ctx: PageContext) -> str:
    events = list(ctx.paper.activity)[:6]
    if not events:
        return card(
            "Recent activity",
            empty_state("No activity yet", "Audit events from the paper platform appear here, newest first."),
        )
    items = []
    for event in events:
        name = str(event.get("event_type", "event"))
        allowed = event.get("allowed")
        tone = "" if allowed is None else ("ok" if allowed else "warn")
        when = str(event.get("recorded_at") or event.get("timestamp") or "")
        items.append(
            f"<li class='feed-item'><span class='feed-dot {'feed-dot--' + tone if tone else ''}'></span>"
            f"<span><strong>{escape(name)}</strong>"
            f"<span class='muted small'> {escape(str(event.get('mode', '') or ''))}</span></span>"
            f"<span class='feed-when'>{escape(when[:19])}</span></li>"
        )
    return card(
        "Recent activity",
        f"<ul class='list-reset feed'>{''.join(items)}</ul>",
        hint="Sanitized audit events. No payloads, prices, or balances are shown here.",
        right=data_chip(ctx.paper_label),
    )


def _health_card(ctx: PageContext) -> str:
    checks = ctx.paper.health_checks
    if not checks:
        return card("System health", empty_state("Health unavailable", "The paper platform did not report checks."))
    items = []
    for name, ok, detail in checks:
        cls = "check-ico--ok" if ok else "check-ico--bad"
        glyph = "✓" if ok else "!"
        items.append(
            f"<li><span class='check-ico {cls}' aria-hidden='true'>{glyph}</span>"
            f"<span><strong>{escape(name)}</strong> <span class='muted small'>{escape(detail)}</span>"
            f"<span class='visually-hidden'>{' pass' if ok else ' attention'}</span></span></li>"
        )
    status_chip = chip(
        ctx.paper.health_status or "UNKNOWN",
        "ok" if ctx.paper.health_status == "HEALTHY" else "warn",
        dot=True,
    )
    return card("System health", f"<ul class='check-list'>{''.join(items)}</ul>", right=status_chip)


def _safety_card() -> str:
    from .setup import safety_statement

    safety = safety_statement()
    items = "".join(f"<li>{escape(line)}</li>" for line in safety["lines"])
    return card(
        "What this workstation will not do",
        f"<ul class='check-list' style='list-style:none'>{items}</ul>",
        hint="These are enforced by construction, not by convention.",
    )


def _mode_view(ctx: PageContext) -> Mapping[str, Any]:
    return ctx.execution_mode or {}


def mode_banner(ctx: PageContext) -> str:
    """One loud line saying which execution mode is in force, and what it does."""

    view = _mode_view(ctx)
    live = str(view.get("mode", "PAPER")).upper() == "LIVE"
    effective = ctx.live_effective
    klass = "mode-banner mode-banner--live" if live else "mode-banner mode-banner--paper"
    title = "Live trading — MetaTrader 5 execution" if live else "Paper trading — live market data"
    effect = escape(str(view.get("trading_effect", "")))
    right = (
        chip("Confirmed this session", "danger", dot=True)
        if effective
        else (
            chip("Awaiting confirmation", "warn", dot=True)
            if live
            else chip("Simulated fills only", "paper", dot=True)
        )
    )
    market = str(getattr(ctx.terminal, "market_data", "NONE") or "NONE")
    market_note = (
        "Prices: MetaTrader 5 terminal ticks."
        if market == "TERMINAL_TICKS"
        else ("Prices: sample data from the offline simulator." if market == "SAMPLE" else "Prices: none — no terminal connected.")
    )
    return (
        f"<div class='{klass}' role='status'>"
        f"<div><strong>{escape(title)}</strong><p class='small muted' style='margin:4px 0 0'>"
        f"{effect} {escape(market_note)}</p></div>"
        f"<div class='spacer'></div>{right}"
        "<a class='btn btn-sm' href='/mode'>Change trading mode</a>"
        "</div>"
    )


def _mode_card(ctx: PageContext) -> str:
    view = _mode_view(ctx)
    live = str(view.get("mode", "PAPER")).upper() == "LIVE"
    preflight = ctx.preflight or {}
    count = int(preflight.get("count", 0) or 0)
    if live and ctx.live_effective:
        detail = (
            "Live execution is enabled for this session. An order still needs an "
            "approved active strategy, the pre-trade gate, and an armed session; "
            "nothing is sent because of this switch."
        )
        tone = "danger"
    elif live:
        detail = (
            "Live was requested but this process has not confirmed it, so nothing is "
            "routed. A restart never resumes live execution on its own."
        )
        tone = "warn"
    elif count:
        detail = f"Paper is active. {count} check(s) stand between you and live execution."
        tone = "paper"
    else:
        detail = "Paper is active and live execution is ready to be enabled with one confirmation."
        tone = "paper"
    actions = (
        action_form("mode-paper", ctx.action_nonce, "Switch to paper now", tone="")
        + "<a class='btn btn-sm' href='/mode'>Trading mode</a>"
        if live
        else "<a class='btn btn-sm btn--primary' href='/mode'>Review live checks</a>"
    )
    return card(
        "Execution mode",
        f"<div class='stack'><div class='row'>{execution_mode_chip(ctx)}"
        f"{chip(f'{count} live check(s) outstanding', 'warn') if count else chip('live checks clear', 'ok')}"
        f"</div><p class='small' style='margin:0;color:var(--text-2)'>{escape(detail)}</p>"
        f"<div class='row'>{actions}</div></div>",
        hint="Decides how an approved order would be executed. It never places one.",
        right=chip("Paper always available", "muted"),
    )


def _blocker_items(blockers: Sequence[Mapping[str, Any]]) -> str:
    items = []
    for blocker in blockers:
        items.append(
            "<li class='blocker'>"
            f"<div class='row'>{chip(str(blocker.get('category', 'session')).title(), 'warn')}"
            f"<strong>{escape(str(blocker.get('title', '')))}</strong>"
            f"{chip(str(blocker.get('reason', '')), 'muted')}</div>"
            f"<p class='why'>{escape(str(blocker.get('message', '')))}</p>"
            f"<p class='fix'><strong>Fix it:</strong> {escape(str(blocker.get('resolution', '')))}"
            f" <span class='muted'>({escape(str(blocker.get('where', '')))})</span></p>"
            "</li>"
        )
    return "<ul class='blocker-list'>" + "".join(items) + "</ul>"


def render_mode(ctx: PageContext) -> str:
    view = _mode_view(ctx)
    preflight = ctx.preflight or {}
    live = str(view.get("mode", "PAPER")).upper() == "LIVE"
    effective = ctx.live_effective
    blockers: Sequence[Mapping[str, Any]] = tuple(preflight.get("blockers", ()) or ())
    ready = bool(preflight.get("ready", False))

    paper_option = (
        "<div class='mode-option" + (" mode-option--active" if not live else "") + "'>"
        "<h3>" + escape(str(view.get("paper_label", "Paper trading")))
        + (chip("ACTIVE", "paper") if not live else "") + "</h3>"
        "<p>Reads live prices from the selected terminal and simulates every fill "
        "locally. No order is ever sent; the paper ledger keeps the result.</p>"
        "<p class='small muted'>Switching to paper is immediate: no preflight and no "
        "confirmation, because it can only remove execution capability.</p>"
        "<div class='mode-actions'>"
        + (
            chip("Paper is active", "paper", dot=True)
            if not live
            else action_form("mode-paper", ctx.action_nonce, "Switch to paper now", tone="primary")
        )
        + "</div></div>"
    )

    if effective:
        live_actions = (
            chip("Live is enabled for this session", "danger", dot=True)
            + action_form("mode-paper", ctx.action_nonce, "End live mode", tone="danger")
        )
        live_note = (
            "<p class='small muted'>Every order still passes the pre-trade gate and the "
            "session must be armed separately. This switch sends nothing.</p>"
        )
    elif ready:
        live_actions = (
            "<form method='post' action='/actions/mode-live' class='stack' style='gap:10px'>"
            f"<input type='hidden' name='nonce' value='{escape(ctx.action_nonce)}'>"
            "<div class='field'><label for='confirmation'>Type "
            f"<span class='mono'>{escape(str(view.get('confirmation_phrase', '')))}</span> "
            "to confirm</label>"
            "<input type='text' id='confirmation' name='confirmation' value='' "
            "autocomplete='off' spellcheck='false' required></div>"
            "<div><button class='btn btn--danger' type='submit'>Enable live execution</button></div>"
            "</form>"
        )
        live_note = (
            "<p class='small muted'>One confirmation, and it is recorded. Enabling live "
            "does not place an order, connect anything, or arm anything.</p>"
        )
    else:
        live_actions = (
            "<span class='btn btn--danger' aria-disabled='true' role='link' tabindex='-1'>"
            "Enable live execution</span>"
            + chip(f"{len(blockers)} check(s) blocking", "warn")
        )
        live_note = (
            "<p class='small muted'>Resolve every check below, then this button turns "
            "into a confirmation. Nothing here can be skipped or waived.</p>"
        )

    live_option = (
        "<div class='mode-option mode-option--live" + (" mode-option--active" if live else "") + "'>"
        "<h3>" + escape(str(view.get("live_label", "Live trading")))
        + (chip("ACTIVE", "danger") if effective else (chip("REQUESTED", "warn") if live else ""))
        + "</h3>"
        "<p>Routes an approved, gated order through the MetaTrader 5 execution path to the "
        "account you signed in to inside the terminal.</p>"
        + live_note
        + "<div class='mode-actions'>" + live_actions + "</div></div>"
    )

    if blockers:
        checks_body = (
            "<p class='small muted' style='margin:0 0 10px'>"
            + escape(str(preflight.get("summary", "")))
            + "</p>"
            + _blocker_items(blockers)
        )
    else:
        checks_body = notice(
            "ok",
            "Every live check passed",
            "The account identity, session, settings, and confirmations all line up. "
            "Enabling live still sends nothing.",
        )

    requirements = tuple(preflight.get("order_requirements", ()) or ())
    requirement_rows = [
        (
            escape(str(item.get("title", ""))),
            escape(str(item.get("detail", ""))),
            chip(str(item.get("enforced_at", "order time")), "muted"),
        )
        for item in requirements
    ]
    requirements_body = table(
        "Checks re-run for every live order",
        (("Check", ""), ("What stops an order", ""), ("When", "")),
        requirement_rows,
    )

    recent = tuple(view.get("recent", ()) or ())
    recent_rows = [
        (
            escape(str(item.get("event_type", ""))),
            escape(str(item.get("reason", ""))),
            escape(str(item.get("to_mode", item.get("mode", "")) or "")),
            escape(str(item.get("at", ""))),
        )
        for item in reversed(recent)
    ]
    audit_body = table(
        "Execution-mode changes recorded on this machine",
        (("Event", ""), ("Reason", ""), ("Mode", ""), ("When", "")),
        recent_rows,
        empty=empty_state(
            "No mode changes recorded yet",
            "Every switch and every refused switch is written here and to the local "
            "audit document.",
        ),
    )

    account = preflight.get("account", {}) if isinstance(preflight, Mapping) else {}
    account_line = (
        dl(
            [
                ("Selected account (server)", escape(str(account.get("server", "")) or "not read")),
                ("Detected account type", escape(str(account.get("mode", "UNKNOWN")))),
                ("Account fingerprint", "…" + escape(str(account.get("fingerprint", "")) or "—")),
                ("Broker connection", chip("connected", "live") if preflight.get("session_connected") else chip("not connected", "muted")),
            ],
            wide=True,
        )
        if account
        else empty_state("No account read yet", "Connect to the terminal to read the account you signed in to.")
    )

    body = (
        mode_banner(ctx)
        + "<div class='mode-grid'>" + paper_option + live_option + "</div>"
        + "<div class='grid grid--two'>"
        + card(
            "What must pass before live execution",
            checks_body,
            hint="Every genuine blocker, all at once, each with the step that clears it.",
            right=chip("Clear" if ready else f"{len(blockers)} outstanding", "ok" if ready else "warn"),
        )
        + "<div class='grid'>" + card("Selected account", account_line) + "</div></div>"
        + card(
            "Still enforced on every order, whichever mode is on",
            requirements_body,
            hint="The mode switch never replaces, relaxes, or skips any of these.",
        )
        + audit_body
    )
    return render_shell(ctx, body)


def render_overview(ctx: PageContext) -> str:
    body = (
        mode_banner(ctx)
        + _session_kpis(ctx)
        + "<div class='grid grid--two'>"
        + _terminal_card(ctx)
        + "<div class='grid'>"
        + _mode_card(ctx)
        + _market_card(ctx)
        + _paper_card(ctx)
        + "</div></div>"
        + _runner_card(ctx)
        + "<div class='grid grid--two'>"
        + _positions_card(ctx)
        + "<div class='grid'>" + _health_card(ctx) + _safety_card() + "</div>"
        + "</div>"
        + _activity_card(ctx)
    )
    return render_shell(ctx, body)


def _step_item(step: Mapping[str, Any], ctx: PageContext) -> str:
    status = step["status"]
    tone = {
        "complete": ("Complete", "ok"),
        "action_required": ("Action needed", "warn"),
        "blocked": ("Blocked", "danger"),
        "pending": ("Waiting", "muted"),
    }.get(status, (status, "muted"))
    items_html = ""
    if step["items"]:
        items_html = (
            "<div class='table-wrap' style='margin-top:12px'>"
            "<table><tbody>"
            + "".join(
                f"<tr><th scope='row' style='width:38%;background:transparent;text-transform:none;"
                f"letter-spacing:0;font-size:12.5px;color:var(--text-3)'>{escape(item['label'])}</th>"
                f"<td>{escape(item['value'])}</td></tr>"
                for item in step["items"]
            )
            + "</tbody></table></div>"
        )
    detail = f"<div class='detail'>{escape(step['detail'])}</div>" if step["detail"] else ""
    actions = ""
    if step["action"]:
        tone_cls = "primary" if status in {"action_required"} else ""
        if step["action"] == "recheck":
            actions = "<a class='btn btn-sm' href='/setup'>Check again</a>"
        elif step["action"] == "acknowledge-sign-in":
            actions = action_form("acknowledge-sign-in", ctx.action_nonce, step["action_label"], tone=tone_cls)
        elif step["action"] in {"connect", "disconnect"}:
            # The label comes from the step: a simulator's button says so.
            if step["action"] == "connect":
                actions = action_form(
                    "connect",
                    ctx.action_nonce,
                    step["action_label"],
                    tone="" if ctx.terminal.simulated else "primary",
                )
            else:
                actions = action_form(
                    "disconnect", ctx.action_nonce, step["action_label"], tone="danger"
                )
        elif step["action"] == "confirm":
            actions = f"<a class='btn btn-sm btn--primary' href='/account'>Review &amp; confirm</a>"
    return (
        f"<li class='step step--{escape(status)}'>"
        f"<span class='step-num' aria-hidden='true'>{step['number']}</span>"
        "<div class='step-body'>"
        f"<div class='step-title'><h3>{escape(step['title'])}</h3>{chip(tone[0], tone[1])}</div>"
        f"<p>{escape(step['summary'])}</p>{detail}{items_html}"
        + (f"<div class='step-actions'>{actions}</div>" if actions else "")
        + "</div></li>"
    )


def render_setup(ctx: PageContext) -> str:
    view = ctx.wizard.view()
    progress = view["progress"]
    steps = "".join(_step_item(step, ctx) for step in view["steps"])

    prereq = view["prerequisites"]
    if prereq:
        cards = "".join(
            f"<div class='notice notice--warn'><div><strong>{escape(item['title'])}</strong>"
            f"<p>{escape(item['detail'])}</p></div></div>"
            for item in prereq
        )
        prereq_body = f"<div class='stack'>{cards}</div>"
    else:
        prereq_body = notice(
            "ok",
            "All prerequisites look good",
            "Windows, Python, the MetaTrader5 package, and the terminal were all found.",
        )

    template = (
        "<p class='small muted' style='margin:0 0 8px'>Save this as "
        "<span class='mono'>mt5_settings.local.json</span> next to the launcher. It holds no "
        "password: AXIOM uses the terminal's own signed-in session.</p>"
        f"<pre class='template'>{escape(view['settings_template'])}</pre>"
    )

    safety_items = "".join(f"<li>{escape(line)}</li>" for line in view["safety"]["lines"])
    body = (
        "<div class='grid grid--two'>"
        + card(
            "First-run setup",
            f"<div class='spread' style='margin-bottom:16px'>"
            f"<div class='small muted'>{progress['completed']} of {progress['total']} steps complete</div>"
            f"<div class='small muted'>{progress['percent']}%</div></div>"
            f"<div class='bar' role='progressbar' aria-valuenow='{progress['percent']}' "
            f"aria-valuemin='0' aria-valuemax='100' aria-label='Setup progress'><span style='width:{progress['percent']}%'></span></div>"
            f"<ol class='steps' style='margin-top:20px'>{steps}</ol>",
            hint="Seven plain steps. Nothing is assumed about your broker, account, or terminal path.",
        )
        + "<div class='grid'>"
        + card("Prerequisites", prereq_body, right=chip("Step 1", "muted"))
        + card("Local settings template", template)
        + card("Guarantees", f"<ul class='check-list'>{safety_items}</ul>")
        + "</div></div>"
    )
    return render_shell(ctx, body)


def render_account(ctx: PageContext) -> str:
    wizard = ctx.wizard
    terminal = ctx.terminal
    view = wizard.view()
    mode = view["account_mode"]
    sample = terminal.simulated
    prefix = "Sample " if sample else ""

    if not terminal.connected:
        account_body = empty_state(
            "No account detected",
            (
                "This is the offline simulator: start the preview to see sample values, or "
                "launch on the Windows machine without --simulate to read the real terminal."
                if sample
                else "Connect to the terminal first. AXIOM reads the account that MetaTrader 5 "
                "already has selected; it never switches or logs in."
            ),
            action_form("connect", ctx.action_nonce, connect_label(ctx), tone="primary")
            + "<a class='btn' href='/setup'>Setup steps</a>",
        )
    else:
        heading = (
            f"Sample account mode: {mode['detected']}"
            if sample
            else f"Detected account mode: {mode['detected']}"
        )
        note = (
            "Illustrative value from the offline simulator. No account was read, and "
            "nothing here describes a real broker account."
            if sample
            else mode["note"]
        )
        account_body = (
            f"<div class='notice notice--warn' role='status'><div>"
            f"<strong>{escape(heading)}</strong>"
            f"<p>{escape(note)}</p></div>"
            f"{chip(mode['detected'], 'simulated' if sample else ('ok' if mode['is_demo'] else 'warn'), dot=True)}</div>"
            + dl(
                [
                    (
                        f"{prefix}Account fingerprint",
                        f"<span class='mono'>…{escape(terminal.fingerprint)}</span> "
                        f"{chip('login redacted', 'muted')}",
                    ),
                    (f"{prefix}Broker / server", escape(terminal.server or "unknown")),
                    (f"{prefix}Currency", escape(terminal.currency or "unknown")),
                    (f"{prefix}Leverage", escape(terminal.leverage or "unknown")),
                    (f"{prefix}Margin accounting", escape(terminal.margin_mode or "unknown")),
                    (
                        f"{prefix}Account mode",
                        escape(terminal.mode)
                        + ("" if terminal.mode_confirmed or sample else " (unconfirmed)"),
                    ),
                    (f"{prefix}Terminal build", escape(terminal.build or "unknown")),
                    (
                        f"{prefix}Terminal trading permission",
                        escape("allowed" if terminal.trade_allowed else "not allowed"),
                    ),
                ],
                wide=True,
            )
            + "<p class='small muted' style='margin-top:12px'>Balances, equity, and profit are never read "
            "into this interface, and the account number is shown only as a fingerprint."
            + (
                " Sample values exist so the layout can be reviewed offline."
                if sample
                else ""
            )
            + "</p>"
        )

    symbols = terminal.symbols if terminal.connected else ()
    selected = set(view["symbols_selected"])
    if symbols:
        options = "".join(
            f"<label class='symbol'><input type='checkbox' name='symbol' value='{escape(str(item.get('name', '')))}'"
            f"{' checked' if str(item.get('name', '')) in selected else ''}>"
            f"<span>{escape(str(item.get('name', '')))}</span></label>"
            for item in symbols
        )
        source_note = (
            "sample symbols from the simulator"
            if sample
            else "symbols reported by the terminal"
        )
        symbol_body = (
            "<div class='spread' style='margin-bottom:10px'>"
            f"<span class='small muted'>{len(symbols)} {source_note}</span>"
            f"{chip(f'{len(selected)} selected', 'ok' if selected else 'warn')}</div>"
            f"<form method='post' action='/actions/symbols'>"
            f"<input type='hidden' name='nonce' value='{escape(ctx.action_nonce)}'>"
            f"<div class='symbol-grid' role='group' aria-label='Symbols to observe'>{options}</div>"
            "<div class='step-actions'><button class='btn btn--primary' type='submit'>Save selection</button></div>"
            "</form>"
            + (
                "<p class='small muted' style='margin-top:10px'>The selection is kept in memory "
                "for this session only.</p>"
                if sample
                else ""
            )
        )
    else:
        symbol_body = empty_state(
            "No symbols yet",
            "Symbols are listed once the terminal is connected and reports its Market Watch.",
        )

    if not terminal.connected:
        confirm_body = empty_state(
            "Nothing to confirm",
            "Confirmation records that a real account and its symbols were reviewed. "
            "Connect to the terminal first.",
        )
    elif sample:
        confirm_body = (
            notice(
                "warn",
                "A simulator preview cannot be confirmed",
                "Confirmation is a statement about a real account. Start the workstation "
                "without --simulate, on the Windows machine that runs MetaTrader 5.",
            )
            + dl(
                [
                    ("Account", "not connected to a terminal"),
                    ("Symbols", escape(", ".join(view["symbols_selected"]) or "none selected")),
                    (f"{prefix}Mode", escape(mode["detected"])),
                    ("Trading capability", "none"),
                ],
                wide=True,
            )
        )
    else:
        confirm_body = (
            "<div class='stack'>"
            + dl(
                [
                    (
                        "Account",
                        escape(terminal.server or "not connected")
                        + (f" · …{escape(terminal.fingerprint)}" if terminal.fingerprint else ""),
                    ),
                    ("Symbols", escape(", ".join(view["symbols_selected"]) or "none selected")),
                    ("Mode (detected)", escape(mode["detected"])),
                ],
                wide=True,
            )
            + (
                notice(
                    "ok",
                    "Configuration confirmed",
                    f"Confirmed at {view['configured_at']}. You can re-confirm after changing anything.",
                )
                if view["confirmed"]
                else notice(
                    "warn",
                    "Not confirmed yet",
                    "Confirm once the account and symbols match what you intend to observe.",
                )
            )
            + "<div class='step-actions'>"
            + (
                action_form("unconfirm", ctx.action_nonce, "Withdraw confirmation")
                if view["confirmed"]
                else action_form("confirm", ctx.action_nonce, "Confirm configuration", tone="primary")
            )
            + "</div></div>"
        )

    body = (
        "<div class='grid grid--two'>"
        + card(
            "Detected account",
            account_body,
            right=mode_chip(ctx) if terminal.connected else chip("Not connected", "muted"),
        )
        + "<div class='grid'>"
        + card(
            "Symbols to observe",
            symbol_body,
            hint="From the terminal's Market Watch; nothing is invented.",
        )
        + card("Confirm configuration", confirm_body, hint="Step 5 of setup.")
        + "</div></div>"
    )
    return render_shell(ctx, body)


def render_positions(ctx: PageContext) -> str:
    body = (
        notice(
            "warn",
            "Paper positions only",
            "This screen shows the simulated paper ledger. Broker positions are not read "
            "into this build, and no position can be opened, changed, or closed here.",
        )
        + _positions_card(ctx)
        + _runner_card(ctx)
        + card(
            "About this ledger",
            "<p class='small muted' style='margin:0'>Fills come from the offline paper engine. "
            "They are simulated, are not broker confirmations, and are labelled with their "
            "data class throughout.</p>",
        )
    )
    return render_shell(ctx, body)


def render_activity(ctx: PageContext) -> str:
    events = list(ctx.paper.activity)
    rows = [
        (
            escape(str(event.get("recorded_at", ""))[:19]),
            escape(str(event.get("event_type", ""))),
            escape(str(event.get("mode", ""))),
            escape("allowed" if event.get("allowed") else ("refused" if event.get("allowed") is False else "—")),
        )
        for event in events
    ]
    body = (
        notice(
            "ok",
            "Sanitized audit trail",
            "Credential-shaped fields are redacted before they reach this page.",
        )
        + card(
            "Recent events",
            table(
                "Audit events, newest first",
                (("When", "text"), ("Event", "text"), ("Mode", "text"), ("Outcome", "text")),
                rows,
                empty=empty_state("No audit events yet", "Run the offline paper workflow to create some."),
            ),
            right=data_chip(ctx.paper_label),
            flush=True,
        )
        + _runner_card(ctx)
    )
    return render_shell(ctx, body)


def render_health(ctx: PageContext) -> str:
    checks = ctx.paper.health_checks
    rows = [
        (escape(name), chip("OK", "ok") if ok else chip("Attention", "warn"), escape(detail))
        for name, ok, detail in checks
    ]
    body = (
        "<div class='grid grid--two'>"
        + card(
            "Platform health",
            table(
                "Paper platform checks",
                (("Check", "text"), ("Status", "text"), ("Detail", "text")),
                rows,
                empty=empty_state("Health unavailable", "No checks were reported."),
            ),
            right=chip(ctx.paper.health_status or "UNKNOWN", "ok" if ctx.paper.health_status == "HEALTHY" else "warn", dot=True),
            flush=True,
        )
        + "<div class='grid'>"
        + card(
            "Boundaries",
            dl(
                [
                    ("Live trading", chip("disabled", "ok")),
                    ("Broker mode", chip("local paper", "paper")),
                    ("External broker", "not enabled from this UI"),
                    ("Order entry", "not available"),
                    ("Arming", "not available"),
                    ("Market access token", "not used on the MT5 path"),
                ]
            ),
            hint="Read-only view of the platform's own safety state.",
        )
        + _safety_card()
        + "</div></div>"
    )
    return render_shell(ctx, body)


def _kind_chip(kind: str) -> str:
    if kind == "MT5_TICK_EXPORT":
        return chip("broker tick export", "live")
    if kind == "CANONICAL_CSV":
        return chip("canonical bid/ask", "paper")
    return chip("unrecognised", "muted")


def _bytes(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "kB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _convert_form(ctx: PageContext, entry: Mapping[str, Any]) -> str:
    nonce = escape(ctx.action_nonce)
    name = escape(str(entry.get("name", "")))
    # The id only has to be unique on this page; the name travels as a value.
    slug = "".join(ch for ch in str(entry.get("name", "")) if ch.isalnum())[:24] or "file0"
    return (
        "<form method='post' action='/actions/ticks-convert' class='stack' style='gap:10px'>"
        f"<input type='hidden' name='nonce' value='{nonce}'>"
        f"<input type='hidden' name='file' value='{name}'>"
        "<div class='data-grid'>"
        f"<div class='data-field'><label for='pair-{slug}'>Pair (canonical)</label>"
        f"<input type='text' id='pair-{slug}' name='pair' placeholder='EUR/USD' "
        "autocomplete='off' spellcheck='false' required></div>"
        f"<div class='data-field'><label for='offset-{slug}'>Server UTC offset (minutes)</label>"
        f"<input type='number' id='offset-{slug}' name='server_offset_minutes' "
        "placeholder='e.g. 120' step='1' min='-840' max='840'></div>"
        f"<div class='data-field'><label for='zone-{slug}'>…or server zone (IANA)</label>"
        f"<input type='text' id='zone-{slug}' name='server_timezone' "
        "placeholder='Europe/Athens' autocomplete='off' spellcheck='false'></div>"
        f"<div class='data-field'><label for='interval-{slug}'>Bar interval</label>"
        f"<select id='interval-{slug}' name='interval_seconds'>"
        "<option value='60'>1 minute</option><option value='300'>5 minutes</option>"
        "<option value='900'>15 minutes</option><option value='3600'>1 hour</option>"
        "</select></div>"
        "</div>"
        "<div class='row'><button class='btn btn--primary' type='submit'>"
        "Convert to canonical CSV</button>"
        "<span class='small muted'>The server clock basis has no default. Empty windows "
        "are reported as gaps, never filled.</span></div>"
        "</form>"
    )


def _evaluate_form(ctx: PageContext, entry: Mapping[str, Any], classes: Sequence[str]) -> str:
    nonce = escape(ctx.action_nonce)
    name = escape(str(entry.get("name", "")))
    options = "".join(
        f"<option value='{escape(value)}'>{escape(value.replace('_', ' ').lower())}</option>"
        for value in classes
    )
    return (
        f"<form method='post' action='/actions/dataset-evaluate' class='stack' style='gap:10px'>"
        f"<input type='hidden' name='nonce' value='{nonce}'>"
        f"<input type='hidden' name='file' value='{name}'>"
        "<div class='data-grid'>"
        "<div class='data-field'><label>Pair (canonical)</label>"
        "<input type='text' name='pair' value='"
        f"{escape(', '.join(entry.get('pairs') or []))}' autocomplete='off' spellcheck='false' required></div>"
        "<div class='data-field'><label>Declared data class</label>"
        f"<select name='data_class'>{options}</select></div>"
        "</div>"
        "<label class='small' style='display:flex;gap:8px;align-items:flex-start'>"
        "<input type='checkbox' name='declare' value='yes' required>"
        "<span>I exported this file myself and confirm what it is. AXIOM will not guess, "
        "and a sample or demo file can never promote a strategy.</span></label>"
        "<div class='row'><button class='btn btn--primary' type='submit'>"
        "Evaluate candidates</button>"
        "<span class='small muted'>Chronological split, walk-forward folds, and one locked "
        "test window per dataset. Evidence is recorded; nothing is traded.</span></div>"
        "</form>"
    )


def render_data(ctx: PageContext) -> str:
    view = dict((ctx.extra or {}).get("data") or {})
    nonce = escape(ctx.action_nonce)
    last = dict(view.get("last") or {})
    files = list(view.get("files") or [])

    if not view.get("configured"):
        body = (
            notice(
                "warn",
                "No import folder configured",
                "Start the workstation with --imports-dir <folder> to list and convert "
                "files here. Nothing is downloaded, and AXIOM never invents market data.",
            )
            + _runner_card(ctx)
        )
        return render_shell(ctx, body)

    head = (
        notice(
            "ok",
            "Local files only",
            "This page reads the folder below. It connects to nothing, places no order, "
            "arms nothing, and never changes the execution mode.",
        )
        + card(
            "Import folder",
            dl(
                [
                    ("Folder", f"<span class='mono'>{escape(str(view.get('import_dir', '')))}</span>"),
                    ("Files listed", escape(str(len(files)))),
                    (
                        "Scan error",
                        escape(str(view.get("scan_error", "")) or "none"),
                    ),
                ]
            )
            + "<div class='step-actions'>"
            + action_form("imports-refresh", ctx.action_nonce, "Re-scan folder", tone="ghost")
            + "</div>",
            hint="Drop broker exports and canonical CSVs here, then re-scan.",
        )
    )

    rows = []
    for entry in files:
        name = escape(str(entry.get("name", "")))
        flags = []
        if entry.get("looks_like_sample"):
            flags.append(chip("sample — never evaluated as history", "warn"))
        if entry.get("errors"):
            flags.append(chip(f"{entry['errors']} error(s)", "danger"))
        if entry.get("warnings"):
            flags.append(chip(f"{entry['warnings']} warning(s)", "warn"))
        provenance = dict(entry.get("provenance") or {})
        if provenance:
            flags.append(chip("has provenance sidecar", "paper"))
        span = ""
        if entry.get("first_at"):
            span = (
                f"{escape(str(entry['first_at'])[:19])} → "
                f"{escape(str(entry['last_at'])[:19])}"
            )
        detail = (
            f"{escape(_bytes(int(entry.get('size_bytes') or 0)))}"
            + (f" · {escape(', '.join(entry.get('pairs') or []))}" if entry.get("pairs") else "")
            + (f" · {escape(', '.join(entry.get('timeframes') or []))}" if entry.get("timeframes") else "")
        )
        rows.append(
            (
                f"<span class='mono'>{name}</span><div class='small muted'>{detail}</div>",
                _kind_chip(str(entry.get("kind", ""))),
                escape(
                    f"{entry.get('records', 0)} records"
                    f" ({entry.get('bars', 0)} bars, {entry.get('quotes', 0)} quotes/ticks)"
                ),
                escape(span or "—"),
                " ".join(flags) or chip("no flags", "muted"),
                escape(str(entry.get("note", "")) or "—"),
            )
        )

    body = head + card(
        "Files in the import folder",
        table(
            "Files, described rather than interpreted",
            (
                ("File", "text"),
                ("Kind", "text"),
                ("Contents", "text"),
                ("Span (UTC)", "text"),
                ("Flags", "text"),
                ("Next step", "text"),
            ),
            rows,
            empty=empty_state(
                "No files yet",
                "Put a MetaTrader 5 tick export or a canonical CSV in the folder above, "
                "then re-scan. Nothing is fetched for you.",
            ),
        ),
        flush=True,
    )

    actions = []
    tick_files = list(view.get("tick_files") or [])
    dataset_files = list(view.get("dataset_files") or [])
    if tick_files:
        forms = "".join(
            f"<div class='data-form'><div class='row' style='justify-content:space-between'>"
            f"<span class='mono'>{escape(str(entry.get('name', '')))}</span>"
            f"{chip(str(entry.get('records', 0)) + ' ticks', 'live')}</div>"
            + _convert_form(ctx, entry)
            + "</div>"
            for entry in tick_files
        )
        actions.append(card("1 · Convert a broker tick export", forms))
    if dataset_files:
        classes = view.get("data_classes") or []
        forms = "".join(
            f"<div class='data-form'><div class='row' style='justify-content:space-between'>"
            f"<span class='mono'>{escape(str(entry.get('name', '')))}</span>"
            f"{chip(str(entry.get('bars', 0)) + ' bars', 'paper')}</div>"
            + _evaluate_form(ctx, entry, classes)
            + "</div>"
            for entry in dataset_files
        )
        actions.append(card("2 · Evaluate candidates on this dataset", forms))
    if not actions:
        actions.append(
            card(
                "Nothing to convert or evaluate",
                empty_state(
                    "No usable file",
                    "A tick export must be a broker-exported bid/ask file, and an "
                    "evaluable dataset must be a canonical CSV you exported. A candle "
                    "export cannot be turned into bid/ask data here.",
                ),
            )
        )

    last_card = ""
    if last:
        ok = bool(last.get("ok"))
        if last.get("action") == "convert" and ok:
            coverage = dict(last.get("coverage") or {})
            gaps = list(coverage.get("gap_windows") or [])
            gap_rows = [
                (
                    escape(str(item.get("window", ""))[:19]),
                    escape(str(item.get("reason", ""))),
                )
                for item in gaps[:50]
            ]
            last_card = card(
                "Conversion report",
                dl(
                    [
                        ("Source", f"<span class='mono'>{escape(str(last.get('source_file', '')))}</span>"),
                        ("Written", f"<span class='mono'>{escape(str(last.get('output_file', '')))}</span>"),
                        ("Ticks read", escape(str(last.get("tick_count", 0)))),
                        ("Bars written", escape(str(last.get("bars_written", 0)))),
                        ("Duplicate ticks", escape(str(last.get("duplicates", 0)))),
                        ("Rejected rows", escape(str(len(last.get("rejected_rows") or [])))),
                        ("Server time basis", escape(str(last.get("server_time_basis", "")))),
                        ("Time basis detail", escape(str(last.get("time_basis_detail", ""))) or "—"),
                        ("Empty windows", escape(str(coverage.get("empty_windows", 0)))),
                        ("Largest gap (s)", escape(str(coverage.get("largest_gap_seconds", "0")))),
                        (
                            "Trailing partial window",
                            escape(str(coverage.get("trailing_partial_window", "")) or "none"),
                        ),
                        ("Source SHA-256", f"<span class='mono'>{escape(str(last.get('source_sha256', ''))[:32])}…</span>"),
                        ("Provenance", f"<span class='mono'>{escape(str(last.get('provenance_path', '')))}</span>"),
                    ]
                )
                + (
                    table(
                        "Empty windows, reported not filled",
                        (("Window start (UTC)", "text"), ("Reason", "text")),
                        gap_rows,
                    )
                    if gap_rows
                    else "<p class='small muted'>Every window in this range had ticks; no "
                    "gaps were found.</p>"
                ),
                hint="Only complete windows became bars. A gap is named, never interpolated.",
            )
        elif last.get("action") == "evaluate" and ok:
            results = list(last.get("results") or [])
            result_rows = []
            for item in results:
                aggregate = dict(item.get("aggregate") or {})
                result_rows.append(
                    (
                        f"<span class='mono'>{escape(str(item.get('candidate_id', '')))}</span>",
                        chip(
                            str(item.get("status", "")),
                            "ok" if item.get("status") == "SELECTED_FOR_PAPER" else "warn",
                        ),
                        escape(
                            ", ".join(item.get("failed_criteria") or [])
                            or str(item.get("ineligible_reason", ""))
                            or "—"
                        ),
                        escape(str(aggregate.get("return_fraction", "—"))),
                        escape(str(aggregate.get("max_drawdown_fraction", "—"))),
                        escape(str(aggregate.get("closed_trades", "—"))),
                    )
                )
            last_card = card(
                "Evaluation report",
                dl(
                    [
                        ("Dataset", f"<span class='mono'>{escape(str(last.get('dataset_id', '')))}</span>"),
                        ("Pair", escape(str((last.get("meta") or {}).get("pair", "")))),
                        ("Bars", escape(str(last.get("bars", 0)))),
                        ("Declared data class", escape(str(last.get("data_class", "")))),
                        (
                            "Locked test window",
                            chip(
                                "spent on this dataset" if last.get("locked_test_spent") else "not spent",
                                "warn" if last.get("locked_test_spent") else "muted",
                            ),
                        ),
                        (
                            "Candidates selected",
                            escape(", ".join(last.get("selected") or []) or "none"),
                        ),
                    ]
                )
                + table(
                    "Candidate evidence on the sample you supplied",
                    (
                        ("Candidate", "text"),
                        ("Status", "text"),
                        ("Why not selected", "text"),
                        ("Return (gross of nothing)", "num"),
                        ("Max drawdown", "num"),
                        ("Closed trades", "num"),
                    ),
                    result_rows,
                )
                + "<p class='small muted' style='margin-top:10px'>"
                + escape(str(last.get("note", "")))
                + " Assumptions: "
                + escape("; ".join(last.get("assumptions") or []))
                + "</p>",
                hint="Evidence, not a forecast. Paper results follow only from the simulated lane.",
            )
        else:
            last_card = card(
                "Last action",
                "<div class='notice notice--warn' role='alert'><div><strong>"
                + escape(str(last.get("reason", "refused")))
                + "</strong><p>"
                + escape(str(last.get("detail", "")) or "Nothing was written.")
                + "</p></div></div>",
                hint="The folder is unchanged and nothing was evaluated.",
            )

    evidence = dict(view.get("evidence") or {})
    evidence_rows = []
    for row in list(evidence.get("rows") or []):
        evidence_rows.append(
            (
                f"<span class='mono'>{escape(str(row.get('candidate_id', '')))}</span>",
                chip(
                    str(row.get("status", "")),
                    "ok" if row.get("selected") else "muted",
                ),
                escape(str(row.get("dataset_id", "")) or "—"),
                escape(str(row.get("data_class", "")) or "—"),
                escape(str(row.get("evaluated_at", ""))[:19] or "—"),
                escape(", ".join(row.get("failed_criteria") or []) or str(row.get("detail", "")) or "—"),
            )
        )
    evidence_card = card(
        "Recorded evidence",
        table(
            "What the research database holds for each candidate",
            (
                ("Candidate", "text"),
                ("Status", "text"),
                ("Dataset", "text"),
                ("Data class", "text"),
                ("Evaluated (UTC)", "text"),
                ("Blocking criteria", "text"),
            ),
            evidence_rows,
            empty=empty_state(
                "No evidence recorded",
                "No candidate has been evaluated on an imported dataset yet."
                if evidence.get("available", True)
                else f"Evidence store unavailable ({escape(str(evidence.get('reason', '')))}).",
            ),
        ),
        hint="The paper runner reads exactly this; it will not start without a selected candidate.",
    )

    body = (
        head
        + (last_card if last_card else "")
        + "".join(actions)
        + evidence_card
        + _runner_card(ctx)
    )
    return render_shell(ctx, body)

# ---------------------------------------------------------------------------
# provider setup
# ---------------------------------------------------------------------------

_SETUP_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("account", "Account", "A label, the starting balance, your declared conversion source, and the entry cap."),
    ("costs", "Costs", "Commission, slippage, and financing, from your broker's schedule."),
    ("rollover", "Rollover and swap", "The broker's daily cutoff, triple-swap weekday, and swap schedule."),
    ("risk", "Risk limits", "Every limit the pre-trade gate enforces. None of them has a default."),
    ("calendar", "Trading calendar", "The session windows the market is actually open, with validity bounds."),
    ("execution", "Execution", "Lot size, position count, and whether shorts are allowed."),
)


def _setup_field(ctx: PageContext, item: Mapping[str, Any], facts: Mapping[str, Any]) -> str:
    path = str(item.get("path", ""))
    label = str(item.get("label", ""))
    kind = str(item.get("kind", ""))
    name = escape(path)
    anchor = "f-" + "".join(ch for ch in path if ch.isalnum() or ch == "-")
    value = ""
    if kind == "TERMINAL":
        value = {
            "account.currency": str(facts.get("currency", "")),
            "account.leverage": str(facts.get("leverage", "")),
        }.get(path, "")
    badge = (
        chip("from terminal", "live")
        if kind == "TERMINAL"
        else chip("you supply", "warn")
    )
    placeholder = escape(str(item.get("placeholder", "")))
    if kind == "TERMINAL":
        # Read-only: the terminal's own value, and the operator cannot type over it.
        control = (
            f"<input type='text' id='{anchor}' value='{escape(value)}' readonly "
            "aria-readonly='true'>"
        )
    else:
        control = (
            f"<input type='text' id='{anchor}' name='{name}' value='{escape(value)}' "
            f"placeholder='{placeholder}' autocomplete='off' spellcheck='false'>"
        )
    optional = " (optional)" if item.get("optional") else ""
    return (
        "<div class='data-field'>"
        f"<label for='{anchor}'>{escape(label)}{escape(optional)}</label>"
        f"{control}"
        f"<span class='help'>{escape(str(item.get('help', '')))}</span>"
        f"<span>{badge}</span>"
        "</div>"
    )


def _setup_textareas(ctx: PageContext, formats: Mapping[str, str]) -> str:
    areas = []
    for name, title, hint in (
        ("symbols", "Symbols", "One line per pair. Blank broker symbol and pip size are filled from your terminal."),
        ("swap", "Swap", "Long and short swap per lot per night, as your broker quotes them."),
        ("calendar.windows", "Trading windows", "One line per session window, inside the validity bounds above."),
        ("calendar.holidays", "Holidays", "Optional: one line per closed day."),
        ("conversion_rates", "Conversion rates", "Required when the account currency is not the pair's quote currency."),
    ):
        anchor = "t-" + "".join(ch for ch in name if ch.isalnum() or ch == "-")
        areas.append(
            "<div class='data-field' style='grid-column:1/-1'>"
            f"<label for='{anchor}'>{escape(title)}</label>"
            f"<textarea id='{anchor}' name='{escape(name)}' rows='3' spellcheck='false'></textarea>"
            f"<span class='help'>{escape(hint)}</span>"
            f"<span class='small muted'>Format: {escape(str(formats.get(name, '')))}</span>"
            "</div>"
        )
    return "".join(areas)


def render_provider_setup(ctx: PageContext) -> str:
    view = dict((ctx.extra or {}).get("provider_setup") or {})
    facts = dict(view.get("facts") or {})
    status = dict(view.get("file") or {})
    last = dict(view.get("last") or {})
    fields = list(view.get("fields") or [])
    formats = dict(view.get("list_formats") or {})
    connected = bool(facts.get("connected"))

    head = notice(
        "ok" if connected else "warn",
        "Writes one local file, and nothing else",
        "This page builds provider_setup.local.json from terms you type plus the "
        "account facts your terminal reports. It never reads or changes "
        "mt5_settings.local.json, never stores a password or token, and never "
        "sends an order.",
    )

    if connected:
        fact_card = card(
            "Account facts from your terminal",
            dl(
                [
                    ("Currency", escape(str(facts.get("currency", ""))) or "—"),
                    ("Leverage", escape(str(facts.get("leverage", ""))) or "—"),
                    ("Server", escape(str(facts.get("server", ""))) or "—"),
                    ("Margin mode", escape(str(facts.get("margin_mode", ""))) or "—"),
                    ("Symbols in Market Watch", escape(str(len(facts.get("symbols") or [])))),
                ]
            ),
            hint="These are read from the terminal; they are the only fields AXIOM fills in for you.",
            right=chip("terminal connected", "live", dot=True),
        )
    else:
        fact_card = card(
            "Account facts from your terminal",
            empty_state(
                "Not connected",
                "Connect on the Setup page first. AXIOM will not guess your account "
                "currency, your leverage, or a symbol's tick size. You can still read "
                "the term list below.",
            ),
            right=chip("not connected", "muted", dot=True),
        )

    if status.get("exists"):
        if status.get("valid"):
            summary = dict(status.get("summary") or {})
            file_body = dl(
                [
                    ("File", f"<span class='mono'>{escape(str(status.get('path', '')))}</span>"),
                    ("Pairs", escape(", ".join(summary.get("pairs") or []) or "—")),
                    ("Account currency", escape(str(summary.get("account_currency", ""))) or "—"),
                    ("Leverage", escape(str(summary.get("leverage", ""))) or "—"),
                    ("Cost model", escape(str(summary.get("cost_model", ""))) or "—"),
                    ("Calendar", escape(str(summary.get("calendar_id", ""))) or "—"),
                    ("Quantity", escape(str(summary.get("quantity", ""))) or "—"),
                ]
            ) + "<p class='small muted'>It loads cleanly with the runner's own loader. It is Git-ignored.</p>"
            right = chip("valid", "ok", dot=True)
        else:
            problems = [dict(item) for item in status.get("problems") or []]
            file_body = dl(
                [("File", f"<span class='mono'>{escape(str(status.get('path', '')))}</span>")]
            ) + _blocker_list(problems)
            right = chip("needs attention", "warn", dot=True)
    else:
        file_body = empty_state(
            "No setup file yet",
            f"{view.get('setup_filename', 'provider_setup.local.json')} does not exist in the "
            "project root. Fill in the terms below and save it.",
        )
        right = chip("not created", "muted", dot=True)

    file_card = card(
        "Current setup file",
        file_body,
        hint="Located in the project root, next to the launcher. Ignored by Git.",
        right=right,
    )

    sections = []
    for key, title, hint in _SETUP_GROUPS:
        group_fields = [item for item in fields if str(item.get("path", "")).startswith(key + ".")]
        if not group_fields:
            continue
        sections.append(
            card(
                title,
                f"<div class='data-grid'>{''.join(_setup_field(ctx, item, facts) for item in group_fields)}</div>",
                hint=hint,
            )
        )

    form = (
        "<form method='post' action='/actions/provider-setup-save' class='stack' style='gap:14px'>"
        f"<input type='hidden' name='nonce' value='{escape(ctx.action_nonce)}'>"
        + "".join(sections)
        + card(
            "Symbols, swap, sessions, and rates",
            f"<div class='data-grid'>{_setup_textareas(ctx, formats)}</div>",
            hint=(
                "Blank broker symbol or pip size is filled from the terminal, because the "
                "terminal is the authority for those two. Everything else you type is "
                "stored exactly as typed."
            ),
        )
        + card(
            "Save",
            "<label class='small' style='display:flex;gap:8px;align-items:center'>"
            "<input type='checkbox' name='overwrite' value='yes'>"
            "Replace an existing provider_setup.local.json</label>"
            "<div class='step-actions'><button class='btn btn--primary' type='submit'>"
            "Validate and save the setup file</button>"
            "<span class='small muted'>Validation uses the same loader the runner uses.</span></div>",
            hint="Nothing is written until every required term validates.",
        )
        + "</form>"
    )

    last_card = ""
    if last:
        if last.get("ok"):
            last_card = notice(
                "ok",
                "Setup file saved",
                "It is local, Git-ignored, and loads with the runner's own loader.",
            )
        else:
            problems = [dict(item) for item in last.get("problems") or []]
            last_card = card(
                "Nothing was written",
                _blocker_list(
                    [
                        {
                            "reason": str(item.get("path", "")),
                            "message": str(item.get("message", "")),
                            "hint": "Fix this field and save again."
                            if item.get("kind") == "missing"
                            else "",
                        }
                        for item in problems
                    ]
                )
                or "<p class='small'>No detail was recorded.</p>",
                hint="The existing file, if any, was left exactly as it was.",
            )

    blockers = list(view.get("blockers") or [])
    blockers_card = card(
        "What is still missing before the runner can trade paper",
        _blocker_list(blockers)
        or "<p class='small muted'>Nothing: the setup file exists, a dataset is imported, "
        "and a candidate has been selected. Start the runner on Positions.</p>",
        hint="The same list appears on Positions, so the two pages cannot disagree.",
    )

    example = card(
        "Example document",
        f"<p class='small'><span class='mono'>{escape(str(view.get('example_path', '')))}</span> "
        "shows every field of the real <span class='mono'>axiom-provider-setup-v1</span> "
        "schema. It is an example only: all of its values are placeholders and it will "
        "not run until you replace them.</p>"
        "<p class='small muted'>It is not "
        "<span class='mono'>profiles/provider_onboarding_template.json</span>, which is a "
        "different schema for a different purpose.</p>",
        hint="Read it for the shape; do not copy it into place.",
    )

    body = head + last_card + fact_card + file_card + form + blockers_card + example
    return render_shell(ctx, body)

# --------------------------------------------------------------------------
# live execution
# --------------------------------------------------------------------------


def _live_view(ctx: PageContext) -> Mapping[str, Any]:
    return dict((ctx.extra or {}).get("live") or {})


def live_status_banner(ctx: PageContext) -> str:
    """The one line that must never be ambiguous: will the next signal send an order?"""

    view = _live_view(ctx)
    sentence = str(
        ((ctx.extra or {}).get("live_summary") or {}).get("sentence", "")
    )
    sending = bool(view.get("next_signal_sends_real_order"))
    armed = bool(view.get("armed"))
    if sending:
        tone, title, klass = "danger", "The next strategy signal WILL send a real order", "live-on"
    elif armed:
        tone, title, klass = "warn", "Armed, but no session is running", "live-armed"
    else:
        tone, title, klass = "ok", "No real order can be sent right now", "live-off"
    return (
        f"<div class='live-banner live-banner--{klass}' role='status'>"
        f"<div><strong>{escape(title)}</strong>"
        f"<p class='small muted' style='margin:4px 0 0'>{escape(sentence)}</p></div>"
        f"<div class='spacer'></div>{chip(tone, tone, dot=True)}</div>"
    )


def _live_blockers(view: Mapping[str, Any]) -> str:
    blockers = [dict(item) for item in view.get("blockers") or []]
    if not blockers:
        return notice("ok", "Every arming precondition holds", "Nothing stands between this plan and a real order.")
    body = "<ul class='list'>" + "".join(
        "<li><strong class='mono'>{}</strong> {}".format(
            escape(str(item.get("reason", ""))), escape(str(item.get("message", "")))
        )
        + (
            "<div class='small muted'>{}</div>".format(escape(str(item.get("hint", ""))))
            if item.get("hint")
            else ""
        )
        + "</li>"
        for item in blockers
    ) + "</ul>"
    return card(
        f"{len(blockers)} reason(s) no order can be sent",
        body,
        hint="Every one of these is checked again for each order, not just here.",
    )


def _live_plan_card(view: Mapping[str, Any]) -> str:
    plan = dict(view.get("plan") or {})
    if not plan.get("present"):
        return card(
            "No live plan on disk",
            empty_state(
                "Nothing is armed",
                "A live plan is written only when you arm one on this page. "
                "Until then no strategy, symbol, size, or limit is chosen for you.",
            ),
            hint="The file is live_plan.local.json in the project root, ignored by Git.",
        )
    validated = bool(plan.get("validated"))
    return card(
        "The plan on disk",
        dl(
            [
                ("Strategy", f"<span class='mono'>{escape(str(plan.get('candidate_id', '')))}</span>"),
                ("Evidence", chip("measured", "ok") if validated else chip("UNVALIDATED", "warn", dot=True)),
                ("Symbol", escape(str(plan.get("pair", ""))) + (
                    f" <span class='small muted'>({escape(str(plan.get('broker_symbol', '')))})</span>"
                    if plan.get("broker_symbol") else ""
                )),
                ("Order size", f"<span class='mono'>{escape(str(plan.get('quantity', '')))}</span> lots"),
                ("Max open positions", escape(str(plan.get("max_open_positions", "")))),
                (
                    "Daily loss limit",
                    f"<span class='mono'>{escape(str(plan.get('max_daily_loss_account', '')))}</span> "
                    f"{escape(str(plan.get('account_currency', '')))}",
                ),
                ("Written", escape(str(plan.get("created_at", "")))),
            ]
        )
        + f"<p class='small muted'>{escape(str(plan.get('evidence_note', '')))}</p>",
        hint="This file records intent only. Arming is a separate, in-process decision.",
    )


def _live_review_card(view: Mapping[str, Any]) -> str:
    review = dict(view.get("review") or {})
    if not review:
        return ""
    problems = [dict(item) for item in review.get("problems") or []]
    facts = review.get("symbol") or {}
    account = review.get("account") or {}
    rows = [
        ("Would trade", escape(str(review.get("values", {}).get("pair", "")) or "—")
         + (f" <span class='small muted'>({escape(str(facts.get('broker_symbol', '') ))})</span>"
            if facts.get("broker_symbol") else "")),
        ("Order size", escape(str(review.get("values", {}).get("quantity", "")) or "—") + " lots"),
        ("Broker volume rules",
         "min {} / step {} / max {}".format(
             escape(str(facts.get("broker_volume_min", facts.get("min_quantity", "—")))),
             escape(str(facts.get("broker_volume_step", facts.get("quantity_step", "—")))),
             escape(str(facts.get("broker_volume_max", facts.get("max_quantity", "—")))),
         )),
        ("Max open positions", escape(str(review.get("values", {}).get("max_open_positions", "")) or "—")),
        ("Daily loss limit", escape(str(review.get("values", {}).get("max_daily_loss_account", "")) or "—")
         + " " + escape(str((review.get("plan") or {}).get("account_currency", "")))),
        ("Account", escape(str(account.get("login_masked", "")) or "—")
         + " · " + escape(str(account.get("account_mode", "")) or "mode unknown")
         + " · " + escape(str(account.get("server", "")) or "server unknown")),
        ("Account signature", f"<span class='mono'>{escape(str(account.get('fingerprint', '')) or '—')}</span>"),
        ("Currency / leverage", escape(str(account.get("currency", "")) or "—")
         + " · 1:" + escape(str(account.get("leverage", "")) or "—")),
    ]
    body = dl(rows)
    if problems:
        body += notice(
            "warn",
            f"{len(problems)} field(s) still need a value",
            "; ".join(f"{item.get('path')}: {item.get('message')}" for item in problems[:4]),
        )
    what = review.get("what_will_happen") or []
    if what:
        body += "<ul class='list'>" + "".join(f"<li>{escape(str(line))}</li>" for line in what) + "</ul>"
    return card(
        "Final review — what arming would authorise",
        body,
        hint="Read this before arming. It is the same document the order path uses.",
        right=chip("review", "muted"),
    )


def render_live(ctx: PageContext) -> str:
    view = _live_view(ctx)
    configured = bool(view.get("configured"))
    armed = bool(view.get("armed"))
    running = bool(view.get("session_running"))
    nonce = ctx.action_nonce
    candidates = [dict(item) for item in view.get("candidates") or []]
    symbols = [str(item) for item in view.get("symbols") or []]

    body = [live_status_banner(ctx)]

    if not configured:
        body.append(
            notice(
                "warn",
                "No MetaTrader 5 trading adapter is wired in",
                "This workstation cannot send a real order at all. Start it against your "
                "terminal with the MT5 settings present to enable the live path.",
            )
        )
        return render_shell(ctx, "".join(body))

    # -- controls ---------------------------------------------------------
    controls = []
    if running:
        controls.append(
            action_form("live-stop", nonce, "STOP and disarm", tone="danger")
        )
    elif armed:
        controls.append(action_form("live-disarm", nonce, "Disarm", tone="danger"))
    if armed and not running:
        controls.append(action_form("live-start", nonce, "Start live session", tone="primary"))
    if controls:
        body.append(
            "<div class='row' style='margin:8px 0 12px'>" + "".join(controls) + "</div>"
        )

    body.append(_live_plan_card(view))
    body.append(_live_blockers(view))
    body.append(_live_review_card(view))

    # -- the plan form ----------------------------------------------------
    options = ["<option value=''>Choose a strategy…</option>"]
    for item in candidates:
        label = str(item.get("candidate_id", ""))
        state = str(item.get("evidence_state", "UNVALIDATED"))
        mark = " · measured" if state == "MEASURED" else " · UNVALIDATED"
        options.append(f"<option value='{escape(label)}'>{escape(label + mark)}</option>")
    symbol_options = ["<option value=''>Choose a symbol…</option>"] + [
        f"<option value='{escape(item)}'>{escape(item)}</option>" for item in symbols
    ]
    form = (
        f"<form method='post' action='/actions/live-arm' class='stack'>"
        f"<input type='hidden' name='nonce' value='{escape(nonce)}'>"
        "<label class='field'><span>Strategy</span>"
        f"<select name='candidate_id' required>{''.join(options)}</select>"
        "<span class='small muted'>A strategy without measured results is labelled "
        "UNVALIDATED. AXIOM will not pick one for you.</span></label>"
        "<label class='field'><span>Symbol</span>"
        f"<select name='pair' required>{''.join(symbol_options)}</select>"
        "<span class='small muted'>Only pairs declared in your provider setup are listed.</span></label>"
        "<label class='field'><span>Order size (lots)</span>"
        "<input type='text' name='quantity' inputmode='decimal' autocomplete='off' "
        "placeholder='e.g. 0.10' required>"
        "<span class='small muted'>Must sit on the broker's own size grid for this symbol.</span></label>"
        "<label class='field'><span>Maximum open positions</span>"
        "<input type='text' name='max_open_positions' inputmode='numeric' autocomplete='off' "
        "placeholder='e.g. 1' required>"
        "<span class='small muted'>May tighten your provider setup, never widen it.</span></label>"
        "<label class='field'><span>Daily loss limit (account currency)</span>"
        "<input type='text' name='max_daily_loss_account' inputmode='decimal' autocomplete='off' "
        "placeholder='e.g. 100' required>"
        "<span class='small muted'>Enforced by AXIOM before each order; it is not a broker "
        "stop-loss.</span></label>"
        "<label class='field field--check'><input type='checkbox' name='reviewed' value='yes'>"
        "<span>I read the review above and I am choosing these terms myself.</span></label>"
        "<label class='field'><span>Type <span class='mono'>"
        f"{escape(str(view.get('confirmation_phrase', 'ENABLE LIVE TRADING')))}</span> to arm</span>"
        "<input type='text' name='confirmation' autocomplete='off' spellcheck='false' required></label>"
        "<div class='row'><button class='btn btn--danger' type='submit'>Arm live orders</button></div>"
        "</form>"
    )
    body.append(
        card(
            "Choose the terms, then arm",
            form,
            hint="Arming writes the plan and authorises the adapter. It sends nothing and starts nothing.",
            right=chip("armed" if armed else "not armed", "danger" if armed else "muted", dot=True),
        )
    )

    # -- account and broker state ----------------------------------------
    account = dict(view.get("account") or {})
    positions = [dict(item) for item in view.get("positions") or []]
    account_card = card(
        "The account this would trade",
        dl(
            [
                ("Connected", chip("yes", "ok") if view.get("connected") else chip("no", "warn", dot=True)),
                ("Mode", escape(str(view.get("mode", "")))),
                ("Armed", chip("yes", "danger") if armed else chip("no", "muted")),
                ("Account mode", escape(str(view.get("account_mode", "")) or "unknown")),
                ("Login", escape(str(account.get("login_masked", "")) or "—")),
                ("Server", escape(str(account.get("server", "")) or "—")),
                ("Signature", f"<span class='mono'>{escape(str(account.get('fingerprint', '')) or '—')}</span>"),
                ("Trading allowed in terminal", chip("yes", "ok") if account.get("trading_allowed") else chip("no", "warn")),
                ("Orders sent this session", escape(str(view.get("orders_sent", 0)))),
                ("Broker positions", escape(str(len(positions)))),
                ("Unresolved orders", escape(str(len(view.get("unresolved") or [])))),
            ]
        ),
        hint="Identifiers are masked. No balance, equity, password, or token is shown or stored.",
        right=chip("live account" if view.get("live_effective") else "paper", "danger" if view.get("live_effective") else "paper"),
    )
    body.append(account_card)

    if positions:
        body.append(
            card(
                "Broker positions",
                table(
                    "Open positions at the terminal",
                    [("Symbol", "mono"), ("Side", ""), ("Volume", "num"), ("Open price", "num")],
                    [
                        [
                            escape(str(item.get("symbol", ""))),
                            escape(str(item.get("side", ""))),
                            escape(str(item.get("volume", ""))),
                            escape(str(item.get("price_open", ""))),
                        ]
                        for item in positions
                    ],
                ),
                hint="Read from the terminal. The broker is authoritative; AXIOM books its fills.",
            )
        )

    last = dict(view.get("last_outcome") or {})
    refusal = dict(view.get("last_refusal") or {})
    if last or refusal:
        rows = []
        if last:
            rows.append(
                [
                    escape(str(last.get("at", ""))),
                    escape(str(last.get("reason", ""))),
                    escape(str(last.get("state", ""))),
                    escape(str(last.get("retcode_name", ""))),
                    escape(str(last.get("volume_filled", ""))),
                    chip("booked", "ok") if last.get("booked") else chip("not booked", "warn"),
                ]
            )
        body.append(
            card(
                "The last order path",
                table(
                    "Most recent live outcome",
                    [("At", ""), ("Reason", "mono"), ("State", ""), ("Retcode", ""), ("Filled", "num"), ("Ledger", "")],
                    rows,
                )
                if rows
                else empty_state("No order attempt yet", "Nothing has been sent in this session."),
                hint="An unknown outcome stops the session and is never re-sent.",
            )
        )

    return render_shell(ctx, "".join(body))



# --------------------------------------------------------------------------
# Kronos: the optional forecasting component
# --------------------------------------------------------------------------


def _kronos_view(ctx: PageContext) -> Mapping[str, Any]:
    return dict((ctx.extra or {}).get("kronos") or {})


def kronos_status_chip(view: Mapping[str, Any]) -> str:
    """One chip, and it never says more than the runtime actually reported."""

    state = dict(view.get("state") or {})
    name = str(state.get("state", "NOT_LOADED"))
    if name == "READY":
        return chip("model loaded · forecast only", "ok", dot=True)
    if name == "LOADING":
        return chip("loading", "warn", dot=True)
    if name == "FAILED":
        return chip(f"failed · {state.get('reason', 'unknown')}", "danger", dot=True)
    if name == "STOPPED":
        return chip("stopped", "muted", dot=True)
    return chip("not loaded", "muted", dot=True)


def _kronos_chart(chart: Mapping[str, Any]) -> str:
    """An inline SVG: recent mid closes, then the forecast with its band.

    No script, no library, no external asset.  Prices are floats only for
    drawing; the numbers themselves are printed as text elsewhere.
    """

    history = [dict(item) for item in chart.get("history") or []]
    forecast = dict(chart.get("forecast") or {})
    if not history:
        return ""
    closes = []
    for item in history:
        try:
            closes.append(float(item.get("close")))
        except (TypeError, ValueError):
            continue
    if not closes:
        return ""

    p10 = [float(value) for value in forecast.get("p10") or []]
    p50 = [float(value) for value in forecast.get("p50") or []]
    p90 = [float(value) for value in forecast.get("p90") or []]
    width, height, pad = 760, 220, 34
    span = max(1, len(closes) + len(p50) - 1)
    values = list(closes) + p10 + p50 + p90
    low, high = min(values), max(values)
    if high <= low:
        high = low + 1e-9
    step_x = (width - pad * 2) / max(1, span)
    scale_y = (height - pad * 2) / (high - low)

    def x(index: int) -> float:
        return pad + index * step_x

    def y(value: float) -> float:
        return height - pad - (value - low) * scale_y

    path = " ".join(
        f"{'M' if index == 0 else 'L'}{x(index):.1f},{y(value):.1f}"
        for index, value in enumerate(closes)
    )
    parts = [
        f"<svg viewBox='0 0 {width} {height}' width='100%' height='{height}' role='img' "
        f"aria-label='Recent mid closes and the last forecast with its uncertainty band'>",
        "<rect x='0' y='0' width='100%' height='100%' fill='none'/>",
        f"<line x1='{pad}' y1='{height - pad}' x2='{width - pad}' y2='{height - pad}' "
        "stroke='#24313f' stroke-width='1'/>",
    ]
    if p10 and p90 and p50:
        upper = " ".join(
            f"{x(len(closes) + index):.1f},{y(value):.1f}" for index, value in enumerate(p90)
        )
        lower = " ".join(
            f"{x(len(closes) + index):.1f},{y(value):.1f}"
            for index, value in reversed(list(enumerate(p10)))
        )
        parts.append(
            f"<polygon points='{upper} {lower}' fill='rgba(94,200,255,0.16)' "
            "stroke='rgba(94,200,255,0.45)' stroke-width='1'/>"
        )
        median = " ".join(
            f"{'M' if index == 0 else 'L'}{x(len(closes) + index):.1f},{y(value):.1f}"
            for index, value in enumerate(p50)
        )
        parts.append(
            f"<path d='{median}' fill='none' stroke='#5ec8ff' stroke-width='2' "
            "stroke-dasharray='5 4'/>"
        )
        parts.append(
            f"<line x1='{x(len(closes) - 1):.1f}' y1='{pad}' x2='{x(len(closes) - 1):.1f}' "
            f"y2='{height - pad}' stroke='#3a4a5c' stroke-width='1' stroke-dasharray='3 4'/>"
        )
    parts.append(f"<path d='{path}' fill='none' stroke='#8fa1b2' stroke-width='2'/>")
    parts.append(
        f"<text x='{pad}' y='{height - 10}' fill='#8fa1b2' font-size='11'>history</text>"
        + (
            f"<text x='{width - pad - 52}' y='{height - 10}' fill='#5ec8ff' "
            "font-size='11'>forecast</text>"
            if p50
            else ""
        )
    )
    parts.append(
        f"<text x='{pad}' y='{pad - 12}' fill='#8fa1b2' font-size='11'>{high:.5f}</text>"
        f"<text x='{pad}' y='{height - pad + 16}' fill='#8fa1b2' font-size='11'>{low:.5f}</text>"
    )
    parts.append("</svg>")
    return "".join(parts)


def _kronos_control(control: Mapping[str, Any]) -> str:
    name = escape(str(control.get("name", "")))
    label = escape(str(control.get("label", "")))
    value = escape(str(control.get("value", "")))
    help_text = escape(str(control.get("help", "")))
    kind = str(control.get("kind", "text"))
    name_attr = f"name='{name}'"
    if kind == "select":
        options = "".join(
            f"<option value='{escape(str(option))}'"
            f"{' selected' if str(option) == str(control.get('value', '')) else ''}>"
            f"{escape(str(option))}</option>"
            for option in control.get("options") or []
        )
        field_html = f"<select {name_attr}>{options}</select>"
    elif kind == "number":
        extra = ""
        if str(control.get("min", "")):
            extra += f" min='{escape(str(control['min']))}'"
        if str(control.get("max", "")):
            extra += f" max='{escape(str(control['max']))}'"
        field_html = (
            f"<input type='number' step='any' {name_attr} value='{value}'{extra}>"
        )
    else:
        field_html = (
            f"<input type='text' {name_attr} value='{value}' autocomplete='off' "
            "spellcheck='false'>"
        )
    help_html = f"<span class='help'>{help_text}</span>" if help_text else ""
    return (
        f"<label class='field'><span>{label}</span>{field_html}{help_html}</label>"
    )


def _kronos_controls_form(view: Mapping[str, Any], ctx: PageContext) -> str:
    controls = [dict(item) for item in view.get("controls") or []]
    groups = []
    for group in ("Basic", "Advanced"):
        items = [control for control in controls if control.get("group") == group]
        if not items:
            continue
        groups.append(
            f"<h3 class='small muted'>{escape(group)}</h3>"
            + "".join(_kronos_control(control) for control in items)
        )
    if not groups:
        return empty_state(
            "No settings exposed", "This build exposes no Kronos setting."
        )
    return (
        f"<form method='post' action='/actions/kronos-save' class='stack'>"
        f"<input type='hidden' name='nonce' value='{escape(ctx.action_nonce)}'>"
        + "".join(groups)
        + "<div class='row'><button class='btn btn--primary' type='submit'>"
        "Save these settings</button></div></form>"
    )


def render_kronos(ctx: PageContext) -> str:
    view = _kronos_view(ctx)
    nonce = ctx.action_nonce
    if not view.get("available"):
        return render_shell(
            ctx,
            notice(
                "warn",
                "The Kronos panel is not wired into this workstation",
                "This process has no forecasting component, so there is nothing to "
                "configure here. It is optional and never required for trading.",
            ),
        )

    state = dict(view.get("state") or {})
    settings = dict(view.get("settings") or {})
    availability = dict(view.get("availability") or {})
    estimate = dict(view.get("estimate") or {})
    history = dict(view.get("history") or {})
    summary = dict(history.get("summary") or {})
    last = dict(view.get("last") or {})
    benchmark = dict(view.get("benchmark") or {})
    body: list[str] = []

    result = dict(view.get("result") or {})
    if result.get("message"):
        body.append(
            notice(
                "ok" if result.get("ok") else "warn",
                "The last Kronos action",
                str(result.get("message")),
            )
        )

    body.append(
        notice(
            "ok",
            "Forecast only — this page cannot place an order",
            "Kronos estimates the next bars. Nothing here sizes a position, sets a "
            "stop, changes an execution mode, or creates an order. Using a forecast "
            "in a strategy is a separate, explicit configuration through the "
            "existing risk gate.",
        )
    )

    # -- status ----------------------------------------------------------
    status_rows = [
        ("Component", chip("optional · replaceable", "muted")),
        ("Model state", kronos_status_chip(view)),
        ("Variant", escape(str(settings.get("model_key", "")))),
        ("Device", escape(str(state.get("device", "")) or "—")),
        ("Settings fingerprint", f"<span class='mono'>{escape(str(view.get('fingerprint', '')))}</span>"),
        ("Scheduled forecasting", chip("running", "ok", dot=True) if view.get("schedule_running") else chip("stopped", "muted")),
        ("Forecasts this session", escape(str(state.get("forecasts", 0)))),
        ("Failures", escape(str(state.get("failures", 0)))),
        ("Last forecast", escape(str(state.get("last_forecast_at", "")) or "—")),
        ("Last error", escape(str(state.get("last_error", "")) or "—")),
    ]
    if state.get("reason"):
        status_rows.append(("Failure reason", escape(str(state.get("detail", "")) or str(state.get("reason", "")))))
    body.append(
        card(
            "Status",
            dl(status_rows),
            hint="A state is only ever what the runtime reported. Nothing retries by itself.",
            right=kronos_status_chip(view),
        )
    )

    if not view.get("has_bar_source"):
        body.append(
            notice(
                "warn",
                "No bar source is wired in",
                "Forecasting needs broker bars. Start this workstation against your "
                "MetaTrader 5 terminal with a connected session, or import bars on the "
                "Data page. AXIOM will not invent prices to fill the gap, so until then "
                "this page can validate settings but cannot produce a forecast.",
            )
        )

    # -- input chart and last forecast ------------------------------------
    chart = dict(view.get("chart") or {})
    svg = _kronos_chart(chart) if chart else ""
    if svg:
        caption = (
            f"{escape(str(chart.get('symbol', '')))} · "
            f"{escape(str(chart.get('timeframe', '')))} · mid price (not tradable)"
        )
        band_note = (
            "The shaded band is the 10th–90th percentile of the sampled paths. A "
            "band that excludes the outcome is a failure of the model's uncertainty, "
            "and it is recorded as one."
            if chart.get("forecast")
            else "History only: no forecast has been run on this page yet."
        )
        body.append(card("Input and forecast", svg, hint=caption + ". " + band_note))

    if last:
        quality = dict(last.get("quality") or {})
        band = dict(last.get("band_at_horizon") or {})
        body.append(
            card(
                "The last forecast",
                dl(
                    [
                        ("Recorded", escape(str(last.get("created_at", "")))),
                        ("Symbol / timeframe", escape(f"{last.get('symbol', '')} {last.get('timeframe', '')}")),
                        ("Model", escape(str(last.get("model_key", ""))) + " · " + escape(str(last.get("model_revision", "")))),
                        ("Horizon", escape(str(last.get("horizon", ""))) + " bar(s)"),
                        ("Last observed close", escape(str(last.get("last_close", "")))),
                        ("Median at horizon", escape(str(band.get("p50", "")) or "—")),
                        ("Band at horizon", escape(f"{band.get('p10', '')} – {band.get('p90', '')}")),
                        ("Expected move", escape(str(last.get("expected_move", ""))) + f" ({escape(str(last.get('expected_move_basis_points', '')))} bp)"),
                        ("Data freshness", escape(str(quality.get("last_at", ""))) + " · " + chip(str(quality.get("volume_mode", "")), "muted")),
                        ("Input quality", escape(f"{quality.get('bars', '')} bars · {quality.get('gaps', 0)} gap(s) · {quality.get('crossed', 0)} crossed")),
                        ("Inference time", escape(str(last.get("runtime_seconds", ""))) + " s for " + escape(str(last.get("rollouts", ""))) + " rollout(s)"),
                        ("Label", chip(str(last.get("label", "")), "paper")),
                    ]
                ),
                hint="Stored before it was shown, with the settings fingerprint and input quality it was built from.",
                right=chip("forecast only", "paper"),
            )
        )

    # -- run controls ----------------------------------------------------
    ready = str(state.get("state", "")) == "READY"
    controls = []
    controls.append(
        action_form("kronos-load", nonce, "Load model", tone="primary")
    )
    controls.append(action_form("kronos-forecast", nonce, "Run one forecast"))
    if view.get("schedule_running"):
        controls.append(action_form("kronos-schedule-stop", nonce, "Stop scheduled forecasting", tone="danger"))
    else:
        controls.append(action_form("kronos-schedule-start", nonce, "Start scheduled forecasting"))
    controls.append(action_form("kronos-stop", nonce, "Stop and unload", tone="danger"))
    body.append(
        card(
            "Run",
            "<div class='row'>" + "".join(controls) + "</div>",
            hint="Load reads the saved settings and the model files you placed on this "
            "machine. It never downloads, installs, or starts anything at boot.",
        )
    )

    # -- settings --------------------------------------------------------
    problems = [dict(item) for item in view.get("problems") or []]
    settings_body = ""
    if problems:
        items = "".join(
            f"<li><strong class='mono'>{escape(str(item.get('path', '')))}</strong> "
            f"{escape(str(item.get('message', '')))}</li>"
            for item in problems
        )
        settings_body += (
            "<div class='notice notice--warn' role='status'><div>"
            "<strong>These settings cannot be loaded yet</strong>"
            f"<ul class='list'>{items}</ul></div></div>"
        )
    settings_body += _kronos_controls_form(view, ctx)
    body.append(
        card(
            "Inference settings",
            settings_body,
            hint="Every control maps to a real Kronos argument or to an AXIOM-side "
            "scheduling choice. Limits come from the selected variant's published "
            "context length.",
            right=chip("basic + advanced", "muted"),
        )
    )

    # presets
    presets = [str(item) for item in view.get("presets") or []]
    preset_options = "".join(
        f"<option value='{escape(item)}'>{escape(item)}</option>" for item in presets
    )
    body.append(
        card(
            "Named presets",
            f"<form method='post' action='/actions/kronos-preset-save' class='stack'>"
            f"<input type='hidden' name='nonce' value='{escape(nonce)}'>"
            "<label class='field'><span>Name</span>"
            "<input type='text' name='preset_name' autocomplete='off' maxlength='60' "
            "placeholder='e.g. eurusd 15m quiet'>"
            "<span class='help'>Saved beside the launcher as kronos_presets.local.json, "
            "which Git ignores.</span></label>"
            "<div class='row'><button class='btn' type='submit'>Save current settings</button>"
            "</div></form>"
            + (
                f"<form method='post' action='/actions/kronos-preset-load' class='stack' "
                "style='margin-top:12px'>"
                f"<input type='hidden' name='nonce' value='{escape(nonce)}'>"
                "<label class='field'><span>Saved presets</span>"
                f"<select name='preset_name'>{preset_options}</select></label>"
                "<div class='row'><button class='btn' type='submit'>Load into the form</button>"
                "</div></form>"
                if presets
                else "<p class='small muted' style='margin-top:12px'>No presets saved yet.</p>"
            ),
            hint="Loading a preset fills the form; it does not load the model.",
        )
    )

    # -- hardware and cost ----------------------------------------------
    packages = [dict(item) for item in availability.get("packages") or []]
    body.append(
        card(
            "Before you load: what it costs and what is installed",
            dl(
                [
                    ("Parameters", escape(str(estimate.get("parameters", "")))),
                    ("Weights (estimated)", escape(str(estimate.get("weights_megabytes", ""))) + " MB at fp32"),
                    ("Context / horizon", escape(f"{estimate.get('context_bars', '')} bars → {estimate.get('horizon_bars', '')} bars")),
                    ("Rollouts", escape(str(estimate.get("rollouts", ""))) + " sequential predict() calls"),
                    ("Python", escape(str(availability.get("python", "")))),
                    ("torch", chip("present", "ok") if availability.get("torch_present") else chip("missing", "danger")),
                    ("CUDA", "—" if availability.get("cuda_available") is None else chip("available", "ok") if availability.get("cuda_available") else chip("not available", "warn")),
                ]
            )
            + table(
                "Dependencies from the official requirements.txt",
                [("Requirement", ""), ("Import name", "mono"), ("State", "")],
                [
                    [
                        escape(str(item.get("requirement", ""))),
                        escape(str(item.get("import_name", ""))),
                        chip("present", "ok") if item.get("present") else chip(
                            "missing" if item.get("required") else "missing (optional)", "danger" if item.get("required") else "warn"
                        ),
                    ]
                    for item in packages
                ],
            ),
            hint="AXIOM never installs software. Missing packages are reported with the exact "
            "requirement so you can install them yourself.",
            right=chip("estimate, not a measurement", "muted"),
        )
    )
    if estimate.get("notes"):
        body.append(
            card(
                "What that estimate does not include",
                "<ul class='list'>"
                + "".join(f"<li>{escape(str(note))}</li>" for note in estimate.get("notes") or [])
                + "</ul>",
            )
        )

    # -- model files and licence ----------------------------------------
    source = dict(view.get("source") or {})
    weights = dict(view.get("weights") or {})
    weights_rows = [
        ("Repository", escape(str(source.get("repo", "")))),
        ("Commit read for these facts", escape(str(source.get("commit_url", "")))),
        ("Code licence", escape(str(source.get("code_licence", ""))) + " · " + escape(str(source.get("code_copyright", "")))),
        ("Paper", escape(str(source.get("paper_url", "")))),
        ("Weights host", escape(str(source.get("weights_host", "")))),
        ("Weights licence", chip(str(source.get("weights_licence", "")), "warn")),
        ("Model file location", escape(str(settings.get("model_path", "")) or "official Hub id")),
        ("Pinned revision", escape(str(settings.get("revision", "")) or "—")),
        ("Local digests", escape(str(weights.get("state", "not inspected")))),
    ]
    weights_body = dl(weights_rows)
    weights_body += (
        "<p class='small muted' style='margin-top:10px'>AXIOM does not download weights, "
        "packages, or code. Obtain the files from the official source yourself, place them "
        "in a folder, and point the setting above at it. The inspection below only hashes "
        "what is already there.</p>"
    )
    if weights.get("files"):
        weights_body += table(
            "Files hashed locally",
            [("File", "mono"), ("Bytes", "num"), ("SHA-256", "mono")],
            [
                [
                    escape(str(item.get("path", ""))),
                    escape(str(item.get("bytes", ""))),
                    escape(str(item.get("sha256", "")) [:24]) + "…",
                ]
                for item in weights.get("files") or []
            ],
        )
    weights_body += (
        f"<form method='post' action='/actions/kronos-inspect-weights' class='stack' "
        "style='margin-top:12px'>"
        f"<input type='hidden' name='nonce' value='{escape(nonce)}'>"
        f"<input type='hidden' name='model_path' value='{escape(str(settings.get('model_path', '')))}'>"
        "<div class='row'><button class='btn' type='submit'>Inspect and hash the model files</button>"
        "</div></form>"
    )
    body.append(
        card(
            "Model, licence, and where the files come from",
            weights_body,
            hint="Licence and version are shown before anything is used; the weights' "
            "licence is not something AXIOM can vouch for.",
            right=chip("no automatic download", "warn"),
        )
    )

    # -- history and outcomes -------------------------------------------
    recent = [dict(item) for item in history.get("recent") or []]
    history_rows = []
    for item in recent:
        outcome = dict(item.get("outcome") or {})
        history_rows.append(
            [
                escape(str(item.get("created_at", ""))),
                escape(f"{item.get('symbol', '')} {item.get('timeframe', '')}"),
                escape(str(item.get("model_key", ""))),
                escape(str((item.get("band_at_horizon") or {}).get("p50", ""))),
                (
                    chip("scored", "ok")
                    + " · "
                    + ("right" if outcome.get("direction_correct") else "wrong")
                    + f" · error {escape(str(outcome.get('absolute_error', '')))}"
                    if outcome
                    else chip("waiting for its horizon", "muted")
                ),
            ]
        )
    body.append(
        card(
            "Recorded forecasts and what happened",
            table(
                "Most recent forecasts from kronos_forecasts.sqlite3",
                [("Recorded", ""), ("Series", ""), ("Model", "mono"), ("Median", "num"), ("Outcome", "")],
                history_rows,
                empty=empty_state(
                    "No forecast recorded yet",
                    "Run one forecast, or start scheduled forecasting. Each result is "
                    "stored with its settings fingerprint before it is shown.",
                ),
            )
            + (
                f"<div class='row' style='margin-top:12px'>"
                f"{action_form('kronos-reconcile', nonce, 'Score forecasts whose horizon has passed')}"
                "</div>"
                if recent
                else ""
            )
            + (
                f"<p class='small muted' style='margin-top:10px'>{escape(str(summary.get('note', '')))}</p>"
                if summary
                else ""
            ),
            hint="Direction, absolute error and band coverage only. No trading costs are "
            "included, so these are not profit figures.",
        )
    )

    # -- benchmark -------------------------------------------------------
    baselines = [dict(item) for item in view.get("baselines") or []]
    benchmark_body = (
        table(
            "The configured comparison set",
            [("Baseline", "mono"), ("What it does", "")],
            [[escape(str(item.get("key", ""))), escape(str(item.get("note", "")))] for item in baselines],
        )
        + f"<form method='post' action='/actions/kronos-benchmark' class='stack' style='margin-top:12px'>"
        f"<input type='hidden' name='nonce' value='{escape(nonce)}'>"
        "<div class='data-grid'>"
        f"<label class='field'><span>Symbol</span><input type='text' name='symbol' value='{escape(str(settings.get('symbol', '')))}' autocomplete='off'></label>"
        f"<label class='field'><span>Timeframe</span><select name='timeframe'>"
        + "".join(
            f"<option value='{escape(str(tf))}'{' selected' if str(tf) == str(settings.get('timeframe')) else ''}>{escape(str(tf))}</option>"
            for tf in view.get("timeframes") or []
        )
        + "</select></label>"
        "<label class='field'><span>Include Kronos</span><select name='include_kronos'>"
        "<option value='yes'>yes, if it is loaded</option>"
        "<option value='no'>no, baselines only</option></select></label>"
        "</div>"
        "<div class='row'><button class='btn btn--primary' type='submit'>Run the benchmark</button></div>"
        "</form>"
    )
    if benchmark.get("scores"):
        benchmark_body += (
            "<h3 class='small muted' style='margin-top:18px'>The last run</h3>"
            + dl(
                [
                    ("Ran at", escape(str(benchmark.get("ran_at", "")))),
                    ("Series", escape(f"{benchmark.get('symbol', '')} {benchmark.get('timeframe', '')}")),
                    ("Steps scored", escape(str(benchmark.get("steps", "")))),
                ]
            )
            + table(
                "Forecast error and spread-adjusted direction. Not profit.",
                [("Model", "mono"), ("Observations", "num"), ("Direction", "num"), ("Mean abs error", "num"), ("Band coverage", "num"), ("Spread-adjusted move", "num")],
                [
                    [
                        escape(str(row.get("key", ""))),
                        escape(str(row.get("observations", ""))),
                        escape(str(row.get("direction_rate", "—"))),
                        escape(str(row.get("mean_absolute_error", "—"))),
                        escape(str(row.get("band_coverage", "—") or "—")),
                        escape(str(row.get("mean_spread_adjusted_move", "—") or "—")),
                    ]
                    for row in sorted(benchmark.get("scores") or [], key=lambda item: str(item.get("key", "")))
                ],
            )
            + "<ul class='list'>"
            + "".join(f"<li>{escape(str(note))}</li>" for note in benchmark.get("notes") or [])
            + "</ul>"
        )
    body.append(
        card(
            "Compare against simple baselines",
            benchmark_body,
            hint="Chronological out-of-sample window only, nothing fitted on it. A "
            "benchmark score is never evidence of profit.",
        )
    )

    # -- mapping ---------------------------------------------------------
    mapping = [dict(item) for item in view.get("mapping") or []]
    body.append(
        card(
            "What is sent to the model",
            table(
                "The input mapping, field by field",
                [("Kronos column", "mono"), ("AXIOM source", ""), ("Basis", "")],
                [
                    [escape(str(item.get("column", ""))), escape(str(item.get("source", ""))), chip(str(item.get("label", "")), "muted")]
                    for item in mapping
                ],
            ),
            hint="Mid prices are not tradable, and MT5 tick volume is a count of price "
            "updates, not exchange volume. Nothing absent is invented.",
        )
    )

    # -- unsupported -----------------------------------------------------
    unsupported = [dict(item) for item in view.get("unsupported") or []]
    body.append(
        card(
            "Settings Kronos does not support",
            "<ul class='list'>"
            + "".join(
                f"<li><strong class='mono'>{escape(str(item.get('setting', '')))}</strong> "
                f"{escape(str(item.get('why', '')))}</li>"
                for item in unsupported
            )
            + "</ul>",
            hint="Documented rather than invented. If you need one of these, it is not a "
            "setting to add here -- it is a different model.",
        )
    )

    # -- where a forecast may be used ------------------------------------
    body.append(
        card(
            "How a forecast could ever reach an order",
            "<p class='small muted'>A forecast is not a signal, and this page cannot "
            "configure one. Using it in trading requires a separately specified "
            "strategy, an explicit operator selection of that strategy, the existing "
            "risk gate, a broker-side protective stop for every position, and a "
            "PAPER/LIVE mode the operator chose. Until results exist on your broker's "
            "data, any strategy built on it is labelled UNVALIDATED.</p>"
            + "<ul class='list'>"
            + "".join(f"<li>{escape(str(note))}</li>" for note in view.get("notes") or [])
            + "</ul>",
        )
    )

    return render_shell(ctx, "".join(body))


# --------------------------------------------------------------------------
# Strategy library
# --------------------------------------------------------------------------


def _strategies_view(ctx: PageContext) -> Mapping[str, Any]:
    return dict((ctx.extra or {}).get("strategies") or {})


def _strategy_status_chip(status: str) -> str:
    name = str(status or "TEMPLATE")
    if name == "SELECTED_BY_OPERATOR":
        return chip("selected by operator", "warn", dot=True)
    if name == "ELIGIBLE_VARIANT":
        return chip("eligible variant recorded", "ok", dot=True)
    if name == "ATTEMPTED":
        return chip("attempted · not eligible", "muted", dot=True)
    if name == "UNAVAILABLE":
        return chip("not evaluable here", "muted")
    return chip("template · untested", "paper")


def _strategy_card(item: Mapping[str, Any], ctx: PageContext, variants: Sequence[Mapping[str, Any]]) -> str:
    card_id = str(item.get("card_id", ""))
    availability = dict(item.get("availability") or {})
    source = dict(item.get("source") or {})
    parameters = [dict(entry) for entry in item.get("parameters") or []]
    requires = [str(entry) for entry in item.get("requires") or []]

    body = [
        f"<p class='small muted'>{escape(str(item.get('summary', '')))}</p>",
        "<h3 class='small muted'>Rules</h3><ol class='list'>",
        "".join(f"<li>{escape(str(rule))}</li>" for rule in item.get("rules") or []),
        "</ol>",
        "<h3 class='small muted'>Parameters</h3>",
        table(
            "The card's declared parameters and the predeclared grid",
            [("Name", "mono"), ("Kind", ""), ("Default", "num"), ("Grid", "mono"), ("Notes", "")],
            [
                [
                    escape(str(entry.get("name", ""))),
                    escape(str(entry.get("kind", ""))),
                    escape(str(entry.get("default", ""))),
                    escape(", ".join(entry.get("grid") or []) or "—"),
                    escape(str(entry.get("description", ""))),
                ]
                for entry in parameters
            ],
        ),
        "<h3 class='small muted'>Where it applies</h3>",
        dl(
            [
                ("Declared timeframes", escape(", ".join(item.get("timeframes") or []))),
                ("Intended horizon", escape(f"{item.get('horizon_bars', '')} bar(s)")),
                ("Minimum bars", escape(str(item.get("min_bars", "")))),
                ("Data required", escape(", ".join(requires))),
                ("Licence", chip(str(item.get("licence", "")), "muted")),
                ("Source", escape(str(source.get("citation", "")))),
                ("Source kind", escape(str(source.get("kind", "")))),
            ]
        ),
        (
            f"<p class='small muted' style='margin-top:8px'>{escape(str(source.get('note', '')))}</p>"
            if source.get("note")
            else ""
        ),
        "<h3 class='small muted'>Assumptions</h3><ul class='list'>",
        "".join(f"<li>{escape(str(note))}</li>" for note in item.get("assumptions") or []),
        "</ul><h3 class='small muted'>Limitations</h3><ul class='list'>",
        "".join(f"<li>{escape(str(note))}</li>" for note in item.get("limitations") or []),
        "</ul>",
    ]
    if not availability.get("evaluable"):
        body.append(
            notice(
                "warn",
                "Not evaluable in this deployment",
                str(availability.get("reason", "")),
            )
        )
    if variants:
        rows = []
        for variant in variants:
            attempt = dict(variant.get("attempt") or {})
            status = str(variant.get("status", "NOT_ATTEMPTED"))
            rows.append(
                [
                    f"<span class='mono'>{escape(str(variant.get('variant_id', '')))}</span>",
                    escape(", ".join(f"{k}={v}" for k, v in (variant.get("parameters") or {}).items())),
                    _strategy_status_chip(status),
                    escape(str(attempt.get("reason", "")) or "—"),
                    escape(str(attempt.get("q_value", "")) or "—"),
                    escape(str(attempt.get("reality_check", "")) or "—"),
                ]
            )
        body.append(
            "<details style='margin-top:12px'><summary class='small muted'>"
            f"{len(variants)} predeclared variant(s) — every attempt is recorded"
            "</summary>"
            + table(
                "Variants of this template, and what the ledger says about each",
                [("Variant", "mono"), ("Parameters", ""), ("Status", ""), ("Reason", ""), ("q", "num"), ("RC p", "num")],
                rows,
            )
            + (
                f"<form method='post' action='/actions/strategy-select' class='stack' "
                "style='margin-top:12px'>"
                f"<input type='hidden' name='nonce' value='{escape(ctx.action_nonce)}'>"
                "<label class='field'><span>Variant to select (only an eligible attempt can be chosen)</span>"
                "<select name='variant_id'>"
                + "".join(
                    f"<option value='{escape(str(entry.get('variant_id', '')))}'>"
                    f"{escape(str(entry.get('variant_id', '')))}</option>"
                    for entry in variants
                    if str(entry.get("status")) == "ELIGIBLE"
                )
                + "</select></label>"
                "<label class='field field--check'><input type='checkbox' name='confirmed' value='yes'>"
                "<span>This is my own choice, and I understand it is a historical "
                "result, not a promise.</span></label>"
                "<div class='row'><button class='btn' type='submit'>Record my selection</button></div>"
                "</form>"
                if any(str(entry.get("status")) == "ELIGIBLE" for entry in variants)
                else "<p class='small muted' style='margin-top:10px'>No variant of this "
                "template has an eligible attempt on record, so nothing here can be "
                "selected.</p>"
            )
            + "</details>"
        )
    return card(
        str(item.get("name", "")),
        "".join(body),
        hint=f"{item.get('family_label', '')} · {item.get('variants', 0)} variant(s) · "
        f"{item.get('attempted', 0)} attempted",
        right=_strategy_status_chip(str(item.get("status", ""))),
    )


def render_strategies(ctx: PageContext) -> str:
    view = _strategies_view(ctx)
    nonce = ctx.action_nonce
    if not view.get("available"):
        return render_shell(
            ctx,
            notice(
                "warn",
                "The strategy library is not wired into this workstation",
                "This process has no library page data. Nothing is lost: the library "
                "is a research index and is not required for trading.",
            ),
        )
    result = dict(view.get("result") or {})
    catalogue = [dict(item) for item in view.get("catalogue") or []]
    counts = dict(view.get("counts") or {})
    library = dict(view.get("library") or {})
    query = dict(view.get("query") or {})
    criteria = dict(view.get("criteria") or {})
    coverage_rows = [dict(item) for item in view.get("coverage") or []]
    selections = [dict(item) for item in view.get("selections") or []]
    body: list[str] = []

    if result.get("message"):
        body.append(
            notice(
                "ok" if result.get("ok") else "warn",
                "The last library action",
                str(result.get("message")),
            )
        )

    body.append(
        notice(
            "ok",
            "A research index, not an order screen",
            "These are documented templates and their parameter variants. Nothing here "
            "selects a strategy for you, sizes a position, sets a stop, arms live "
            "execution, or changes an execution mode. A selected strategy still goes "
            "through the live plan's review, the confirmation phrase, the risk gate, "
            "and the protective-stop rule.",
        )
    )

    # -- honest counts ---------------------------------------------------
    body.append(
        card(
            "What is in the library",
            "<div class='data-grid'>"
            + kpi("Templates", escape(str(library.get("templates", 0))), foot="documented shapes")
            + kpi("Families", escape(str(library.get("families", 0))), foot="trend, breakout, mean reversion, carry, volatility, session")
            + kpi("Evaluable here", escape(str(library.get("evaluable_templates", 0))), foot="the rest name the missing data")
            + kpi("Variants", escape(str(library.get("variants", 0))), foot="predeclared parameter combinations")
            + kpi("Attempts recorded", escape(str(counts.get("attempts", 0))), foot="including every failure")
            + kpi("Selected", escape(str(counts.get("selected", 0))), foot="operator choices")
            + "</div>",
            hint=str(library.get("note", "")),
            right=chip(f"grid {view.get('grid_version', '')}", "muted"),
        )
    )

    # -- search ----------------------------------------------------------
    family_options = "".join(
        f"<option value='{escape(str(item.get('key', '')))}'"
        f"{' selected' if str(item.get('key')) == query.get('family') else ''}>"
        f"{escape(str(item.get('label', '')))}</option>"
        for item in view.get("families") or []
    )
    body.append(
        card(
            "Search",
            f"<form method='get' action='/strategies' class='stack'>"
            "<div class='data-grid'>"
            f"<label class='field'><span>Words</span><input type='text' name='q' "
            f"value='{escape(str(query.get('text', '')))}' autocomplete='off' "
            "placeholder='e.g. breakout, momentum, Bollinger'></label>"
            "<label class='field'><span>Family</span><select name='family'>"
            f"<option value=''>All families</option>{family_options}</select></label>"
            "</div>"
            "<div class='row'><button class='btn' type='submit'>Filter</button>"
            "<a class='btn btn--ghost' href='/strategies'>Clear</a></div></form>",
            hint="Search runs over names, summaries, rules, timeframes and sources. It "
            "does not rank by anything resembling performance.",
        )
    )

    # -- selections ------------------------------------------------------
    if selections:
        body.append(
            card(
                "Your selections",
                table(
                    "Variants you selected, and the attempt each came from",
                    [("Variant", "mono"), ("Symbol", ""), ("Timeframe", ""), ("Dataset", "mono"), ("Selected", "")],
                    [
                        [
                            escape(str(item.get("variant_id", ""))),
                            escape(str(item.get("symbol", ""))),
                            escape(str(item.get("timeframe", ""))),
                            escape(str(item.get("dataset_id", ""))),
                            escape(str(item.get("selected_at", ""))),
                        ]
                        for item in selections
                    ],
                )
                + "".join(
                    f"<form method='post' action='/actions/strategy-deselect' style='display:inline;margin-right:6px'>"
                    f"<input type='hidden' name='nonce' value='{escape(nonce)}'>"
                    f"<input type='hidden' name='variant_id' value='{escape(str(item.get('variant_id', '')))}'>"
                    "<button class='btn btn-sm' type='submit'>Deselect "
                    f"{escape(str(item.get('variant_id', '')))}</button></form>"
                    for item in selections
                ),
                hint="Selecting recorded your choice; it did not trade. Deselecting is "
                "always allowed and takes effect immediately.",
            )
        )

    # -- the catalogue ---------------------------------------------------
    for item in catalogue:
        variants = [dict(entry) for entry in item.get("variants_detail") or []]
        body.append(_strategy_card(item, ctx, variants))

    # -- screening -------------------------------------------------------
    card_options = "".join(
        f"<option value='{escape(str(item.get('card_id', '')))}'>"
        f"{escape(str(item.get('name', '')))}</option>"
        for item in catalogue
        if (item.get("availability") or {}).get("evaluable")
    )
    screening = (
        "<p class='small muted'>Screening applies the predeclared criteria and the "
        "multiple-testing control to evaluations recorded on the Data &amp; evaluation "
        "page, then writes every attempt to the ledger — including the ones that fail."
        "</p>"
        f"<form method='post' action='/actions/strategy-screen' class='stack'>"
        f"<input type='hidden' name='nonce' value='{escape(nonce)}'>"
        "<div class='data-grid'>"
        f"<label class='field'><span>Template</span><select name='card_id'>"
        f"<option value=''>All evaluable templates</option>{card_options}</select></label>"
        "<label class='field'><span>Symbol</span><input type='text' name='symbol' "
        "value='' placeholder='e.g. EURUSD' autocomplete='off'></label>"
        "<label class='field'><span>Timeframe</span><input type='text' name='timeframe' "
        "value='' placeholder='blank = every declared timeframe' autocomplete='off'></label>"
        "</div>"
        "<div class='row'><button class='btn btn--primary' type='submit'>Screen recorded evaluations</button></div>"
        "</form>"
    )
    if not view.get("has_evidence_source"):
        screening += notice(
            "warn",
            "No evaluation source is wired in",
            "This deployment cannot read recorded evaluations, so screening would have "
            "nothing to read. Evaluate a candidate on Data & evaluation first.",
        )
    body.append(
        card(
            "Screen and record attempts",
            screening,
            hint=f"Criteria version {criteria.get('criteria_version', '')}. The thresholds "
            "are AXIOM's declared choices, fixed before any result is read.",
            right=chip("predeclared", "muted"),
        )
    )

    # -- evaluate one variant -------------------------------------------
    evaluable_cards = [item for item in catalogue if (item.get("availability") or {}).get("evaluable")]
    if evaluable_cards:
        body.append(
            card(
                "Evaluate one variant",
                "<p class='small muted'>This runs the same chronological evaluator, "
                "costs and locked-test custody as the paper lane, then records the "
                "attempt. Both the dataset id and the data class must be yours: the "
                "class decides whether a result may ever promote anything.</p>"
                f"<form method='post' action='/actions/strategy-evaluate' class='stack'>"
                f"<input type='hidden' name='nonce' value='{escape(nonce)}'>"
                "<div class='data-grid'>"
                "<label class='field'><span>Variant</span><input type='text' "
                "name='variant_id' autocomplete='off' placeholder='trend_sma_cross::fast_period=8-slow_period=21'></label>"
                "<label class='field'><span>Symbol</span><input type='text' name='symbol' autocomplete='off' placeholder='EURUSD'></label>"
                "<label class='field'><span>Timeframe</span><input type='text' name='timeframe' autocomplete='off' placeholder='5m'></label>"
                "<label class='field'><span>Dataset id</span><input type='text' name='dataset_id' autocomplete='off' placeholder='e.g. eurusd-2026-q1'></label>"
                "<label class='field'><span>Data class</span><select name='data_class'>"
                "<option value=''>Choose…</option>"
                "<option value='USER_SUPPLIED_HISTORICAL'>USER_SUPPLIED_HISTORICAL</option>"
                "<option value='USER_SUPPLIED_PRODUCTION'>USER_SUPPLIED_PRODUCTION</option>"
                "<option value='USER_SUPPLIED_DEMO'>USER_SUPPLIED_DEMO</option>"
                "<option value='SYNTHETIC_FIXTURE'>SYNTHETIC_FIXTURE</option>"
                "</select></label>"
                "</div>"
                "<div class='row'><button class='btn btn--primary' type='submit'>Evaluate and record</button></div>"
                "</form>",
                hint="A demo or fixture class can be studied, but it can never make a "
                "variant eligible.",
            )
        )

    # -- criteria --------------------------------------------------------
    body.append(
        card(
            "The predeclared criteria",
            dl(
                [
                    ("Closed trades (locked test)", escape(str(criteria.get("min_closed_trades", "")))),
                    ("Max drawdown", escape(str(criteria.get("max_drawdown_fraction", "")))),
                    ("Walk-forward folds", escape(str(criteria.get("min_walk_forward_folds", "")))),
                    ("Positive fold fraction", escape(str(criteria.get("min_positive_fold_fraction", "")))),
                    ("Validation positive", "yes" if criteria.get("require_positive_validation") else "no"),
                    ("Locked test positive", "yes" if criteria.get("require_positive_locked_test") else "no"),
                    ("Data class must be able to promote", "yes" if criteria.get("require_promoting_data_class") else "no"),
                    ("FDR q-value limit", escape(str(criteria.get("max_q_value", "")))),
                    ("Reality Check p limit", escape(str(criteria.get("max_reality_check", "")))),
                ]
            ),
            hint="Changing a threshold invalidates the attempts recorded under the old "
            "version, which is why the version is stored with every attempt.",
        )
    )

    body.append(
        card(
            "Multiple testing",
            f"<p class='small muted'>{escape(str(view.get('multiplicity_note', '')))}</p>",
            right=chip("selection bias controlled, not removed", "muted"),
        )
    )

    # -- monitoring coverage --------------------------------------------
    coverage_body = (
        f"<div class='row' style='margin-bottom:10px'>"
        f"{action_form('strategy-monitor', nonce, 'Measure what can be monitored')}</div>"
    )
    if coverage_rows:
        coverage_body += table(
            "Symbols and timeframes, and whether a template declares them",
            [("Symbol", ""), ("Timeframe", ""), ("Bars", "num"), ("Templates", "num"), ("Monitored", ""), ("Why not", "")],
            [
                [
                    escape(str(row.get("symbol", ""))),
                    escape(str(row.get("timeframe", ""))),
                    escape(str(row.get("bars", ""))),
                    escape(str(len(row.get("templates") or []))),
                    chip("yes", "ok") if row.get("monitorable") else chip("no", "muted"),
                    escape(str(row.get("reason", "")) or "—"),
                ]
                for row in coverage_rows
            ],
        )
    else:
        coverage_body += empty_state(
            "No coverage measured yet",
            "Coverage needs bars from this feed and a symbol list. A timeframe no "
            "template declares is never analysed: a bar feed is not evidence that a "
            "strategy supports it.",
        )
    body.append(
        card(
            "Automatic monitoring: what can be analysed",
            coverage_body,
            hint="Monitoring scores only on the timeframes each template declares, and "
            "never promotes anything by itself.",
        )
    )

    body.append(
        card(
            "What this page does not claim",
            "<ul class='list'>"
            + "".join(f"<li>{escape(str(note))}</li>" for note in view.get("non_claims") or [])
            + "</ul>",
        )
    )

    return render_shell(ctx, "".join(body))


def render_message(ctx: PageContext, *, tone: str, title: str, message: str, actions: str = "") -> str:
    body = (
        f"<div class='notice notice--{escape(tone)}' role='alert'><div><strong>{escape(title)}</strong>"
        f"<p>{escape(message)}</p></div></div>"
        + (f"<div class='step-actions'>{actions}</div>" if actions else "")
    )
    return render_shell(ctx, body)


def render_connecting(ctx: PageContext) -> str:
    body = (
        "<div class='card'><div class='notice' role='status'>"
        "<span class='spinner' aria-hidden='true'></span>"
        "<div><strong>Connecting to the terminal…</strong>"
        "<p>Waiting for MetaTrader 5 to answer. If the terminal is closed the attempt will "
        "fail and nothing will retry on its own.</p></div></div></div>"
    )
    return render_shell(ctx, body)


PAGES = {
    "overview": render_overview,
    "mode": render_mode,
    "setup": render_setup,
    "account": render_account,
    "positions": render_positions,
    "activity": render_activity,
    "data": render_data,
    "provider-setup": render_provider_setup,
    "live": render_live,
    "kronos": render_kronos,
    "strategies": render_strategies,
    "health": render_health,
}


def render_page(ctx: PageContext) -> str:
    renderer = PAGES.get(ctx.page, render_overview)
    return renderer(ctx)


__all__ = [
    "APP_NAME",
    "NAV",
    "PAGES",
    "STYLES",
    "render_account",
    "render_activity",
    "render_data",
    "render_provider_setup",
    "render_connecting",
    "render_health",
    "render_kronos",
    "render_strategies",
    "render_live",
    "render_message",
    "render_mode",
    "render_overview",
    "render_page",
    "render_positions",
    "render_setup",
    "render_shell",
]
