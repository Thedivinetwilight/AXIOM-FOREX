import json
import re
from dataclasses import fields
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.audit.log import sanitize_audit_value
from app.common.types import TradingMode
from app.config.settings import SafetySettings
from fixtures.m1 import EVALUATED_AT, make_environment


FIRST_PARTY_DIRS = ("app", "fixtures", "axiom_forex", "desktop", "scripts")
FORBIDDEN_SNIPPETS = (
    "live_trading=True",
    "allow_live_orders=True",
    "supports_live_orders=True",
    "http://",
    "https://",
    "ws://",
    "wss://",
    "import requests",
    "import httpx",
    "import urllib",
    "import socket",
    "import websockets",
    "import aiohttp",
    "http.client",
    "urllib3",
    "import os",
    "os.environ",
    "urlopen(",
    "requests.",
    "httpx.",
    "urllib.request",
    "socket.",
    "websockets.",
    "aiohttp.",
    "subprocess",
    "os.system",
    "os.getenv",
    "load_dotenv",
    "dotenv",
    "getenv(",
    "eval(",
    "exec(",
    "__import__",
    "BEGIN PRIVATE KEY",
    "UPSTOX",
    "API_TOKEN",
    "DATABASE_URL",
)
SECRET_ASSIGNMENT = re.compile(
    r"^\s*(api_key|access_token|token|secret|password|credential)\s*=",
    re.IGNORECASE | re.MULTILINE,
)


def _first_party_sources():
    root = Path(__file__).resolve().parents[1]
    for directory in FIRST_PARTY_DIRS:
        path = root / directory
        if path.exists():
            yield from path.rglob("*.py")


def test_sanitize_audit_value_redacts_secrets_urls_and_non_finite_values():
    raw = {
        "api_token": "must-not-appear",
        "nested": {
            "password": "must-not-appear",
            "url": "https://example.invalid/path",
            "nan": float("nan"),
            "decimal": Decimal("1.2300"),
            "when": datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc),
            "object": object(),
        },
    }
    clean = sanitize_audit_value(raw)
    assert clean["api_token"] == "[REDACTED]"
    assert clean["nested"]["password"] == "[REDACTED]"
    assert clean["nested"]["url"] == "[URL_REDACTED]"
    assert clean["nested"]["nan"] == "[NON_FINITE]"
    assert clean["nested"]["decimal"] == "1.2300"
    assert clean["nested"]["object"] == "[object]"
    assert "must-not-appear" not in json.dumps(clean)
    json.dumps(clean)


def test_default_safety_settings_are_paper_only_and_choose_no_account_or_strategy():
    settings = SafetySettings()
    assert settings.mode in (TradingMode.RESEARCH, TradingMode.PAPER)
    assert settings.live_trading is False
    assert settings.allow_live_orders is False
    assert settings.broker_mode.value == "LOCAL_PAPER"
    field_names = {field.name for field in fields(SafetySettings)}
    assert field_names.isdisjoint(
        {
            "account_currency",
            "leverage",
            "strategy",
            "broker",
            "data_provider",
            "calendar",
            "cost_model",
        }
    )


def test_first_party_code_has_no_live_adapter_credentials_or_network_calls():
    sources = list(_first_party_sources())
    assert sources
    offenders = []
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for snippet in FORBIDDEN_SNIPPETS:
            if snippet in text:
                offenders.append(f"{path}:{snippet}")
        if SECRET_ASSIGNMENT.search(text):
            offenders.append(f"{path}:secret_assignment")
    assert offenders == [], "unsafe first-party constructs: " + ", ".join(offenders)


def test_gate_audit_events_are_json_safe_for_allow_and_reject():
    allowed_env = make_environment()
    allowed = allowed_env.gate().evaluate(allowed_env.request, allowed_env.context())
    assert allowed.allowed
    json.dumps(allowed.audit_event)

    rejected_env = make_environment()
    rejected_request = replace(
        rejected_env.request, target_mode=TradingMode.LIVE
    )
    rejected = rejected_env.gate().evaluate(rejected_request, rejected_env.context())
    assert not rejected.allowed
    json.dumps(rejected.audit_event)
    json.dumps(rejected_env.audit_log.last())


def test_pyproject_keeps_runtime_dependency_free():
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert "dependencies = []" in text
    assert "requires-python = \">=3.11\"" in text
