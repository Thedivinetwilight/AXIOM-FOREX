"""Offline verification and reporting commands; no server or external calls."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Sequence

from app.audit.log import sanitize_audit_value

from .brokers.mt5 import (
    DEMO_ONLY,
    SELECTED_ACCOUNT,
    MT5ModuleUnavailable,
    MT5SessionCycle,
    MT5SessionRefused,
    MT5SettingsFileError,
    SessionPolicy,
    import_mt5,
    load_settings_file,
    make_mt5_adapter,
    make_password_store,
)
from .config.vault import default_vault
from .facade import build_fixture_platform
from .evaluation.stress import load_stress_scenarios
from .ingest import ImportRequest
from .strategies.examples import SmaCrossStrategy


def _instant(value: str | None) -> datetime:
    """Parse a required CLI instant; missing values stay missing on purpose."""

    if not value:
        return None  # type: ignore[return-value]
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _run_evaluation(platform: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Import the requested canonical CSVs, then run the research evaluation.

    The evaluation is research-only: it never creates an order, never touches
    the paper order stream, and never enables external execution.  Pairs that
    are not covered by the platform's declared calendar and correlation terms
    are reported as blocked rather than guessed.
    """

    if not args.import_paths:
        raise SystemExit("--import-paths is required for the evaluate command")
    if not args.import_data_class:
        raise SystemExit("--import-data-class is required for the evaluate command")
    paths = [item.strip() for item in args.import_paths.split(",") if item.strip()]
    pairs = [
        item.strip() for item in str(args.import_pairs or "").split(",") if item.strip()
    ]
    if pairs and len(pairs) != len(paths):
        raise SystemExit("--import-pairs must align with --import-paths")
    for index, path in enumerate(paths):
        pair = pairs[index] if pairs else (args.import_pair or platform.pair)
        request = ImportRequest(
            pair=pair,
            timeframe=args.import_timeframe or platform.manifest.timeframe,
            data_class=args.import_data_class,
            source=args.import_source or "",
            source_version=args.import_source_version or "",
            valid_from=_instant(args.import_valid_from),
            valid_until=_instant(args.import_valid_until),
            documented_at=args.import_documented_at,
        )
        result = platform.import_dataset(request, path=path)
        if not result.passed:
            return {
                "status": "BLOCKED",
                "reason": "import_failed:" + str(result.reason),
                "import": result.audit_view(),
                "note": "Research evaluation did not run; the import failed closed.",
            }
    quantities = (
        {
            pair: Decimal(args.evaluate_quantity)
            for pair in {
                item.pair for item in platform.research_evaluation_datasets()
            }
        }
        if args.evaluate_quantity
        else None
    )
    config = platform.make_research_evaluation_config(quantities=quantities)
    report = platform.research_evaluation(config=config)
    payload = platform.research_evaluation_summary()
    payload["markdown_available"] = bool(platform.research_evaluation_markdown())
    payload["evaluation_reason"] = report.reason

    if args.stress_scenarios:
        # Scenarios are explicit operator input; nothing is invented here.
        scenarios = load_stress_scenarios(args.stress_scenarios)
        suite = platform.research_evaluation_stress(
            scenarios, config=config, baseline=report
        )
        payload["stress"] = platform.research_evaluation_stress_summary()
        payload["stress_status"] = suite.status
        payload["stress_reasons"] = list(suite.reasons)
    return payload


def _run_mt5(args: argparse.Namespace) -> int:
    """Local MT5 settings actions.  No terminal connection, no order.

    The password is accepted only from an interactive prompt: there is no CLI
    flag, environment variable, or file path that can carry it, so it cannot
    end up in shell history, a process list, or a log.
    """

    try:
        settings = load_settings_file(args.mt5_settings_path)
    except MT5SettingsFileError as exc:
        print(json.dumps({"status": "BLOCKED", "reason": exc.reason, "detail": exc.detail}, indent=2))
        return 2
    if settings is None:
        print(
            json.dumps(
                {
                    "status": "NOT_CONFIGURED",
                    "settings_path": "<local-settings-file>",
                    "reason": "mt5_settings_file_missing",
                    "next": (
                        "Create the local settings document (terminal_path, server, "
                        "account_login, mode) and run `set-password`."
                    ),
                    "password_storage": "os_credential_vault",
                    "plaintext_fallback": False,
                },
                indent=2,
            )
        )
        return 0
    store = make_password_store(settings, vault=default_vault())
    try:
        if args.mt5_action == "set-password":
            import getpass

            first = getpass.getpass("MT5 terminal password (not echoed): ")
            second = getpass.getpass("Repeat to confirm: ")
            if first != second:
                print(json.dumps({"status": "REFUSED", "reason": "password_confirmation_mismatch"}))
                return 2
            payload = store.save(first)
        elif args.mt5_action == "clear-password":
            payload = store.clear()
        else:
            payload = store.status()
    except Exception as exc:  # SecureStorageUnavailable, MT5SettingsError
        print(
            json.dumps(
                {
                    "status": "REFUSED",
                    "reason": getattr(exc, "reason", type(exc).__name__),
                    "detail": getattr(exc, "detail", ""),
                    "plaintext_fallback": False,
                },
                indent=2,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "status": "OK",
                "mt5": settings.audit_view(),
                "password": sanitize_audit_value(payload),
                "token_required_by_broker": False,
                "note": "No terminal connection was made and no order was sent.",
            },
            indent=2,
            default=str,
        )
    )
    return 0


def _mt5_refusal(payload: dict[str, Any], code: int) -> int:
    print(json.dumps({"status": "BLOCKED", **payload, "order_sent": False}, indent=2, default=str))
    return code


def _run_mt5_session(args: argparse.Namespace) -> int:
    """One bounded MT5 session cycle.  Sends nothing unless every gate passes.

    Configuration is never invented here: this repository ships a *synthetic*
    fixture catalog and no approved live strategy, so the cycle runs, reports
    the account mode, and states exactly what blocked trading instead of
    constructing a pair, size, or order.  A deployment that has a real catalog
    and an approved strategy injects them through the programmatic
    :class:`~axiom_forex.brokers.mt5.session.MT5SessionCycle` API.
    """

    try:
        settings = load_settings_file(args.mt5_settings_path)
    except MT5SettingsFileError as exc:
        return _mt5_refusal({"reason": exc.reason, "detail": exc.detail}, 2)
    if settings is None:
        return _mt5_refusal(
            {
                "reason": "mt5_settings_file_missing",
                "next": "Create the local settings document and run `mt5 --mt5-action set-password`.",
            },
            2,
        )
    policy = SessionPolicy(
        account_mode_policy=args.mt5_account_mode_policy,
        max_orders=args.mt5_max_orders,
        max_duration_seconds=args.mt5_duration_seconds,
    )
    try:
        policy.validate()
    except MT5SessionRefused as exc:
        return _mt5_refusal({"reason": exc.reason, "detail": exc.detail}, 2)

    try:
        module = import_mt5()
    except MT5ModuleUnavailable as exc:
        return _mt5_refusal(
            {
                "reason": exc.reason,
                "detail": exc.detail,
                "next": (
                    "Install the official MetaTrader5 package in the Windows interpreter "
                    "that runs AXIOM-FOREX, on the same machine as the logged-in MT5 "
                    "terminal.  No connection was attempted, nothing was armed, and no "
                    "order was sent."
                ),
            },
            3,
        )

    from .database.audit import DurableAuditLog
    from .database.repository import ForexRepository

    repository = ForexRepository(args.database_path)
    audit_log = DurableAuditLog(repository=repository)
    try:
        try:
            adapter = make_mt5_adapter(
                settings=settings,
                password_store=make_password_store(settings, audit_log=audit_log),
                repository=repository,
                audit_log=audit_log,
                module=module,
                lifecycle_lookup=None,
            )
        except Exception as exc:
            return _mt5_refusal(
                {"reason": getattr(exc, "reason", type(exc).__name__)}, 2
            )
        cycle = MT5SessionCycle(
            adapter,
            policy=policy,
            spec=None,
            strategy_id=args.mt5_strategy_id,
            lifecycle=None,
            gate=None,
            request_factory=None,
            context_factory=None,
            signal_source=None,
        )
        report = cycle.run()
        payload = {
            "status": "COMPLETED",
            "session": report.audit_view(),
            "order_sent": report.orders_sent > 0,
            "note": (
                "Bounded cycle: the session is disarmed and disconnected afterwards.  "
                "Account identifiers, balances, equity, and credentials are not reported."
            ),
        }
        print(json.dumps(payload, indent=2, default=str))
        return 0
    finally:
        repository.close()


def _run_live_run(args: argparse.Namespace) -> int:
    """Run the approved strategy against the live MT5 path, for real.

    This is the operator's live entry point, and it is deliberately explicit: the
    settings document, the provider setup, the pair, the candidate, and the exact
    arming phrase must all be given.  Without the phrase nothing is armed; without
    an armed adapter the runner refuses.  The process never arms itself and never
    changes the execution mode as a side effect.
    """

    from axiom_forex.brokers.mt5.catalogue import SymbolRequest, build_catalogue
    from axiom_forex.config.provider_setup import load_provider_setup
    from axiom_forex.paper_runner.live_run import (
        LiveRunConfig,
        LiveRunRefused,
        run_live_session,
    )

    for flag, value in (
        ("--mt5-settings-path", args.mt5_settings_path),
        ("--provider-setup", args.provider_setup),
        ("--paper-runner-pair", args.paper_runner_pair),
        ("--paper-runner-candidate", args.paper_runner_candidate),
        ("--confirm", args.confirm),
    ):
        if not value:
            print(f"live-run: {flag} is required", file=sys.stderr)
            return 2

    try:
        settings = load_settings_file(args.mt5_settings_path)
    except MT5SettingsFileError as exc:
        return _mt5_refusal({"reason": exc.reason, "detail": exc.detail}, 2)
    if settings is None:
        return _mt5_refusal({"reason": "mt5_settings_file_missing"}, 2)
    try:
        module = import_mt5()
    except MT5ModuleUnavailable as exc:
        return _mt5_refusal(
            {
                "reason": exc.reason,
                "detail": exc.detail,
                "next": (
                    "Live execution needs the official MetaTrader5 package on the "
                    "Windows machine that runs the logged-in terminal.  Nothing was "
                    "connected, armed, or sent."
                ),
            },
            3,
        )

    from .database.audit import DurableAuditLog
    from .database.repository import ForexRepository

    repository = ForexRepository(args.database_path)
    audit_log = DurableAuditLog(repository=repository)
    adapter = None
    try:
        setup = load_provider_setup(args.provider_setup)
        try:
            adapter = make_mt5_adapter(
                settings=settings,
                password_store=make_password_store(settings, audit_log=audit_log),
                repository=repository,
                audit_log=audit_log,
                module=module,
                lifecycle_lookup=None,
            )
            adapter.connect()
            adapter.arm_trading(
                account_login=settings.account_login,
                server=settings.server,
                confirm=args.confirm,
            )
        except Exception as exc:
            return _mt5_refusal(
                {
                    "reason": getattr(exc, "reason", type(exc).__name__),
                    "detail": getattr(exc, "detail", ""),
                    "note": "Nothing was sent. Arming is refused unless the phrase "
                    "matches the selected account exactly.",
                },
                3,
            )

        terminal = adapter.terminal
        symbol_inputs = []
        for terms in setup.symbols:
            symbol_inputs.append(
                SymbolRequest(
                    pair=terms.pair,
                    pip_size=terms.pip_size,
                    calendar_id=terms.calendar_id,
                    calendar_version=terms.calendar_version,
                    correlation_group=terms.correlation_group,
                    margin_multiplier=terms.margin_multiplier,
                    spec_version=terms.spec_version,
                    broker_symbol=terms.broker_symbol,
                    suffix=terms.suffix,
                    source=terms.source,
                )
            )
        catalogue = build_catalogue(terminal, symbol_inputs)
        resolved = catalogue.resolved_for(args.paper_runner_pair)
        if resolved is None:
            refusal = catalogue.refusal_for(args.paper_runner_pair)
            return _mt5_refusal(
                {
                    "reason": getattr(refusal, "reason", "symbol_unresolved"),
                    "detail": getattr(refusal, "detail", ""),
                    "catalogue": catalogue.audit_view(),
                    "note": "The symbol is never guessed. Fix the setup's symbol "
                    "terms (or the terminal's Market Watch) and try again.",
                },
                3,
            )
        try:
            report = run_live_session(
                adapter=adapter,
                terminal=terminal,
                setup=setup,
                config=LiveRunConfig(
                    pair=args.paper_runner_pair,
                    candidate_id=args.paper_runner_candidate,
                    quantity=Decimal(str(args.paper_runner_quantity or setup.execution.quantity)),
                    max_cycles=max(int(args.paper_runner_max_cycles), 1),
                    interval_seconds=max(int(args.paper_runner_interval), 1),
                ),
                repository=repository,
                resolved=resolved,
                catalogue=catalogue,
                audit_log=audit_log,
                lifecycle=None,
            )
        except LiveRunRefused as exc:
            return _mt5_refusal({"reason": exc.reason, "detail": exc.detail}, 3)
        payload = {
            "status": "COMPLETED",
            "live_run": report,
            "note": (
                "Live orders were possible in this run. Nothing is retried: an unknown "
                "outcome stays unresolved until reconciliation. Account identifiers, "
                "balances, equity, and credentials are not reported."
            ),
        }
        print(json.dumps(sanitize_audit_value(payload), indent=2, default=str))
        return 0
    finally:
        if adapter is not None:
            try:
                adapter.disarm(reason="live_run_finished")
            except Exception:
                pass
            try:
                adapter.disconnect(reason="live_run_finished")
            except Exception:
                pass
        repository.close()


def _run_convert_ticks(args: argparse.Namespace) -> int:
    """Convert a broker tick export into the canonical CSV, with provenance."""

    from axiom_forex.ingest.mt5_ticks import TickExportRefused, convert

    if not args.ticks_path:
        print("convert-ticks: --ticks-path is required", file=sys.stderr)
        return 2
    if not args.ticks_pair:
        print("convert-ticks: --ticks-pair is required", file=sys.stderr)
        return 2
    try:
        report = convert(
            source=args.ticks_path,
            pair=args.ticks_pair,
            output=args.ticks_output,
            server_offset_minutes=args.ticks_server_offset_minutes,
            server_timezone=args.ticks_server_timezone,
            interval_seconds=max(int(args.ticks_interval_seconds), 1),
        )
    except TickExportRefused as exc:
        print(
            json.dumps(
                {"converted": False, "reason": exc.reason, "detail": exc.detail},
                indent=2,
            )
        )
        return 3
    print(json.dumps({"converted": True, **report}, default=str, indent=2, sort_keys=True))
    # A gap is not an error, but it is never silent: importing a partial export is
    # the operator's decision, and the coverage block above is how they see it.
    return 0


def _run_data_audit(args: argparse.Namespace) -> int:
    """Report what history exists here, without downloading or inventing any."""

    from .paper_runner import data_audit

    repository = None
    platform = None
    try:
        platform = build_fixture_platform(database_path=args.database_path)
        repository = platform.repository
        report = data_audit.audit(repository=repository, directory=args.import_dir)
    finally:
        if platform is not None:
            platform.close()
    if args.evaluate_markdown:
        print(data_audit.markdown(report), end="")
    else:
        print(json.dumps(sanitize_audit_value(report), sort_keys=True, indent=2))
    return 0


def _run_paper_evaluate(platform: Any, args: argparse.Namespace) -> int:
    """Evaluate the candidate grid on one imported dataset, once.

    The locked test window is spent here and recorded, so the same dataset cannot
    be re-evaluated later with a better-looking answer.  The output is evidence
    about a historical sample, not a profitability claim.
    """

    from .paper_runner import evaluation
    from .paper_runner.strategy import candidate_family, status_of

    if not args.import_path:
        raise SystemExit("--import-path is required for paper-evaluate")
    if not args.import_data_class:
        raise SystemExit("--import-data-class is required for paper-evaluate")
    request = ImportRequest(
        pair=args.import_pair or platform.pair,
        timeframe=args.import_timeframe or platform.manifest.timeframe,
        data_class=args.import_data_class,
        source=args.import_source or "",
        source_version=args.import_source_version or "",
        valid_from=_instant(args.import_valid_from),
        valid_until=_instant(args.import_valid_until),
        documented_at=args.import_documented_at,
    )
    imported = platform.import_dataset(request, path=args.import_path)
    if not imported.passed or imported.dataset is None:
        print(
            json.dumps(
                {
                    "imported": False,
                    "reason": "import_failed",
                    "note": "Nothing was evaluated: the import did not pass.",
                },
                indent=2,
            )
        )
        return 2
    dataset = imported.dataset
    dataset_id = dataset.import_manifest.import_id
    if evaluation.locked_test_consumed(repository=platform.repository, dataset_id=dataset_id):
        print(
            json.dumps(
                {
                    "evaluated": False,
                    "reason": "locked_test_already_consumed",
                    "dataset_id": dataset_id,
                    "note": (
                        "This dataset's locked test window has already been spent. "
                        "Re-running it would turn the test set into a tuning set."
                    ),
                },
                indent=2,
            )
        )
        return 3
    if not dataset.usable:
        print(
            json.dumps(
                {
                    "evaluated": False,
                    "reason": "dataset_quality_not_usable",
                    "dataset_id": dataset_id,
                    "quality": dataset.quality.audit_view(),
                },
                default=str,
                indent=2,
            )
        )
        return 4
    results = []
    test_window_spent = False
    for item in candidate_family():
        outcome = evaluation.evaluate_candidate(
            engine_factory=platform.new_engine,
            candidate=item,
            bars=dataset.bars,
            data_class=dataset.data_class,
            dataset_id=dataset_id,
        )
        evaluation.record_evaluation(
            repository=platform.repository, payload=outcome.payload
        )
        test_window_spent = test_window_spent or bool(
            outcome.payload.get("locked_test_consumed")
        )
        results.append(
            {
                "candidate_id": item.candidate_id,
                "status": status_of(item.candidate_id, outcome.payload),
                "passed": outcome.payload["passed"],
                "failed_criteria": outcome.payload["failed_criteria"],
                "locked_test": outcome.payload["metrics"].get("locked_test", {}),
                "walk_forward": outcome.payload.get("walk_forward_summary", {}),
                "baselines": outcome.payload.get("baselines", {}),
            }
        )
    if test_window_spent:
        # The window is only spent when a run actually walked into it: a sample
        # too short to have a test window leaves the dataset still usable once.
        evaluation.claim_locked_test(
            repository=platform.repository,
            dataset_id=dataset_id,
            at=datetime.now(timezone.utc).isoformat(),
        )
    print(
        json.dumps(
            {
                "dataset_id": dataset_id,
                "data_class": dataset.data_class,
                "locked_test_spent": test_window_spent,
                "bars": len(dataset.bars),
                "criteria": evaluation.DEFAULT_CRITERIA.audit_view(),
                "assumptions": list(evaluation.ASSUMPTIONS),
                "results": results,
                "note": (
                    "Historical evidence only. No result here is a promise of future "
                    "returns, and nothing was sent to a broker."
                ),
            },
            default=str,
            sort_keys=True,
            indent=2,
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="axiom-forex",
        description="Repository-local AXIOM-FOREX research verification",
    )
    parser.add_argument(
        "command",
        choices=(
            "verify",
            "health",
            "backtest",
            "status",
            "report",
            "workflow",
            "conformance",
            "profile",
            "import",
            "evaluate",
            "mt5",
            "mt5-session",
            "data-audit",
            "paper-evaluate",
            "convert-ticks",
            "live-run",
        ),
    )
    parser.add_argument("--database-path", default=":memory:")
    parser.add_argument("--bar-count", type=int, default=40)
    parser.add_argument("--workflow-id", default=None)
    parser.add_argument("--profile-path", default=None)
    parser.add_argument("--import-path", default=None)
    parser.add_argument("--import-pair", default=None)
    parser.add_argument("--import-timeframe", default=None)
    parser.add_argument("--import-data-class", default=None)
    parser.add_argument("--import-source", default=None)
    parser.add_argument("--import-source-version", default=None)
    parser.add_argument("--import-documented-at", default="")
    parser.add_argument("--import-valid-from", default=None)
    parser.add_argument("--import-valid-until", default=None)
    parser.add_argument("--import-backtest", action="store_true")
    parser.add_argument("--import-paths", default=None, help="comma-separated canonical CSVs")
    parser.add_argument(
        "--import-pairs",
        default=None,
        help="comma-separated pairs aligned with --import-paths",
    )
    parser.add_argument(
        "--import-dir",
        default="imports",
        help="directory the data audit reads (never written to)",
    )
    parser.add_argument(
        "--mt5-settings-path",
        default="mt5_settings.local.json",
        help="local, non-secret MT5 settings document",
    )
    parser.add_argument(
        "--mt5-action",
        default="status",
        choices=("status", "set-password", "clear-password"),
        help="local MT5 settings action; the password is read from a prompt, never an argument",
    )
    parser.add_argument(
        "--mt5-max-orders",
        type=int,
        default=1,
        help="orders this bounded session cycle may submit (0 or 1)",
    )
    parser.add_argument(
        "--mt5-duration-seconds", type=float, default=30.0, help="session cycle budget"
    )
    parser.add_argument(
        "--mt5-account-mode-policy",
        default=SELECTED_ACCOUNT,
        choices=(SELECTED_ACCOUNT, DEMO_ONLY),
        help=(
            "SELECTED_ACCOUNT (default) uses whichever account the MT5 terminal has "
            "selected; DEMO_ONLY is an explicit opt-in that refuses to arm unless the "
            "account is positively demo"
        ),
    )
    parser.add_argument("--mt5-strategy-id", default=None)
    parser.add_argument("--evaluate-markdown", action="store_true")
    parser.add_argument(
        "--ticks-path",
        default=None,
        help="MetaTrader 5 tick export to convert into the canonical quote-bar CSV",
    )
    parser.add_argument(
        "--ticks-output",
        default=None,
        help="where to write the canonical CSV (default: next to the export)",
    )
    parser.add_argument("--ticks-pair", default=None, help="canonical pair of the export")
    parser.add_argument(
        "--ticks-server-offset-minutes",
        type=int,
        default=None,
        help=(
            "the broker server clock's offset from UTC in minutes, exactly as you "
            "read it in the terminal; there is no default"
        ),
    )
    parser.add_argument(
        "--ticks-server-timezone",
        default="",
        help="IANA zone instead of a fixed offset (ambiguous local times are refused)",
    )
    parser.add_argument(
        "--ticks-interval-seconds",
        type=int,
        default=60,
        help="bar interval to aggregate the ticks into",
    )
    parser.add_argument(
        "--provider-setup",
        default=None,
        help="the operator's provider setup document (axiom-provider-setup-v1)",
    )
    parser.add_argument(
        "--paper-runner-pair",
        default=None,
        help="the catalogue pair a live run may trade (required, never defaulted)",
    )
    parser.add_argument(
        "--paper-runner-candidate",
        default=None,
        help="the approved candidate id to run (required, never defaulted)",
    )
    parser.add_argument(
        "--paper-runner-quantity",
        default=None,
        help="size per order in the setup's lots (defaults to the setup's own quantity)",
    )
    parser.add_argument(
        "--paper-runner-max-cycles",
        type=int,
        default=1,
        help="how many runner cycles a live run may take before it stops",
    )
    parser.add_argument(
        "--paper-runner-interval",
        type=int,
        default=60,
        help="bar interval in seconds for the live feed",
    )
    parser.add_argument(
        "--confirm",
        default=None,
        help=(
            'the exact arming phrase, e.g. "ENABLE LIVE TRADING 55500123"; a live run '
            "refuses to arm without it"
        ),
    )
    parser.add_argument(
        "--stress-scenarios",
        default=None,
        help="JSON document of explicitly configured stress scenarios",
    )
    parser.add_argument(
        "--evaluate-quantity",
        default=None,
        help="research size per pair (defaults to each spec's minimum quantity)",
    )
    args = parser.parse_args(argv)
    if args.command == "data-audit":
        return _run_data_audit(args)
    if args.command == "convert-ticks":
        return _run_convert_ticks(args)
    if args.command == "live-run":
        return _run_live_run(args)
    if args.command == "mt5":
        return _run_mt5(args)
    if args.command == "mt5-session":
        return _run_mt5_session(args)
    platform = build_fixture_platform(
        database_path=args.database_path,
        bar_count=args.bar_count,
        provider_profile_path=args.profile_path,
    )
    try:
        if args.command == "verify":
            payload: Any = platform.verify()
        elif args.command == "health":
            payload = platform.health().audit_view()
        elif args.command == "status":
            payload = platform.status()
        elif args.command == "report":
            print(platform.report_markdown(), end="")
            return 0
        elif args.command == "workflow":
            result = platform.run_paper_workflow(workflow_id=args.workflow_id)
            payload = result.audit_view()
        elif args.command == "conformance":
            payload = platform.conformance_report()
        elif args.command == "profile":
            payload = platform.provider_profile_report()
        elif args.command == "import":
            request = ImportRequest(
                pair=args.import_pair or platform.pair,
                timeframe=args.import_timeframe or platform.manifest.timeframe,
                data_class=args.import_data_class or "",
                source=args.import_source or "",
                source_version=args.import_source_version or "",
                valid_from=_instant(args.import_valid_from),
                valid_until=_instant(args.import_valid_until),
                documented_at=args.import_documented_at,
            )
            if not args.import_path:
                raise SystemExit("--import-path is required for the import command")
            result = platform.import_dataset(request, path=args.import_path)
            payload = result.audit_view()
            if args.import_backtest and result.passed and result.dataset is not None:
                backtest = platform.run_imported_backtest(
                    SmaCrossStrategy(
                        fast_period=2, slow_period=3, name="imported-dataset-smoke"
                    ),
                    run_id="imported-dataset-smoke",
                )
                payload["imported_backtest"] = {
                    "run_id": backtest.run_id,
                    "usable": backtest.usable,
                    "quality": backtest.quality.classification,
                    "trades": len(backtest.trades),
                    "dataset_id": backtest.config.dataset_id,
                    "execution_eligible": False,
                    "note": "Historical simulation is not a profitability claim.",
                }
        elif args.command == "paper-evaluate":
            return _run_paper_evaluate(platform, args)
        elif args.command == "evaluate":
            payload = _run_evaluation(platform, args)
            if args.evaluate_markdown:
                for markdown in (
                    platform.research_evaluation_markdown(),
                    platform.research_evaluation_stress_markdown(),
                ):
                    if markdown:
                        print(markdown, end="")
        else:
            strategy = SmaCrossStrategy(
                fast_period=3, slow_period=8, name="cli-fixture"
            )
            result = platform.run_backtest(strategy)
            payload = {
                "run_id": result.run_id,
                "usable": result.usable,
                "quality": result.quality.audit_view(),
                "trades": len(result.trades),
                "rejections": len(result.rejections),
                "net_pnl_account": result.net_pnl_account,
                "cost_total_account": result.cost_total_account,
                "rollover_total_account": result.rollover_total_account,
                "final_bar_signal_cancelled": result.final_bar_signal_cancelled,
                "open_positions": len(result.open_positions),
                "note": "Historical simulation is not a profitability claim.",
            }
        print(json.dumps(sanitize_audit_value(payload), sort_keys=True, indent=2))
        return 0
    finally:
        platform.close()


if __name__ == "__main__":
    raise SystemExit(main())
