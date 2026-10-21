"""Synthetic NO_SEND controls for public CI selection and diagnostics."""
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


NEW_MODULE = 'tests/test_public_ci_reliability.py'
BASELINE_PARTITIONS = (
    'e66a6222b9ad4dfd31b4d6b9c50d1be1d7a6022bee01466868578571bc83c74e',
    '70554ac08d00cfd0fa8cf31daea187ba46a51e2b1926cc8cab069124c60f0051',
    '56a565b299e24e2c348f3ae6398ffd241b52607af9ac5f97b17136bf7ee0990d',
)


def test_exact_public_partition_and_reviewed_deselections():
    files, deselect = runner.load_classification()
    public = sorted(p for p, meta in files.items() if meta['status'] == 'public')
    buckets = runner.balanced_shards(public, 3)
    flat = sum(buckets, [])
    assert len(flat) == len(set(flat)) == len(public) == 190
    assert set(flat) == set(public)
    assert runner.balanced_shards(list(reversed(public)), 3) == buckets
    assert runner.balanced_shards(public, 1) == [public]
    assert sum(v['status'] == 'private' for v in files.values()) == 2
    assert sum(v['status'] == 'historical' for v in files.values()) == 7
    assert not set(flat) & {p for p, v in files.items() if v['status'] != 'public'}
    for bucket, expected in zip(buckets, BASELINE_PARTITIONS, strict=True):
        old = sorted(p for p in bucket if p != NEW_MODULE)
        assert len(old) == 63
        assert hashlib.sha256('\n'.join(old).encode()).hexdigest() == expected
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


@pytest.mark.parametrize('case', ['empty', 'duplicate', 'missing', 'extra', 'bad_shards', 'empty_bucket'])
def test_partition_negative_controls(tmp_path, monkeypatch, case):
    public = ['tests/test_a.py', 'tests/test_b.py', 'tests/test_c.py']
    data = {'version': 1, 'shards': [[public[0]], [public[1]], [public[2]]]}
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
    else:
        data['shards'][0] = []
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
    assert len(sum(runner.balanced_shards(public, 3), [])) == 190


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
    (repo / 'tests/public_shard_schedule.json').write_text(json.dumps({
        'version': 1, 'strategy': 'synthetic', 'shards': [[p] for p in public]}), encoding='utf-8')
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
