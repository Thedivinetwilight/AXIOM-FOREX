"""The workstation revamp: launcher, chrome, and the ledger that would not reconcile.

Three things are proved here, each of them a defect seen in the running workstation
rather than a theory:

* **The launcher and the dependency it needs.**  ``scripts\\start-workstation.cmd``
  started whatever ``py -3`` resolved to -- on the reported machine a global Python
  3.15 that did not have the project installed -- while
  ``scripts\\update-workstation.cmd`` refreshed ``.venv``.  The two halves of the
  product disagreed about which interpreter runs it, and the global one then failed
  at import with ``ZoneInfoNotFoundError``, because ``tzdata`` was only in the
  ``dev`` extra and ``pip install -e .`` is what the instructions run.

* **The chrome.**  Every page draws the same frame, and the frame said
  ``href='/start'`` for "Refresh" on the home page -- a route that does not exist --
  sent the classic overview to the start page, and listed Autopilot at the bottom of
  "Advanced" while the revamp is built around it.  One page also handed a *list* to
  ``render_shell``, so its text carried ``["``, ``, "`` and ``"]`` between cards.

* **The ledger that would not reconcile.**  ``/positions`` could say "These
  positions can be resumed exactly" directly underneath "two open entry fills share
  the position key".  Both were printed by our own code from the same folder.  The
  runner kept one position per key, so the second stored record silently replaced
  the first while the ledger kept both fills.

Everything is a fake terminal, a fixture book and a temporary folder.  No order is
sent, no MetaTrader 5 position is touched, and nothing here is a profitability or
live-readiness claim.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import timedelta, timezone
from pathlib import Path

import pytest

from axiom_forex.paper_runner import accounting
from axiom_forex.paper_runner.accounting import QuoteMark
from axiom_forex.paper_runner.catalog_paper import (
    EVENT_FILL,
    REASON_STATE_RECOVERY,
    STATE_VERSION,
    CatalogPaperTest,
)
from axiom_forex.workstation import views
from axiom_forex.workstation.app import GET_ROUTES, POST_ACTIONS
from axiom_forex.workstation.context import PAGE_PATHS
from axiom_forex.workstation.probes import probe_environment, prerequisite_help

from tests.test_autopilot_ui import _autopilot
from tests.test_catalog_autopilot import TIMEFRAME, _candidate_id, _passing_row
from tests.test_catalog_panel import _panel
from tests.test_paper_accounting import _account_app, _text
from tests.test_paper_flatten import (
    NOW,
    FakeTerminal,
    _app as _panel_app,
    _entry_fill,
    _runner,
    _stored_record,
    _watch,
)

UTC = timezone.utc
REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "scripts" / "start-workstation.cmd"
UPDATER = REPO / "scripts" / "update-workstation.cmd"


# ---------------------------------------------------------------------------
# the launcher, and the dependency the launcher has to provide
# ---------------------------------------------------------------------------


def test_the_launcher_runs_the_project_virtual_environment():
    script = LAUNCHER.read_text(encoding="utf-8")

    # The interpreter it runs is the one the updater maintains.
    assert '.venv\\Scripts\\python.exe' in script
    # ... and it says which interpreter that was, so a global Python cannot pass
    # for the project one in a screenshot.
    assert "Interpreter:" in script
    assert "sys.executable" in script


def test_the_launcher_never_falls_back_to_a_global_python_silently():
    script = LAUNCHER.read_text(encoding="utf-8")

    # A missing .venv stops the launcher with the three commands that create it.
    assert "py -3 -m venv .venv" in script
    assert ".venv\\Scripts\\python.exe -m pip install -e ." in script
    # A system Python is an explicit request, never a default.
    assert "--system-python" in script
    assert "WARNING: running on a system Python" in script
    # The default branch is the venv; the fallback is behind the flag.
    first_choice = script.index('set "PYTHON=%CD%\\.venv\\Scripts\\python.exe"')
    global_choice = script.index("where py >nul 2>nul")
    assert first_choice < global_choice
    # The updater and the launcher agree on the environment.
    assert ".venv\\Scripts\\python.exe" in UPDATER.read_text(encoding="utf-8")


def test_the_launcher_forwards_arguments_but_not_its_own_flag():
    script = LAUNCHER.read_text(encoding="utf-8")

    assert "--system-python" in script
    assert 'for %%a in (%*) do if /i not "%%a"=="--system-python"' in script
    assert "axiom_forex.workstation.launch%LAUNCH_ARGS%" in script
    # The old launcher never passed arguments through at all, so --simulate and
    # --port were unreachable from the supported entry point.
    assert "axiom_forex.workstation.launch %*" not in script


def test_tzdata_is_a_runtime_dependency_not_only_a_dev_one():
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")

    dependencies = text.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert "tzdata" in dependencies
    assert "platform_system == 'Windows'" in dependencies
    # `pip install -e .` installs the runtime dependencies and nothing else, and it
    # is what both scripts tell the operator to run.
    assert "tzdata" in LAUNCHER.read_text(encoding="utf-8")


def test_the_package_imports_without_a_time_zone_database():
    """The reported failure: no tz database, so the whole package would not import.

    The subprocess hides ``tzdata`` and empties the system zone path, which is what
    a Windows machine without the package looks like.  Importing the workstation
    used to raise ``ZoneInfoNotFoundError`` from the synthetic fixture module before
    any of our code could report anything.
    """

    script = (
        "import os, sys\n"
        "os.environ['PYTHONTZPATH'] = ''\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'tzdata' or name.startswith('tzdata.'):\n"
        "            raise ImportError('tzdata hidden for this test')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "import axiom_forex\n"
        "import axiom_forex.workstation.app\n"
        "import axiom_forex.workstation.launch\n"
        "from axiom_forex.workstation.probes import probe_environment\n"
        "report = probe_environment(settings_path=None)\n"
        "assert report.zone_database_ok is False, report.zone_database_ok\n"
        "assert 'time_zone_database_missing' in report.blocking, report.blocking\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=120,
        env={"PYTHONPATH": ".", "PATH": "/usr/bin:/bin"},
    )

    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_the_fixture_calendar_names_the_command_that_fixes_a_missing_zone():
    script = (
        "import os, sys\n"
        "os.environ['PYTHONTZPATH'] = ''\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'tzdata' or name.startswith('tzdata.'):\n"
        "            raise ImportError('tzdata hidden for this test')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "try:\n"
        "    import fixtures.m1\n"
        "except RuntimeError as error:\n"
        "    print(str(error))\n"
        "else:\n"
        "    raise SystemExit('the fixture imported without time-zone data')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=120,
        env={"PYTHONPATH": ".", "PATH": "/usr/bin:/bin"},
    )

    assert result.returncode == 0, result.stderr
    # Fail closed, and say what fixes it -- never invent an offset.
    assert "pip install -e ." in result.stdout
    assert "tzdata" in result.stdout or "time-zone data" in result.stdout


def test_the_prerequisites_probe_names_a_missing_time_zone_database():
    report = probe_environment(settings_path=None, zone_probe=lambda key: False)

    assert report.zone_database_ok is False
    assert "time_zone_database_missing" in report.blocking
    assert report.audit_view()["zone_database_ok"] is False
    codes = {item["code"] for item in prerequisite_help(report)}
    assert "time_zone_database_missing" in codes
    detail = next(
        item["detail"] for item in prerequisite_help(report)
        if item["code"] == "time_zone_database_missing"
    )
    assert ".venv\\Scripts\\python.exe -m pip install -e ." in detail
    # And a machine that has one is not told anything about it.
    present = probe_environment(settings_path=None, zone_probe=lambda key: True)
    assert present.zone_database_ok is True
    assert "time_zone_database_missing" not in present.blocking


def test_every_page_still_renders_without_a_time_zone_database():
    """The other half of the reported failure: the window opened, then 500s.

    The prerequisites probe already refused to *connect* on such a machine, but
    the workstation itself still served: every page rendered except the home page,
    which called into the import tooling and raised from the synthetic calendar.
    A missing runtime dependency is a named blocker on one page, not a dead
    product -- and the page that cannot work must say so in its own words.
    """

    script = (
        "import os, sys\n"
        "os.environ['PYTHONTZPATH'] = ''\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'tzdata' or name.startswith('tzdata.'):\n"
        "            raise ImportError('tzdata hidden for this test')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "from axiom_forex.workstation import launch\n"
        "from axiom_forex.workstation.app import GET_ROUTES\n"
        "from axiom_forex.workstation.setup import SetupWizard\n"
        "report = launch.probe_environment(settings_path=None)\n"
        "assert report.zone_database_ok is False, report.zone_database_ok\n"
        "session = launch._build_session(simulate=True, settings=None, failure=None)\n"
        "app = launch.WorkstationApp(\n"
        "    wizard=SetupWizard(environment=report, session=session),\n"
        "    platform=launch._fixture_platform(),\n"
        "    version='0.1.0',\n"
        "    settings=None,\n"
        "    allow_framing=True,\n"
        ")\n"
        "for path in sorted(GET_ROUTES):\n"
        "    status, html = app.page(path)\n"
        "    assert status == 200, (path, status)\n"
        "status, data = app.page('/data')\n"
        "assert 'data tools could not be loaded' in data.lower(), 'no reason given'\n"
        "status, home = app.page('/')\n"
        "assert status == 200, status\n"
        "print('rendered', len(GET_ROUTES), 'pages')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=180,
        env={"PYTHONPATH": ".", "PATH": "/usr/bin:/bin"},
    )

    assert result.returncode == 0, result.stderr
    assert "rendered" in result.stdout


def _make_dataflow_unimportable(monkeypatch) -> None:
    """Block the import the data page does lazily, on any test order.

    ``from . import dataflow`` first looks for the attribute on the package, so a
    module imported by an earlier test would otherwise satisfy it: the block has to
    cover both the attribute and ``sys.modules``.
    """

    import axiom_forex.workstation as package

    monkeypatch.setitem(sys.modules, "axiom_forex.workstation.dataflow", None)
    monkeypatch.delattr(package, "dataflow", raising=False)


def test_the_data_page_names_an_unimportable_tool_instead_of_failing(tmp_path, monkeypatch):
    """In process, and deliberately blunt: the module cannot be imported at all."""

    app = _account_app(tmp_path)
    _make_dataflow_unimportable(monkeypatch)

    status, html = app.page("/data")
    text = _text(html)

    assert status == 200
    assert "could not be loaded on this machine" in text
    # It says what the interpreter reported, and what to run about it.
    assert "pip install -e ." in text
    assert "Nothing here is hidden or guessed" in text
    # The page still carries the frame, so it is a page and not an error body.
    assert "Data &amp; evaluation" in html or "Data & evaluation" in html


def test_an_unimportable_tool_stays_reported_and_is_never_retried_silently(tmp_path, monkeypatch):
    """A warning that disappears on reload is the disagreement this page forbids."""

    app = _account_app(tmp_path)
    _make_dataflow_unimportable(monkeypatch)

    first = _text(app.page("/data")[1])
    assert "could not be loaded on this machine" in first

    # Even with the import working again, this process keeps its first answer:
    # a half-imported module chain can succeed on a retry while the dependency
    # that failed is still missing, which would flip the page between renders.
    monkeypatch.undo()
    second = _text(app.page("/data")[1])
    assert "could not be loaded on this machine" in second
    assert app.page("/")[0] == 200


def _outline(html: str) -> list[int]:
    body = html[html.index("<main"):] if "<main" in html else html
    return [int(level) for level in re.findall(r"<h([1-6])[\s>]", body)]


@pytest.mark.parametrize("state", ["simulated", "connected", "live"])
def test_no_page_skips_a_heading_level(state):
    """A heading outline a screen reader can follow, on every page and state."""

    app = _chrome_apps()[state]
    for path in sorted(GET_ROUTES):
        status, html = app.page(path)
        assert status == 200, path
        levels = _outline(html)
        assert levels.count(1) == 1, (path, levels)
        assert levels[0] == 1, (path, levels)
        previous = None
        for level in levels:
            if previous is not None and level > previous + 1:
                raise AssertionError(f"{path} jumps h{previous} -> h{level}: {levels}")
            previous = level


def test_the_execution_modes_are_headed_by_an_h2_above_the_two_options():
    """``/mode`` drew two ``h3`` options directly under the page's ``h1``."""

    cards = views.card("A heading", "<p>body</p>")
    assert "<h2" in cards
    app = _chrome_apps()["connected"]
    status, html = app.page("/mode")
    text = _text(html)

    assert status == 200
    assert "Which execution mode is in force" in text
    # The sentence that stops the page being read as "both are on".
    assert "Exactly one of these is active" in text
    # Both options are still there, still named.
    assert "Paper trading" in text and "Live trading" in text


def test_the_overview_does_not_call_the_fixture_account_a_paper_ledger():
    """Two pages, two paper equities, one of them invented.

    ``/overview`` rendered ``platform.status()`` under the title "Paper account"
    with the hint "Simulated paper ledger".  That platform is the synthetic
    conformance fixture, so the card showed ``Equity 10000`` and currency ``CCC``
    while ``/performance`` -- reading the fills this machine actually recorded --
    said the paper equity was unavailable because no starting capital was set.
    The fixture figure is an illustration, and it may not borrow the ledger's name.
    """

    from tests.test_workstation_ui import connected_simulator_app

    app = connected_simulator_app()
    status, html = app.page("/overview")

    assert status == 200
    text = _text(html)
    assert "Fixture account (synthetic)" in text
    assert "not your paper ledger" in text
    # It still shows the fixture's own numbers, labelled for what they are.
    assert "SAMPLE FIXTURE" in text
    # And it points at the page that owns the operator's real figures.
    assert "href='/performance'" in html

    # The performance page is the one place a paper equity is claimed, and it
    # refuses to claim one here rather than borrowing the fixture's.
    performance = _text(app.page("/performance")[1])
    assert "no paper starting capital is configured" in performance


def test_the_guided_start_points_at_the_autopilot_where_nothing_is_wired():
    """The last step used to be a dead end: "no paper session to start"."""

    from tests.test_workstation_ui import connected_simulator_app

    html = connected_simulator_app().page("/")[1]
    text = _text(html)

    assert "NOT WIRED IN THIS BUILD" in text
    # The run happens on Autopilot; the step has to say so and go there.
    assert "Open Autopilot" in text
    assert "href='/autopilot'" in html
    # ... and it names the page that scores the strategies, rather than leaving
    # step 4 as another dead end.
    assert "href='/quick-setup'" in html


def test_the_guided_start_signposts_the_autopilot_as_the_one_page_route():
    """Five steps are one route; the page the navigation leads with is the other."""

    from tests.test_workstation_ui import connected_simulator_app

    html = connected_simulator_app().page("/")[1]
    text = _text(html)

    assert "Or run the whole loop from one page" in text
    assert "Open Autopilot" in text
    # One hop from the home page to the page that runs everything.
    assert html.count("href='/autopilot'") >= 2  # the nav entry and the button
    # The claim it makes is only that the inputs are shared -- no profit, no promise.
    assert "same terminal, the same catalog record and the same paper ledger" in text


def test_the_stylesheet_shrinks_to_one_column_on_a_narrow_window():
    """No browser here, so the guard is the rule itself."""

    css = views.STYLES

    assert "@media (max-width:560px)" in css
    narrow = css[css.index("@media (max-width:560px)") :]
    narrow = narrow[: narrow.index("}") + 1] if "}" in narrow else narrow
    block = css[css.index("@media (max-width:560px)") : css.index("@media (prefers-reduced-motion")]
    assert "grid-template-columns:minmax(0,1fr)" in block
    # A phone-sized window must not have to scroll the whole page sideways.
    assert ".content{padding:var(--s3)" in block
    # Tables stay scrollable inside their own box, at every width.
    assert ".table-wrap{overflow-x:auto}" in css


# ---------------------------------------------------------------------------
# the Autopilot page's own money and its own counters
# ---------------------------------------------------------------------------


def test_the_runner_counters_are_read_as_a_sentence_in_a_fixed_order():
    text = views._runner_counts_text(
        {"closes": 0, "cycles": 1, "stale_quotes_skipped": 0, "signals": 2, "fills": 3}
    )

    # The order is the lifecycle, not the dictionary's: cycles, signals, entries,
    # exits, then the reasons a cycle did nothing.
    assert text.index("cycles run 1") < text.index("signals 2")
    assert text.index("signals 2") < text.index("entry fills 3")
    assert text.index("entry fills 3") < text.index("exit fills 0")
    # No raw dict, no braces, no quotes -- the earlier page printed
    # "Counts: closes 0, cycles 1, ..." as if that were prose.
    for token in ("{", "}", "'", "[", "]"):
        assert token not in text
    assert text.startswith("cycles run 1")


def test_a_counter_this_build_does_not_know_is_named_not_dropped():
    text = views._runner_counts_text({"cycles": 4, "quote_retries": 7})

    assert "cycles run 4" in text
    # Humanised, in its own place, and never silently missing.
    assert "quote retries 7" in text
    assert text.endswith("quote retries 7")
    # Unknown keys come after the known ones, sorted, so two renders agree.
    other = views._runner_counts_text({"alpha_beta": 1, "cycles": 4, "zeta": 2})
    assert other.endswith("alpha beta 1, zeta 2")


def test_no_counters_is_said_in_words():
    assert views._runner_counts_text({}) == "nothing has run yet"


def _pnl_context(account):
    class Ctx:
        extra = {"account": account}

    return Ctx()


def test_the_autopilot_money_card_never_turns_an_unknown_into_zero():
    account = {
        "available": True,
        "source": "durable paper ledger",
        "latest_update": None,
        "counts": {"open_positions": 0, "closed_trades": 0, "fills": 0},
        "metric_by_key": {
            "realized_net": {
                "label": "Realised P&L (after costs)",
                "value": "0.00",
                "available": True,
                "note": "price P&L minus commission and swap",
            },
            "unrealized_gross": {
                "label": "Unrealised P&L (price)",
                "value": "",
                "available": False,
                "note": "needs a fresh quote",
            },
            "equity": {
                "label": "Paper equity",
                "value": "",
                "available": False,
                "note": "no paper starting capital is configured",
            },
            "net_pnl": {
                "label": "Net P&L (after costs)",
                "value": "0.00",
                "available": True,
                "note": "after commission and swap",
            },
        },
    }

    html = views._autopilot_paper_pnl_card(_pnl_context(account))
    text = _text(html)

    assert "Paper P&amp;L" in html or "Paper P&L" in text
    # The measured zero is a zero ...
    assert "Realised P&amp;L (after costs)" in html
    # ... and the two the ledger cannot support say so, with the ledger's reason,
    # instead of borrowing a zero or the fixture's balance.
    assert text.count("Unavailable") >= 2
    assert "no paper starting capital is configured" in text
    assert "needs a fresh quote" in text
    # One reading serves the page, and it links to the full figures.
    assert "href='/performance'" in html
    # The broker is never blended in.
    assert "broker account's balance" in text


def test_the_autopilot_money_card_says_so_when_there_is_no_ledger():
    for account in ({}, {"available": False, "error": "no_platform"}):
        html = views._autopilot_paper_pnl_card(_pnl_context(account))
        text = _text(html)

        assert "could not be read" in text
        # No number is invented to fill the space.
        assert "0.00" not in text or "no_platform" in text


def test_the_autopilot_states_the_size_its_fills_will_actually_use(tmp_path):
    """Quick setup's lot size never reaches this loop, so the page must say which
    size does.  Otherwise an operator who set 0.50 on the research sweep reads a
    paper result that was filled at 0.10 and has no way to know."""

    autopilot, app = _autopilot(tmp_path)
    autopilot._panel.lot = 0.25  # the panel's own size, which the paper test uses
    choices = autopilot.choices()

    assert choices["lot"] == 0.25
    text = _text(app.page("/autopilot")[1])
    assert "Every simulated fill is" in text
    assert "0.25" in text
    # It says which control it is *not*, so the two sizes cannot be confused.
    assert "research sweep" in text


# ---------------------------------------------------------------------------
# the chrome: navigation, the refresh control, the loop's state
# ---------------------------------------------------------------------------


def _chrome_apps():
    from tests.test_workstation_ui import (
        app_for,
        confirmed_live_app,
        connected_simulator_app,
        simulated_wizard,
    )

    return {
        "simulated": app_for(simulated_wizard()),
        "connected": connected_simulator_app(),
        "live": confirmed_live_app(),
    }


def test_the_navigation_leads_with_the_autopilot_page():
    assert views.NAV[0][0] == "autopilot"
    assert views.NAV[0][1] == "/autopilot"
    assert views.NAV_GROUPS[0][1][0] == "autopilot"
    # Nothing is hidden from the groups, and no group names an entry that is absent.
    grouped = [key for _title, keys in views.NAV_GROUPS for key in keys]
    assert grouped == [entry[0] for entry in views.NAV]
    # The guided start page is still the home route.
    assert GET_ROUTES["/"] == ("start", "Start paper trading")
    assert PAGE_PATHS["start"] == "/"


def test_every_page_key_maps_to_the_route_that_renders_it():
    reverse = {key: route for route, (key, _title) in GET_ROUTES.items()}

    assert PAGE_PATHS == reverse
    # Both directions, so a new route cannot be added without a path.
    assert set(PAGE_PATHS) == {key for key, _title in GET_ROUTES.values()}


@pytest.mark.parametrize("state", ["simulated", "connected", "live"])
def test_refresh_on_every_page_returns_to_that_page(state):
    app = _chrome_apps()[state]

    for path in sorted(GET_ROUTES):
        status, html = app.page(path)
        assert int(status) == 200, path
        refresh = re.findall(r"href='([^']*)'>Refresh</a>", html)
        if not refresh:
            # Only a connected session draws it; an unconnected one offers setup.
            continue
        assert refresh == [path], (path, refresh)
        assert refresh[0] in GET_ROUTES, (path, refresh[0])
        # The reported defect, named: "Refresh" on the home page pointed at /start.
        assert refresh[0] != "/start" or path == "/start"


def test_no_page_offers_a_refresh_link_to_a_route_that_does_not_exist():
    for state, app in _chrome_apps().items():
        for path in sorted(GET_ROUTES):
            _status, html = app.page(path)
            for href in set(re.findall(r"href='(/[^'#?]*)'", html)):
                assert href in GET_ROUTES, (state, path, href)


def test_the_paper_loop_state_is_in_the_chrome_of_every_page(tmp_path):
    _autopilot_obj, app = _autopilot(tmp_path, report=True)

    for path in sorted(GET_ROUTES):
        _status, html = app.page(path)
        topbar = html[html.index("<header class='topbar'"):]
        topbar = topbar[: topbar.index("</header>")]
        assert "Paper loop:" in topbar, path
        assert "Paper loop: IDLE" in topbar, path
        # Stated once, in the frame; the page body may add its own detail.
        assert html.count("Paper loop:") == 1, path


def test_a_running_loop_is_linked_from_the_chrome(tmp_path):
    autopilot, app = _autopilot(tmp_path, report=True)
    autopilot.state_summary = lambda: {
        "available": True,
        "state": "RUNNING",
        "running": True,
        "reason": "",
        "open_positions": 2,
    }

    _status, html = app.page("/positions")
    topbar = html[html.index("<header class='topbar'"):]
    topbar = topbar[: topbar.index("</header>")]
    assert "Paper loop: RUNNING · 2 open" in topbar
    # A running or blocked loop is one click from the page that owns it.
    assert "<a class='row' href='/autopilot'" in topbar


def test_no_page_renders_a_python_list_or_dict_repr():
    """The reported leak: ``["`` at the top of the Autopilot page, ``", "`` between
    every two cards, ``"]`` at the end -- a list interpolated into the frame."""

    leak = re.compile(r"\[&#x27;|\[\"|\"\]|&#x27;\]|\", \"|', '|\{'|\'}")
    apps = _chrome_apps()
    for state, app in apps.items():
        for path in sorted(GET_ROUTES):
            _status, html = app.page(path)
            body = re.sub(r"<style.*?</style>", " ", html, flags=re.S)
            text = re.sub(r"<[^>]+>", " ", body)
            assert not leak.search(text), (state, path, leak.search(text).group(0))


def test_render_shell_accepts_the_fragments_a_page_built(tmp_path):
    """``render_shell`` joins a sequence; it must never interpolate its repr."""

    _autopilot_obj, app = _autopilot(tmp_path, report=True)
    ctx = app._context("autopilot", "Autopilot", path="/autopilot")

    joined = views.render_shell(ctx, ["<p>one</p>", "<p>two</p>"])
    scalars = views.render_shell(ctx, ["<p>one</p>", 3])  # type: ignore[list-item]

    assert "<p>one</p><p>two</p>" in joined
    assert "['<p>one</p>'" not in joined
    assert "&#x27;" not in joined
    assert "<p>one</p>3" in scalars


def test_every_visible_control_points_at_a_real_handler():
    for state, app in _chrome_apps().items():
        for path in sorted(GET_ROUTES):
            status, html = app.page(path)
            assert int(status) == 200, (state, path)
            for action in set(re.findall(r"action='(/actions/([^']*))'", html)):
                target, verb = action
                assert verb in POST_ACTIONS, (state, path, target)
            for href in set(re.findall(r"href='(/[^'#?]*)'", html)):
                assert href in GET_ROUTES, (state, path, href)
            for match in re.finditer(r"<button[^>]*>(.*?)</button>", html, re.S):
                assert re.sub(r"<[^>]+>", "", match.group(1)).strip(), (
                    state,
                    path,
                    "a button with no accessible name",
                )


# ---------------------------------------------------------------------------
# one ledger entry per key, or an honest refusal
# ---------------------------------------------------------------------------


def _seed_duplicate_book(root: Path) -> Path:
    """A resumable book whose *second* record reuses the first one's position key.

    This is the shape behind the screenshot: the ledger holds two entry fills for
    one key (AUDUSD there, EURUSD.pro here), the state file holds two records for
    it, and every record carries the fields the exit rules read -- so the recovery
    report used to come back clean.
    """

    watch = _watch()
    paper = root / "data" / "catalog" / "paper"
    paper.mkdir(parents=True, exist_ok=True)
    record = _stored_record(0, symbol="EURUSD.pro", at=NOW.isoformat(), entry=1.10000)
    record["key"] = watch.key
    record["candidate_id"] = watch.candidate_id
    twin = json.loads(json.dumps(record))

    document = {
        "version": STATE_VERSION,
        "updated_at": NOW.isoformat(),
        "status": "STOPPED",
        "running": False,
        "started_at": "",
        "stopped_at": "",
        "reason": "",
        "lots": 0.10,
        "max_quote_age_seconds": 120.0,
        "watches": [{"key": watch.key, "symbol": "EURUSD.pro", "timeframe": "15m"}],
        "stats": {"cycles": 3, "signals": 2, "fills": 2, "closes": 0, "waiting": 0},
        "positions": [record, twin],
        "last_bar": {},
        "recovery": {},
    }
    (paper / "catalog-paper-state.json").write_text(
        json.dumps(document, indent=2), encoding="utf-8"
    )
    fill = _entry_fill(record)
    with (paper / "catalog-paper-ledger.jsonl").open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(fill, sort_keys=True) + "\n")
        handle.write(json.dumps(dict(fill), sort_keys=True) + "\n")
    return paper


def _digest(paper: Path) -> tuple[str, str]:
    return (
        (paper / "catalog-paper-state.json").read_text(encoding="utf-8"),
        (paper / "catalog-paper-ledger.jsonl").read_text(encoding="utf-8"),
    )


def test_two_stored_records_with_one_key_cannot_be_resumed(tmp_path):
    paper = _seed_duplicate_book(tmp_path)
    runner = _runner(paper, terminal=FakeTerminal())

    report = runner._restore_state()

    assert report["ok"] is False
    assert report["restored"] == 1, report  # one position, not two records
    assert [item["symbol"] for item in report["failed"]] == ["EURUSD.pro"]
    reason = report["failed"][0]["reason"]
    assert "share the position key" in reason
    assert "share one position key" in report["detail"]
    # The first record is kept; the second does not overwrite it behind our back.
    assert list(runner._positions) == [_watch().key]


def test_two_stored_records_with_one_key_block_the_start_and_leave_the_state(tmp_path):
    paper = _seed_duplicate_book(tmp_path)
    state_before = (paper / "catalog-paper-state.json").read_text(encoding="utf-8")
    ledger_before = (paper / "catalog-paper-ledger.jsonl").read_text(encoding="utf-8")
    runner = _runner(paper, terminal=FakeTerminal())

    outcome = runner.start()

    assert outcome["started"] is False
    assert outcome["status"] == "BLOCKED"
    assert outcome["blockers"][0]["reason"] == REASON_STATE_RECOVERY
    assert "share" in outcome["blockers"][0]["detail"] or "resume" in (
        outcome["blockers"][0]["detail"]
    )
    assert runner.running is False
    # The state file -- the evidence of what could not be resumed -- is untouched.
    assert (paper / "catalog-paper-state.json").read_text(encoding="utf-8") == state_before
    # The ledger is append-only: the fills are byte-identical, and the refusal is
    # recorded after them rather than replacing anything.
    ledger_after = (paper / "catalog-paper-ledger.jsonl").read_text(encoding="utf-8")
    assert ledger_after.startswith(ledger_before)
    added = [
        json.loads(line)
        for line in ledger_after[len(ledger_before):].splitlines()
        if line.strip()
    ]
    # The one record it could read is noted as restored, and then the refusal is
    # recorded: an append-only ledger is how a later reader learns what happened.
    assert [event["kind"] for event in added] == [
        "position_restored",
        "state_recovery_failed",
    ]


def test_the_positions_page_does_not_call_a_duplicated_book_resumable(tmp_path):
    paper = _seed_duplicate_book(tmp_path)
    state = json.loads((paper / "catalog-paper-state.json").read_text(encoding="utf-8"))
    # A clean recovery report, as a previous run could have written it.
    state["recovery"] = {
        "ok": True,
        "version": STATE_VERSION,
        "restored": 2,
        "migrated": 0,
        "failed": [],
        "detail": "",
    }
    (paper / "catalog-paper-state.json").write_text(json.dumps(state), encoding="utf-8")

    app = _account_app(paper)
    account = app.paper_account()
    warnings = [item["detail"] for item in account["integrity"]["warnings"]]
    assert any("share the position key" in item for item in warnings)
    # The figures that depend on the pairing are unavailable, not reconciled.
    metrics = {item["key"]: item["available"] for item in account["metrics"]}
    assert metrics["equity"] is False

    html = views.render_page(app._context("positions", "Paper positions"))
    text = _text(html)

    assert "can be resumed exactly" not in text
    assert "The ledger and the stored book do not agree" in text
    assert "share the position key" in text
    assert "action='/actions/catalog-paper-close-all'" in html
    # And it does not pretend the book is flat, or closed by itself.
    assert "Nothing open" not in text


def test_the_confirmed_close_repairs_a_duplicated_book_and_the_loop_then_runs(tmp_path):
    paper = _seed_duplicate_book(tmp_path)
    runner = _runner(paper, terminal=FakeTerminal())
    assert runner.start()["started"] is False

    # The one action the page offers, at the level the operator clicks -- with the
    # fields that page's own form carries.
    app = _panel_app(tmp_path)
    page = views.render_page(app._context("positions", "Paper positions"))
    assert "name='return_to' value='/positions'" in page
    status, redirect, message = app.act(
        "catalog-paper-close-all",
        {
            "nonce": [app.nonce],
            "confirm": ["close-all-paper-positions"],
            "return_to": ["/positions"],
        },
    )
    assert int(status) == 303, message
    assert redirect == "/positions"

    after = accounting.read_account(paper, now=NOW)
    assert after["counts"]["open_positions"] == 0
    assert after["integrity"]["ok"] is True
    assert after["integrity"]["warnings"] == []
    # Two entry fills, two closing fills: the ledger pairs again, nothing invented.
    events = [
        json.loads(line)
        for line in (paper / "catalog-paper-ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert sum(1 for event in events if event.get("kind") == EVENT_FILL) == 2
    assert sum(1 for event in events if event.get("kind") == "simulated_close") == 2

    resumed = _runner(paper, terminal=FakeTerminal())
    outcome = resumed.start()
    try:
        assert outcome["started"] is True, outcome
        assert outcome["status"] == "RUNNING"
    finally:
        resumed.stop()


def test_a_duplicated_book_is_still_resumable_once_the_duplicate_is_gone(tmp_path):
    """The refusal is about the duplicate, not about the book being old."""

    paper = _seed_duplicate_book(tmp_path)
    state = json.loads((paper / "catalog-paper-state.json").read_text(encoding="utf-8"))
    state["positions"] = state["positions"][:1]
    (paper / "catalog-paper-state.json").write_text(json.dumps(state), encoding="utf-8")
    # The ledger still holds two entry fills.  One stored record, one key: the
    # runner can resume it, and the page still refuses to call the *counts* clean,
    # because the ledger pairs two entries against one record.
    runner = _runner(paper, terminal=FakeTerminal())
    report = runner._restore_state()

    assert report["restored"] == 1
    assert report["failed"] == []


# ---------------------------------------------------------------------------
# a stale quote is named, not summarised
# ---------------------------------------------------------------------------


def test_a_stale_quote_names_the_symbol_the_age_and_the_allowance(tmp_path):
    paper = _seed_duplicate_book(tmp_path)
    stale = FakeTerminal(age_seconds=900.0)
    runner = CatalogPaperTest(
        quotes=stale,
        watches=(_watch(),),
        root=paper,
        clock=lambda: NOW,
    )

    quote, seen = runner._quote_evidence("EURUSD.pro")

    # No quote came back: a price that old may not be filled from.
    assert quote is None
    assert seen["state"] == "stale"
    assert seen["reason"] == "stale_quote"
    assert seen["symbol"] == "EURUSD.pro"
    assert "900 s old" in seen["detail"]
    assert f"{seen['allowance_seconds']:.0f} s" in seen["detail"]


def test_a_stale_quote_pauses_decisions_by_name(tmp_path):
    """The pause is one pair, at one age, against one allowance -- not "the feed"."""

    paper = _seed_duplicate_book(tmp_path)
    stale = QuoteMark(
        symbol="EURUSD.pro",
        bid=1.10000,
        ask=1.10004,
        quote_time=NOW - timedelta(minutes=12),
        checked_at=NOW.isoformat(),
    )

    account = accounting.read_account(paper, now=NOW, marks={"EURUSD.pro": stale})

    assert account["trading"]["paused"] is True
    reasons = " ".join(account["trading"]["reasons"])
    assert "EURUSD.pro" in reasons
    assert "12.0 min old" in reasons
    assert f"{account['trading']['allowance_seconds']:.0f} s" in reasons
    # And the page prints that sentence rather than a summary of it.
    html = _account_app(paper, marks={"EURUSD.pro": stale}).page("/positions")[1]
    text = _text(html)
    assert "Trading decisions are paused" in text
    assert "EURUSD.pro" in text


# ---------------------------------------------------------------------------
# PAPER cannot reach a broker, from the UI layer either
# ---------------------------------------------------------------------------


def test_no_ui_or_paper_module_can_submit_or_close_a_broker_order():
    """The paper path holds no adapter, and the pages that show it hold none either.

    Call-shaped patterns, not prose: ``per_order_checks`` is a handoff *key* and
    ``order_sending`` is a boolean the runner reports, while ``order_send(`` would
    be the broker's own submission call.  ``brokers.mt5`` is a module path, so only
    attribute access (``MetaTrader5.``) and a real import are treated as reach.
    """

    files = sorted(Path("axiom_forex/workstation").glob("*.py")) + [
        Path("axiom_forex/paper_runner/catalog_paper.py"),
        Path("axiom_forex/paper_runner/accounting.py"),
        Path("axiom_forex/strategies/catalog/autopilot.py"),
        Path("axiom_forex/strategies/catalog/panel.py"),
    ]
    forbidden = (
        r"\border_send\s*\(",
        r"\border_check\s*\(",
        r"\bpositions_get\s*\(",
        r"\borders_get\s*\(",
        r"\bposition_close\s*\(",
        r"\bclose_position\s*\(",
        r"\bimport\s+MetaTrader5\b",
        r"\bMetaTrader5\.",
    )

    assert files
    for path in files:
        source = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            match = re.search(pattern, source)
            assert match is None, (str(path), pattern, source[: match.start()].count("\n") + 1)


def test_the_guided_start_page_points_at_the_autopilot_page():
    """Two ways in, and the guide names the other one instead of hiding it."""

    from tests.test_workstation_ui import connected_simulator_app

    html = connected_simulator_app().page("/")[1]

    # One signpost, above the five steps.  It used to be drawn twice: once here
    # and once after the list, which is how a page ends up saying the same thing
    # on two screens of scroll.
    assert html.count("Or run the whole loop from one page") == 1
    assert "Or run all of this from the Autopilot page" not in html
    assert "href='/autopilot'" in html
    # The signpost is a card body, so the link is markup rather than escaped text.
    assert "&lt;a href=" not in html


def test_the_updater_installs_the_time_zone_data_an_older_venv_is_missing():
    """`--no-deps` keeps the update offline-safe, so the check has to be explicit.

    An .venv created before tzdata was declared would refresh the code and still
    die at run time with ZoneInfoNotFoundError, on the one platform that has no
    zone database of its own.
    """

    script = UPDATER.read_text(encoding="utf-8")

    # The refresh stays dependency-free ...
    assert "-m pip install -e . --no-deps" in script
    # ... and is followed by the question the code asks, with the same interpreter.
    check = 'zoneinfo.ZoneInfo(\'America/New_York\')'
    assert script.count(check) == 2  # once to decide, once to report
    assert script.index(check) > script.index("--no-deps")
    # Only the declared dependency is installed, and only when the check fails.
    assert "Installing the dependency declared in pyproject.toml" in script
    assert ":zone_ok" in script
    # A failure is named, leaves the clone at its new commit, and says what to run.
    assert ":zone_failed" in script
    start = script.index("\n:zone_failed\n")  # the label, not the goto to it
    failure = script[start : script.index("\n:pip_failed\n", start)]
    assert ".venv\\Scripts\\python.exe -m pip install -e ." in failure
    assert "Nothing was changed" not in failure  # the code *was* updated
    assert "Your settings and data files are untouched" in failure
    # The summary reports it either way, so the operator is never left guessing.
    assert "time-zone data: %TZ_NOTE%" in script


def test_both_scripts_name_the_same_interpreter():
    launcher = LAUNCHER.read_text(encoding="utf-8")
    updater = UPDATER.read_text(encoding="utf-8")

    for script in (launcher, updater):
        assert ".venv\\Scripts\\python.exe" in script
    # The launcher must not send the operator to a global Python for the install
    # that the updater performs inside .venv.
    assert "py -3 -m venv .venv" in launcher
    assert "py -3 -m venv .venv" in updater


# ---------------------------------------------------------------------------
# one session, one story: the durable book, the panel, and the loop
# ---------------------------------------------------------------------------


def test_the_stored_session_never_contradicts_itself(tmp_path):
    """``status: RUNNING`` beside ``running: false``, written by ``start()``.

    ``running`` was ``thread.is_alive()``, and ``start()`` writes the state file
    *before* it creates the thread.  The first write of a live session therefore
    recorded a session that was running and not running at once -- and every page
    that reads the durable book (paper account, positions, performance) said "the
    paper session is not running" while the Autopilot page said RUNNING, from the
    same folder.
    """

    paper = CatalogPaperTest(
        quotes=FakeTerminal(),
        watches=(_watch(),),
        root=tmp_path,
        lot=0.10,
        clock=lambda: NOW,
    )
    started = paper.start()
    try:
        assert started["started"] is True, started
        payload = json.loads((tmp_path / "catalog-paper-state.json").read_text(encoding="utf-8"))
        assert payload["status"] == "RUNNING"
        # The file has to agree with itself, at every instant it exists.
        assert payload["running"] is True
    finally:
        paper.stop(reason="test")


def test_a_stopped_book_says_stopped_in_the_file_too(tmp_path):
    paper = CatalogPaperTest(
        quotes=FakeTerminal(), watches=(_watch(),), root=tmp_path, lot=0.10, clock=lambda: NOW
    )
    paper.start()
    paper.stop(reason="operator_stopped")
    payload = json.loads((tmp_path / "catalog-paper-state.json").read_text(encoding="utf-8"))
    assert payload["status"] == "STOPPED"
    assert payload["running"] is False


def test_the_paper_account_reads_a_running_book_as_running(tmp_path):
    """The pause banner is derived from that same file, so it follows it."""

    paper = CatalogPaperTest(
        quotes=FakeTerminal(), watches=(_watch(),), root=tmp_path, lot=0.10, clock=lambda: NOW
    )
    paper.start()
    try:
        # The reader the pages use, pointed at the folder the test just wrote.
        account = accounting.read_account(tmp_path, now=NOW, max_quote_age_seconds=120.0)
        assert account["session"]["running"] is True
        assert account["trading"]["paused"] is False, account["trading"]
    finally:
        paper.stop(reason="test")


def test_the_autopilot_publishes_its_session_to_the_panel(tmp_path):
    """The pages that read the panel must see the session the loop is driving.

    The Autopilot builds its own test on the panel's folder.  Until it registered
    that test, the start page said "Not trading yet" and "Build the test from a
    stored evaluation first" while the Autopilot page said RUNNING.
    """

    from tests.test_catalog_autopilot import _start_with_report

    candidate_id = _candidate_id()
    autopilot = _start_with_report(
        tmp_path,
        lambda boundaries: [
            _passing_row(boundaries[("EURUSD.pro", TIMEFRAME)] - timedelta(hours=1), candidate_id)
        ],
    )
    try:
        panel = autopilot._panel
        view = panel.paper_view()
        assert view["built"] is True
        assert view["running"] is True
        # The watches are the panel's too, so its table lists the followed row.
        assert [watch.symbol for watch in panel._paper_watches] == ["EURUSD.pro"]
        snapshot = panel.paper_snapshot()
        assert snapshot.get("running") is True
        assert snapshot.get("watches")
    finally:
        autopilot.stop()


def test_adopting_never_swaps_the_book_under_a_running_test(tmp_path):
    """A guard, not a courtesy: two owners of one ledger would be worse than none."""

    panel = _panel(tmp_path)
    running = CatalogPaperTest(quotes=FakeTerminal(), watches=(_watch(),), root=tmp_path, lot=0.10)
    running.start()
    try:
        assert panel.adopt_paper(running, ()) is True
        second = CatalogPaperTest(quotes=FakeTerminal(), watches=(), root=tmp_path, lot=0.10)
        assert panel.adopt_paper(second, ()) is False
        assert panel.paper_snapshot().get("running") is True
    finally:
        running.stop(reason="test")
        # The refusal left the panel's own test stopping normally.
        assert panel.paper_snapshot().get("running") in (False, None)


def test_a_running_test_is_not_told_no_candidate_passed_the_screen(tmp_path):
    """The record's conditions are about a rebuild, not about the test running now."""

    from tests.test_catalog_autopilot import _start_with_report

    candidate_id = _candidate_id()
    autopilot = _start_with_report(
        tmp_path,
        lambda boundaries: [
            _passing_row(boundaries[("EURUSD.pro", TIMEFRAME)] - timedelta(hours=1), candidate_id)
        ],
    )
    try:
        view = autopilot._panel.paper_view()
        reasons = {item["reason"] for item in view["blockers"]}
        # The stored record in this fixture has no paper set; that must not be a
        # *current* blocker while the test is following a selected row.
        assert "no_screened_candidate_in_the_record" not in reasons
        assert "no_screened_candidate_in_the_record" in {
            item["reason"] for item in view["rebuild_blockers"]
        }
    finally:
        autopilot.stop()


def test_a_stopped_autopilot_says_where_the_blocker_is_cleared(tmp_path):
    """A refusal with a route is a next step; one without is a scavenger hunt."""

    from axiom_forex.strategies.catalog.autopilot import Autopilot
    from tests.test_autopilot_ui import _app as _ui_app, _panel as _ui_panel

    panel = _ui_panel(tmp_path)
    panel.terminal_provider = lambda: None  # no terminal at all
    app = _ui_app(panel)
    autopilot = Autopilot(panel)
    app.autopilot = autopilot
    autopilot.start(symbols=["EURUSD.pro"], timeframes=[TIMEFRAME])
    autopilot.wait(timeout=60.0)

    status, html = app.page("/autopilot")
    text = _text(html)

    assert status == 200
    assert autopilot.status()["state"] == "BLOCKED"
    assert "What stopped it" in text
    assert "No terminal" in text
    # The route to the control that clears it, and it is a real route.
    assert "Connect the terminal" in text
    assert "href='/setup'" in html
    for href in re.findall(r"href='(/[^']*)'", html):
        assert href.split("?")[0] in GET_ROUTES, href


def test_the_autopilot_offers_the_pairs_the_terminal_verified(tmp_path):
    """Ticking a pair the terminal reports beats typing one and being refused."""

    autopilot, app = _autopilot(tmp_path)
    choices = autopilot.choices()

    assert choices["symbols_available"] is True
    offered = {item["broker_symbol"]: item for item in choices["pairs"]}
    assert offered["EURUSD.pro"]["pair"] == "EUR/USD"
    # Both majors the fixture terminal reports, each with its pair name.
    assert set(offered) == {"EURUSD.pro", "GBPUSD.pro"}

    status, html = app.page("/autopilot")
    text = _text(html)

    assert status == 200
    assert "Forex pairs the terminal reports" in text
    # They are checkboxes named 'symbols', which the start action already accepts.
    assert html.count("name='symbols'") == 2
    assert "value='EURUSD.pro'" in html and "value='GBPUSD.pro'" in html
    # The recommended ones are pre-ticked, so Start does something sensible.
    assert html.count(" checked") >= 2
    # A symbol the terminal does not class as a pair is named, not silently absent.
    assert "Not offered as forex" in text
    assert "XAUUSD" in text
    # And the "tick none" case is stated rather than left to be discovered.
    assert "Tick none and the run uses the pairs the terminal recommends" in text


def test_the_autopilot_falls_back_to_typing_when_no_pair_is_verified(tmp_path):
    from tests.test_autopilot_ui import _app as _ui_app, _panel as _ui_panel

    panel = _ui_panel(tmp_path)
    panel.terminal_provider = lambda: None
    app = _ui_app(panel)
    from axiom_forex.strategies.catalog.autopilot import Autopilot

    app.autopilot = Autopilot(panel)

    status, html = app.page("/autopilot")
    text = _text(html)

    assert status == 200
    # No offer to show, so the form says why it is asking for a name instead.
    assert "the terminal reported no verified forex pair, so type one" in text
    assert "name='symbols'" in html


def test_the_live_banner_says_a_state_and_not_a_colour(tmp_path):
    """The chip read ``ok`` -- the *tone* name, printed as if it were the state."""

    from tests.test_workstation_ui import connected_simulator_app

    html = connected_simulator_app().page("/live")[1]
    text = _text(html)

    assert "No real order can be sent right now" in text
    assert "Not sending" in text
    # The bare tone word is gone; the dot carries the tone, the word carries the answer.
    assert ">ok</span>" not in html
    assert ">warn</span>" not in html


def test_the_setup_page_counts_its_own_steps(tmp_path):
    """It said "Seven plain steps" above a list of six."""

    from tests.test_workstation_ui import app_for, simulated_wizard

    html = app_for(simulated_wizard()).page("/setup")[1]
    text = _text(html)

    assert "6 plain steps" in text
    assert "Seven plain steps" not in text
    # The count it prints is the count it renders.
    assert "0 of 6 steps complete" in text


def test_an_outstanding_condition_is_named_with_the_step_that_clears_it():
    from axiom_forex.workstation.setup import outstanding_words

    said = outstanding_words(
        ["not_windows", "metatrader5_package_missing", "not_connected", "brand_new_code"]
    )

    # No raw codes in the sentence ...
    for code in ("not_windows", "metatrader5_package_missing", "not_connected"):
        assert code not in said
    # ... each condition names the step that fixes it ...
    assert "step 1:" in said and "step 3:" in said
    # ... and a condition with no known step is still named, with its underscores
    # read as spaces rather than dropped -- and with no step invented for it.
    assert "brand new code" in said
    assert "step" not in said.split(";")[-1]
    assert outstanding_words([]) == ""


def test_a_blocker_keeps_the_sentence_its_author_wrote():
    """The catalog's blockers carry ``detail``; only ``message`` was read."""

    html = views._blocker_list(
        [
            {"reason": "no_evaluation_on_record", "detail": "Run the evaluation first."},
            {"reason": "paper_test_not_built", "message": "Build it from a record."},
            {"reason": "a_code_with_no_sentence"},
        ]
    )
    text = _text(html)

    # Both shapes of sentence are shown, not just the one with ``message``.
    assert "Run the evaluation first." in text
    assert "Build it from a record." in text
    # And a blocker with no sentence gets a human heading, not a bare token.
    assert "A code with no sentence" in text
    # The code stays available, quietly, so it can be quoted and searched.
    assert "no_evaluation_on_record" in text


# -- long pages, and the navigation that keeps them usable ------------------


def _jump_targets(html: str) -> list[str]:
    match = re.search(r"<nav class='page-jump' aria-label='On this page'>(.*?)</nav>", html, re.S)
    if match is None:
        return []
    return re.findall(r"href='#([^']*)'", match.group(1))


def test_the_autopilot_page_lists_only_the_sections_it_drew(tmp_path):
    """A contents list whose entries land nowhere is worse than none."""

    _autopilot_instance, app = _autopilot(tmp_path, report=True)
    status, html = app.page("/autopilot")

    assert status == 200
    targets = _jump_targets(html)
    assert targets, "the page is long enough to need one"
    # Every entry is a real heading on the page, and the marker is gone.
    for anchor in targets:
        assert f"<h2 id='{anchor}'>" in html, anchor
    assert "<!--jump-->" not in html
    # The first thing it offers to jump to is the control block, not a card
    # that may not exist in another state.
    assert targets[0] == "start-stop"
    # No entry is a heading the page did not draw.
    headings = set(re.findall(r"<h2 id='([^']+)'>", html))
    assert set(targets) == headings


def test_a_short_page_carries_no_contents_list(tmp_path):
    """One section does not need a table of contents."""

    from tests.test_workstation_ui import app_for, simulated_wizard

    html = app_for(simulated_wizard()).page("/live")[1]
    assert "class='page-jump'" not in html


def test_the_quick_setup_page_can_be_scanned_from_the_top(tmp_path):
    from tests.test_catalog_view import _app as _catalog_app
    from tests.test_catalog_panel import _panel as _catalog_panel

    html = _page_for_quick_setup(_catalog_app(_catalog_panel(tmp_path)))
    targets = _jump_targets(html)

    # The paper test is a section, not an unheaded strip of counters wedged
    # between two cards.
    assert "paper-test" in targets
    assert "<h2 id='paper-test'>Paper test</h2>" in html
    for anchor in targets:
        assert f"<h2 id='{anchor}'>" in html, anchor


def _page_for_quick_setup(app) -> str:
    return views.render_page(app._context("quick-setup", "Quick setup"))


def test_the_paper_test_counters_sit_inside_their_own_card(tmp_path):
    from tests.test_catalog_view import _app as _catalog_app
    from tests.test_catalog_panel import _panel as _catalog_panel

    html = _page_for_quick_setup(_catalog_app(_catalog_panel(tmp_path)))

    # The card names the source of its numbers, and "Active symbols" is stated
    # once rather than twice under two different headings.
    assert _text(html).count("Active symbols:") == 1
    assert "simulated locally from the terminal's own quotes" in _text(html)


def test_an_unset_capital_says_why_it_is_unset_and_not_the_same_thing_twice(tmp_path):
    """The metric said "not configured"; the reason repeated it word for word."""

    from axiom_forex.paper_runner import accounting

    capital = accounting.read_capital(tmp_path)

    assert capital.configured is False
    assert "no paper starting capital is configured" not in capital.reason
    assert capital.reason == "no capital document exists yet"


def test_the_research_runner_points_at_a_page_by_its_real_route(tmp_path):
    """It said "select one on the strategy library page" and gave no route."""

    from tests.test_quick_setup_ui import app_with_panel

    html = _page_for_quick_setup(app_with_panel())
    text = _text(html)

    assert "/strategies" in text
    # And it says what the runner will not do: choose for you.
    assert "it never picks one for you" in text or "this runner follows that choice" in text


def test_every_health_check_says_a_reason_and_not_a_python_value():
    """The page read "live_bridge_disabled | OK | False", which is a contradiction."""

    from axiom_forex.config.runtime import ForexRuntimeSettings
    from axiom_forex.monitoring.health import build_health

    health = build_health(
        settings=ForexRuntimeSettings(),
        quality_usable=True,
        calendar_open=True,
        quote_fresh=True,
        gate_allowed=None,
        unresolved_orders=0,
        anomalies=(),
        repository_health={"schema_version": "2", "expected_schema_version": "2"},
        conformance_passed=True,
        profile_activation_allowed=True,
    )

    assert health.status == "HEALTHY"
    for check in health.checks:
        detail = str(check["detail"])
        # No bare literals: every line explains the status printed beside it.
        assert detail not in {"True", "False", "None", ""}, check["name"]
        assert detail[0].isalpha(), (check["name"], detail)
    by_name = {check["name"]: check for check in health.checks}
    # The one that read "OK / False" now says which way round it is.
    assert by_name["live_bridge_disabled"]["ok"] is True
    assert by_name["live_bridge_disabled"]["detail"] == "no live path can send an order"
    # A check with no decision yet says so instead of printing "None".
    assert by_name["last_gate_decision"]["detail"] == "no decision has been taken yet"


def test_a_failing_health_check_still_explains_itself():
    from axiom_forex.config.runtime import ForexRuntimeSettings
    from axiom_forex.monitoring.health import build_health
    from dataclasses import replace

    health = build_health(
        settings=replace(ForexRuntimeSettings(), live_bridge_enabled=True),
        quality_usable=False,
        calendar_open=False,
        quote_fresh=False,
        gate_allowed=False,
        unresolved_orders=3,
        anomalies=(),
        repository_health={"schema_version": "1", "expected_schema_version": "2"},
    )

    by_name = {check["name"]: check for check in health.checks}
    assert health.status == "UNSAFE"
    assert by_name["live_bridge_disabled"]["ok"] is False
    assert by_name["live_bridge_disabled"]["detail"] == "a live path is open; it must be closed"
    assert by_name["unresolved_orders"]["detail"] == "3 order(s) left unresolved"
    assert by_name["persistence"]["detail"] == "schema 1 of 2 expected"
