"""Installing Kronos from the page: one action, official sources, mocked bytes.

Every download in this file is a test double.  Nothing contacts Hugging Face,
GitHub, or a terminal, and no weight file is fetched.  What is pinned here:

* the recommended setup is the smallest official variant, and its three sources are
  the registry's own ids and the verified code commit;
* nothing is fetched while a page is rendered, on import, or at startup;
* a check reads sizes and revisions only, and a download needs a fresh check;
* the size, destination, free space, storage, internet use and licence are on the
  page before the download button does anything;
* a digest mismatch, a cancelled download, and a failed download all leave a
  retryable state and no installed manifest;
* cleanup removes only the AXIOM-managed folder;
* the install cannot change an execution mode, arm anything, or place an order;
* the first forecast defaults to the first verified symbol from Account & symbols
  and the symbol and timeframe can be changed without reloading the model.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from axiom_forex.kronos.install import (
    CODE_ARCHIVE,
    FAILED,
    INSTALLED,
    KronosInstaller,
    AssetPlan,
    FileFact,
    FetchResult,
    InstallRefused,
    human_bytes,
    recommended_assets,
)
from axiom_forex.kronos.panel import KronosPanel
from axiom_forex.kronos.registry import (
    RECOMMENDED_VARIANT,
    SOURCE_COMMIT_FULL,
    SOURCE_REPO,
)
from axiom_forex.kronos.settings import KronosSettings
from axiom_forex.workstation.app import POST_ACTIONS
from tests.test_workstation_kronos_page import app_with_kronos, panel
from tests.test_workstation_ui import render

# ---------------------------------------------------------------------------
# doubles: a machine, a source, and a download
# ---------------------------------------------------------------------------


@dataclass
class FakeAvailability:
    python: str = "3.12.4"
    platform: str = "Windows-10-10.0.19045"
    torch_present: bool = True
    cuda_available: bool | None = False
    cuda_devices: int = 0
    device_names: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    note: str = ""


def hub_payload(repo_id: str) -> dict[str, bytes]:
    """The files a Hub snapshot of this repo would hold, as small stand-ins."""

    return {
        "config.json": b'{"model": "%s"}' % repo_id.encode(),
        "model.safetensors": hashlib.sha256(repo_id.encode()).digest() * 8,
    }


class FakeFetcher:
    """A download that never leaves the machine.  Records what was asked for."""

    def __init__(
        self,
        *,
        fail_on: str = "",
        cancel_on: str = "",
        corrupt: bool = False,
        describe_error: str = "",
        revision: str = "c0ffee" + "0" * 34,
    ) -> None:
        self.fail_on = fail_on
        self.cancel_on = cancel_on
        self.corrupt = corrupt
        self.describe_error = describe_error
        self.revision = revision
        self.described: list[str] = []
        self.fetched: list[str] = []
        self.bytes_fetched = 0
        self.on_fetch = None  # a hook a test can use to cancel mid-install

    def describe(self, asset, revision: str = "") -> AssetPlan:
        self.described.append(asset.key)
        if self.describe_error:
            raise InstallRefused(self.describe_error, "the source did not answer", "Try again.")
        if asset.kind == CODE_ARCHIVE:
            return AssetPlan(asset=asset, revision=asset.revision, bytes=2048)
        payload = hub_payload(asset.repo_id)
        files = tuple(
            FileFact(path=name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            for name, data in payload.items()
        )
        return AssetPlan(
            asset=asset,
            revision=revision or self.revision,
            files=files,
            bytes=sum(item.bytes for item in files),
        )

    def fetch(self, asset, plan, destination, *, progress, cancelled) -> FetchResult:
        self.fetched.append(asset.key)
        if self.on_fetch is not None:
            self.on_fetch()
        if self.cancel_on == asset.key:
            # A download that notices the operator's cancel before writing anything.
            raise InstallRefused("cancelled")
        if self.fail_on == asset.key:
            raise InstallRefused("sources_unavailable", "the connection dropped", "Try again.")
        destination.mkdir(parents=True, exist_ok=True)
        files: list[FileFact] = []
        if asset.kind == CODE_ARCHIVE:
            (destination / "model").mkdir(parents=True, exist_ok=True)
            source = "# official Kronos predictor\n"
            (destination / "model" / "kronos.py").write_text(source)
            files.append(
                FileFact(
                    path="model/kronos.py",
                    bytes=len(source),
                    sha256=hashlib.sha256(source.encode()).hexdigest(),
                )
            )
        else:
            for fact in plan.files:
                data = hub_payload(asset.repo_id)[fact.path]
                if self.corrupt:
                    data = b"tampered"
                (destination / fact.path).write_bytes(data)
                files.append(
                    FileFact(
                        path=fact.path,
                        bytes=len(data),
                        sha256=hashlib.sha256(data).hexdigest(),
                    )
                )
                progress(len(data))
                self.bytes_fetched += len(data)
        return FetchResult(
            revision=plan.revision,
            files=tuple(files),
            bytes=sum(item.bytes for item in files),
        )


def make_installer(tmp_path: Path, fetcher: FakeFetcher | None = None, **kwargs) -> KronosInstaller:
    params = dict(
        fetcher=fetcher if fetcher is not None else FakeFetcher(),
        availability=lambda: FakeAvailability(),
        start_threads=kwargs.pop("start_threads", False),
    )
    params.update(kwargs)
    return KronosInstaller(tmp_path, **params)


def installable_panel(tmp_path: Path, *, fetcher: FakeFetcher | None = None, symbols=()) -> tuple[KronosPanel, FakeFetcher]:
    fake = fetcher if fetcher is not None else FakeFetcher()
    instance = panel(tmp_path)
    instance.install_system = make_installer(tmp_path, fake)
    instance.view(symbols=list(symbols))
    return instance, fake


# ---------------------------------------------------------------------------
# 1. official sources, smallest variant, nothing fetched on its own
# ---------------------------------------------------------------------------


def test_the_recommended_setup_is_the_smallest_official_variant():
    from axiom_forex.kronos.registry import selectable_variants, variant

    spec = variant(RECOMMENDED_VARIANT)
    assert spec is not None and spec.open_weights is True
    smallest = min(
        (item for item in selectable_variants() if item.open_weights),
        key=lambda item: item.parameters,
    )
    assert spec.key == smallest.key


def test_the_recommended_setup_names_three_official_sources():
    assets = recommended_assets(RECOMMENDED_VARIANT)
    keys = [item.key for item in assets]
    assert keys == ["code", "model", "tokenizer"]
    by_key = {item.key: item for item in assets}
    assert by_key["code"].repo_id == SOURCE_REPO
    assert by_key["code"].revision == SOURCE_COMMIT_FULL
    assert len(SOURCE_COMMIT_FULL) == 40
    assert by_key["model"].repo_id.startswith("NeoQuasar/")
    assert by_key["tokenizer"].repo_id.startswith("NeoQuasar/")
    # One model, one tokenizer, one code tree: nothing else is ever fetched.
    assert {item.kind for item in assets} == {"OFFICIAL_CODE_ARCHIVE", "OFFICIAL_HUB_SNAPSHOT"}


def test_a_variant_without_public_weights_cannot_be_installed():
    with pytest.raises(InstallRefused) as caught:
        recommended_assets("kronos-large")
    assert caught.value.reason == "variant_not_installable"


def test_rendering_the_page_fetches_nothing(tmp_path):
    instance, fake = installable_panel(tmp_path)
    assert fake.described == [] and fake.fetched == []
    view = instance.view(symbols=["EURUSD"])
    assert view["install"]["installed"] is False
    assert view["install"]["plan"]["destination"].endswith(
        str(Path("data") / "kronos" / RECOMMENDED_VARIANT)
    )
    # A plan is an estimate until a check reads the real sizes.
    assert view["install"]["plan"]["exact"] is False
    assert fake.fetched == []


# ---------------------------------------------------------------------------
# 2. the check shows size and destination; only Install downloads
# ---------------------------------------------------------------------------


def test_the_check_reports_exact_size_and_free_space_without_downloading(tmp_path):
    instance, fake = installable_panel(tmp_path)
    outcome = instance.act("kronos-install-check")

    assert outcome["ok"] is True
    assert "Nothing has been downloaded" in outcome["message"]
    assert fake.fetched == []
    plan = instance.installer().view()["plan"]
    assert plan["exact"] is True
    assert plan["download_bytes"] == sum(
        len(part) for asset in recommended_assets(RECOMMENDED_VARIANT) if asset.key != "code"
        for part in hub_payload(asset.repo_id).values()
    ) + 2048
    assert plan["destination"].startswith(str(tmp_path))
    assert instance.act("kronos-install-check")["ok"] is True


def test_install_without_a_fresh_check_checks_first_and_downloads_nothing(tmp_path):
    instance, fake = installable_panel(tmp_path)
    outcome = instance.act("kronos-install")

    assert outcome["ok"] is True
    assert "Pressed Install" not in outcome["message"]
    assert fake.fetched == []
    assert "Nothing has been downloaded" in outcome["message"]
    assert instance.installer().view()["job"]["state"] == "NOT_INSTALLED"


def test_a_stale_check_never_starts_a_download(tmp_path):
    ticks = [0.0]

    class Clock:
        def now(self):
            from datetime import datetime, timedelta, timezone

            return datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc) + timedelta(seconds=ticks[0])

    instance, fake = installable_panel(tmp_path)
    instance.install_system = make_installer(tmp_path, fake, clock=Clock().now, start_threads=False)
    assert instance.act("kronos-install-check")["ok"] is True
    ticks[0] = 3600  # an hour later: the sizes and revisions may have moved

    outcome = instance.act("kronos-install")

    assert fake.fetched == []
    assert "Nothing has been downloaded" in outcome["message"]


def test_not_enough_disk_space_is_reported_before_anything_is_fetched(tmp_path):
    instance, fake = installable_panel(tmp_path)
    instance.install_system = make_installer(tmp_path, fake, free_bytes=lambda _path: 1024)

    outcome = instance.act("kronos-install-check")

    assert outcome["ok"] is False
    assert outcome["reason"] == "not_enough_disk_space"
    assert "not enough free space" in outcome["message"]
    assert fake.fetched == []
    assert instance.act("kronos-install")["ok"] is False
    assert fake.fetched == []


def test_missing_runtime_packages_are_reported_and_never_downloaded(tmp_path):
    instance, fake = installable_panel(tmp_path)
    instance.install_system = make_installer(
        tmp_path,
        fake,
        availability=lambda: FakeAvailability(torch_present=False, missing=("torch>=2.0.0",)),
    )

    outcome = instance.act("kronos-install-check")

    assert outcome["ok"] is False
    assert outcome["reason"] == "runtime_packages_missing"
    assert "torch>=2.0.0" in outcome["message"]
    assert "does not install software" in outcome["message"]
    assert fake.fetched == []


def test_a_source_that_does_not_answer_is_reported_not_guessed(tmp_path):
    instance, _fake = installable_panel(tmp_path)
    instance.install_system = make_installer(
        tmp_path, FakeFetcher(describe_error="sources_unavailable")
    )

    outcome = instance.act("kronos-install-check")

    assert outcome["ok"] is False
    assert outcome["reason"] == "sources_unavailable"
    assert "Nothing was downloaded" in outcome["message"]


# ---------------------------------------------------------------------------
# 3. the download: paths, revisions, digests, progress, cancel, cleanup
# ---------------------------------------------------------------------------


def installed_panel(tmp_path: Path, **kwargs):
    instance, fake = installable_panel(tmp_path, **kwargs)
    assert instance.act("kronos-install-check")["ok"] is True
    outcome = instance.act("kronos-install")
    assert outcome["ok"] is True, outcome["message"]
    return instance, fake


def test_a_successful_install_records_paths_revisions_and_digests(tmp_path):
    instance, fake = installed_panel(tmp_path)
    manifest = json.loads((tmp_path / "kronos_install.local.json").read_text())

    assert manifest["state"] == INSTALLED
    assert manifest["model_key"] == RECOMMENDED_VARIANT
    assert manifest["revisions"]["code"] == SOURCE_COMMIT_FULL
    assert manifest["revisions"]["model"] == fake.revision
    assert manifest["revisions"]["tokenizer"] == fake.revision
    assert manifest["paths"]["model"].endswith("model")
    assert manifest["paths"]["code"].endswith("code")
    assert manifest["bytes"] > 0
    for records in manifest["files"].values():
        assert records and all(len(item["sha256"]) == 64 for item in records)
    # Everything landed under the managed folder, and nowhere else.
    managed = tmp_path / "data" / "kronos"
    for name in ("code", "model", "tokenizer"):
        assert (managed / RECOMMENDED_VARIANT / name).is_dir()
    assert (managed / RECOMMENDED_VARIANT / "code" / "model" / "kronos.py").is_file()


def test_the_install_writes_its_paths_into_the_settings_so_they_are_remembered(tmp_path):
    instance, _fake = installed_panel(tmp_path)
    saved = json.loads((tmp_path / "kronos_settings.local.json").read_text())["settings"]

    assert saved["model_path"].endswith("model")
    assert saved["tokenizer_path"].endswith("tokenizer")
    assert saved["code_path"].endswith("code")
    assert saved["revision"] == "c0ffee" + "0" * 34
    assert saved["model_key"] == RECOMMENDED_VARIANT


def test_a_custom_path_under_advanced_settings_is_never_overwritten(tmp_path):
    instance, _fake = installable_panel(tmp_path)
    instance.save({"symbol": ["EURUSD"], "model_path": ["D:/my/kronos/model"]})
    instance.act("kronos-install-check")
    instance.act("kronos-install")

    assert instance.settings().model_path == "D:/my/kronos/model"
    assert instance.settings().code_path.endswith("code")


def test_a_digest_mismatch_aborts_the_install_and_writes_no_manifest(tmp_path):
    instance, fake = installable_panel(tmp_path, fetcher=FakeFetcher(corrupt=True))
    assert instance.act("kronos-install-check")["ok"] is True
    outcome = instance.act("kronos-install")

    assert outcome["ok"] is False
    assert outcome["reason"] == "digest_mismatch"
    assert not (tmp_path / "kronos_install.local.json").exists()
    assert instance.installer().installed() is None
    assert instance.installer().view()["job"]["state"] == FAILED


def test_a_failed_download_leaves_a_retryable_state_and_a_cleanup_route(tmp_path):
    instance, fake = installable_panel(tmp_path, fetcher=FakeFetcher(fail_on="tokenizer"))
    assert instance.act("kronos-install-check")["ok"] is True

    outcome = instance.act("kronos-install")

    assert outcome["ok"] is False
    hint = instance.installer().view()["job"]["hint"]
    assert "retry" in hint.lower() and "Clean up" in hint
    assert (tmp_path / "data" / "kronos" / RECOMMENDED_VARIANT).exists() is False  # staged, not moved
    assert not (tmp_path / "kronos_install.local.json").exists()

    # Retry with a working source finishes the job.
    instance.install_system = make_installer(tmp_path, FakeFetcher())
    assert instance.act("kronos-install-check")["ok"] is True
    assert instance.act("kronos-install")["ok"] is True
    assert instance.installer().installed() is not None


def test_cancelling_stops_the_download_and_records_cancelled(tmp_path):
    instance, fake = installable_panel(tmp_path)
    instance.install_system = make_installer(
        tmp_path,
        FakeFetcher(cancel_on="model"),
        start_threads=False,
        logger=None,
    )
    assert instance.act("kronos-install-check")["ok"] is True

    # Cancel is only offered while a download runs; the synchronous double lets the
    # test press it at the moment the second asset starts.
    job_holder = {}

    def press_cancel() -> None:
        if job_holder.get("done"):
            return
        job_holder["done"] = True
        instance.installer().cancel()

    fake.on_fetch = press_cancel
    outcome = instance.act("kronos-install")

    assert outcome["ok"] is False
    assert instance.installer().view()["job"]["state"] == "CANCELLED"
    assert not (tmp_path / "kronos_install.local.json").exists()


def test_cleanup_removes_only_the_managed_folder(tmp_path):
    instance, _fake = installed_panel(tmp_path)
    outside = tmp_path / "keep-me.txt"
    outside.write_text("mine")

    outcome = instance.act("kronos-install-cleanup")

    assert outcome["ok"] is True
    assert outside.read_text() == "mine"
    assert not (tmp_path / "data" / "kronos" / RECOMMENDED_VARIANT).exists()
    assert not (tmp_path / "kronos_install.local.json").exists()
    saved = json.loads((tmp_path / "kronos_settings.local.json").read_text())["settings"]
    assert saved["model_path"] == ""
    assert saved["code_path"] == ""


def test_cleanup_stops_a_model_that_was_loaded_from_the_installed_files(tmp_path):
    instance, _fake = installed_panel(tmp_path)
    instance.load()
    assert instance.runtime().view()["state"] in {"READY", "FAILED"}

    instance.act("kronos-install-cleanup")

    assert instance.runtime().view()["state"] != "READY"


def test_progress_is_reported_while_a_download_runs(tmp_path):
    instance, fake = installable_panel(tmp_path)
    assert instance.act("kronos-install-check")["ok"] is True
    job = instance.installer()._job
    job.state = "DOWNLOADING"
    job.bytes_total = 1000
    job.bytes_done = 250
    job.step, job.steps, job.asset_label = 1, 3, "Kronos-mini weights"

    view = instance.installer().view()["job"]

    assert view["percent"] == 25.0
    assert view["step"] == 1 and view["steps"] == 3
    assert "Kronos-mini weights" in view["asset_label"]


def test_a_threaded_install_reports_a_running_state_and_finishes(tmp_path):
    instance, fake = installable_panel(tmp_path)
    job = instance.installer()._job
    # A job started with threads must be observable while it runs.
    instance.install_system = make_installer(tmp_path, fake, start_threads=True)
    instance.install_system.check(RECOMMENDED_VARIANT)
    outcome = instance.install_system.install(RECOMMENDED_VARIANT)
    assert outcome["ok"] is True
    deadline = time.time() + 10
    while time.time() < deadline and instance.install_system.view()["job"]["running"]:
        time.sleep(0.01)
    assert instance.install_system.installed() is not None
    assert job.state in {"NOT_INSTALLED", "DOWNLOADING", "INSTALLED"}


# ---------------------------------------------------------------------------
# 4. the page: one clear action, the explanation, and collapsed advanced bits
# ---------------------------------------------------------------------------


def test_the_page_shows_one_install_action_in_this_sandbox(tmp_path):
    app = app_with_kronos(tmp_path)
    html = render(app, "/kronos")

    assert "Install the recommended setup" in html
    assert "value='kronos-install'" in html or "action='/actions/kronos-install'" in html
    assert "Check this machine and the download" in html
    # The old "find these three folders yourself" flow is gone from the top.
    assert "no automatic download" not in html
    # Nothing invites an order.
    assert "order_send" not in html and "Send order" not in html


def test_the_page_explains_storage_internet_and_licence_before_downloading(tmp_path):
    app = app_with_kronos(tmp_path)
    text = render(app, "/kronos")

    for phrase in ("Storage.", "Internet.", "Licence.", "nothing downloaded until you press Install"):
        assert phrase in text, phrase
    assert "MIT" in text
    assert "UNVERIFIED_BY_AXIOM" in text
    assert "data/kronos" in text or "data\\kronos" in text


def test_advanced_settings_are_collapsed_and_hold_variants_paths_and_revisions(tmp_path):
    app = app_with_kronos(tmp_path)
    html = render(app, "/kronos")

    start = html.index("<details class='advanced'>")
    summary = html.index("Advanced settings", start)
    end = html.index("</details>", start)
    inside = html[summary:end]
    for name in ("model_key", "model_path", "tokenizer_path", "revision", "code_path", "device"):
        assert f"name='{name}'" in inside, name
    # The forecast controls are not buried in the collapsed section.
    assert "name='timeframe'" in html[:start]


# ---------------------------------------------------------------------------
# 5. first forecast: default symbol, changing it, and forecast-only
# ---------------------------------------------------------------------------


def test_the_first_forecast_defaults_to_the_first_verified_selected_symbol(tmp_path):
    instance, _fake = installable_panel(tmp_path, symbols=("EURUSD.a", "GBPUSD"))
    instance._settings = KronosSettings(symbol="", lookback=120, pred_len=4, rollouts=2)

    view = instance.view(symbols=["EURUSD.a", "GBPUSD"])

    assert view["settings"]["symbol"] == "EURUSD.a"
    assert view["symbol_choices"] == ["EURUSD.a", "GBPUSD"]
    assert view["symbol_default"] == "EURUSD.a"


def test_installation_never_waits_for_a_symbol(tmp_path):
    instance, fake = installable_panel(tmp_path, symbols=())
    instance._settings = KronosSettings(symbol="", lookback=120, pred_len=4, rollouts=2)

    assert instance.act("kronos-install-check")["ok"] is True
    assert instance.act("kronos-install")["ok"] is True
    assert instance.installer().installed() is not None
    assert fake.fetched == ["code", "model", "tokenizer"]


def test_the_symbol_and_timeframe_can_be_changed_without_reloading(tmp_path):
    instance, _fake = installable_panel(tmp_path, symbols=("EURUSD", "GBPUSD"))
    assert instance.act("kronos-install-check")["ok"] is True
    assert instance.act("kronos-install")["ok"] is True
    loaded = instance.load()
    assert loaded["ok"] is True, loaded["message"]
    assert instance.runtime().view()["state"] == "READY"

    outcome = instance.act(
        "kronos-forecast", {"symbol": "GBPUSD", "timeframe": "15m", "pred_len": "4"}
    )

    assert outcome["ok"] is True, outcome["message"]
    assert instance.settings().symbol == "GBPUSD"
    assert instance.settings().timeframe == "15m"
    assert instance.runtime().view()["state"] == "READY"  # no reload happened
    stored = instance.store().recent(limit=1)[0]
    assert stored["symbol"] == "GBPUSD" and stored["timeframe"] == "15m"


def test_a_symbol_outside_the_selected_set_is_refused(tmp_path):
    instance, _fake = installable_panel(tmp_path, symbols=("EURUSD",))
    instance._settings = KronosSettings(symbol="EURUSD", lookback=120, pred_len=4, rollouts=2)
    instance.load()

    outcome = instance.act("kronos-forecast", {"symbol": "XAUUSD", "timeframe": "1m"})

    assert outcome["ok"] is False
    assert "not in the provider setup's declared symbols" in outcome["message"]


def test_the_install_page_cannot_change_mode_arm_or_trade(tmp_path):
    from tests.test_workstation_ui import app_for, simulated_wizard

    app = app_for(simulated_wizard())
    app.kronos = panel(tmp_path)
    app.kronos.install_system = make_installer(tmp_path, FakeFetcher())
    mode_before = app.mode_view()
    connects = app.wizard.session.connect_count

    for action in ("kronos-install-check", "kronos-install"):
        app.act(action, {"nonce": [app.nonce]})

    assert app.wizard.session.connect_count == connects
    assert app.mode_view()["mode"] == mode_before["mode"]
    for name in ("kronos-install", "kronos-install-check", "kronos-install-cancel", "kronos-install-cleanup"):
        assert name in POST_ACTIONS
        for forbidden in ("order", "trade", "arm", "mode", "risk", "live", "signal"):
            assert forbidden not in name


def test_bytes_are_rendered_as_units_not_raw_counts():
    assert human_bytes(0) == "0 B"
    assert human_bytes(1536) == "1.5 KB"
    assert human_bytes(None) == "unknown"


# ---------------------------------------------------------------------------
# 6. the official archive is unpacked defensively (no network involved)
# ---------------------------------------------------------------------------


def _write_tarball(path: Path, members: dict[str, bytes], *, prefix: str = "Kronos-abc123") -> Path:
    import io
    import tarfile

    with tarfile.open(path, "w:gz") as bundle:
        for name, payload in members.items():
            data = io.BytesIO(payload)
            info = tarfile.TarInfo(name=f"{prefix}/{name}" if prefix else name)
            info.size = len(payload)
            bundle.addfile(info, data)
    return path


def test_the_official_archive_is_unpacked_into_the_managed_folder(tmp_path):
    from axiom_forex.kronos.install import _extract_official_tree

    archive = _write_tarball(
        tmp_path / "source.tar.gz",
        {"model/kronos.py": b"# predictor\n", "LICENSE": b"MIT\n"},
    )
    destination = tmp_path / "incoming" / "code"

    files = _extract_official_tree(archive, destination)

    assert (destination / "model" / "kronos.py").read_text() == "# predictor\n"
    assert (destination / "LICENSE").read_text() == "MIT\n"
    assert {item.path for item in files} == {"model/kronos.py", "LICENSE"}
    assert all(len(item.sha256) == 64 for item in files)


def test_an_archive_without_the_predictor_is_refused(tmp_path):
    from axiom_forex.kronos.install import _extract_official_tree

    archive = _write_tarball(tmp_path / "source.tar.gz", {"README.md": b"hello\n"})

    with pytest.raises(InstallRefused) as caught:
        _extract_official_tree(archive, tmp_path / "incoming" / "code")

    assert caught.value.reason == "official_code_incomplete"
    assert not (tmp_path / "incoming" / "code").exists()


def test_an_archive_that_escapes_its_tree_is_refused(tmp_path):
    from axiom_forex.kronos.install import _extract_official_tree

    archive = _write_tarball(
        tmp_path / "source.tar.gz",
        {"model/kronos.py": b"# predictor\n"},
        prefix="",
    )
    # Rewrite one member name to a parent traversal.
    import tarfile

    hostile = tmp_path / "hostile.tar.gz"
    with tarfile.open(archive, "r:gz") as source, tarfile.open(hostile, "w:gz") as target:
        for member in source.getmembers():
            if member.name.endswith("kronos.py"):
                member.name = "../escape/kronos.py"
            target.addfile(member, source.extractfile(member) if member.isfile() else None)

    with pytest.raises(InstallRefused) as caught:
        _extract_official_tree(hostile, tmp_path / "incoming" / "code")

    assert caught.value.reason == "official_code_archive_unsafe"
    assert not (tmp_path / "escape").exists()


def test_the_code_fetcher_refuses_without_torch_rather_than_hiding_it(tmp_path, monkeypatch):
    from axiom_forex.kronos.install import CodeFetcher

    fetcher = CodeFetcher()
    asset = recommended_assets(RECOMMENDED_VARIANT)[0]
    plan = fetcher.describe(asset)

    def no_torch():
        raise ImportError("torch is not installed")

    import sys

    monkeypatch.setitem(sys.modules, "torch", None)
    with pytest.raises(InstallRefused) as caught:
        fetcher.fetch(
            asset,
            plan,
            tmp_path / "code",
            progress=lambda _count: None,
            cancelled=lambda: False,
        )
    assert caught.value.reason == "runtime_packages_missing"
    assert "torch>=2.0.0" in caught.value.hint


# ---------------------------------------------------------------------------
# 7. the page itself: one install control, a forecast control, safe copy
# ---------------------------------------------------------------------------


def test_the_page_offers_install_and_forecast_without_asking_for_paths(tmp_path):
    from tests.test_workstation_kronos_page import app_with_kronos
    from tests.test_workstation_ui import parse, render

    instance, _fetcher = installable_panel(tmp_path)
    app = app_with_kronos(tmp_path)
    app.kronos = instance

    body = parse(render(app, "/kronos")).text_content

    assert "Install the recommended setup" in body
    assert "Run one forecast" in body
    assert "Installs into" in body
    assert "Free on this machine" in body
    # Never the old "point the page at these folders" workflow, and never a
    # filesystem path the user has to produce themselves.
    assert "Type the path" not in body
    assert "Repository path" not in body
