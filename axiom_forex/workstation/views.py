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
    ("setup", "/setup", "Setup"),
    ("account", "/account", "Account & symbols"),
    ("positions", "/positions", "Positions"),
    ("activity", "/activity", "Activity"),
    ("health", "/health", "System health"),
)

ICONS: dict[str, str] = {
    "overview": "M3 12h4l3 7 4-14 3 7h4",
    "mode": "M4 7h10l-3-3M20 17H10l3 3M14 7h6v6M10 17H4v-6",
    "setup": "M4 6h10M4 12h16M4 18h7M18 4v4M16 18h4M20 16v4",
    "account": "M12 12a4 4 0 100-8 4 4 0 000 8zM4 20a8 8 0 0116 0",
    "positions": "M4 18V9M10 18V5M16 18v-6M22 18h-20",
    "activity": "M3 12h3l2-6 4 12 3-7h6",
    "health": "M12 21s-7-4.5-7-10a4 4 0 017-2.5A4 4 0 0119 11c0 5.5-7 10-7 10z",
    "connection": "M5 12a7 7 0 0114 0M8 12a4 4 0 018 0M12 12v0",
    "shield": "M12 3l7 3v6c0 5-3.5 8-7 9-3.5-1-7-4-7-9V6l7-3z",
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
.legend{display:flex;gap:var(--s4);flex-wrap:wrap;font-size:12px;color:var(--text-3)}

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
    "render_connecting",
    "render_health",
    "render_message",
    "render_mode",
    "render_overview",
    "render_page",
    "render_positions",
    "render_setup",
    "render_shell",
]
