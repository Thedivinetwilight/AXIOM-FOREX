"""Offline verification and reporting commands; no server or external calls."""

from __future__ import annotations

import argparse
import json
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
