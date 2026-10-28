"""Synthetic NO_SEND controls for public CI selection and diagnostics."""
import ast
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_public_tests as runner
from scripts import public_test_telemetry as telemetry
from scripts import publication_guard as guard


GOAT_UNATTENDED_MODULE = 'tests/test_goat_unattended.py'
SYNTHETIC_WITNESS_MODULE = 'tests/test_synthetic_goat_batch_witness.py'
MEASURED_PARTITIONS = (
    '583019901ff74d5050ee63cd3967d9b12815e7a04cb19deb788dbedbc8eef9bd',
    '8fcd8a6f9089a5f0b3d580931efb588f42140e98cf127501873e58f9eec28cbd',
    'befb70dc1796f57ba86aa85a55cf364556267e385b69abbd77d1bdb6caeb7386',
)


def _synthetic_schedule(public):
    weights = {p: 1 for p in public}
    return {'version': 2, 'strategy': 'measured-module-lpt-39696534289',
            'measurement_source': runner.TIMING_SOURCE, 'module_seconds': weights,
            'shards': runner.measured_shards(weights)}


def test_exact_public_partition_and_reviewed_deselections():
    files, deselect = runner.load_classification()
    public = sorted(p for p, meta in files.items() if meta['status'] == 'public')
    buckets = runner.balanced_shards(public, 3)
    flat = sum(buckets, [])
    assert len(flat) == len(set(flat)) == len(public) == 197
    assert set(flat) == set(public)
    assert SYNTHETIC_WITNESS_MODULE in flat
    assert flat.count(GOAT_UNATTENDED_MODULE) == 1
    assert runner.balanced_shards(list(reversed(public)), 3) == buckets
    assert runner.balanced_shards(public, 1) == [public]
    assert sum(v['status'] == 'private' for v in files.values()) == 2
    assert sum(v['status'] == 'historical' for v in files.values()) == 7
    assert not set(flat) & {p for p, v in files.items() if v['status'] != 'public'}
    schedule = runner.read_json(runner.SCHEDULE)
    assert schedule['measurement_source'] == runner.TIMING_SOURCE
    assert risk.digest(schedule['module_seconds']) == 'e4395d5152c1439d71a9cb16c9e05ab74832769e0c385c465868d7db90ff77b9'
    # Original published weights and hosted provenance remain independently pinned.
    assert risk.digest(schedule['published_schedule']['module_seconds']) == (
        '51c7190fc112d8dcbea59d8b45b316a01d140565abf86d0bd7c0a1211e02d8aa')
    assert risk.digest({k: schedule[k] for k in ('measurement_source', 'artifacts', 'old_shards')}) == (
        '5847227f4168111d9ed24c394a045f885dcdd6309945c83e29b8fdaca9a9649e')
    assert risk.digest(schedule['supplemental_measurements'][0]) == '7c12a824bacefa730cdcef3ddf20f412f201d306bcf098aecba78bd1ce64f830'
    for bucket, expected in zip(buckets, MEASURED_PARTITIONS, strict=True):
        assert hashlib.sha256('\n'.join(bucket).encode()).hexdigest() == expected
    assert [len(b) for b in buckets] == [64, 67, 66]
    assert [sum(schedule['module_seconds'][p] for p in b) for b in buckets] == pytest.approx(
        [3200.201488, 3200.2001272, 3200.1936559])
    assert schedule['shards'] == runner.measured_shards(schedule['module_seconds'])
    assert max(schedule['variability']['conservative_seconds']) < 4500
    assert max(schedule['variability']['sensitivity_seconds']) < 4500
    assert {f'tests/test_goat_periodic_audit_finalization_part_{i}.py' for i in range(1, 5)} <= set(flat)
    assert {'tests/test_completed_uncertain_history.py', 'tests/test_retroactive_transactions.py',
            'tests/test_auxiliary_response_reconciliation.py', 'tests/test_goat_batch_plan.py',
            'tests/test_experimental_goat_audit_proof.py', 'tests/test_goat_batch_standalone_receipts.py',
            'tests/test_production_paid_request_journal.py', 'tests/test_production_run_lock.py',
            'tests/test_build_publication_chapters.py', 'tests/test_build_publication_epub.py'} <= set(flat)
    assert len(deselect) == 15
    assert hashlib.sha256(json.dumps(deselect, separators=(',', ':')).encode()).hexdigest() == (
        '792817b3b0524eb25d6bb0e9267327ce85911c5c7ea9058cda53b979f708fc32')


@pytest.fixture
def synthetic_classification(tmp_path, monkeypatch):
    repo = tmp_path / 'synthetic_public'
    (repo / 'tests').mkdir(parents=True)
    dpapi_file = runner.DPAPI_NODE.split('::')[0]
    files = {dpapi_file: {'status': 'public'}, 'tests/test_private.py': {'status': 'private'},
             'tests/test_historical.py': {'status': 'historical'}}
    for rel in files:
        (repo / rel).write_text('# synthetic\n', encoding='utf-8')
    path = repo / 'tests/classification.json'
    data = {'version': 2, 'files': files, 'deselect': [runner.DPAPI_NODE],
            'deselect_reason': {runner.DPAPI_NODE: 'real DPAPI excluded'}}
    path.write_text(json.dumps(data), encoding='utf-8')
    monkeypatch.setattr(runner, 'REPO', repo)
    monkeypatch.setattr(runner, 'CLASSIFICATION', path)
    return path, data


@pytest.mark.parametrize('case', ['missing', 'stale', 'status', 'duplicate', 'no_dpapi',
                                  'unknown_node', 'private_node', 'duplicate_node', 'no_reason', 'version'])
def test_classification_refuses_invalid_inputs(synthetic_classification, case):
    path, data = synthetic_classification
    if case == 'missing':
        data['files'].pop('tests/test_private.py')
    elif case == 'stale':
        data['files']['tests/test_stale.py'] = {'status': 'public'}
    elif case == 'status':
        data['files']['tests/test_private.py']['status'] = 'unreviewed'
    elif case == 'duplicate':
        raw = json.dumps(data).replace('"version": 2', '"version": 2, "version": 2')
        path.write_text(raw, encoding='utf-8')
        with pytest.raises(ValueError, match='duplicate'):
            runner.load_classification()
        return
    elif case == 'no_dpapi':
        data['deselect'] = []
        data['deselect_reason'] = {}
    elif case in {'unknown_node', 'private_node'}:
        node = ('tests/test_unknown.py' if case == 'unknown_node' else 'tests/test_private.py') + '::test_x'
        data['deselect'].append(node)
        data['deselect_reason'][node] = 'synthetic invalid node'
    elif case == 'duplicate_node':
        data['deselect'].append(runner.DPAPI_NODE)
    elif case == 'no_reason':
        data['deselect_reason'] = {}
    else:
        data['version'] = 999
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError):
        runner.load_classification()


def test_missing_classification_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'CLASSIFICATION', tmp_path / 'absent.json')
    with pytest.raises(OSError):
        runner.load_classification()


@pytest.mark.parametrize('case', ['empty', 'duplicate', 'missing', 'extra', 'bad_shards', 'empty_bucket', 'stale', 'tampered', 'weight', 'unknown_weight', 'nan', 'private'])
def test_partition_negative_controls(tmp_path, monkeypatch, case):
    public = ['tests/test_a.py', 'tests/test_b.py', 'tests/test_c.py']
    data = _synthetic_schedule(public)
    shards = 3
    if case == 'empty':
        public = []
    elif case == 'duplicate':
        data['shards'][0].append(public[1])
    elif case == 'missing':
        public.append('tests/test_new.py')
    elif case == 'extra':
        data['shards'][0].append('tests/test_excluded.py')
    elif case == 'bad_shards':
        shards = 2
    elif case == 'empty_bucket':
        data['shards'][0] = []
    elif case == 'stale':
        data['measurement_source'] = dict(runner.TIMING_SOURCE, tree='f' * 40)
    elif case == 'tampered':
        data['shards'][0], data['shards'][1] = data['shards'][1], data['shards'][0]
    elif case == 'weight':
        data['module_seconds'].pop(public[0])
    elif case == 'unknown_weight':
        data['module_seconds']['tests/test_unknown.py'] = 1
    elif case == 'nan':
        data['module_seconds'][public[0]] = float('nan')
    else:
        data['shards'][0].append('tests/test_private.py')
    path = tmp_path / 'schedule.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    monkeypatch.setattr(runner, 'SCHEDULE', path)
    with pytest.raises(ValueError):
        runner.balanced_shards(public, shards)


def test_partition_never_probes_source_sizes_or_other_drives(monkeypatch):
    files, _ = runner.load_classification()
    public = [p for p, v in files.items() if v['status'] == 'public']
    def forbidden_stat(*args, **kwargs):
        raise AssertionError('allocation must not probe filesystem sizes')
    monkeypatch.setattr(Path, 'stat', forbidden_stat)
    assert len(sum(runner.balanced_shards(public, 3), [])) == 197


@pytest.mark.parametrize('args', [['--shard', '-1'], ['--shard', '3'], ['--shards', '0'],
                                  ['--shards', '2'], ['--timeout-seconds', '0'],
                                  ['--timeout-seconds', '4501']])
def test_invalid_cli_refused_before_execution(args, monkeypatch):
    monkeypatch.setattr(runner, 'load_classification', lambda: pytest.fail('must refuse before selection'))
    with pytest.raises(SystemExit) as exc:
        runner.main(args)
    assert exc.value.code == 2


def test_dirty_or_changed_source_fails_closed(monkeypatch):
    monkeypatch.setattr(runner, 'git', lambda *args: ' M synthetic.py')
    with pytest.raises(ValueError, match='dirty'):
        runner.source_identity()
    before = {'commit': 'a', 'tree': 'b', 'index_tree': 'b'}
    monkeypatch.setattr(runner, 'source_identity', lambda: dict(before, commit='c'))
    with pytest.raises(ValueError, match='changed'):
        runner.assert_source_unchanged(before)


def test_git_identity_commands_are_confined_to_public_repo(monkeypatch):
    calls = []
    def fake_output(command, **kwargs):
        calls.append(command)
        return b'' if 'status' in command else b'synthetic\n'
    monkeypatch.setattr(subprocess, 'check_output', fake_output)
    runner.source_identity()
    assert calls and all(c[:3] == ['git', '-C', str(runner.REPO)] for c in calls)


def test_bounded_timeout_kills_and_returns_nonzero(monkeypatch):
    class Process:
        def wait(self, timeout):
            raise subprocess.TimeoutExpired('synthetic', timeout)
    process = Process()
    stopped = []
    ticks = iter([0, 0, 1, 2])
    monkeypatch.setattr(runner, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda *a, **k: process)
    monkeypatch.setattr(runner, 'stop_process', lambda p: stopped.append(p))
    assert runner.run_bounded(['synthetic'], 1) == 124
    assert stopped == [process]


def test_bounded_preserves_pytest_failure(monkeypatch):
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda *a, **k: SimpleNamespace(wait=lambda **kw: 5))
    assert runner.run_bounded(['synthetic'], 1) == 5


def test_real_child_timeout_is_bounded():
    # This synthetic sleeper has no file, credential, network or provider access.
    assert runner.run_bounded([sys.executable, '-c', 'import time; time.sleep(30)'], 1) == 124


@pytest.mark.parametrize('path', [runner.REPO, runner.REPO.parent, Path('relative-checkout-child')])
def test_diagnostics_reject_checkout_and_non_temp_paths(path):
    with pytest.raises(ValueError):
        runner.external_directory(path)


def test_parameter_text_is_not_emitted():
    identity = telemetry.node_identity('tests/test_synthetic.py::test_x[sensitive fixture text]')
    assert identity['function'] == 'test_x'
    assert 'sensitive' not in json.dumps(identity)
    assert len(identity['node_sha256']) == 64


@pytest.fixture
def meter(tmp_path):
    directory = tmp_path / 'diagnostics'
    directory.mkdir()
    runner.write_json(directory / 'selection.json', {
        'source': {'commit': 'synthetic', 'tree': 'synthetic'},
        'selected': ['tests/test_synthetic.py'], 'selected_deselect': ['tests/test_synthetic.py::test_omitted']})
    records = []
    instance = telemetry.Telemetry(directory, send=records.append)
    instance.records = records
    return instance


def test_telemetry_enforces_exact_collection_and_deselection(meter):
    session = SimpleNamespace(items=[SimpleNamespace(nodeid='tests/test_synthetic.py::test_x[fixture]')])
    with pytest.raises(pytest.UsageError):
        meter.pytest_collection_finish(session)
    meter.deselected = ['tests/test_synthetic.py::test_omitted']
    meter.pytest_collection_finish(session)
    assert meter.collection_valid
    assert 'fixture' not in json.dumps(meter.records)
    with pytest.raises(pytest.UsageError):
        meter.safe_node('tests/test_unselected.py::test_x')


@pytest.mark.parametrize('missing', ['collection', 'teardown'])
def test_incomplete_telemetry_cannot_pass(meter, missing):
    meter.collection_valid = missing != 'collection'
    meter.collected = 1
    meter.finished = 0 if missing == 'teardown' else 1
    session = SimpleNamespace(exitstatus=0)
    meter.pytest_sessionfinish(session, 0)
    result = meter.records[-1]
    assert session.exitstatus != 0
    assert not result['successful']


def test_events_survive_incomplete_session_and_exclude_output(meter):
    node = 'tests/test_synthetic.py::test_x[fixture prose]'
    meter.pytest_runtest_logstart(node, None)
    meter.pytest_runtest_logreport(SimpleNamespace(nodeid=node, duration=1.25, when='call', outcome='skipped'))
    events = '\n'.join(json.dumps(r) for r in meter.records)
    assert 'fixture prose' not in events
    assert [json.loads(line)['event'] for line in events.splitlines()] == ['test_start', 'phase_end']
    assert meter.outcomes['call:skipped'] == 1
    assert meter.modules['tests/test_synthetic.py']['phase_seconds'] == 1.25


def test_publication_guard_rejects_missing_inventory_and_symlink(monkeypatch):
    monkeypatch.setattr(guard, 'inventory_from_index', lambda: {'synthetic.py'})
    monkeypatch.setattr(guard, 'git', lambda args: b'120000 abc 0\tsymlink.py\0')
    with pytest.raises(guard.GuardError, match='unsupported index mode'):
        guard.validate_index()
    monkeypatch.setattr(guard, 'git', lambda args: b'')
    with pytest.raises(guard.GuardError, match='empty index'):
        guard.validate_index()


def test_workflow_preserves_full_guarded_windows_suite():
    workflow = (runner.REPO / '.github/workflows/ci.yml').read_text(encoding='utf-8')
    for required in ('contents: read', "python-version: '3.14.4'", 'shard: [0, 1, 2]',
                     'needs: guard', 'fail-fast: false', 'publication_guard.py --push-check',
                     'fetch-depth: 0', 'persist-credentials: false', 'timeout-minutes: 90',
                     '--timeout-seconds 4500', 'if: always()', 'git status --porcelain --untracked-files=no',
                     'if-no-files-found: error', 'RUNNER_TEMP'):
        assert required in workflow
    assert 'continue-on-error' not in workflow
    assert 'pull_request_target' not in workflow
    wrapper = runner.WRAPPER.read_text(encoding='utf-8')
    assert 'socket.connect' in wrapper and 'CryptUnprotectData' in wrapper
    assert 'explicit real-DPAPI node deselection is required' in wrapper

@pytest.fixture
def synthetic_repo(tmp_path):
    repo = tmp_path / 'public-engine'
    (repo / 'scripts').mkdir(parents=True)
    (repo / 'tests').mkdir()
    for rel in ('scripts/run_public_tests.py', 'scripts/public_test_telemetry.py', 'tests/zero_provider_validation.py'):
        (repo / rel).write_bytes((runner.REPO / rel).read_bytes())
    public = [runner.DPAPI_NODE.split('::')[0], 'tests/test_b.py', 'tests/test_c.py']
    for rel in public[1:]:
        (repo / rel).write_text('def test_one():\n    pass\n', encoding='utf-8')
    for rel in ('tests/test_private.py', 'tests/test_historical.py'):
        (repo / rel).write_text('raise AssertionError("excluded module collected")\n', encoding='utf-8')
    (repo / 'marker.txt').write_text('original', encoding='utf-8')
    (repo / 'pytest.ini').write_text('[pytest]\ntestpaths = tests\n', encoding='utf-8')
    (repo / 'tests/public_shard_schedule.json').write_text(json.dumps(_synthetic_schedule(public)), encoding='utf-8')
    files = {p: {'status': 'public'} for p in public}
    files.update({'tests/test_private.py': {'status': 'private'},
                  'tests/test_historical.py': {'status': 'historical'}})
    (repo / 'tests/public_test_classification.json').write_text(json.dumps({
        'version': 2, 'files': files, 'deselect': [runner.DPAPI_NODE],
        'deselect_reason': {runner.DPAPI_NODE: 'synthetic forbidden control'}}), encoding='utf-8')
    return repo, public[0]


@pytest.mark.parametrize('mode', ['pass', 'failure', 'timeout', 'dirty', 'abrupt', 'zero_exit', 'telemetry_failure'])
def test_guarded_selector_end_to_end(synthetic_repo, tmp_path, mode):
    repo, selected = synthetic_repo
    body = '''import socket
import sys
from pathlib import Path
import pytest

def test_real_windows_dpapi_round_trip():
    pytest.fail('must be deselected')

def test_guards_and_capture(capsys):
    assert capsys.readouterr().out == ''
    print('synthetic-output')
    assert capsys.readouterr().out == 'synthetic-output\\n'
    with pytest.raises(RuntimeError, match='network forbidden'):
        socket.getaddrinfo('example.invalid', 443)
    with pytest.raises(RuntimeError, match='real DPAPI forbidden'):
        sys.audit('ctypes.dlsym', None, 'CryptUnprotectData')

@pytest.mark.skip(reason='synthetic skip audit')
def test_skip():
    pass
'''
    body += '''

def test_no_file_io(monkeypatch):
    import builtins
    import os
    def forbidden(*args, **kwargs):
        raise AssertionError('synthetic IO protection')
    monkeypatch.setattr(builtins, 'open', forbidden)
    monkeypatch.setattr(Path, 'open', forbidden)
    monkeypatch.setattr(os, 'getenv', forbidden)
    monkeypatch.setattr(os, 'environ', {})

def test_after_cleanup(tmp_path):
    path = tmp_path / 'synthetic.txt'
    path.write_text('cleanup restored', encoding='utf-8')
    assert path.read_text(encoding='utf-8') == 'cleanup restored'
'''
    if mode == 'failure':
        body += '\ndef test_failure():\n    assert False\n'
    elif mode == 'timeout':
        body += '\ndef test_wait():\n    import time\n    time.sleep(30)\n'
    elif mode == 'dirty':
        body += '\ndef test_mutation():\n    Path("marker.txt").write_text("changed", encoding="utf-8")\n'
    elif mode in {'abrupt', 'zero_exit'}:
        body += '\ndef test_hard_exit():\n    import os\n    os._exit(' + ('0' if mode == 'zero_exit' else '7') + ')\n'
    elif mode == 'telemetry_failure':
        body += '''

def test_reporting_failure(request):
    plugin = request.config.pluginmanager.getplugin('public-ci-telemetry')
    def broken(record):
        raise OSError('synthetic channel failure')
    plugin.send = broken
    assert False, 'primary assertion preserved'
'''
    (repo / selected).write_text(body, encoding='utf-8')
    def git(*args):
        subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, timeout=20)
    git('init', '--quiet')
    git('config', 'user.name', 'Synthetic CI')
    git('config', 'user.email', 'synthetic@example.invalid')
    git('add', 'scripts', 'tests', 'marker.txt', 'pytest.ini')
    git('-c', 'commit.gpgsign=false', 'commit', '--quiet', '-m', 'Synthetic fixture')
    diagnostics = Path(os.environ['TEMP']) / ('synthetic_ci_' + tmp_path.name + mode)
    command = [sys.executable, str(repo / 'scripts/run_public_tests.py'), '--shards', '1',
               '--diagnostics', str(diagnostics), '--timeout-seconds', '5' if mode == 'timeout' else '20']
    proc = subprocess.run(command, cwd=repo, capture_output=True, text=True, timeout=30)
    assert (diagnostics / 'result.json').is_file(), proc.stdout + proc.stderr
    result = runner.read_json(diagnostics / 'result.json')
    manifest = runner.read_json(diagnostics / 'selection.json')
    assert len(manifest['selected']) == 3
    assert len(manifest['excluded']) == 2
    if mode == 'pass':
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert result['status'] == 'passed' and result['source_unchanged']
        times = runner.read_json(diagnostics / 'timings.json')
        assert times['successful'] and times['outcomes']['setup:skipped'] == 1
        assert times['collected'] == times['finished'] == 6
        assert 'synthetic-output' not in (diagnostics / 'events.jsonl').read_text(encoding='utf-8')
    else:
        assert proc.returncode != 0
        assert result['status'] == {'failure': 'failed', 'timeout': 'timeout', 'dirty': 'source_changed', 'abrupt': 'failed', 'zero_exit': 'failed', 'telemetry_failure': 'failed'}[mode]
        assert result['source_unchanged'] == (mode != 'dirty')
        if mode == 'failure':
            assert proc.returncode == 1
            assert runner.read_json(diagnostics / 'timings.json')['complete']
        if mode in {'abrupt', 'zero_exit'}:
            assert not (diagnostics / 'timings.json').exists()
            assert 'test_hard_exit' in (diagnostics / 'events.jsonl').read_text(encoding='utf-8')
        if mode == 'telemetry_failure':
            assert proc.returncode == 1
            assert 'primary assertion preserved' in proc.stdout
            assert 'INTERNALERROR' not in proc.stdout + proc.stderr
        if mode == 'timeout':
            assert result['exit_code'] == 124
            assert 'test_wait' in (diagnostics / 'events.jsonl').read_text(encoding='utf-8')
    assert bool(subprocess.check_output(['git', '-C', str(repo), 'status', '--porcelain', '--untracked-files=no'])) == (mode == 'dirty')

@pytest.mark.parametrize('evidence', ['missing', 'incomplete', 'stale'])
def test_zero_exit_without_matching_completion_is_refused(tmp_path, monkeypatch, evidence):
    before = {'commit': 'synthetic', 'tree': 'synthetic', 'index_tree': 'synthetic'}
    monkeypatch.setattr(runner, 'source_identity', lambda: before.copy())
    diagnostics = tmp_path / 'refused-evidence'
    def fake_success(*args, **kwargs):
        if evidence != 'missing':
            runner.write_json(diagnostics / 'timings.json', {
                'source': dict(before, tree='stale') if evidence == 'stale' else before,
                'successful': evidence != 'incomplete', 'complete': evidence != 'incomplete', 'exit_code': 0})
        return 0
    monkeypatch.setattr(runner, 'run_bounded', fake_success)
    with pytest.raises((OSError, ValueError)):
        runner.main(['--diagnostics', str(diagnostics)])
    result = runner.read_json(diagnostics / 'result.json')
    assert result['status'] == 'incomplete'
    assert result['exit_code'] is None

@pytest.mark.parametrize('exitstatus', [0, 1])
def test_reporting_exception_never_masks_existing_status(meter, exitstatus):
    def broken(record):
        raise OSError('synthetic reporting exception')
    meter.send = broken
    meter.pytest_runtest_logstart('tests/test_synthetic.py::test_x', None)
    assert meter.failed
    session = SimpleNamespace(exitstatus=exitstatus)
    meter.pytest_sessionfinish(session, exitstatus)
    assert session.exitstatus == 1


def test_receiver_rejects_unsanitized_metadata_and_preserves_primary_output(meter, capsys):
    from io import BytesIO
    receiver = runner.MetadataReceiver(meter.directory)
    record = {'event': 'collection_start', 'elapsed_seconds': 0, 'module': 'tests/test_synthetic.py',
              'fixture': 'synthetic fixture text must not be stored'}
    stream = BytesIO(runner.FRAME_PREFIX + json.dumps(record).encode() + b'\nprimary assertion failure\n')
    receiver.read_output(stream)
    assert receiver.failed
    assert not (meter.directory / 'events.jsonl').exists()
    assert 'primary assertion failure' in capsys.readouterr().out


@pytest.mark.parametrize('encoding', ['cp1252', 'ascii', 'utf-8'])
@pytest.mark.parametrize('exitstatus', [0, 1])
def test_receiver_drains_unicode_output_without_losing_terminal_or_failure(meter, monkeypatch, encoding, exitstatus):
    from io import BytesIO, TextIOWrapper
    meter.deselected = ['tests/test_synthetic.py::test_omitted']
    node = 'tests/test_synthetic.py::test_x'
    meter.pytest_collection_finish(SimpleNamespace(items=[SimpleNamespace(nodeid=node)]))
    for phase in ('setup', 'call', 'teardown'):
        outcome = 'failed' if exitstatus and phase == 'call' else 'passed'
        meter.pytest_runtest_logreport(SimpleNamespace(nodeid=node, duration=0, when=phase, outcome=outcome))
    meter.pytest_sessionfinish(SimpleNamespace(exitstatus=exitstatus), exitstatus)
    raw = 'primary assertion: \u7532 \ud55c\uad6d\n'
    frames = b''.join(runner.FRAME_PREFIX + json.dumps(r).encode() + b'\n' for r in meter.records)
    output = BytesIO()
    sink = TextIOWrapper(output, encoding=encoding, errors='strict', write_through=True)
    receiver = runner.MetadataReceiver(meter.directory)
    with monkeypatch.context() as scope:
        scope.setattr(sys, 'stdout', sink)
        receiver.read_output(BytesIO(raw.encode('utf-8') + frames + b'last line\n'))
    assert not receiver.failed and receiver.terminal and receiver.collection_valid
    assert b'primary assertion:' in output.getvalue() and b'last line' in output.getvalue()
    times = runner.read_json(meter.directory / 'timings.json')
    assert times['exit_code'] == exitstatus and times['successful'] == (exitstatus == 0)
    assert 'primary assertion:' not in (meter.directory / 'events.jsonl').read_text()


@pytest.mark.parametrize('case', ['truncated', 'oversized', 'budget'])
def test_receiver_rejects_incomplete_or_excessive_frames(meter, monkeypatch, case):
    from io import BytesIO
    receiver = runner.MetadataReceiver(meter.directory)
    if case == 'truncated':
        frame = runner.FRAME_PREFIX + b'{'
    elif case == 'oversized':
        frame = runner.FRAME_PREFIX + b'x' * runner.MAX_FRAME_BYTES + b'\n'
    else:
        monkeypatch.setattr(runner, 'MAX_EVENTS', 0)
        frame = runner.FRAME_PREFIX + json.dumps({
            'event': 'collection_start', 'elapsed_seconds': 0, 'module': 'tests/test_synthetic.py'}).encode() + b'\n'
    receiver.read_output(BytesIO(frame))
    assert receiver.failed
    assert not (meter.directory / 'events.jsonl').exists()


def test_phase_hooks_perform_no_filesystem_writes_before_cleanup(meter, monkeypatch):
    import builtins
    def forbidden(*args, **kwargs):
        raise AssertionError('synthetic phase forbids filesystem I/O')
    node = 'tests/test_synthetic.py::test_x'
    with monkeypatch.context() as scope:
        scope.setattr(builtins, 'open', forbidden)
        scope.setattr(Path, 'open', forbidden)
        meter.pytest_runtest_logstart(node, None)
        meter.pytest_runtest_call(SimpleNamespace(nodeid=node))
        meter.pytest_runtest_logreport(SimpleNamespace(nodeid=node, duration=1, when='call', outcome='passed'))
        meter.pytest_runtest_teardown(SimpleNamespace(nodeid=node))
        meter.pytest_sessionfinish(SimpleNamespace(exitstatus=1), 1)
    assert not meter.failed
    assert meter.records[-1]['event'] == 'session_finish'
    assert not (meter.directory / 'timings.json').exists()

@pytest.mark.parametrize('exit_code', [0, 1])
def test_parent_persistence_failure_cannot_pass_or_hide_primary(meter, monkeypatch, capsys, exit_code):
    def broken(self, record):
        raise OSError('synthetic parent persistence failure')
    monkeypatch.setattr(runner.MetadataReceiver, 'accept', broken)
    record = {'event': 'collection_start', 'elapsed_seconds': 0, 'module': 'tests/test_synthetic.py'}
    frame = '\n' + runner.FRAME_PREFIX.decode() + json.dumps(record) + '\n'
    code = 'import sys; print(' + repr(frame) + "); print('primary failure retained'); sys.exit(" + str(exit_code) + ')'
    result = runner.run_bounded([sys.executable, '-c', code], 20, diagnostics=meter.directory)
    assert result == (exit_code or 2)
    assert 'primary failure retained' in capsys.readouterr().out

# Qualification controls retain the exact public exclusions and deselections.
from scripts import public_ci as risk


def _change(path, status='M', target=None):
    return {'status': status, 'paths': [path] if target is None else [path, target]}


def test_risk_policy_has_demonstrated_narrow_coverage():
    files, _ = runner.load_classification()
    policy = risk.load_policy(files)
    assert set(policy['impact']) == set(risk.L2_IMPACT)
    assert policy['non_normative_docs'] == ['docs/DEVELOPMENT_TIPS.md']
    for caller in ('audit_semantic_completeness_all', 'audit_semantic_completeness_candidates', 'audit_semantic_kinship'):
        assert 'from source_language_filter import source_language_matches' in (runner.REPO / (caller + '.py')).read_text(encoding='utf-8')
        module = 'tests/test_' + caller + '.py'
        assert module in policy['impact']['source_language_filter.py']['modules']
        assert 'zh-Hant' in (runner.REPO / module).read_text(encoding='utf-8')


@pytest.mark.parametrize('paths,kind,expected', [
    (['docs/DEVELOPMENT_TIPS.md'], 'pull_request', 'L1'),
    (['source_language_filter.py'], 'pull_request', 'L2'),
    (['source_language_filter.py', 'docs/DEVELOPMENT_TIPS.md'], 'pull_request', 'L3'),
    (['README.md'], 'pull_request', 'L3'),
    (['AGENTS.md'], 'pull_request', 'L3'),
    (['docs/OPERATIONS.md'], 'pull_request', 'L3'),
    (['docs/public_ci_reliability.md'], 'pull_request', 'L3'),
    (['pyproject.toml'], 'pull_request', 'L3'),
    (['pytest.ini'], 'pull_request', 'L3'),
    (['.github/workflows/ci.yml'], 'pull_request', 'L3'),
    (['scripts/public_ci.py'], 'pull_request', 'L3'),
    (['scripts/public_ci_policy.json'], 'pull_request', 'L3'),
    (['scripts/publication_guard.py'], 'pull_request', 'L3'),
    (['scripts/public_test_telemetry.py'], 'pull_request', 'L3'),
    (['tests/zero_provider_validation.py'], 'pull_request', 'L3'),
    (['command_code_provider_credentials.py'], 'pull_request', 'L3'),
    (['unclassified.py'], 'pull_request', 'L3'),
    (['tests/test_new.py'], 'pull_request', 'L3'),
    ([], 'pull_request', 'L3'),
    (['docs/DEVELOPMENT_TIPS.md'], 'push', 'L3'),
    (['source_language_filter.py'], 'workflow_dispatch', 'L3'),
])
def test_risk_tier_selection(paths, kind, expected):
    policy = risk.load_policy(runner.load_classification()[0])
    assert risk.classify([_change(p) for p in paths], kind, policy)[0] == expected


@pytest.mark.parametrize('status', ['A', 'D', 'T', 'R100', 'C100', 'U'])
def test_risk_non_modifications_always_full(status):
    policy = risk.load_policy(runner.load_classification()[0])
    change = _change('source_language_filter.py', status,
                     'renamed.py' if status.startswith(('R', 'C')) else None)
    assert risk.classify([change], 'pull_request', policy)[0] == 'L3'


@pytest.mark.parametrize('case', ['absent', 'changed', 'version', 'empty', 'private', 'unknown', 'duplicate', 'rationale', 'risky', 'smoke', 'partial', 'missing_source'])
def test_risk_bad_policy_never_selects_lower_tier(monkeypatch, case):
    files, _ = runner.load_classification()
    original = risk.load_policy(files)
    if case in {'absent', 'changed'}:
        assert risk.classify([_change('source_language_filter.py')], 'pull_request',
                             None if case == 'absent' else original, case != 'changed')[0] == 'L3'
        return
    policy = json.loads(json.dumps(original))
    entry = policy['impact']['source_language_filter.py']
    if case == 'version':
        policy['version'] = 999
    elif case == 'empty':
        entry['modules'] = []
    elif case in {'private', 'unknown'}:
        entry['modules'] = [next(p for p, v in files.items() if v['status'] == 'private') if case == 'private' else 'tests/test_missing.py']
    elif case == 'duplicate':
        entry['modules'] *= 2
    elif case == 'rationale':
        entry['reason'] = ''
    elif case == 'partial':
        entry['modules'].pop()
    elif case == 'missing_source':
        policy['impact'].pop('semantic_kinship.py')
    elif case == 'risky':
        policy['impact']['command_code_provider_credentials.py'] = entry
    else:
        policy['smoke'] = []
    monkeypatch.setattr(runner, 'read_json', lambda p: policy)
    with pytest.raises(ValueError):
        risk.load_policy(files)


@pytest.fixture
def risk_identity(monkeypatch):
    source = {'commit': 'a' * 40, 'tree': 'b' * 40, 'index_tree': 'b' * 40}
    event = {'number': 7, 'repository': {'full_name': 'synthetic/public'},
             'pull_request': {'head': {'sha': 'c' * 40}, 'base': {'sha': 'd' * 40, 'ref': 'main'}}}
    env = {'GITHUB_SHA': source['commit'], 'GITHUB_EVENT_NAME': 'pull_request',
           'GITHUB_REF': 'refs/pull/7/merge', 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1',
           'GITHUB_REPOSITORY': 'synthetic/public'}
    original_git = runner.git
    def fake_git(*args):
        if args[0] == 'rev-list':
            return ' '.join([source['commit'], event['pull_request']['base']['sha'], event['pull_request']['head']['sha']])
        if args[0] == 'rev-parse':
            return 'e' * 40
        if args[0] == 'diff':
            return 'M\0docs/DEVELOPMENT_TIPS.md\0'
        return original_git(*args)
    monkeypatch.setattr(runner, 'source_identity', lambda: dict(source))
    monkeypatch.setattr(runner, 'git', fake_git)
    return source, event, env


@pytest.mark.parametrize('case', ['sha', 'tree', 'parents', 'base', 'head', 'ref', 'event', 'run', 'attempt', 'repository'])
def test_risk_identity_rejects_conflicts(risk_identity, monkeypatch, case):
    source, event, env = risk_identity
    if case == 'sha':
        env['GITHUB_SHA'] = 'f' * 40
    elif case == 'tree':
        source['index_tree'] = 'f' * 40
    elif case == 'parents':
        monkeypatch.setattr(runner, 'git', lambda *args: ' '.join(['a' * 40, 'c' * 40, 'd' * 40]))
    elif case in {'head', 'base'}:
        event['pull_request'][case]['sha'] = 'not-a-sha'
    elif case == 'ref':
        env['GITHUB_REF'] = 'refs/heads/topic'
    elif case == 'event':
        env['GITHUB_EVENT_NAME'] = 'pull_request_target'
    elif case in {'run', 'attempt'}:
        env['GITHUB_RUN_ID' if case == 'run' else 'GITHUB_RUN_ATTEMPT'] = ''
    else:
        env['GITHUB_REPOSITORY'] = 'other/repo'
    with pytest.raises(ValueError):
        risk.identity(event, env)


@pytest.mark.parametrize('tier', ['L1', 'L2', 'L3'])
def test_risk_decision_binds_exact_delta_and_selection(risk_identity, monkeypatch, tier):
    _, event, env = risk_identity
    path = {'L1': 'docs/DEVELOPMENT_TIPS.md', 'L2': 'source_language_filter.py', 'L3': 'scripts/public_ci.py'}[tier]
    calls = []
    original = runner.git
    def git(*args):
        if args[0] == 'diff':
            calls.append(args)
            return 'M\0' + path + '\0'
        return original(*args)
    monkeypatch.setattr(runner, 'git', git)
    decision = risk.decide(event, env)
    assert decision['tier'] == tier
    assert decision['identity']['head'] == 'c' * 40 and decision['identity']['base'] == 'd' * 40
    assert calls == [('diff', '--name-status', '-z', '--find-renames', 'd' * 40, 'a' * 40)]
    assert decision['l4'].startswith('NOT VERIFIED')
    assert risk.select(decision, 3 if tier == 'L3' else 1, 0)[1]
    with pytest.raises(ValueError):
        risk.select(decision, 1 if tier == 'L3' else 3, 0)
    assert set(risk.SMOKE) <= set(decision['selected'])
    if tier == 'L3':
        assert len(decision['selected']) == 197 and len(decision['deselect']) == 15
    elif tier == 'L2':
        assert len(decision['selected']) == 5


@pytest.mark.parametrize('case', ['missing', 'invalid', 'stale'])
def test_risk_decide_falls_back_for_unusable_policy(risk_identity, monkeypatch, case):
    _, event, env = risk_identity
    if case in {'missing', 'invalid'}:
        def fail(files):
            raise (OSError if case == 'missing' else ValueError)('synthetic policy failure')
        monkeypatch.setattr(risk, 'load_policy', fail)
    else:
        original = runner.git
        monkeypatch.setattr(runner, 'git', lambda *args: 'f' * 40 if args[0] == 'rev-parse' and args[1].startswith('d') else original(*args))
    assert risk.decide(event, env)['tier'] == 'L3'


@pytest.mark.parametrize('field', ['tier', 'identity', 'selected', 'policy_sha', 'excluded', 'changes'])
def test_risk_tampered_decision_rejected(risk_identity, monkeypatch, tmp_path, field):
    _, event, env = risk_identity
    decision = risk.decide(event, env)
    monkeypatch.setattr(risk, 'environment_decision', lambda: decision)
    altered = dict(decision, **{field: None})
    path = tmp_path / 'decision.json'
    runner.write_json(path, altered)
    with pytest.raises(ValueError, match='tampered'):
        risk.verified_decision(path)


def _risk_jobs(tier):
    return {job: {'result': 'success' if job in {'guard', risk.JOBS[tier]} else 'skipped'}
            for job in ('guard', 'l1', 'l2', 'test')}


@pytest.mark.parametrize('tier', ['L1', 'L2', 'L3'])
@pytest.mark.parametrize('bad', ['missing', 'cancelled', 'skipped', 'failure', 'timed_out', 'pending', None])
def test_risk_required_job_negative_controls(tier, bad):
    jobs = _risk_jobs(tier)
    selected = risk.JOBS[tier]
    if bad == 'missing':
        jobs.pop(selected)
    else:
        jobs[selected]['result'] = bad
    with pytest.raises(ValueError):
        risk.verify_jobs(tier, jobs)


@pytest.mark.parametrize('case', ['guard_skipped', 'extra', 'unexpected_success'])
def test_risk_guard_and_unselected_jobs_checked(case):
    jobs = _risk_jobs('L1')
    if case == 'guard_skipped':
        jobs['guard']['result'] = 'skipped'
    elif case == 'extra':
        jobs['l4'] = {'result': 'success'}
    else:
        jobs['l2']['result'] = 'success'
    with pytest.raises(ValueError):
        risk.verify_jobs('L1', jobs)


@pytest.mark.parametrize('field', ['head', 'base', 'merge', 'state', 'number'])
def test_risk_live_pr_staleness(risk_identity, field):
    _, event, env = risk_identity
    bound = risk.identity(event, env)
    live = {'head': {'sha': bound['head']}, 'base': {'sha': bound['base']},
            'merge_commit_sha': bound['commit'], 'state': 'open', 'number': 7}
    risk.verify_live(bound, live)
    if field in {'head', 'base'}:
        live[field]['sha'] = 'f' * 40
    else:
        live[{'merge': 'merge_commit_sha', 'state': 'state', 'number': 'number'}[field]] = None
    with pytest.raises(ValueError):
        risk.verify_live(bound, live)


def _risk_write_evidence(directory, manifest, skipped_module=None):
    directory.mkdir()
    runner.write_json(directory / 'selection.json', manifest)
    receiver = runner.MetadataReceiver(directory)
    def emit(event, **fields):
        receiver.accept({'event': event, 'elapsed_seconds': 0, **fields})
    nodes = [telemetry.node_identity(p + '::test_synthetic') for p in manifest['selected']]
    for node in nodes:
        emit('collection_node', **node)
    emit('collection_finish', tests=len(nodes), deselected=len(manifest['selected_deselect']),
         deselected_sha256=[hashlib.sha256(n.encode()).hexdigest() for n in manifest['selected_deselect']], skipped_modules=[])
    for node in nodes:
        for phase in ('setup', 'call', 'teardown'):
            emit('phase_end', **node, phase=phase, seconds=0,
                 outcome='skipped' if phase == 'call' and node['module'] == skipped_module else 'passed')
    emit('session_finish', source=manifest['source'], complete=True, successful=True,
         collected=len(nodes), finished=len(nodes), exit_code=0, outcomes=receiver.outcomes,
         modules={})
    runner.write_json(directory / 'result.json', {'status': 'passed', 'exit_code': 0, 'source_unchanged': True})


@pytest.mark.parametrize('tier', ['L1', 'L2', 'L3'])
def test_risk_gate_integrated_happy_paths(risk_identity, monkeypatch, tmp_path, tier):
    _, event, env = risk_identity
    path = {'L1': 'docs/DEVELOPMENT_TIPS.md', 'L2': 'source_language_filter.py', 'L3': 'AGENTS.md'}[tier]
    original = runner.git
    monkeypatch.setattr(runner, 'git', lambda *args: 'M\0' + path + '\0' if args[0] == 'diff' else original(*args))
    decision = risk.decide(event, env)
    guard = {'version': 1, 'decision_sha256': risk.digest(decision), 'identity': decision['identity']}
    evidence = tmp_path / 'evidence'
    evidence.mkdir()
    source = runner.source_identity()
    for i, selected in enumerate(decision['shards']):
        manifest = runner.selection_manifest(source, decision['shards'], i, selected, decision['deselect'], 4500, decision)
        _risk_write_evidence(evidence / f'public-ci-123-1-{i}', manifest)
    live = {'state': 'open', 'number': 7, 'head': {'sha': 'c' * 40}, 'base': {'sha': 'd' * 40}, 'merge_commit_sha': 'a' * 40}
    risk.gate(decision, guard, _risk_jobs(tier), live, evidence)
    # Every required shard must exist; even L1's smoke is mandatory.
    (evidence / 'public-ci-123-1-0').rename(evidence / 'unexpected-shard')
    with pytest.raises(ValueError, match='shard evidence'):
        risk.gate(decision, guard, _risk_jobs(tier), live, evidence)


@pytest.mark.parametrize('case', ['no_events', 'no_result', 'no_timings', 'no_collection', 'incomplete', 'exit', 'dirty', 'identity',
                                  'selection', 'deselect', 'excluded', 'timings', 'collection', 'truncated', 'phase', 'no_terminal', 'extra_event', 'all_skipped'])
def test_risk_gate_rejects_tampered_or_incomplete_evidence(meter, case):
    directory = meter.directory.parent / ('evidence_' + case)
    manifest = {'source': {'commit': 'a' * 40, 'tree': 'b' * 40}, 'selected': ['tests/test_synthetic.py'],
                'selected_deselect': [], 'excluded': {'tests/test_private.py': {'status': 'private'}}}
    _risk_write_evidence(directory, manifest)
    if case.startswith('no_') and case != 'no_terminal':
        filename = {'no_events': 'events.jsonl', 'no_result': 'result.json', 'no_timings': 'timings.json', 'no_collection': 'collected.json'}[case]
        (directory / filename).unlink()
    elif case in {'incomplete', 'exit', 'dirty'}:
        result = runner.read_json(directory / 'result.json')
        result[{'incomplete': 'status', 'exit': 'exit_code', 'dirty': 'source_unchanged'}[case]] = None
        runner.write_json(directory / 'result.json', result)
    elif case in {'identity', 'selection', 'deselect', 'excluded'}:
        data = dict(manifest)
        data[{'identity': 'source', 'selection': 'selected', 'deselect': 'selected_deselect', 'excluded': 'excluded'}[case]] = None
        runner.write_json(directory / 'selection.json', data)
    elif case in {'timings', 'collection'}:
        filename = 'timings.json' if case == 'timings' else 'collected.json'
        runner.write_json(directory / filename, {})
    else:
        path = directory / 'events.jsonl'
        records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        if case == 'truncated':
            path.write_text('{', encoding='utf-8')
        else:
            if case == 'phase':
                records[2]['node_sha256'] = 'f' * 64
            elif case == 'no_terminal':
                records.pop()
            elif case == 'extra_event':
                records.append(records[0])
            elif case == 'all_skipped':
                for record in records:
                    if record.get('phase') == 'call':
                        record['outcome'] = 'skipped'
                records[-1]['outcomes'] = {'setup:passed': 1, 'call:skipped': 1, 'teardown:passed': 1}
                times = {k: v for k, v in records[-1].items() if k != 'event'}
                runner.write_json(directory / 'timings.json', times)
            path.write_text(''.join(json.dumps(r) + '\n' for r in records), encoding='utf-8')
    with pytest.raises((ValueError, OSError)):
        risk.verify_evidence(directory, manifest, 'L2')


def test_risk_workflow_always_gate_and_exact_attempt_artifacts():
    workflow = (runner.REPO / '.github/workflows/ci.yml').read_text(encoding='utf-8')
    assert 'name: Public CI gate\n    if: always()' in workflow
    assert 'needs: [guard, l1, l2, test]' in workflow
    assert 'public_ci.py gate' in workflow
    assert 'github.rest.pulls.get' in workflow
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in workflow
    assert 'public-decision-${{ github.run_id }}-${{ github.run_attempt }}' in workflow
    assert workflow.count('--decision "${{ runner.temp }}/qualification/decision.json"') == 3
    assert 'paths-ignore' not in workflow and 'continue-on-error' not in workflow
    assert workflow.count('name: Isolate TEMP/TMP to the runner temp root') == 5

@pytest.mark.parametrize('tier', ['L1', 'L2', 'L3'])
def test_risk_real_merge_selected_runner_and_gate(synthetic_repo, tmp_path, monkeypatch, tier):
    """No provider calls: real Git parents, exact delta, real guarded child telemetry."""
    repo, dpapi_file = synthetic_repo
    for name in ('public_ci.py', 'public_ci_policy.json'):
        (repo / 'scripts' / name).write_bytes((runner.REPO / 'scripts' / name).read_bytes())
    modules = sorted({dpapi_file, *risk.SMOKE, *(p for tests in risk.L2_IMPACT.values() for p in tests)})
    for module in modules:
        body = 'def test_one():\n    pass\n'
        if module == dpapi_file:
            body += '\ndef test_real_windows_dpapi_round_trip():\n    assert False\n'
        (repo / module).write_text(body, encoding='utf-8')
    for name in ('test_b.py', 'test_c.py', 'test_private.py', 'test_historical.py'):
        (repo / 'tests' / name).unlink()
    files = {p: {'status': 'public'} for p in modules}
    runner.write_json(repo / 'tests/public_test_classification.json', {
        'version': 2, 'files': files, 'deselect': [runner.DPAPI_NODE],
        'deselect_reason': {runner.DPAPI_NODE: 'synthetic forbidden node'}})
    runner.write_json(repo / 'tests/public_shard_schedule.json', _synthetic_schedule(modules))
    (repo / 'docs').mkdir()
    (repo / 'docs/DEVELOPMENT_TIPS.md').write_text('Synthetic navigation tips\n', encoding='utf-8')
    for source_path in risk.L2_IMPACT:
        (repo / source_path).write_bytes((runner.REPO / source_path).read_bytes())
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], timeout=20).decode().strip()
    git('init', '--quiet', '-b', 'main')
    git('config', 'user.name', 'Synthetic CI')
    git('config', 'user.email', 'synthetic@example.invalid')
    git('add', 'scripts', 'tests', 'docs', *risk.L2_IMPACT, 'marker.txt', 'pytest.ini')
    git('-c', 'commit.gpgsign=false', 'commit', '--quiet', '-m', 'Synthetic base')
    git('checkout', '--quiet', '-b', 'topic')
    changed = {'L1': 'docs/DEVELOPMENT_TIPS.md', 'L2': 'source_language_filter.py', 'L3': 'scripts/public_ci.py'}[tier]
    with (repo / changed).open('a', encoding='utf-8') as stream:
        stream.write('\n# Synthetic change\n')
    git('add', changed)
    git('-c', 'commit.gpgsign=false', 'commit', '--quiet', '-m', 'Synthetic change')
    head = git('rev-parse', 'HEAD')
    git('checkout', '--quiet', 'main')
    (repo / 'marker.txt').write_text('Synthetic base advanced\n', encoding='utf-8')
    git('add', 'marker.txt')
    git('-c', 'commit.gpgsign=false', 'commit', '--quiet', '-m', 'Synthetic base advance')
    base = git('rev-parse', 'HEAD')
    git('-c', 'commit.gpgsign=false', 'merge', '--quiet', '--no-ff', 'topic', '-m', 'Synthetic tested merge')
    merge = git('rev-parse', 'HEAD')
    event = {'number': 7, 'repository': {'full_name': 'synthetic/public'},
             'pull_request': {'head': {'sha': head}, 'base': {'sha': base, 'ref': 'main'}}}
    event_path = tmp_path / 'event.json'
    runner.write_json(event_path, event)
    env = dict(os.environ, GITHUB_SHA=merge, GITHUB_EVENT_NAME='pull_request', GITHUB_REF='refs/pull/7/merge',
               GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1', GITHUB_REPOSITORY='synthetic/public',
               GITHUB_EVENT_PATH=str(event_path), GITHUB_OUTPUT=str(tmp_path / 'github-output'))
    diagnostic_root = Path(os.environ['TEMP']) / ('prb_real_' + tmp_path.name + tier)
    diagnostic_root.mkdir()
    qualification = diagnostic_root / 'qualification'
    proc = subprocess.run([sys.executable, str(repo / 'scripts/public_ci.py'), 'decide', '--directory', str(qualification)],
                          cwd=repo, env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    decision = runner.read_json(qualification / 'decision.json')
    assert decision['tier'] == tier
    assert decision['changes'] == [_change(changed)]  # Excludes base-only advance.
    evidence = diagnostic_root / 'evidence'
    evidence.mkdir()
    for shard in range(3 if tier == 'L3' else 1):
        proc = subprocess.run([sys.executable, str(repo / 'scripts/run_public_tests.py'),
                               '--decision', str(qualification / 'decision.json'), '--shards', '3' if tier == 'L3' else '1',
                               '--shard', str(shard), '--diagnostics', str(evidence / f'public-ci-123-1-{shard}')],
                              cwd=repo, env=env, capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stdout + proc.stderr
    live = tmp_path / 'live.json'
    runner.write_json(live, {'state': 'open', 'number': 7, 'head': {'sha': head}, 'base': {'sha': base}, 'merge_commit_sha': merge})
    proc = subprocess.run([sys.executable, str(repo / 'scripts/public_ci.py'), 'gate', '--directory', str(qualification),
                           '--needs', json.dumps(_risk_jobs(tier)), '--live', str(live), '--evidence', str(evidence)],
                          cwd=repo, env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert 'NOT VERIFIED' in proc.stdout
    # Same head with a different tested merge tree cannot reuse this decision.
    git('checkout', '--quiet', 'main')
    (repo / 'marker.txt').write_text('Different merge tree\n', encoding='utf-8')
    git('add', 'marker.txt')
    git('-c', 'commit.gpgsign=false', 'commit', '--quiet', '-m', 'Synthetic tree drift')
    env['GITHUB_SHA'] = git('rev-parse', 'HEAD')
    proc = subprocess.run([sys.executable, str(repo / 'scripts/public_ci.py'), 'gate', '--directory', str(qualification),
                           '--needs', json.dumps(_risk_jobs(tier)), '--live', str(live), '--evidence', str(evidence)],
                          cwd=repo, env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0

@pytest.mark.parametrize('case', ['digest', 'guard_identity', 'source_tree', 'attempt'])
def test_risk_gate_rejects_guard_and_run_conflicts(risk_identity, tmp_path, case):
    source, event, env = risk_identity
    decision = risk.decide(event, env)
    proof = {'version': 1, 'decision_sha256': risk.digest(decision), 'identity': decision['identity']}
    if case == 'digest':
        proof['decision_sha256'] = 'f' * 64
    elif case == 'guard_identity':
        proof['identity'] = {}
    elif case == 'source_tree':
        source['tree'] = source['index_tree'] = 'f' * 40
    else:
        proof['identity'] = dict(decision['identity'], run={})
    with pytest.raises(ValueError, match='identity'):
        risk.gate(decision, proof, _risk_jobs('L1'), {}, tmp_path)


@pytest.mark.parametrize('path', sorted(risk.L2_IMPACT))
def test_each_reviewed_source_selects_exact_tests(risk_identity, monkeypatch, path):
    _, event, env = risk_identity
    original = runner.git
    monkeypatch.setattr(runner, 'git', lambda *args: 'M\0' + path + '\0' if args[0] == 'diff' else original(*args))
    decision = risk.decide(event, env)
    assert decision['tier'] == 'L2'
    assert decision['selected'] == sorted(set(risk.SMOKE + risk.L2_IMPACT[path]))
    assert decision['shards'] == [decision['selected']]


def test_reviewed_l2_reverse_dependency_closure_is_complete():
    # Scan actual source and tests without importing production code. The audits
    # are CLI leaves; the two helpers have only these reviewed advisory callers.
    paths = [*runner.REPO.glob('*.py'), *(runner.REPO / 'scripts').glob('*.py'),
             *(runner.REPO / 'tests').glob('test_*.py')]
    graph = {}
    for path in paths:
        rel = path.relative_to(runner.REPO).as_posix()
        graph[rel] = set(risk.import_footprint(path)) if path.name in risk.L2_IMPACT else set()
        if path.name not in risk.L2_IMPACT:
            tree = ast.parse(path.read_text(encoding='utf-8-sig'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    graph[rel].update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    graph[rel].add(node.module)
    for source, tests in risk.L2_IMPACT.items():
        affected = {source}
        while True:
            names = {Path(p).stem for p in affected if '/' not in p}
            callers = {p for p, deps in graph.items() if names & deps}
            if callers <= affected:
                break
            affected |= callers
        assert {p for p in affected if p.startswith('tests/')} == set(tests)
        expected_callers = ({'audit_semantic_kinship.py'} if source == 'semantic_kinship.py' else
                            {'audit_semantic_completeness_all.py', 'audit_semantic_completeness_candidates.py',
                             'audit_semantic_kinship.py'} if source == 'source_language_filter.py' else set())
        assert affected - set(tests) - {source} == expected_callers


@pytest.mark.parametrize('path', sorted(risk.L2_IMPACT))
@pytest.mark.parametrize('addition', ['\nimport production_paid_request_journal\n',
                                      "\n__import__('production_paid_request_journal')\n",
                                      "\nloader.import_module('production_paid_request_journal')\n",
                                      '\nfrom . import unknown\n', '\ndef invalid(\n'])
def test_new_dependency_or_unparseable_source_requires_l3(risk_identity, monkeypatch, tmp_path, path, addition):
    _, event, env = risk_identity
    altered = tmp_path / path
    altered.write_text((runner.REPO / path).read_text(encoding='utf-8') + addition, encoding='utf-8')
    original_footprint, original_git = risk.import_footprint, runner.git
    monkeypatch.setattr(risk, 'import_footprint', lambda p: original_footprint(altered if p.name == path else p))
    monkeypatch.setattr(runner, 'git', lambda *args: 'M\0' + path + '\0' if args[0] == 'diff' else original_git(*args))
    assert risk.decide(event, env)['tier'] == 'L3'


@pytest.mark.parametrize('unsafe', ['novel_project.py', 'chapter_library.py', 'semantic_completeness.py',
    'semantic_integrity_audit.py', 'command_code_provider_credentials.py', 'production_paid_request_journal.py',
    'production_accounting.py', 'production_chapter_commit.py', 'production_rolling_continuity.py',
    'retroactive_transactions.py', 'glossary_audit.py', 'scripts/public_ci_policy.json',
    'tests/public_shard_schedule.json', 'tests/test_semantic_kinship.py', 'unknown.py'])
def test_mapped_source_with_dependency_or_unsafe_delta_requires_l3(unsafe):
    policy = risk.load_policy(runner.load_classification()[0])
    for paths in ([unsafe], ['semantic_kinship.py', unsafe]):
        assert risk.classify([_change(p) for p in paths], 'pull_request', policy)[0] == 'L3'


def test_multiple_reviewed_sources_select_union(risk_identity, monkeypatch):
    _, event, env = risk_identity
    original = runner.git
    monkeypatch.setattr(runner, 'git', lambda *args: ''.join('M\0' + p + '\0' for p in risk.L2_IMPACT)
                        if args[0] == 'diff' else original(*args))
    decision = risk.decide(event, env)
    assert decision['tier'] == 'L2'
    assert decision['selected'] == sorted(set(risk.SMOKE + [p for tests in risk.L2_IMPACT.values() for p in tests]))


@pytest.mark.parametrize('module', sorted(set(risk.SMOKE + [p for tests in risk.L2_IMPACT.values() for p in tests])))
def test_l2_requires_actual_passing_call_in_every_selected_module(tmp_path, module):
    directory = tmp_path / 'proof'
    manifest = {'source': {'commit': 'a' * 40, 'tree': 'b' * 40},
                'selected': sorted(set(risk.SMOKE + [p for tests in risk.L2_IMPACT.values() for p in tests])),
                'selected_deselect': [], 'excluded': {}}
    _risk_write_evidence(directory, manifest, skipped_module=module)
    with pytest.raises(ValueError, match='mapped/smoke coverage'):
        risk.verify_evidence(directory, manifest, 'L2')


@pytest.mark.parametrize('path', sorted(risk.L2_IMPACT))
def test_every_source_mapping_must_be_complete(monkeypatch, path):
    files, _ = runner.load_classification()
    policy = risk.load_policy(files)
    policy['impact'][path]['modules'].pop()
    monkeypatch.setattr(runner, 'read_json', lambda p: policy)
    with pytest.raises(ValueError, match='impact coverage'):
        risk.load_policy(files)


# Publication performance must preserve the frozen accepted guard's decisions.
@pytest.fixture(scope="module")
def original_publication_guard():
    from types import ModuleType
    baseline = "57187ab7fc67979c72abf3ee71063bf2a23adcdb"
    repo = Path(__file__).resolve().parents[1]
    source = subprocess.check_output(["git", "-C", str(repo), "show",
                                      baseline + ":scripts/publication_guard.py"])
    assert subprocess.check_output(["git", "-C", str(repo), "show",
        baseline + ":scripts/guard_allowlist.py"]) == (repo / "scripts/guard_allowlist.py").read_bytes().replace(b"\r\n", b"\n")
    module = ModuleType("_original_publication_guard")
    module.__file__ = str(repo / "scripts/publication_guard.py")
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def _publication_git(repo, *args, data=None):
    return subprocess.check_output(["git", "-C", str(repo), *args], input=data,
                                   stderr=subprocess.PIPE)


def _publication_commit(repo, files, *, inventory=None, message="synthetic safe commit"):
    # Only disposable repositories in the guarded external TEMP are mutated.
    for name in _publication_git(repo, "ls-files").decode().splitlines():
        path = repo / name
        assert path.resolve().is_relative_to(repo.resolve())
        if path.exists():
            path.unlink()
    values = dict(files)
    values["public_inventory.txt"] = (inventory if inventory is not None else
        ("\n".join(sorted([*files, "public_inventory.txt"])) + "\n").encode())
    for name, data in values.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    _publication_git(repo, "add", "-A")
    _publication_git(repo, "commit", "-qm", message, "--allow-empty")
    return _publication_git(repo, "rev-parse", "HEAD").decode().strip()


@pytest.fixture
def publication_history(tmp_path):
    repo = tmp_path / "public_history"
    repo.mkdir()
    _publication_git(repo, "init", "-q", "--initial-branch=main")
    _publication_git(repo, "config", "user.name", "Synthetic Guard Fixture")
    _publication_git(repo, "config", "user.email", "guard@example.invalid")
    _publication_git(repo, "config", "core.autocrlf", "false")
    _publication_git(repo, "config", "core.hooksPath", ".no-hooks")
    _publication_commit(repo, {"stable.txt": b"safe repeated contents\n"})
    return repo


def _publication_decision(module, repo, *, updates=None):
    module.REPO = str(repo)
    try:
        checked = (module.validate_push_check() if updates is None else
                   module.validate_pre_push(updates))
        return "accept", checked
    except module.GuardError:
        return "reject", None


@pytest.mark.parametrize("case", [
    "repeated", "inventory_evolution", "inventory_mismatch", "duplicate_inventory",
    "empty_inventory", "invalid_inventory", "forbidden_path", "changed_content",
    "invalid_utf8", "binary", "unsafe_mode", "unsafe_message", "divergent_branch",
    "nested_tag", "malformed_tag", "missing_blob", "missing_parent", "shallow",
    "path_scoped_exception",
])
def test_publication_cached_history_matches_original(
    publication_history, original_publication_guard, monkeypatch, case,
):
    repo = publication_history
    original = original_publication_guard
    # Construct a synthetic match at runtime; never commit a credential literal.
    synthetic = "sk" + "-" + chr(81) * 24
    safe = {"stable.txt": b"safe repeated contents\n"}
    base = _publication_git(repo, "rev-parse", "HEAD").decode().strip()
    if case == "repeated":
        for _ in range(6):
            _publication_commit(repo, safe)
    elif case == "inventory_evolution":
        _publication_commit(repo, {**safe, "added.txt": b"new safe text\n"})
        _publication_commit(repo, {"renamed.txt": safe["stable.txt"]})
    elif case in {"inventory_mismatch", "duplicate_inventory", "empty_inventory", "invalid_inventory"}:
        inventory = {"inventory_mismatch": b"public_inventory.txt\n",
            "duplicate_inventory": b"public_inventory.txt\nstable.txt\nstable.txt\n",
            "empty_inventory": b"", "invalid_inventory": b"\xff"}[case]
        _publication_commit(repo, safe, inventory=inventory)
        _publication_commit(repo, safe)  # A clean tip must not hide bad history.
    elif case == "forbidden_path":
        _publication_commit(repo, {**safe, "nested/output/hidden.txt": b"safe text"})
        _publication_commit(repo, safe)
    elif case in {"changed_content", "invalid_utf8", "binary"}:
        value = {"changed_content": synthetic.encode(), "invalid_utf8": b"\xff",
                 "binary": b"safe\0binary"}[case]
        _publication_commit(repo, {"stable.txt": value})
        _publication_commit(repo, safe)
    elif case == "unsafe_mode":
        blob = _publication_git(repo, "rev-parse", "HEAD:stable.txt").decode().strip()
        _publication_git(repo, "update-index", "--cacheinfo", "120000," + blob + ",stable.txt")
        _publication_git(repo, "commit", "-qm", "unsupported mode")
        _publication_commit(repo, safe)
    elif case == "unsafe_message":
        _publication_commit(repo, safe, message=synthetic)
        _publication_commit(repo, safe)
    elif case == "divergent_branch":
        _publication_git(repo, "switch", "-qc", "other", base)
        _publication_commit(repo, {"stable.txt": synthetic.encode()})
        _publication_git(repo, "switch", "-q", "main")
        _publication_commit(repo, safe)
    elif case == "nested_tag":
        _publication_git(repo, "tag", "-a", "inner", "-m", synthetic)
        _publication_git(repo, "tag", "-a", "outer", "inner", "-m", "safe outer tag")
    elif case == "malformed_tag":
        target = _publication_git(repo, "rev-parse", "HEAD").strip()
        raw = b"object " + target + b"\ntag malformed\n\nmissing type\n"
        oid = _publication_git(repo, "hash-object", "-w", "-t", "tag", "--literally", "--stdin", data=raw).decode().strip()
        # update-ref itself rejects a malformed tag; install only disposable
        # corrupt ref evidence so both guards must encounter and reject it.
        (repo / ".git/refs/tags").mkdir(parents=True, exist_ok=True)
        (repo / ".git/refs/tags/malformed").write_bytes(oid.encode() + b"\n")
    elif case == "missing_blob":
        oid = _publication_git(repo, "rev-parse", "HEAD:stable.txt").decode().strip()
        import stat
        loose = repo / ".git/objects" / oid[:2] / oid[2:]
        loose.chmod(stat.S_IWRITE)  # Git loose objects are read-only on Windows.
        loose.unlink()
    elif case == "missing_parent":
        tree = _publication_git(repo, "rev-parse", "HEAD^{tree}").strip()
        raw = (b"tree " + tree + b"\nparent " + b"0" * 40 +
               b"\nauthor Fixture <fixture@example.invalid> 1 +0000\n"
               b"committer Fixture <fixture@example.invalid> 1 +0000\n\nmissing parent\n")
        oid = _publication_git(repo, "hash-object", "-w", "-t", "commit", "--literally", "--stdin", data=raw).decode().strip()
        _publication_git(repo, "update-ref", "refs/heads/main", oid)
    elif case == "shallow":
        (repo / ".git/shallow").write_bytes(base.encode() + b"\n")
    else:
        for module in (original, guard):
            monkeypatch.setattr(module, "EXACT_SYNTHETIC",
                {"allowed.txt": {hashlib.sha256(synthetic.encode()).hexdigest()}})
        _publication_commit(repo, {"allowed.txt": synthetic.encode(), "z-other.txt": synthetic.encode()})
    expected = "accept" if case in {"repeated", "inventory_evolution"} else "reject"
    monkeypatch.setattr(guard, "REPO", str(repo))
    before = {p.relative_to(repo).as_posix(): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    old = _publication_decision(original, repo)
    new = _publication_decision(guard, repo)
    assert old == new and new[0] == expected
    assert {p.relative_to(repo).as_posix(): p.read_bytes() for p in repo.rglob("*") if p.is_file()} == before
    if case == "nested_tag":
        outer = _publication_git(repo, "rev-parse", "refs/tags/outer").decode().strip()
        updates = [("refs/custom/outgoing", outer, "refs/custom/outgoing", "0" * 40)]
        assert _publication_decision(original, repo, updates=updates) == _publication_decision(guard, repo, updates=updates) == ("reject", None)


def test_publication_cache_reuses_scans_but_rechecks_each_inventory(
    publication_history, original_publication_guard, monkeypatch,
):
    repo = publication_history
    for _ in range(5):
        _publication_commit(repo, {"stable.txt": b"safe repeated contents\n"})
    measurements = []
    for module in (original_publication_guard, guard):
        reads, inventories, messages = [], [], []
        real_git = module.git
        def observed(args, **kwargs):
            if args[:2] == ["cat-file", "blob"]:
                reads.append(args[2])
            if args[:1] == ["show"]:
                inventories.append(args[1])
            if args[:1] == ["log"]:
                messages.append(args[-1])
            return real_git(args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(module, "git", observed)
            patch.setattr(module, "REPO", str(repo))
            checked = module.validate_push_check()
        measurements.append((checked, len(reads), len(inventories), len(messages)))
    assert measurements == [(12, 12, 6, 6), (12, 2, 6, 6)]
    # Force eviction: it must only cost more reads, never alter the decision.
    monkeypatch.setattr(guard, "REPO", str(repo))
    monkeypatch.setattr(guard, "MAX_SCANNED_BLOBS", 1)
    assert guard.validate_push_check() == 12


def test_publication_cache_is_local_to_each_validation(publication_history, monkeypatch):
    repo = publication_history
    synthetic = "sk" + "-" + chr(81) * 24
    _publication_commit(repo, {"allowed.txt": synthetic.encode()})
    monkeypatch.setattr(guard, "REPO", str(repo))
    monkeypatch.setattr(guard, "EXACT_SYNTHETIC", {"allowed.txt": {hashlib.sha256(synthetic.encode()).hexdigest()}})
    assert guard.validate_push_check() > 0
    monkeypatch.setattr(guard, "EXACT_SYNTHETIC", {})
    with pytest.raises(guard.GuardError):
        guard.validate_push_check()


def test_publication_cache_never_stores_failure_or_inconsistent_evidence(monkeypatch):
    data = b"safe text"
    oid = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
    cache = {}
    monkeypatch.setattr(guard, "git", lambda args: data)
    with pytest.raises(guard.GuardError, match="inconsistent tree blob size"):
        guard.scan_blob("stable.txt", oid, len(data) + 1, cache)
    assert cache == {}
    with pytest.raises(guard.GuardError, match="blob identity mismatch"):
        guard.scan_blob("stable.txt", "0" * 40, len(data), cache)
    assert cache == {}
    guard.scan_blob("stable.txt", oid, len(data), cache)
    with pytest.raises(guard.GuardError, match="inconsistent cached blob size"):
        guard.scan_blob("stable.txt", oid, len(data) + 1, cache)
    invalid = b"\xff"
    invalid_oid = hashlib.sha1(b"blob 1\0" + invalid).hexdigest()
    monkeypatch.setattr(guard, "git", lambda args: invalid)
    with pytest.raises(guard.GuardError, match="invalid UTF-8"):
        guard.scan_blob("invalid.txt", invalid_oid, 1, cache)
    assert cache == {(oid, "stable.txt"): len(data)}
    def timed_out(args):
        raise subprocess.TimeoutExpired("synthetic git", 180)
    monkeypatch.setattr(guard, "git", timed_out)
    with pytest.raises(subprocess.TimeoutExpired):
        guard.scan_blob("unseen.txt", oid, len(data), cache)
    assert len(cache) == 1


def test_publication_timeout_never_becomes_success(monkeypatch, capsys):
    def timed_out():
        raise subprocess.TimeoutExpired("synthetic git", 180)
    monkeypatch.setattr(guard, "validate_push_check", timed_out)
    assert guard.main(["--push-check", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False and report["checked"] == 0
    assert report["error"] == "git command timed out"


@pytest.mark.parametrize("implementation", ["original", "cached"])
def test_publication_missing_object_after_successful_scan_still_blocks(
    publication_history, original_publication_guard, monkeypatch, implementation,
):
    import stat
    repo = publication_history
    _publication_commit(repo, {"stable.txt": b"safe repeated contents\n"})
    module = original_publication_guard if implementation == "original" else guard
    oid = _publication_git(repo, "rev-parse", "HEAD:stable.txt").decode().strip()
    real_git = module.git
    trees = []
    def disappear(args, **kwargs):
        if args[:1] == ["ls-tree"]:
            trees.append(args[-1])
            if len(trees) == 2:
                loose = repo / ".git/objects" / oid[:2] / oid[2:]
                loose.chmod(stat.S_IWRITE)
                loose.unlink()
        return real_git(args, **kwargs)
    monkeypatch.setattr(module, "REPO", str(repo))
    monkeypatch.setattr(module, "git", disappear)
    with pytest.raises(module.GuardError):
        module.validate_push_check()
    assert len(trees) == 2


@pytest.mark.parametrize("algorithm", ["sha1", "sha256"])
def test_publication_cache_proves_supported_git_object_formats(monkeypatch, algorithm):
    data = b"safe text"
    oid = hashlib.new(algorithm, b"blob 9\0" + data).hexdigest()
    cache = {}
    monkeypatch.setattr(guard, "git", lambda args: data)
    guard.scan_blob("stable.txt", oid, len(data), cache)
    assert cache == {(oid, "stable.txt"): len(data)}


def test_publication_benign_nested_tags_and_custom_refs_match_original(
    publication_history, original_publication_guard, monkeypatch,
):
    repo = publication_history
    _publication_commit(repo, {"stable.txt": b"safe repeated contents\n"})
    _publication_git(repo, "tag", "-a", "inner", "-m", "safe inner tag")
    _publication_git(repo, "tag", "-a", "outer", "inner", "-m", "safe outer tag")
    outer = _publication_git(repo, "rev-parse", "refs/tags/outer").decode().strip()
    updates = [("refs/custom/outgoing", outer, "refs/custom/outgoing", "0" * 40)]
    monkeypatch.setattr(guard, "REPO", str(repo))
    for operation in (None, updates):
        assert _publication_decision(original_publication_guard, repo, updates=operation) == (
            _publication_decision(guard, repo, updates=operation)) == ("accept", 4)


def test_finalization_repartition_preserves_original_sources_and_cases():
    mapping = runner.read_json(runner.REPO / 'docs/AUT2_FINALIZATION_CASE_MAPPING.json')
    original = subprocess.check_output([
        'git', '-C', str(runner.REPO), 'show',
        mapping['baseline_commit'] + ':' + mapping['original_module']], timeout=30)
    sha = lambda raw: hashlib.sha256(raw).hexdigest()
    normalized = lambda raw: raw.replace(b'\r\n', b'\n')
    assert sha(original) == mapping['original_git_blob_sha256']
    assert sha(original.replace(b'\n', b'\r\n')) == mapping['original_source_sha256']
    support = (runner.REPO / mapping['support_module']).read_bytes()
    assert sha(normalized(support)) == mapping['support_normalized_sha256']
    original_lines = original.splitlines(keepends=True)
    assert normalized(support) == b''.join(original_lines[:166])
    original_tree = ast.parse(original.decode())
    assert all(n.end_lineno <= 166 or
               (isinstance(n, ast.FunctionDef) and n.name.startswith('test_'))
               for n in original_tree.body)
    original_tests = {n.name: n for n in original_tree.body
                      if isinstance(n, ast.FunctionDef) and n.name.startswith('test_')}
    assert len(original_tests) == len(mapping['functions']) == 22
    seen = set()
    for module, meta in mapping['modules'].items():
        raw = (runner.REPO / module).read_bytes()
        assert sha(normalized(raw)) == meta['normalized_sha256']
        source = normalized(raw).decode()
        lines = source.splitlines(keepends=True)
        tree = ast.parse(source)
        # Every original import, constant and module marker is copied verbatim.
        assert normalized(raw).startswith(b''.join(original_lines[:mapping['original_import_header_lines']]))
        imports = [n for n in tree.body if isinstance(n, ast.ImportFrom)
                   and n.module == 'goat_audit_finalization_support']
        assert len(imports) == 1
        header = ast.parse(b''.join(original_lines[:mapping['original_import_header_lines']]).decode())
        assert all(n in tree.body[:len(header.body)] or n in imports or
                   (isinstance(n, ast.FunctionDef) and n.name.startswith('test_'))
                   for n in tree.body)
        assert [n.name for n in imports[0].names] == ['locked_finalize', 'snapshot', 'incident', 'plan', 'execute']
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            assert node.name not in seen and mapping['functions'][node.name]['module'] == module
            seen.add(node.name)
            old = original_tests[node.name]
            assert ast.dump(node, include_attributes=False) == ast.dump(old, include_attributes=False)
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            segment = ''.join(lines[start - 1:node.end_lineno]).encode()
            before_start, before_end = mapping['functions'][node.name]['original_lines']
            before = b''.join(original_lines[before_start - 1:before_end])
            assert segment == before
            assert sha(segment) == mapping['functions'][node.name]['decorated_normalized_sha256']
    assert seen == set(original_tests) == set(mapping['functions'])
    batch_before = subprocess.check_output(['git', '-C', str(runner.REPO), 'show',
        mapping['baseline_commit'] + ':tests/test_goat_batch_plan.py'], timeout=30)
    batch_after = (runner.REPO / 'tests/test_goat_batch_plan.py').read_bytes()
    assert batch_before.count(b'from test_goat_periodic_audit_finalization import') == 2
    assert normalized(batch_after) == batch_before.replace(
        b'from test_goat_periodic_audit_finalization import',
        b'from goat_audit_finalization_support import')
    cases = mapping['cases']
    assert mapping['case_count'] == len(cases) == 97
    assert len({c['old'] for c in cases}) == len({c['new'] for c in cases}) == 97
    assert [sum(c['new'].split('::', 1)[0] == p for c in cases)
            for p in mapping['modules']] == [26, 20, 31, 20]
    for case in cases:
        assert case['old'].split('::', 1)[0] == mapping['original_module']
        identity = case['old'].split('::', 1)[1]
        assert identity == case['new'].split('::', 1)[1]
        assert case['function'] + case['parameter_identity'] == identity
        assert mapping['functions'][case['function']]['module'] == case['new'].split('::', 1)[0]
        assert case['original_source_sha256'] == mapping['original_source_sha256']

    before_classification = json.loads(subprocess.check_output([
        'git', '-C', str(runner.REPO), 'show',
        mapping['baseline_commit'] + ':tests/public_test_classification.json'], timeout=30))
    after_classification = runner.read_json(runner.CLASSIFICATION)
    assert after_classification['deselect'] == before_classification['deselect']
    assert after_classification['deselect_reason'] == before_classification['deselect_reason']
    before_files, after_files = before_classification['files'], after_classification['files']
    assert set(before_files) - set(after_files) == {mapping['original_module']}
    additions = set(mapping['modules']) | {'tests/test_experimental_goat_audit_proof.py',
                                         'tests/test_goat_batch_standalone_receipts.py'}
    assert set(after_files) - set(before_files) == additions
    assert all(after_files[p]['status'] == 'public' for p in additions)
    assert all(after_files[p] == meta for p, meta in before_files.items()
               if p != mapping['original_module'])


def test_refreshed_schedule_weights_bind_complete_current_sources():
    schedule = runner.read_json(runner.SCHEDULE)
    published = schedule['published_schedule']['module_seconds']
    latest = {m['module']: m for m in schedule['supplemental_measurements']}
    for path, seconds in schedule['module_seconds'].items():
        if path in latest:
            measurement = latest[path]
            assert measurement['hosted_run_id'] is None
            assert measurement['module_seconds'] == seconds
            expected_skips = (['tests/test_build_publication_epub.py::test_real_rogue_publication_temporary_integration']
                              if path == 'tests/test_build_publication_epub.py' else [])
            assert measurement['skipped'] == len(expected_skips)
            assert measurement.get('existing_skipped_nodes', []) == expected_skips
            assert measurement['collected'] == measurement['passed'] + measurement['skipped']
            source = (runner.REPO / path).read_bytes()
            if 'module_normalized_sha256' in measurement:
                assert hashlib.sha256(source.replace(b'\r\n', b'\n')).hexdigest() == measurement['module_normalized_sha256']
            else:
                assert hashlib.sha256(source).hexdigest() == measurement['module_sha256']
        else:
            assert seconds == published[path]
    variability = schedule['variability']
    totals = [sum(schedule['module_seconds'][p] for p in b) for b in schedule['shards']]
    assert variability['execution_deadline_seconds'] == 4500
    assert variability['fraction'] == 0.25 and variability['sensitivity_fraction'] == 0.30
    assert variability['unattributed_seconds_per_shard'] == 60
    assert variability['conservative_seconds'] == [round(x * 1.25 + 60, 3) for x in totals]
    assert variability['sensitivity_seconds'] == [round(x * 1.30 + 60, 3) for x in totals]
