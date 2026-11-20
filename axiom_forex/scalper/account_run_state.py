"""Account-isolated run-state paths without moving or deleting legacy data.

The marker enables account scoping only after an explicit switch is requested.
The legacy path remains in use for the one identity it already belongs to.
All claims in old files are untrusted until the normal recovery checks run.
"""
from __future__ import annotations

import hashlib
import json
from os import fsync
from pathlib import Path
from typing import Any, Mapping

from .live_autonomous import JsonRunStateStore, RUN_STATE_SCHEMA, STOPPED
from .mutation_journal import MutationJournal


class AccountStateError(RuntimeError):
    pass


class AccountRunStates:
    def __init__(self, legacy: str | Path):
        self.legacy = Path(legacy)
        self.marker = self.legacy.with_name(self.legacy.name + '.account-scoping')
        self.prefix = self.legacy.name + '.account-'

    def enable(self, identity: Mapping[str, str]) -> None:
        if self.marker.exists():
            if not self.marker.is_file() or self.marker.is_symlink():
                raise AccountStateError('account_scope_marker_invalid')
            return
        try:
            legacy = JsonRunStateStore(self.legacy).load()
            if legacy is not None:
                declared = legacy.get('account_identity')
                fields = ('server', 'fingerprint', 'currency', 'account_mode')
                if not isinstance(declared, Mapping) or any(declared.get(k) != identity.get(k) for k in fields):
                    raise AccountStateError('legacy_account_not_selected')
            with self.marker.open('x', encoding='ascii') as f:
                f.write(json.dumps({'schema': 'axiom-account-scoping-v1', 'legacy_key': identity['key']}))
                f.flush()
                fsync(f.fileno())
            from .live_autonomous import _fsync_directory
            _fsync_directory(self.marker.parent)
        except Exception as exc:
            raise AccountStateError('account_scope_marker_unavailable') from exc

    @staticmethod
    def identity(status: Any) -> dict[str, str]:
        from ..brokers.mt5.identity import account_fingerprint
        if (not status.connected or not status.terminal_connected or status.account_login <= 0
                or not status.account_server or not status.account_currency
                or status.account_mode not in {'DEMO', 'REAL'} or not status.account_mode_confirmed):
            raise AccountStateError('account_identity_unverified')
        # A full digest of server and login scopes the file path; the shorter
        # display fingerprint is never the sole key used to separate accounts.
        key = hashlib.sha256((status.account_server + '\0' + str(status.account_login)).encode()).hexdigest()
        return {'server': status.account_server, 'fingerprint': account_fingerprint(status.account_login),
                'currency': status.account_currency, 'account_mode': status.account_mode, 'key': key}

    def path_for(self, identity: Mapping[str, str]) -> Path:
        if not self.marker.exists():
            return self.legacy
        if not self.marker.is_file() or self.marker.is_symlink():
            raise AccountStateError('account_scope_marker_invalid')
        try:
            marker = json.loads(self.marker.read_text(encoding='ascii'))
            if (not isinstance(marker, dict) or marker.get('schema') != 'axiom-account-scoping-v1'
                    or not isinstance(marker.get('legacy_key'), str) or len(marker['legacy_key']) != 64):
                raise ValueError('invalid_marker')
        except Exception as exc:
            raise AccountStateError('account_scope_marker_invalid') from exc
        fields = ('server', 'fingerprint', 'currency', 'account_mode')
        old = JsonRunStateStore(self.legacy).load()
        if old is not None:
            declared = old.get('account_identity')
            if not isinstance(declared, Mapping) or not all(declared.get(k) for k in fields):
                raise AccountStateError('legacy_account_identity_unverified')
            if marker['legacy_key'] == identity['key'] and all(declared.get(k) == identity[k] for k in fields):
                return self.legacy
        path = self.legacy.with_name(self.prefix + identity['key'] + '.local.json')
        scoped = JsonRunStateStore(path).load()
        if scoped is not None:
            declared = scoped.get('account_identity')
            if not isinstance(declared, Mapping) or any(declared.get(k) != identity[k] for k in fields):
                raise AccountStateError('scoped_account_identity_mismatch')
        return path

    def paths(self) -> set[Path]:
        paths = {self.legacy}
        paths.update(self.legacy.parent.glob(self.prefix + '*.local.json'))
        paths.update(Path(str(path)[:-len('.mutations.sqlite3')]) for path in
                     self.legacy.parent.glob(self.prefix + '*.local.json.mutations.sqlite3'))
        return paths

    def assert_all_resolved(self) -> None:
        """Do not abandon any legacy or account-scoped run or journal."""
        for path in sorted(self.paths()):
            state = JsonRunStateStore(path).load()
            journal_path = path.with_name(path.name + '.mutations.sqlite3')
            if state is None:
                if journal_path.exists():
                    raise AccountStateError('run_state_missing_with_journal')
                continue
            if state.get('schema') != RUN_STATE_SCHEMA:
                raise AccountStateError('stored_run_schema_unverified')
            book = state.get('position_book')
            identity = state.get('account_identity')
            if (not isinstance(identity, Mapping) or identity.get('account_mode') not in {'DEMO', 'REAL'}
                    or any(not identity.get(k) for k in ('server', 'fingerprint', 'currency', 'account_mode'))):
                raise AccountStateError('stored_run_identity_unverified')
            if (not isinstance(book, Mapping) or any(not isinstance(book.get(k), list) for k in
                    ('open_positions', 'close_blockers', 'pending_closes', 'pending_protection_modifications'))
                    or 'pending_submission' not in state or not isinstance(state.get('open_positions'), list)
                    or state['open_positions'] != book.get('open_positions')):
                raise AccountStateError('stored_run_state_unverified')
            if (state.get('state') != STOPPED or state.get('pending_submission') is not None
                    or book['open_positions'] or book['close_blockers']
                    or book['pending_closes'] or book['pending_protection_modifications']):
                raise AccountStateError('saved_run_recovery_required')
            if not journal_path.is_file():
                raise AccountStateError('mutation_journal_missing')
            journal = MutationJournal(journal_path)
            try:
                if journal.unresolved():
                    raise AccountStateError('mutation_journal_unresolved')
            finally:
                journal.close()
