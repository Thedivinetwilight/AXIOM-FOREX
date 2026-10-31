"""One explicit install, into a folder AXIOM owns, from official sources only.

The page used to ask for three paths and a revision before it would do anything.
This module replaces that with a single, reversible action:

**What it installs.**  The recommended variant (:data:`RECOMMENDED_VARIANT`, the
smallest official model with public weights), its official tokenizer repository,
and the official source tree at a pinned commit.  Nothing else, from nowhere else:
the repository ids and the commit come from :mod:`axiom_forex.kronos.registry`,
which records where each fact was read.

**Where it goes.**  ``<root>/data/kronos/<variant>/`` -- ``code/``, ``model/`` and
``tokenizer/`` -- with a manifest beside the launcher
(``kronos_install.local.json``, Git-ignored) recording the paths, the resolved
revisions, the byte count and the SHA-256 of every file.  The operator never types
a path, and the panel writes the recorded paths into its own settings so they are
remembered.

**When it happens.**  Only when the operator presses Install.  Nothing here runs
on import, on startup, or on a page render.  A *check* first reads the sources'
metadata (sizes and the resolved commit) so the download size and the free space
can be shown before a byte is fetched; then the button that says Install starts
the download on a worker thread, reports progress, and can be cancelled.

**How it is verified.**  The code tree is fetched at the pinned commit.  Every
downloaded file is hashed, and the weights are compared with the digest the Hub
reports for that revision; a mismatch aborts the install and writes no manifest.
A Hub revision is pinned when it is first resolved and re-checked afterwards --
AXIOM will not invent a Hub commit id it has never read.

**What it cannot do.**  It cannot trade, change an execution mode, arm anything, or
reach the broker.  The only things it writes are files under the managed folder and
one manifest beside the launcher.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import tempfile
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .registry import (
    CODE_LICENCE,
    CODE_LICENCE_COPYRIGHT,
    CODE_REVISION_POLICY,
    HUB_REVISION_POLICY,
    RECOMMENDED_REASON,
    RECOMMENDED_VARIANT,
    SOURCE_COMMIT_FULL,
    SOURCE_COMMIT_VERIFIED_AT,
    SOURCE_REPO,
    WEIGHTS_HOST,
    WEIGHTS_LICENCE_STATE,
    code_archive_url,
    variant,
)

INSTALL_SCHEMA = "axiom-kronos-install-v1"
INSTALL_FILENAME = "kronos_install.local.json"
#: Everything AXIOM installs lives here, under the launcher's own folder.
MANAGED_DIRNAME = "kronos"
INCOMING_DIRNAME = ".incoming"
CODE_SUBDIR = "code"
MODEL_SUBDIR = "model"
TOKENIZER_SUBDIR = "tokenizer"

#: A check is a snapshot of the sources.  After this long it has to be repeated,
#: because a Hub revision can move and the free space certainly can.
CHECK_TTL_SECONDS = 900

#: Install lifecycle.  ``DOWNLOADING`` is the only state with a worker behind it.
NOT_INSTALLED = "NOT_INSTALLED"
CHECKED = "CHECKED"
DOWNLOADING = "DOWNLOADING"
INSTALLED = "INSTALLED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

#: Asset kinds.  One is a Hub snapshot (weights + configs), the other the official
#: repository archive at a pinned commit.
HUB_SNAPSHOT = "OFFICIAL_HUB_SNAPSHOT"
CODE_ARCHIVE = "OFFICIAL_CODE_ARCHIVE"

#: Weight files a usable model folder must contain, and the config it must have.
WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pth", ".pt")

REASON_DISK = "not_enough_disk_space"
REASON_CHECK_REQUIRED = "check_required"
REASON_SOURCES = "sources_unavailable"
REASON_DIGEST = "digest_mismatch"
REASON_INCOMPLETE = "download_incomplete"
REASON_CANCELLED = "cancelled"
REASON_BUSY = "install_already_running"
REASON_PACKAGES = "runtime_packages_missing"
REASON_ARCHIVE = "official_code_archive_unsafe"
REASON_NO_SOURCE_TREE = "official_code_incomplete"
REASON_WRITE = "destination_not_writable"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def managed_root(root: str | Path) -> Path:
    """The one folder AXIOM installs into.  Nothing outside it is ever written."""

    return Path(root) / "data" / MANAGED_DIRNAME


def install_manifest_path(root: str | Path) -> Path:
    return Path(root) / INSTALL_FILENAME


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _free_bytes(path: Path) -> int | None:
    """Free space on the volume that holds ``path``, walking up to something real."""

    candidate = Path(path)
    while True:
        try:
            return int(shutil.disk_usage(str(candidate)).free)
        except FileNotFoundError:
            parent = candidate.parent
            if parent == candidate:
                return None
            candidate = parent
        except OSError:
            return None


def human_bytes(count: int | float | None) -> str:
    if count is None:
        return "unknown"
    value = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


@dataclass(frozen=True)
class FileFact:
    """One file the install will fetch, or did fetch."""

    path: str
    bytes: int
    sha256: str = ""

    def audit_view(self) -> dict[str, Any]:
        return {"path": self.path, "bytes": self.bytes, "sha256": self.sha256}


@dataclass(frozen=True)
class InstallAsset:
    """One official source, and where its bytes land."""

    key: str
    label: str
    kind: str
    repo_id: str
    subdir: str
    revision: str = ""
    revision_policy: str = HUB_REVISION_POLICY

    def audit_view(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "repo_id": self.repo_id,
            "subdir": self.subdir,
            "revision": self.revision,
            "revision_policy": self.revision_policy,
        }


@dataclass(frozen=True)
class AssetPlan:
    """What a check learned about one asset.  ``bytes`` is exact when known."""

    asset: InstallAsset
    revision: str
    files: tuple[FileFact, ...] = ()
    bytes: int | None = None
    note: str = ""

    def audit_view(self) -> dict[str, Any]:
        return {
            "key": self.asset.key,
            "label": self.asset.label,
            "repo_id": self.asset.repo_id,
            "revision": self.revision,
            "bytes": self.bytes,
            "files": len(self.files),
            "note": self.note,
        }


@dataclass(frozen=True)
class FetchResult:
    """What a fetcher produced: files on disk, and the revision it fetched."""

    revision: str
    files: tuple[FileFact, ...]
    bytes: int = 0


class InstallRefused(Exception):
    """A refusal with an operator-facing reason.  Never a traceback on the page."""

    def __init__(self, reason: str, detail: str = "", hint: str = "") -> None:
        self.reason = reason
        self.detail = detail
        self.hint = hint
        super().__init__(reason if not detail else f"{reason}: {detail}")

    def audit_view(self) -> dict[str, str]:
        return {"reason": self.reason, "detail": self.detail, "hint": self.hint}


# ---------------------------------------------------------------------------
# the official sources
# ---------------------------------------------------------------------------


def recommended_assets(model_key: str = RECOMMENDED_VARIANT) -> tuple[InstallAsset, ...]:
    """The three official sources for one variant.  Never a fourth."""

    spec = variant(model_key)
    if spec is None or not spec.open_weights:
        raise InstallRefused(
            "variant_not_installable",
            f"{model_key} has no official public weights",
            "Choose one of the variants with public weights, or install the recommended one.",
        )
    return (
        InstallAsset(
            key="code",
            label="Official Kronos source at the pinned commit",
            kind=CODE_ARCHIVE,
            repo_id=SOURCE_REPO,
            subdir=CODE_SUBDIR,
            revision=SOURCE_COMMIT_FULL,
            revision_policy=CODE_REVISION_POLICY,
        ),
        InstallAsset(
            key="model",
            label=f"{spec.name} weights",
            kind=HUB_SNAPSHOT,
            repo_id=spec.model_id,
            subdir=MODEL_SUBDIR,
        ),
        InstallAsset(
            key="tokenizer",
            label=f"{spec.name} tokenizer ({spec.tokenizer_id.split('/')[-1]})",
            kind=HUB_SNAPSHOT,
            repo_id=spec.tokenizer_id,
            subdir=TOKENIZER_SUBDIR,
        ),
    )


def estimated_weights_bytes(model_key: str = RECOMMENDED_VARIANT) -> int:
    """A published parameter count times fp32, labelled as an estimate everywhere."""

    spec = variant(model_key)
    if spec is None:
        return 0
    return int(spec.parameters) * 4


# ---------------------------------------------------------------------------
# fetchers
# ---------------------------------------------------------------------------


class HubFetcher:
    """Official Hub snapshots, fetched with the package the project requires.

    ``huggingface_hub`` is imported inside the methods and only when an install is
    actually running: importing it on a page render would be pointless work, and
    this module must stay importable on a machine without it.
    """

    def describe(self, asset: InstallAsset, revision: str = "") -> AssetPlan:
        try:
            from huggingface_hub import HfApi
        except Exception as exc:
            raise InstallRefused(
                REASON_PACKAGES,
                "huggingface_hub is not installed",
                "Install the official requirements line huggingface_hub==0.33.1, then press "
                "Install again.",
            ) from exc
        try:
            info = HfApi().model_info(
                asset.repo_id, revision=revision or None, files_metadata=True
            )
        except Exception as exc:
            raise InstallRefused(
                REASON_SOURCES,
                f"the Hub did not answer for {asset.repo_id}",
                "Check this machine's internet connection and the Hub's own status, "
                "then press Check again. Nothing was downloaded.",
            ) from exc
        revision = str(getattr(info, "sha", "") or "")
        if not revision:
            raise InstallRefused(
                REASON_SOURCES,
                f"the Hub returned no commit for {asset.repo_id}",
                "AXIOM pins a commit; without one it will not download. Try the check again.",
            )
        files = tuple(
            FileFact(
                path=str(getattr(sibling, "rfilename", "") or ""),
                bytes=int(getattr(sibling, "size", 0) or 0),
                sha256=_lfs_sha256(sibling),
            )
            for sibling in getattr(info, "siblings", ()) or ()
            if str(getattr(sibling, "rfilename", "") or "")
        )
        total = sum(item.bytes for item in files)
        return AssetPlan(
            asset=asset,
            revision=revision,
            files=files,
            bytes=total,
            note=(
                "Only this repository, at the commit shown; the file list and sizes "
                "are the Hub's own."
            ),
        )

    def fetch(
        self,
        asset: InstallAsset,
        plan: AssetPlan,
        destination: Path,
        *,
        progress: Callable[[int], None],
        cancelled: Callable[[], bool],
    ) -> FetchResult:
        try:
            from huggingface_hub import hf_hub_download
        except Exception as exc:
            raise InstallRefused(
                REASON_PACKAGES,
                "huggingface_hub is not installed",
                "Install the official requirements first, then press Install again.",
            ) from exc
        destination.mkdir(parents=True, exist_ok=True)
        fetched: list[FileFact] = []
        for fact in plan.files:
            if cancelled():
                raise InstallRefused(REASON_CANCELLED)
            try:
                hf_hub_download(
                    repo_id=asset.repo_id,
                    filename=fact.path,
                    revision=plan.revision,
                    local_dir=str(destination),
                )
            except Exception as exc:
                raise InstallRefused(
                    REASON_SOURCES,
                    f"{fact.path} could not be downloaded",
                    "Nothing was recorded as installed. Check the connection, then press "
                    "Install again to retry.",
                ) from exc
            target = destination / fact.path
            if not target.is_file():
                raise InstallRefused(
                    REASON_INCOMPLETE,
                    f"{fact.path} is missing after the download",
                    "Press Clean up, then Install again.",
                )
            size = int(target.stat().st_size)
            digest = _sha256_file(target)
            if fact.sha256 and digest != fact.sha256:
                raise InstallRefused(
                    REASON_DIGEST,
                    f"{fact.path} does not match the digest the Hub reports",
                    "Nothing was recorded as installed. Clean up the partial download and "
                    "try again; if it repeats, the source has changed.",
                )
            fetched.append(FileFact(path=fact.path, bytes=size, sha256=digest))
            progress(size)
        return FetchResult(
            revision=plan.revision, files=tuple(fetched), bytes=sum(f.bytes for f in fetched)
        )


class CodeFetcher:
    """The official repository archive, at the pinned commit.

    The bytes are pulled by ``torch.hub`` -- the same runtime the model needs, and a
    downloader that ships with it -- so AXIOM's own modules contain no network
    client.  The archive is unpacked with a member-by-member check: no absolute
    paths, no parent traversal, no links, no devices.
    """

    def describe(self, asset: InstallAsset, revision: str = "") -> AssetPlan:
        return AssetPlan(
            asset=asset,
            revision=SOURCE_COMMIT_FULL,
            files=(),
            bytes=None,
            note=(
                "The official repository archive at the pinned commit. Its exact size "
                "is reported by the server at download time; it is a source tree of a "
                "few hundred kilobytes, not weights."
            ),
        )

    def fetch(
        self,
        asset: InstallAsset,
        plan: AssetPlan,
        destination: Path,
        *,
        progress: Callable[[int], None],
        cancelled: Callable[[], bool],
    ) -> FetchResult:
        if cancelled():
            raise InstallRefused(REASON_CANCELLED)
        try:
            from torch.hub import download_url_to_file
        except Exception as exc:
            raise InstallRefused(
                REASON_PACKAGES,
                "torch is not installed, so the code archive cannot be fetched",
                "Install the official requirements line torch>=2.0.0, then press Install "
                "again. Inference needs torch anyway.",
            ) from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        archive = destination.parent / ".kronos-source.tar.gz"
        try:
            download_url_to_file(code_archive_url(), str(archive), progress=False)
        except Exception as exc:
            raise InstallRefused(
                REASON_SOURCES,
                "the official code archive could not be downloaded",
                "Check the connection, then press Install again. Nothing was recorded "
                "as installed.",
            ) from exc
        if cancelled():
            raise InstallRefused(REASON_CANCELLED)
        progress(int(archive.stat().st_size))
        files = _extract_official_tree(archive, destination)
        try:
            archive.unlink()
        except OSError:
            pass
        return FetchResult(
            revision=SOURCE_COMMIT_FULL, files=files, bytes=sum(item.bytes for item in files)
        )


class CompositeFetcher:
    """Route each official asset to the fetcher that can fetch it."""

    def __init__(self, hub: Any | None = None, code: Any | None = None) -> None:
        self._hub = hub if hub is not None else HubFetcher()
        self._code = code if code is not None else CodeFetcher()

    def for_asset(self, asset: InstallAsset) -> Any:
        return self._code if asset.kind == CODE_ARCHIVE else self._hub


def _lfs_sha256(sibling: Any) -> str:
    lfs = getattr(sibling, "lfs", None)
    if lfs is None:
        return ""
    for name in ("sha256", "oid"):
        value = getattr(lfs, name, None)
        if value:
            return str(value)
    return ""


def _extract_official_tree(archive: Path, destination: Path) -> tuple[FileFact, ...]:
    """Unpack the official archive, refusing anything that could escape the folder.

    The archive is unpacked beside the destination, checked to contain exactly one
    source tree with ``model/kronos.py``, and only then moved into place.  A member
    with an absolute path, a parent reference, a link, or a device node refuses the
    whole archive: the install never unpacks something it would have to trust.
    """

    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f".extract-{destination.name}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            members = []
            for member in bundle.getmembers():
                name = member.name
                parts = Path(name).parts
                if member.issym() or member.islnk() or member.isdev():
                    raise InstallRefused(
                        REASON_ARCHIVE,
                        "the archive contains a link or device entry",
                        "The official archive is a plain source tree; this one is not. "
                        "Nothing was installed.",
                    )
                if Path(name).is_absolute() or ".." in parts:
                    raise InstallRefused(
                        REASON_ARCHIVE,
                        "the archive contains a path outside its own tree",
                        "Nothing was installed. Try again, and do not use this archive.",
                    )
                members.append(member)
            try:
                bundle.extractall(staging, members=members, filter="data")
            except TypeError:  # Python 3.11 before the PEP 706 backport
                bundle.extractall(staging, members=members)
    except InstallRefused:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except (tarfile.TarError, OSError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise InstallRefused(
            REASON_ARCHIVE,
            "the official code archive could not be unpacked",
            "Nothing was installed. Press Install again to retry.",
        ) from exc

    roots = [item for item in staging.iterdir() if item.is_dir()]
    if len(roots) != 1:
        shutil.rmtree(staging, ignore_errors=True)
        raise InstallRefused(
            REASON_ARCHIVE,
            "the archive did not contain a single source tree",
            "Nothing was installed.",
        )
    source = roots[0]
    if not (source / "model" / "kronos.py").is_file():
        shutil.rmtree(staging, ignore_errors=True)
        raise InstallRefused(
            REASON_NO_SOURCE_TREE,
            "the archive holds no model/kronos.py",
            "This build imports the predictor from that file; without it nothing was "
            "installed.",
        )
    if destination.exists():
        shutil.rmtree(destination, ignore_errors=True)
    shutil.move(str(source), str(destination))
    shutil.rmtree(staging, ignore_errors=True)
    files: list[FileFact] = []
    for item in sorted(destination.rglob("*")):
        if not item.is_file():
            continue
        files.append(
            FileFact(
                path=str(item.relative_to(destination)),
                bytes=int(item.stat().st_size),
                sha256=_sha256_file(item),
            )
        )
    return tuple(files)


# ---------------------------------------------------------------------------
# the installer
# ---------------------------------------------------------------------------


@dataclass
class _Job:
    """The one download that can be running.  Never more than one."""

    thread: threading.Thread | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    state: str = NOT_INSTALLED
    reason: str = ""
    detail: str = ""
    hint: str = ""
    asset: str = ""
    asset_label: str = ""
    step: int = 0
    steps: int = 0
    bytes_done: int = 0
    bytes_total: int | None = None
    started_at: str = ""
    finished_at: str = ""

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def view(self, destination: str) -> dict[str, Any]:
        percent: float | None = None
        if self.bytes_total:
            percent = round(100.0 * min(self.bytes_done, self.bytes_total) / self.bytes_total, 1)
        return {
            "state": self.state,
            "running": self.running,
            "percent": percent,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "bytes_done_human": human_bytes(self.bytes_done),
            "bytes_total_human": human_bytes(self.bytes_total) if self.bytes_total else "",
            "asset": self.asset,
            "asset_label": self.asset_label,
            "step": self.step,
            "steps": self.steps,
            "reason": self.reason,
            "detail": self.detail,
            "hint": self.hint,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "destination": destination,
            "cancelled": bool(self.cancel.is_set()),
        }


class KronosInstaller:
    """Installs the recommended setup once, on request, into AXIOM's own folder."""

    def __init__(
        self,
        root: str | Path,
        *,
        fetcher: Any | None = None,
        availability: Callable[[], Any] | None = None,
        free_bytes: Callable[[Path], int | None] | None = None,
        clock: Callable[[], datetime] | None = None,
        logger: Callable[[str], None] | None = None,
        start_threads: bool = True,
    ) -> None:
        self.root = Path(root)
        self._fetcher = fetcher
        self._availability = availability
        self._free = free_bytes or _free_bytes
        self._clock = clock or _utc_now
        self._log = logger or (lambda message: None)
        self._start_threads = start_threads
        self._lock = threading.RLock()
        self._job = _Job()
        self._plan: dict[str, Any] | None = None
        self._asset_plans: tuple[AssetPlan, ...] = ()
        self._plan_at: datetime | None = None
        self._manifest: dict[str, Any] | None = None

    # -- sources ---------------------------------------------------------

    @property
    def fetcher(self) -> Any:
        if self._fetcher is None:
            self._fetcher = CompositeFetcher()
        return self._fetcher

    def fetcher_for(self, asset: InstallAsset) -> Any:
        """The component that fetches this asset.

        A composite fetcher routes by kind; a single fetcher (as a test double can
        be) is used for everything it is given.
        """

        fetcher = self.fetcher
        router = getattr(fetcher, "for_asset", None)
        return router(asset) if callable(router) else fetcher

    def availability(self, *, deep: bool = False) -> Any:
        """What this machine has.  Shallow on a render, deep only on a check.

        A deep probe imports torch to ask about CUDA; that is slow and touches the
        GPU, so a page render never does it.
        """

        if self._availability is not None:
            return self._availability()
        from .runtime import probe_availability

        return probe_availability(deep=deep)

    # -- paths -----------------------------------------------------------

    def destination(self, model_key: str = RECOMMENDED_VARIANT) -> Path:
        return managed_root(self.root) / str(model_key)

    @property
    def manifest_path(self) -> Path:
        return install_manifest_path(self.root)

    def manifest(self, *, refresh: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._manifest is None or refresh:
                self._manifest = self._read_manifest()
            return dict(self._manifest)

    def _read_manifest(self) -> dict[str, Any]:
        path = self.manifest_path
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(payload, dict) or payload.get("schema") != INSTALL_SCHEMA:
            return {}
        return payload

    def _write_manifest(self, payload: Mapping[str, Any]) -> None:
        path = self.manifest_path
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w", prefix=".kronos-install-", dir=str(path.parent), delete=False, encoding="utf-8"
        )
        try:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            handle.close()
            Path(handle.name).replace(path)
        finally:
            leftover = Path(handle.name)
            if leftover.exists():
                leftover.unlink()
        with self._lock:
            self._manifest = dict(payload)

    def installed(self, model_key: str = "") -> dict[str, Any] | None:
        """The recorded install, when it is still on disk and complete."""

        manifest = self.manifest(refresh=True)
        if manifest.get("state") != INSTALLED:
            return None
        key = str(model_key or manifest.get("model_key") or RECOMMENDED_VARIANT)
        if key != str(manifest.get("model_key", "")):
            return None
        paths = dict(manifest.get("paths") or {})
        for name in (CODE_SUBDIR, MODEL_SUBDIR, TOKENIZER_SUBDIR):
            folder = Path(str(paths.get(name, "")))
            if not paths.get(name) or not folder.is_dir():
                return None
        if not (Path(str(paths[CODE_SUBDIR])) / "model" / "kronos.py").is_file():
            return None
        return manifest

    def paths(self, model_key: str = "") -> dict[str, str]:
        """The three paths and the pinned revision, when the install is complete."""

        manifest = self.installed(model_key)
        if manifest is None:
            return {}
        paths = {str(k): str(v) for k, v in (manifest.get("paths") or {}).items()}
        revisions = dict(manifest.get("revisions") or {})
        return {
            "model_path": paths.get(MODEL_SUBDIR, ""),
            "tokenizer_path": paths.get(TOKENIZER_SUBDIR, ""),
            "code_path": paths.get(CODE_SUBDIR, ""),
            "revision": str(revisions.get(MODEL_SUBDIR, "")),
        }

    # -- planning --------------------------------------------------------

    def plan(self, model_key: str = RECOMMENDED_VARIANT, *, deep: bool = False) -> dict[str, Any]:
        """What an install would do here, with no network contact at all."""

        key = str(model_key or RECOMMENDED_VARIANT)
        spec = variant(key)
        assets = recommended_assets(key)
        availability = self.availability(deep=deep)
        destination = self.destination(key)
        free = self._free(destination)
        estimate = estimated_weights_bytes(key)
        blockers: list[str] = []
        missing = list(getattr(availability, "missing", ()) or ())
        if missing:
            blockers.append(REASON_PACKAGES)
        if free is not None and estimate and free < estimate:
            blockers.append(REASON_DISK)
        devices = list(getattr(availability, "device_names", ()) or ())
        hardware = {
            "python": str(getattr(availability, "python", "")),
            "platform": str(getattr(availability, "platform", "")),
            "torch_present": bool(getattr(availability, "torch_present", False)),
            "cuda_available": getattr(availability, "cuda_available", None),
            "cuda_devices": int(getattr(availability, "cuda_devices", 0) or 0),
            "device_names": devices,
            "device_hint": (
                "CUDA device(s) found; the predictor's device=auto will use cuda:0."
                if devices
                else "No CUDA device was reported, so the model would run on the CPU."
            ),
            "missing_packages": missing,
            "note": str(getattr(availability, "note", "")),
        }
        return {
            "model_key": key,
            "variant_name": spec.name if spec else key,
            "recommended": key == RECOMMENDED_VARIANT,
            "why": RECOMMENDED_REASON,
            "destination": str(destination),
            "managed_root": str(managed_root(self.root)),
            "assets": [asset.audit_view() for asset in assets],
            "estimated_bytes": estimate,
            "estimated_human": human_bytes(estimate) if estimate else "unknown",
            "download_bytes": None,
            "download_human": "unknown",
            "exact": False,
            "checked_at": "",
            "free_bytes": free,
            "free_human": human_bytes(free),
            "blockers": blockers,
            "hardware": hardware,
            "licence": {
                "code": CODE_LICENCE,
                "copyright": CODE_LICENCE_COPYRIGHT,
                "weights_host": WEIGHTS_HOST,
                "weights_state": WEIGHTS_LICENCE_STATE,
            },
            "revision_policies": {
                "code": CODE_REVISION_POLICY,
                "hub": HUB_REVISION_POLICY,
            },
            "code_commit": SOURCE_COMMIT_FULL,
            "code_commit_verified_at": SOURCE_COMMIT_VERIFIED_AT,
            "notes": [
                "Weight size is estimated from the project's published parameter count "
                "at 4 bytes per parameter. The check below reads the real file sizes "
                "from the Hub before anything is downloaded.",
                "Nothing is downloaded by showing this page. The download starts only "
                "when Install is pressed after a check.",
            ],
        }

    def _fresh_plan(self) -> dict[str, Any] | None:
        with self._lock:
            plan = dict(self._plan) if self._plan else None
            at = self._plan_at
        if not plan or at is None:
            return None
        if self._clock() - at > timedelta(seconds=CHECK_TTL_SECONDS):
            return None
        return plan

    def check(self, model_key: str = RECOMMENDED_VARIANT) -> dict[str, Any]:
        """Read the sources' metadata: exact sizes, revisions, free space.

        This contacts the official sources -- and only their metadata.  It never
        downloads a weight file.  It refuses rather than guessing when a size or a
        revision cannot be read.
        """

        key = str(model_key or RECOMMENDED_VARIANT)
        with self._lock:
            if self._job.running:
                return {
                    "ok": False,
                    "reason": REASON_BUSY,
                    "message": "A download is already running; cancel it before checking again.",
                }
        base = self.plan(key, deep=True)
        if REASON_PACKAGES in base["blockers"]:
            return {
                "ok": False,
                "reason": REASON_PACKAGES,
                "plan": base,
                "message": (
                    "This machine is missing runtime packages ("
                    + ", ".join(base["hardware"]["missing_packages"])
                    + "). AXIOM does not install software: install the official "
                    "requirements first, then press Install."
                ),
            }
        descriptions: list[dict[str, Any]] = []
        described_plans: list[AssetPlan] = []
        total = 0
        exact = True
        try:
            for asset in recommended_assets(key):
                pin = self._pin_for(asset)
                described = self.fetcher_for(asset).describe(asset, revision=pin)
                if not described.revision:
                    raise InstallRefused(
                        REASON_SOURCES,
                        f"{asset.repo_id} did not report a revision",
                        "AXIOM pins a revision before it downloads; without one nothing "
                        "was fetched.",
                    )
                if pin and described.revision != pin:
                    raise InstallRefused(
                        REASON_SOURCES,
                        f"{asset.repo_id} no longer resolves to the recorded commit",
                        "The installed copy is pinned to the commit in the manifest. Clean "
                        "up and install again if you intend to move to a new revision.",
                    )
                if described.bytes is None or described.bytes <= 0:
                    exact = False
                else:
                    total += int(described.bytes)
                described_plans.append(described)
                descriptions.append(described.audit_view())
        except InstallRefused as exc:
            return {
                "ok": False,
                "reason": exc.reason,
                "plan": base,
                "message": (
                    f"The sources could not be checked ({exc.reason})"
                    + (f": {exc.detail}" if exc.detail else "")
                    + f". {exc.hint} Nothing was downloaded."
                ).strip(),
            }
        except Exception as exc:  # curated: never a traceback on the page
            self._log(f"kronos install check failed: {type(exc).__name__}")
            return {
                "ok": False,
                "reason": REASON_SOURCES,
                "plan": base,
                "message": (
                    "The sources could not be checked ("
                    + type(exc).__name__
                    + "). Nothing was downloaded."
                ),
            }
        free = base["free_bytes"]
        blockers = list(base["blockers"])
        if exact and free is not None and free < total:
            blockers.append(REASON_DISK)
        plan = dict(base)
        plan.update(
            {
                "assets": descriptions,
                "download_bytes": total if exact else None,
                "download_human": human_bytes(total) if exact else "unknown",
                "exact": bool(exact),
                "blockers": sorted(set(blockers)),
                "checked_at": self._clock().isoformat(),
                "notes": list(base["notes"])
                + [
                    "Sizes and commits above were read from the official sources just now; "
                    "they are what the download will use.",
                ],
            }
        )
        with self._lock:
            self._plan = dict(plan)
            self._asset_plans = tuple(described_plans)
            self._plan_at = self._clock()
        if blockers:
            what = (
                "there is not enough free space"
                if REASON_DISK in blockers
                else "this machine is missing runtime packages"
            )
            return {
                "ok": False,
                "reason": blockers[0],
                "plan": plan,
                "message": (
                    f"Checked, but nothing can be installed: {what}. "
                    f"The download needs {plan['download_human']} and "
                    f"{plan['free_human']} is free."
                ),
            }
        return {
            "ok": True,
            "reason": "",
            "plan": plan,
            "message": (
                f"Checked: {plan['download_human']} from {len(plan['assets'])} official "
                f"source(s), {plan['free_human']} free. Nothing has been downloaded. "
                "Press Install recommended setup to download it."
            ),
        }

    # -- install ---------------------------------------------------------

    def install(self, model_key: str = RECOMMENDED_VARIANT) -> dict[str, Any]:
        """Start the download the operator asked for.  Nothing starts by itself."""

        key = str(model_key or RECOMMENDED_VARIANT)
        with self._lock:
            if self._job.running:
                return {
                    "ok": False,
                    "reason": REASON_BUSY,
                    "message": "A download is already running; cancel it or wait for it to finish.",
                }
        plan = self._fresh_plan()
        if plan is None or str(plan.get("model_key")) != key:
            outcome = self.check(key)
            if not outcome["ok"]:
                return outcome
            return {
                "ok": True,
                "reason": "",
                "checked_only": True,
                "plan": outcome["plan"],
                "message": outcome["message"],
            }
        if plan.get("blockers"):
            return {
                "ok": False,
                "reason": str(plan["blockers"][0]),
                "plan": plan,
                "message": "This machine cannot take the download right now; check the blockers.",
            }
        destination = self.destination(key)
        total = plan.get("download_bytes")
        with self._lock:
            asset_plans = tuple(self._asset_plans)
            self._job = _Job(
                cancel=threading.Event(),
                state=DOWNLOADING,
                steps=len(plan.get("assets") or ()),
                bytes_total=int(total) if isinstance(total, int) else None,
                started_at=self._clock().isoformat(),
            )
            job = self._job
        if not self._start_threads:
            self._run(plan, job, asset_plans)
            return self._install_outcome(job)
        thread = threading.Thread(
            target=self._run,
            args=(plan, job, asset_plans),
            name="axiom-kronos-install",
            daemon=True,
        )
        job.thread = thread
        thread.start()
        return {
            "ok": True,
            "reason": "",
            "started": True,
            "message": (
                f"Downloading {plan.get('download_human', 'the recommended setup')} into "
                f"{destination}. Progress is shown on this page; Cancel stops it."
            ),
        }

    def _install_outcome(self, job: _Job) -> dict[str, Any]:
        if job.state == INSTALLED:
            return {"ok": True, "reason": "", "message": self._installed_message()}
        return {
            "ok": False,
            "reason": job.reason or job.state.lower(),
            "message": f"Install did not finish ({job.reason or job.state}). {job.hint}".strip(),
        }

    def _installed_message(self) -> str:
        manifest = self.manifest(refresh=True)
        paths = dict(manifest.get("paths") or {})
        return (
            "Installed the official Kronos setup into "
            f"{manifest.get('destination', managed_root(self.root))} "
            f"({human_bytes(manifest.get('bytes'))}). Paths recorded; press Load model "
            "to use it. Nothing was loaded or started by the install."
        ).replace("(unknown)", "")

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            job = self._job
            if not job.running and job.state != DOWNLOADING:
                return {"ok": False, "reason": "nothing_running", "message": "No download is running."}
            job.cancel.set()
        self._log("kronos install cancel requested")
        return {
            "ok": True,
            "reason": "",
            "message": (
                "Cancelling. The file in flight is finished or discarded, then the partial "
                "download stays in place so you can retry or clean it up."
            ),
        }

    def cleanup(self, model_key: str = "") -> dict[str, Any]:
        """Remove the managed folder and the manifest.  Nothing outside it is touched."""

        with self._lock:
            if self._job.running:
                self._job.cancel.set()
                thread = self._job.thread
            else:
                thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=10.0)
        manifest = self.manifest(refresh=True)
        key = str(model_key or manifest.get("model_key") or RECOMMENDED_VARIANT)
        destination = self.destination(key)
        root = managed_root(self.root)
        removed = False
        try:
            if destination.exists() and (root in destination.parents):
                shutil.rmtree(destination, ignore_errors=True)
                removed = True
            incoming = root / INCOMING_DIRNAME
            if incoming.exists():
                shutil.rmtree(incoming, ignore_errors=True)
            if self.manifest_path.exists():
                self.manifest_path.unlink()
                removed = True
        except OSError:
            return {
                "ok": False,
                "reason": "cleanup_failed",
                "message": "The managed folder could not be removed; close any file using it.",
            }
        with self._lock:
            self._manifest = {}
            self._plan = None
            self._plan_at = None
            self._job = _Job()
        return {
            "ok": True,
            "reason": "",
            "removed": removed,
            "message": (
                f"Removed {destination} and its manifest. Nothing outside the AXIOM-managed "
                "folder was touched."
                if removed
                else "There was nothing installed to remove."
            ),
        }

    # -- the worker ------------------------------------------------------

    def _run(
        self,
        plan: Mapping[str, Any],
        job: _Job,
        asset_plans: Sequence[AssetPlan],
    ) -> None:
        key = str(plan.get("model_key") or RECOMMENDED_VARIANT)
        destination = self.destination(key)
        incoming = (
            managed_root(self.root)
            / INCOMING_DIRNAME
            / f"{key}-{job.started_at.replace(':', '')}"
        )
        assets = recommended_assets(key)
        revisions: dict[str, str] = {}
        paths: dict[str, str] = {}
        digests: dict[str, list[dict[str, Any]]] = {}
        try:
            incoming.mkdir(parents=True, exist_ok=True)
            for index, asset in enumerate(assets, start=1):
                if job.cancel.is_set():
                    raise InstallRefused(REASON_CANCELLED)
                with self._lock:
                    job.asset = asset.key
                    job.asset_label = asset.label
                    job.step = index
                target = incoming / asset.subdir
                fetcher = self.fetcher_for(asset)
                asset_plan = self._plan_for(fetcher, asset, asset_plans)
                result = fetcher.fetch(
                    asset,
                    asset_plan,
                    target,
                    progress=lambda count: self._advance(job, count),
                    cancelled=job.cancel.is_set,
                )
                if job.cancel.is_set():
                    raise InstallRefused(REASON_CANCELLED)
                self._verify_asset(asset, target, result, asset_plan)
                revisions[asset.subdir] = result.revision
                digests[asset.subdir] = [item.audit_view() for item in result.files]
            if job.cancel.is_set():
                raise InstallRefused(REASON_CANCELLED)
            # Only now does anything leave the incoming folder.
            destination.parent.mkdir(parents=True, exist_ok=True)
            staged = destination.parent / f".{key}-staged"
            if staged.exists():
                shutil.rmtree(staged, ignore_errors=True)
            shutil.move(str(incoming), str(staged))
            if destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            shutil.move(str(staged), str(destination))
            for asset in assets:
                paths[asset.subdir] = str(destination / asset.subdir)
            total = sum(
                int(item.get("bytes") or 0) for entry in digests.values() for item in entry
            )
            manifest = {
                "schema": INSTALL_SCHEMA,
                "state": INSTALLED,
                "model_key": key,
                "installed_at": self._clock().isoformat(),
                "destination": str(destination),
                "managed_root": str(managed_root(self.root)),
                "paths": paths,
                "revisions": revisions,
                "files": digests,
                "bytes": total,
                "sources": [asset.audit_view() for asset in assets],
                "code_commit": SOURCE_COMMIT_FULL,
                "revision_policies": {
                    "code": CODE_REVISION_POLICY,
                    "hub": HUB_REVISION_POLICY,
                },
                "licence": {
                    "code": CODE_LICENCE,
                    "copyright": CODE_LICENCE_COPYRIGHT,
                    "weights_host": WEIGHTS_HOST,
                    "weights_state": WEIGHTS_LICENCE_STATE,
                },
                "note": (
                    "Paths, revisions and digests recorded at install time. Nothing was "
                    "loaded, started, or connected to a broker."
                ),
            }
            self._write_manifest(manifest)
            with self._lock:
                job.state = INSTALLED
                job.finished_at = self._clock().isoformat()
            self._log(f"kronos installed: {destination} ({human_bytes(total)})")
        except InstallRefused as exc:
            self._fail(job, exc.reason, exc.detail, exc.hint)
        except Exception as exc:  # curated: the page never sees a traceback
            self._log(f"kronos install failed: {type(exc).__name__}")
            self._fail(
                job,
                "install_failed",
                type(exc).__name__,
                "Nothing was recorded as installed. Retry, or clean up the partial download.",
            )

    def _plan_for(
        self, fetcher: Any, asset: InstallAsset, plans: Sequence[AssetPlan]
    ) -> AssetPlan:
        for item in plans:
            if item.asset.key == asset.key:
                return item
        return fetcher.describe(asset, revision=self._pin_for(asset))

    def _pin_for(self, asset: InstallAsset) -> str:
        """The recorded commit for this asset, if a previous install pinned one."""

        if asset.kind == CODE_ARCHIVE:
            return SOURCE_COMMIT_FULL
        manifest = self.manifest()
        revisions = manifest.get("revisions")
        if isinstance(revisions, Mapping):
            return str(revisions.get(asset.subdir, "") or "")
        return ""

    def _advance(self, job: _Job, count: int) -> None:
        if not count:
            return
        with self._lock:
            job.bytes_done += int(count)

    def _verify_asset(
        self,
        asset: InstallAsset,
        folder: Path,
        result: FetchResult,
        plan: AssetPlan,
    ) -> None:
        """Check the bytes on disk, including the digest the source published.

        A file that does not match the digest recorded in the plan aborts the whole
        install: nothing is moved into place and no manifest is written.
        """

        fetched = {item.path: item for item in result.files}
        for expected in plan.files:
            if not expected.sha256:
                continue
            actual = fetched.get(expected.path)
            on_disk = folder / expected.path
            digest = actual.sha256 if actual and actual.sha256 else ""
            if not digest and on_disk.is_file():
                digest = _sha256_file(on_disk)
            if digest and digest != expected.sha256:
                raise InstallRefused(
                    REASON_DIGEST,
                    f"{expected.path} does not match the digest the source publishes",
                    "Nothing was installed. Clean up the partial download and try again; if "
                    "it repeats, the source has changed.",
                )
        if asset.kind == CODE_ARCHIVE:
            if not (folder / "model" / "kronos.py").is_file():
                raise InstallRefused(
                    REASON_NO_SOURCE_TREE,
                    "the official source tree is missing model/kronos.py",
                    "Nothing was installed; press Install again.",
                )
            return
        names = sorted(item.name for item in folder.iterdir()) if folder.is_dir() else []
        if not any(name.endswith(WEIGHT_SUFFIXES) for name in names):
            raise InstallRefused(
                REASON_INCOMPLETE,
                f"{asset.repo_id} produced no weight file",
                "Nothing was installed; press Install again.",
            )
        if "config.json" not in names:
            raise InstallRefused(
                REASON_INCOMPLETE,
                f"{asset.repo_id} produced no config.json",
                "Nothing was installed; press Install again.",
            )

    def _fail(self, job: _Job, reason: str, detail: str, hint: str) -> None:
        with self._lock:
            job.state = CANCELLED if reason == REASON_CANCELLED else FAILED
            job.reason = reason
            job.detail = detail
            retry = (
                "Nothing was recorded as installed. Press Install recommended setup to "
                "retry, or Clean up partial download to remove what was written."
            )
            job.hint = f"{hint} {retry}".strip() if hint else retry
            job.finished_at = self._clock().isoformat()
        self._log(f"kronos install {job.state.lower()}: {reason}")

    # -- view ------------------------------------------------------------

    def view(self, model_key: str = RECOMMENDED_VARIANT) -> dict[str, Any]:
        key = str(model_key or RECOMMENDED_VARIANT)
        manifest = self.manifest(refresh=True)
        installed = self.installed(key)
        plan = self._fresh_plan()
        if plan is not None and str(plan.get("model_key")) != key:
            plan = None
        with self._lock:
            job = self._job.view(str(self.destination(key)))
        return {
            "state": job["state"] if job["state"] != NOT_INSTALLED else (
                INSTALLED if installed else (FAILED if manifest.get("state") == FAILED else NOT_INSTALLED)
            ),
            "job": job,
            "installed": installed is not None,
            "manifest": dict(installed or {}),
            "manifest_state": str(manifest.get("state", "")),
            "destination": str(self.destination(key)),
            "managed_root": str(managed_root(self.root)),
            "plan": plan or self.plan(key),
            "checked": plan is not None,
            "check_ttl_seconds": CHECK_TTL_SECONDS,
            "paths": self.paths(key),
            "sources": [asset.audit_view() for asset in recommended_assets(key)],
            "code_commit": SOURCE_COMMIT_FULL,
            "note": (
                "AXIOM downloads only when Install is pressed, only from the official "
                "sources listed here, and only into its own managed folder. Nothing is "
                "installed or fetched when the app starts."
            ),
        }


__all__ = [
    "AssetPlan",
    "CANCELLED",
    "CHECKED",
    "CHECK_TTL_SECONDS",
    "CODE_ARCHIVE",
    "CODE_SUBDIR",
    "CodeFetcher",
    "CompositeFetcher",
    "DOWNLOADING",
    "FAILED",
    "FetchResult",
    "FileFact",
    "HUB_SNAPSHOT",
    "HubFetcher",
    "INSTALLED",
    "INSTALL_FILENAME",
    "INSTALL_SCHEMA",
    "InstallAsset",
    "InstallRefused",
    "KronosInstaller",
    "MANAGED_DIRNAME",
    "MODEL_SUBDIR",
    "NOT_INSTALLED",
    "REASON_CHECK_REQUIRED",
    "REASON_DIGEST",
    "REASON_DISK",
    "RECOMMENDED_VARIANT",
    "TOKENIZER_SUBDIR",
    "estimated_weights_bytes",
    "human_bytes",
    "install_manifest_path",
    "managed_root",
    "recommended_assets",
]
