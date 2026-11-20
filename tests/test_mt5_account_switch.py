"""Account switching tests: injected fake only; no MT5 connection or real orders."""
from dataclasses import replace
from http import HTTPStatus
from types import SimpleNamespace

import pytest

from axiom_forex.brokers.mt5 import MT5PasswordStore, MT5TradingAdapter, MT5SubmitRefused
from axiom_forex.brokers.mt5.identity import account_fingerprint
from axiom_forex.database.threaded import ThreadLocalRepository
from axiom_forex.paper_runner.live_workflow import ArmedStrategy, LiveTrading
from axiom_forex.paper_runner.scalper_live_host import ScalperLiveAutonomousHost
from axiom_forex.scalper.live_bridge import KIND
from axiom_forex.scalper.run_lock import RunLock
from axiom_forex.workstation.app import WorkstationApp
from axiom_forex.workstation.mode import ExecutionModeController
from axiom_forex.workstation.session import MT5ObservationSource, ObservationSession
from tests.fake_mt5 import FakeMT5, make_position
from tests.test_mt5_adapter import FakeVault, mt5_settings
from tests.test_workstation_ui import live_wizard

OLD = 55500123
NEW = 44556677
SECRET = 'a-onetime-switch-secret'


class SwitchFake(FakeMT5):
    def __init__(self):
        super().__init__()
        self.target_mode = 2
        self.switch_result = True
        self.kwarg_names = []
        self.password_seen = False
        self.orders = ()
        self.break_observation = False

    def login(self, login, **kwargs):
        self.kwarg_names.append(set(kwargs))
        self.password_seen = kwargs.get('password') == SECRET
        self.login_calls.append({'login': login, 'server': kwargs['server']})
        if self.switch_result == 'exception':
            raise RuntimeError(SECRET)
        if self.switch_result is True:
            self.account_login = login
            self.account_server = kwargs['server']
            self.trade_mode = self.target_mode
        if self.switch_result == 'disconnect':
            self.terminal_connected = False
            return True
        return self.switch_result

    def orders_get(self, **kwargs):
        return self.orders


@pytest.fixture
def rig(tmp_path):
    fake = SwitchFake()
    settings = mt5_settings()
    session = ObservationSession(MT5ObservationSource(settings, module=fake))
    session.connect()
    repo = ThreadLocalRepository(str(tmp_path / 'db.sqlite'))
    adapter = MT5TradingAdapter(settings=settings, password_store=MT5PasswordStore(settings=settings, vault=FakeVault(), audit_log=None),
                                repository=repo, audit_log=None, terminal_factory=lambda: session.source.terminal)
    adapter.connect_adopted()
    mode = ExecutionModeController()
    live = LiveTrading(adapter=adapter, mode=mode, armed_strategy=ArmedStrategy())
    host = ScalperLiveAutonomousHost(session=session, adapter=adapter, mode=mode, platform_factory=lambda _: None,
                                     admission_host=None, live_workflow=live, settings_path=tmp_path/'settings.json',
                                     state_path=tmp_path/'run.local.json', poll_seconds=0.1)
    live.scalper_autonomous = host
    app = WorkstationApp(wizard=live_wizard(session=session), live=live, mode=mode)
    yield SimpleNamespace(fake=fake, settings=settings, session=session, repo=repo, adapter=adapter, mode=mode,
                          live=live, host=host, app=app, root=tmp_path)
    host.close()
    repo.close()


def form(app, login=NEW, server='Broker-Real', password=''):
    return {'nonce': [app.nonce], 'switch_login': [str(login)], 'switch_login_confirm': [str(login)],
            'switch_server': [server], 'switch_server_confirm': [server], 'switch_password': [password]}


def test_explicit_switch_shared_terminal_identity_and_secret(rig):
    rig.adapter.arm_trading(confirm='ENABLE LIVE TRADING')
    rig.mode.confirmed = True
    rig.mode.session_account = account_fingerprint(OLD)
    rig.app.wizard.choices = replace(rig.app.wizard.choices, confirmed=True)
    payload = form(rig.app, password=SECRET)
    status, _, message = rig.app.act('switch-account', payload)
    assert status == HTTPStatus.SEE_OTHER, message
    assert 'switch_password' not in payload
    assert SECRET not in message and SECRET not in str(rig.app.action_feedback)
    assert rig.fake.password_seen and 'password' in rig.fake.kwarg_names[0]
    assert len(rig.fake.initialize_calls) == 1 and len(rig.fake.login_calls) == 1
    assert not rig.fake.send_requests and not rig.fake.protection_requests
    assert rig.session.source.terminal is rig.adapter.terminal
    assert rig.session.account()['mode'] == 'REAL'
    assert rig.session.account()['masked_login'] == '***6677'
    assert rig.session.account()['fingerprint'] == account_fingerprint(NEW)
    assert rig.adapter.status()['account_fingerprint'] == account_fingerprint(NEW)
    assert not rig.adapter.armed and not rig.mode.confirmed and not rig.app.wizard.choices.confirmed
    assert rig.adapter._switch_identity_verified
    assert rig.app.preflight_facts().settings.server == 'Broker-Real'
    assert rig.host.state_store.path != rig.host.account_states.legacy
    assert SECRET not in (rig.root/'db.sqlite').read_bytes().decode('latin1')


def test_switch_back_demo_uses_saved_credentials_and_separate_paths(rig):
    assert rig.app.act('switch-account', form(rig.app))[0] == HTTPStatus.SEE_OTHER
    real = rig.host.state_store.path
    rig.fake.target_mode = 0
    assert rig.app.act('switch-account', form(rig.app, OLD, 'Broker-Demo'))[0] == HTTPStatus.SEE_OTHER
    assert rig.session.account()['mode'] == 'DEMO'
    assert rig.host.state_store.path != real
    assert 'password' not in rig.fake.kwarg_names[0] and 'password' not in rig.fake.kwarg_names[1]
    assert len(rig.fake.initialize_calls) == 1


@pytest.mark.parametrize('result', [False, 'exception', 'disconnect'])
def test_failure_does_not_authorize_or_leak(rig, result):
    rig.fake.switch_result = result
    answer = rig.app.act('switch-account', form(rig.app, password=SECRET))
    assert answer[0] == HTTPStatus.CONFLICT
    assert SECRET not in str(answer) and SECRET not in str(rig.app.action_feedback)
    assert 'Actual account' in answer[2]
    assert not rig.adapter._switch_identity_verified
    with pytest.raises(MT5SubmitRefused):
        rig.adapter.arm_trading(confirm='ENABLE LIVE TRADING')
    assert len(rig.fake.initialize_calls) == 1 and not rig.fake.send_requests


def test_unsupported_or_disagreeing_target_never_verified(rig):
    rig.fake.target_mode = 1
    status, _, message = rig.app.act('switch-account', form(rig.app))
    assert status == HTTPStatus.CONFLICT
    assert 'CONTEST' in message
    assert not rig.adapter._switch_identity_verified


@pytest.mark.parametrize('problem', ['position', 'pending_order', 'unreadable_orders', 'other_process', 'runtime', 'live_session', 'inconsistent_runner_state'])
def test_blockers_prevent_login(rig, problem):
    lock = None
    if problem == 'position':
        rig.fake.positions = [make_position(ticket=123, symbol='EURUSD.pro', side='BUY', volume=0.1)]
    elif problem == 'pending_order':
        rig.fake.orders = (object(),)
    elif problem == 'unreadable_orders':
        rig.fake.orders = None
    elif problem == 'other_process':
        lock = RunLock(str(rig.host.account_states.legacy) + '.owner.lock')
    elif problem == 'runtime':
        rig.host._runtime = object()
    elif problem == 'live_session':
        rig.live.session = SimpleNamespace(view=lambda: {'running': True})
    else:
        rig.host._state = 'RECOVERY_REQUIRED'
    try:
        status, _, _ = rig.app.act('switch-account', form(rig.app))
        assert status == HTTPStatus.CONFLICT
        assert not rig.fake.login_calls and not rig.fake.send_requests
    finally:
        if lock:
            lock.close()
        if problem == 'runtime':
            rig.host._runtime = None
        if problem == 'live_session':
            rig.live.session = None
        if problem == 'inconsistent_runner_state':
            rig.host._state = 'STOPPED'


def test_approval_invalidated_without_deleting_proposal(rig):
    rig.repo.save_snapshot('axiom-scalper-live-intent:one', KIND,
                           {'kind': KIND, 'status': 'APPROVED', 'intent': {'intent_id': 'one'},
                            'operator_review': {'ok': True}, 'operator_approval': {'ok': True},
                            'signed_approval': {'ok': True}, 'armed': {'ok': True}})
    assert rig.app.act('switch-account', form(rig.app))[0] == HTTPStatus.SEE_OTHER
    state = rig.repo.get_snapshot('axiom-scalper-live-intent:one')
    assert state['status'] == 'BLOCKED'
    assert not state['operator_review'] and not state['operator_approval'] and not state['armed']


def test_form_confirmation_and_secret_clearing_on_refusal(rig):
    payload = form(rig.app, password=SECRET)
    payload['switch_login_confirm'] = ['5']
    assert rig.app.act('switch-account', payload)[0] == HTTPStatus.BAD_REQUEST
    assert 'switch_password' not in payload and SECRET not in repr(payload)
    assert not rig.fake.login_calls


def _saved_state(rig):
    from axiom_forex.scalper.live_autonomous import RUN_STATE_SCHEMA
    return {'schema': RUN_STATE_SCHEMA, 'state': 'STOPPED', 'pending_submission': None,
            'account_identity': {'server': 'Broker-Demo', 'fingerprint': account_fingerprint(OLD),
                                 'currency': 'USD', 'account_mode': 'DEMO'},
            'open_positions': [], 'position_book': {'open_positions': [], 'close_blockers': [], 'pending_closes': [], 'pending_protection_modifications': []}}


@pytest.mark.parametrize('condition', ['open', 'entry', 'close', 'protection', 'active_state', 'missing_journal', 'unresolved_journal', 'invalid_state'])
def test_all_saved_recovery_blockers_preserve_files(rig, condition):
    from axiom_forex.scalper.live_autonomous import JsonRunStateStore
    from axiom_forex.scalper.mutation_journal import MutationJournal
    state = _saved_state(rig)
    if condition == 'open':
        state['position_book']['open_positions'] = [{'position_id': 'x'}]
        state['open_positions'] = list(state['position_book']['open_positions'])
    if condition == 'entry': state['pending_submission'] = {'request_id': 'e'}
    if condition == 'close': state['position_book']['pending_closes'] = [{'request_id': 'c'}]
    if condition == 'protection': state['position_book']['pending_protection_modifications'] = [{'request_id': 'p'}]
    if condition == 'active_state': state['state'] = 'RECOVERY_REQUIRED'
    if condition == 'invalid_state': state['schema'] = 'bad'
    store = JsonRunStateStore(rig.host.account_states.legacy)
    store.save(state)
    journal_path = store.path.with_name(store.path.name + '.mutations.sqlite3')
    if condition != 'missing_journal':
        journal = MutationJournal(journal_path, create=True)
        if condition == 'unresolved_journal':
            identity = {key: 'x' for key in ('run_id', 'account_fingerprint', 'account_server', 'account_currency',
                                               'strategy_fingerprint', 'evidence_digest', 'config_digest', 'symbol')}
            identity['account_mode'] = 'DEMO'
            journal.prepare(request_id='req', action='entry', identity=identity, intent={'request_id': 'req'})
        journal.close()
    original = store.path.read_bytes()
    result = rig.app.act('switch-account', form(rig.app))
    assert result[0] == HTTPStatus.CONFLICT and 'saved account run' in result[2]
    assert not rig.fake.login_calls and store.path.read_bytes() == original


def test_legacy_and_new_accounts_never_share_run_path_across_restarts(rig):
    from axiom_forex.scalper.live_autonomous import JsonRunStateStore
    from axiom_forex.scalper.mutation_journal import MutationJournal
    from axiom_forex.scalper.account_run_state import AccountRunStates
    store = JsonRunStateStore(rig.host.account_states.legacy)
    store.save(_saved_state(rig))
    journal = MutationJournal(str(store.path) + '.mutations.sqlite3', create=True)
    journal.close()
    before = store.path.read_bytes()
    assert rig.app.act('switch-account', form(rig.app))[0] == HTTPStatus.SEE_OTHER
    real_path = rig.host.state_store.path
    assert real_path != store.path and store.path.read_bytes() == before
    recreated = AccountRunStates(store.path)
    assert recreated.path_for(recreated.identity(rig.session.source.terminal.status())) == real_path
    rig.fake.target_mode = 0
    assert rig.app.act('switch-account', form(rig.app, OLD, 'Broker-Demo'))[0] == HTTPStatus.SEE_OTHER
    assert rig.host.state_store.path == store.path and store.path.read_bytes() == before


def test_unresolved_durable_order_and_protection_block_switch(rig):
    rig.repo.save_orders([{'request_id': 'unresolved', 'broker_id': 'mt5_terminal', 'state': 'UNRESOLVED'}])
    assert rig.app.act('switch-account', form(rig.app))[0] == HTTPStatus.CONFLICT
    assert not rig.fake.login_calls
    rig.repo.save_orders([{'request_id': 'unresolved', 'broker_id': 'mt5_terminal', 'state': 'FILLED'}])
    rig.adapter._protection_modification_results['p'] = ({}, {'state': 'UNRESOLVED'})
    assert rig.app.act('switch-account', form(rig.app))[0] == HTTPStatus.CONFLICT
    assert not rig.fake.login_calls


def test_observation_adapter_disagreement_is_unverified(rig):
    original = rig.session.source.snapshot
    def mismatched():
        value = original()
        value['account']['mode'] = 'DEMO'
        return value
    rig.session.source.snapshot = mismatched
    answer = rig.app.act('switch-account', form(rig.app))
    assert answer[0] == HTTPStatus.CONFLICT
    assert 'Actual account: REAL' in answer[2]
    assert not rig.adapter._switch_identity_verified


def test_paper_runner_blockers_and_form_render(rig):
    rig.app.runner = SimpleNamespace(view=lambda: {'available': True, 'running': True})
    answer = rig.app.act('switch-account', form(rig.app))
    assert answer[0] == HTTPStatus.CONFLICT and 'paper runner' in answer[2]
    assert not rig.fake.login_calls
    rig.app.runner = None
    status, html = rig.app.page('/setup', {})
    assert status == HTTPStatus.OK
    assert "name='switch_password'" in html and "autocomplete='off'" in html
    assert SECRET not in html


def test_same_login_different_server_invalidates_armed_identity(rig):
    from tests.test_mt5_adapter import make_intent, allow, make_spec
    rig.adapter.arm_trading(confirm='ENABLE LIVE TRADING')
    rig.fake.account_server = 'Different-Server'
    rig.adapter.refresh_account()
    assert not rig.adapter.armed_account_matches_current
    intent = make_intent()
    with pytest.raises(MT5SubmitRefused, match='mt5_account_changed_since_confirmation'):
        rig.adapter.submit(allow(intent), intent, spec=make_spec())
    assert not rig.fake.send_requests


def test_switch_waits_for_inflight_adapter_operation(rig):
    import threading
    entered, finished = threading.Event(), threading.Event()
    result = []
    def attempt():
        entered.set()
        result.append(rig.app.act('switch-account', form(rig.app)))
        finished.set()
    with rig.adapter.account_switch_lock:
        thread = threading.Thread(target=attempt)
        thread.start()
        assert entered.wait(2)
        assert not finished.wait(0.2)
        assert not rig.fake.login_calls
    thread.join(5)
    assert finished.is_set() and result[0][0] == HTTPStatus.SEE_OTHER
    assert len(rig.fake.initialize_calls) == 1 and not rig.fake.send_requests


def test_target_exposure_cannot_be_verified_or_armed(rig):
    original_login = rig.fake.login
    def login(*args, **kwargs):
        result = original_login(*args, **kwargs)
        rig.fake.positions = [make_position(ticket=11, symbol='EURUSD.pro', side='BUY', volume=0.1)]
        return result
    rig.fake.login = login
    answer = rig.app.act('switch-account', form(rig.app))
    assert answer[0] == HTTPStatus.CONFLICT
    assert 'Actual account: REAL' in answer[2]
    assert not rig.adapter._switch_identity_verified
    assert not rig.fake.send_requests


def test_bad_scope_marker_prevents_login(rig):
    rig.host.account_states.marker.write_text('corrupt', encoding='utf-8')
    result = rig.app.act('switch-account', form(rig.app))
    assert result[0] == HTTPStatus.CONFLICT and not rig.fake.login_calls


def test_full_login_digest_separates_colliding_display_fingerprints(tmp_path):
    from axiom_forex.scalper.account_run_state import AccountRunStates
    from axiom_forex.scalper.live_autonomous import JsonRunStateStore
    states = AccountRunStates(tmp_path/'run.local.json')
    old = {'server': 'same', 'fingerprint': 'same-short-hash', 'currency': 'USD',
           'account_mode': 'DEMO', 'key': 'a' * 64}
    other = dict(old, key='b' * 64)
    JsonRunStateStore(states.legacy).save({'account_identity': {k: old[k] for k in ('server', 'fingerprint', 'currency', 'account_mode')}})
    states.enable(old)
    assert states.path_for(old) == states.legacy
    assert states.path_for(other) != states.legacy


def test_current_identity_disagreement_blocks_arm_even_without_login(rig):
    original = rig.session.source.snapshot
    def mismatched():
        value = original()
        value['account']['server'] = 'unexpected-server'
        return value
    rig.session.source.snapshot = mismatched
    result = rig.app.act('switch-account', form(rig.app))
    assert result[0] == HTTPStatus.CONFLICT and not rig.fake.login_calls
    assert not rig.adapter._switch_identity_verified
    assert 'Actual account' in result[2]
    with pytest.raises(MT5SubmitRefused):
        rig.adapter.arm_trading(confirm='ENABLE LIVE TRADING')


def test_http_password_is_one_time_not_echoed_or_logged(rig, capsys):
    import http.client
    import threading
    from urllib.parse import urlencode
    from axiom_forex.workstation.app import make_server

    server = make_server(rig.app, host='127.0.0.1', port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        values = {key: value[0] for key, value in form(rig.app, password=SECRET).items()}
        conn.request('POST', '/actions/switch-account', urlencode(values),
                     {'Content-Type': 'application/x-www-form-urlencoded'})
        response = conn.getresponse()
        assert response.status == 303
        assert SECRET not in response.read().decode()
        conn.request('GET', '/setup')
        response = conn.getresponse()
        html = response.read().decode()
        assert response.getheader('Cache-Control') == 'no-store'
        assert SECRET not in html and "name='switch_password'" in html
        conn.close()
        captured = capsys.readouterr()
        assert SECRET not in captured.out + captured.err
        assert SECRET not in (rig.root/'db.sqlite').read_bytes().decode('latin1')
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_foreign_legacy_state_cannot_be_rebound_to_current_login(rig):
    from axiom_forex.scalper.live_autonomous import JsonRunStateStore
    from axiom_forex.scalper.mutation_journal import MutationJournal
    state = _saved_state(rig)
    state['account_identity']['server'] = 'Different-Server'
    store = JsonRunStateStore(rig.host.account_states.legacy)
    store.save(state)
    journal = MutationJournal(str(store.path) + '.mutations.sqlite3', create=True)
    journal.close()
    original = store.path.read_bytes()
    answer = rig.app.act('switch-account', form(rig.app))
    assert answer[0] == HTTPStatus.CONFLICT
    assert 'legacy run' in answer[2] and not rig.fake.login_calls
    assert store.path.read_bytes() == original
    assert not rig.host.account_states.marker.exists()


def test_secret_not_present_in_audit_on_success_or_exception(rig):
    from app.audit.log import InMemoryAuditLog
    audit = InMemoryAuditLog()
    rig.adapter.audit_log = audit
    rig.live.audit_log = audit
    rig.fake.switch_result = 'exception'
    answer = rig.app.act('switch-account', form(rig.app, password=SECRET))
    assert answer[0] == HTTPStatus.CONFLICT
    assert SECRET not in repr(audit.events)
    assert SECRET not in repr(rig.app.action_feedback)


@pytest.mark.parametrize('lane', ['autopilot', 'catalog', 'scalper_paper', 'scalper_recovery', 'unreadable'])
def test_each_paper_lane_or_unknown_status_blocks_switch(rig, lane):
    if lane == 'autopilot':
        rig.app.autopilot = SimpleNamespace(status=lambda: {'state': 'PREPARING'})
    elif lane == 'catalog':
        rig.app.catalog = SimpleNamespace(paper_view=lambda: {'running': True})
    elif lane == 'scalper_paper':
        rig.app.scalper_paper = SimpleNamespace(status=lambda: {'state': 'PAUSED', 'recovery': []})
    elif lane == 'scalper_recovery':
        rig.app.scalper_paper = SimpleNamespace(status=lambda: {'state': 'STOPPED', 'recovery': [{}]})
    else:
        def unreadable():
            raise RuntimeError('unavailable')
        rig.app.runner = SimpleNamespace(view=unreadable)
    answer = rig.app.act('switch-account', form(rig.app))
    assert answer[0] == HTTPStatus.CONFLICT and not rig.fake.login_calls
    assert not rig.fake.send_requests


def test_false_return_after_actual_account_change_is_still_unverified(rig):
    original_login = rig.fake.login
    def false_after_change(*args, **kwargs):
        original_login(*args, **kwargs)
        return False
    rig.fake.login = false_after_change
    answer = rig.app.act('switch-account', form(rig.app))
    assert answer[0] == HTTPStatus.CONFLICT and 'Actual account: REAL' in answer[2]
    assert not rig.adapter._switch_identity_verified and not rig.adapter.armed
    assert not rig.fake.send_requests


def test_password_field_cleared_even_when_nonce_is_wrong(rig):
    payload = form(rig.app, password=SECRET)
    password_list = payload['switch_password']
    payload['nonce'] = ['wrong']
    result = rig.app.act('switch-account', payload)
    assert result[0] == HTTPStatus.FORBIDDEN
    assert 'switch_password' not in payload and password_list == []
    assert SECRET not in repr(result) and not rig.fake.login_calls


def test_prepared_one_shot_owner_blocks_switch(rig):
    rig.live.scalper_bridge = SimpleNamespace(_stack_prepared=True)
    result = rig.app.act('switch-account', form(rig.app))
    assert result[0] == HTTPStatus.CONFLICT
    assert 'one-shot owner' in result[2] and not rig.fake.login_calls


def test_stale_one_shot_planning_stack_is_released_before_switch(rig):
    class Loader:
        _owner_host = None
        _planning_platform = object()
        released = False
        def release(self):
            self.released = True
            self._planning_platform = None
    loader = Loader()
    rig.live.scalper_bridge = SimpleNamespace(_stack_prepared=False, stack_loader=loader)
    result = rig.app.act('switch-account', form(rig.app))
    assert result[0] == HTTPStatus.SEE_OTHER and loader.released
