#!/usr/bin/env python3
"""Run a complete reviewed public shard, with bounded, source-bound diagnostics."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import threading
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLASSIFICATION = REPO / 'tests/public_test_classification.json'
SCHEDULE = REPO / 'tests/public_shard_schedule.json'
WRAPPER = REPO / 'tests/zero_provider_validation.py'
FRAME_PREFIX = b'[public-ci-record] '
MAX_FRAME_BYTES = 65536
MAX_EVENTS = 100000
MAX_TELEMETRY_BYTES = 64 * 1024 * 1024
L3_SHARDS = 4

TIMING_SOURCE = {'run_id': 39696534289, 'attempt': 1,
                 'commit': 'ff7e0975dcf172038b2376e3b3c5730b37cfff56',
                 'tree': 'e80c1515fdf8d621dc02848de141f7d053609a6a'}

DPAPI_NODE = 'tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip'


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique_object)


def load_classification():
    data = read_json(CLASSIFICATION)
    if data.get('version') != 2 or not isinstance(data.get('files'), dict):
        raise ValueError('invalid classification schema')
    files = data['files']
    discovered = {p.relative_to(REPO).as_posix() for p in (REPO / 'tests').glob('test_*.py')}
    if discovered != set(files):
        raise ValueError('classification mismatch: missing or stale modules')
    for rel, meta in files.items():
        if not isinstance(meta, dict) or meta.get('status') not in {'public', 'private', 'historical'}:
            raise ValueError('invalid classification status')
    deselect = data.get('deselect')
    reasons = data.get('deselect_reason')
    if not isinstance(deselect, list) or not all(isinstance(n, str) and '::' in n for n in deselect):
        raise ValueError('invalid deselect entries')
    if len(set(deselect)) != len(deselect):
        raise ValueError('duplicate deselect entries')
    if not isinstance(reasons, dict) or set(reasons) != set(deselect) or not all(isinstance(v, str) and v.strip() for v in reasons.values()):
        raise ValueError('every deselection requires a reviewed reason')
    if DPAPI_NODE not in deselect:
        raise ValueError('classification must deselect the real-DPAPI node')
    for node in deselect:
        if files.get(node.split('::', 1)[0], {}).get('status') != 'public':
            raise ValueError('deselect references non-public/unknown file')
    return files, deselect


def measured_shards(weights):
    """Longest measured module first; path and bucket index break ties."""
    buckets, loads = [[] for _ in range(L3_SHARDS)], [0.0] * L3_SHARDS
    for path in sorted(weights, key=lambda p: (-weights[p], p)):
        shard = min(range(L3_SHARDS), key=lambda i: (loads[i], i))
        buckets[shard].append(path)
        loads[shard] += weights[path]
    return buckets


def balanced_shards(public, shards):
    """Replay the reviewed Windows module timings; never infer costs from size."""
    if shards not in (1, L3_SHARDS) or not public or len(set(public)) != len(public):
        raise ValueError('invalid or empty public partition')
    schedule = read_json(SCHEDULE)
    buckets = schedule.get('shards')
    if (schedule.get('version') != 2 or schedule.get('strategy') != 'measured-module-lpt-39696534289'
            or schedule.get('measurement_source') != TIMING_SOURCE
            or not isinstance(buckets, list) or len(buckets) != L3_SHARDS):
        raise ValueError('invalid or stale measured schedule')
    if not all(isinstance(b, list) and b and all(isinstance(p, str) for p in b) for b in buckets):
        raise ValueError('empty or malformed scheduled shard')
    flat = [p for bucket in buckets for p in bucket]
    if len(flat) != len(set(flat)) or set(flat) != set(public):
        raise ValueError('schedule must partition every public module exactly once')
    weights = schedule.get('module_seconds')
    if (not isinstance(weights, dict) or set(weights) != set(public)
            or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in weights.values())):
        raise ValueError('missing or invalid measured module timings')
    if buckets != measured_shards(weights):
        raise ValueError('schedule differs from measured allocation')
    return buckets if shards == L3_SHARDS else [sorted(public)]


def git(*args):
    return subprocess.check_output(['git', '-C', str(REPO), *args], timeout=30).decode('utf-8').strip()


def source_identity():
    if git('status', '--porcelain', '--untracked-files=no'):
        raise ValueError('tracked source state is dirty')
    return {'commit': git('rev-parse', 'HEAD'), 'tree': git('rev-parse', 'HEAD^{tree}'),
            'index_tree': git('write-tree')}


def assert_source_unchanged(before):
    if source_identity() != before:
        raise ValueError('source identity changed during testing')


def external_directory(value):
    path = Path(value).resolve()
    system_temp = Path(tempfile.gettempdir()).resolve()
    if system_temp not in path.parents or path == REPO or REPO in path.parents or path in REPO.parents:
        raise ValueError('diagnostics must be in an external system-temp child')
    return path


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def stop_process(proc):
    # Kill descendants too; no background test may keep writing after a timeout.
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=True)
    else:
        proc.kill()
    proc.wait(timeout=15)


class MetadataReceiver:
    """The supervisor owns persistence and accepts only bounded metadata schemas."""
    def __init__(self, directory):
        self.directory = external_directory(directory)
        self.manifest = read_json(self.directory / 'selection.json')
        self.selected = set(self.manifest['selected'])
        self.nodes = {}
        self.phases = set()
        self.outcomes = {}
        self.collection_valid = False
        self.terminal = False
        self.failed = False
        self.events = 0
        self.bytes = 0

    def accept(self, record):
        common = {'event', 'elapsed_seconds'}
        node = {'module', 'function', 'node_sha256'}
        schemas = {
            'collection_start': {'module'},
            'collection_end': {'module', 'seconds', 'outcome'},
            'collection_node': node,
            'collection_finish': {'tests', 'deselected', 'deselected_sha256', 'skipped_modules'},
            'test_start': node,
            'phase_start': node | {'phase'},
            'phase_end': node | {'phase', 'seconds', 'outcome'},
            'session_finish': {'source', 'complete', 'successful', 'collected', 'finished', 'exit_code', 'outcomes', 'modules'},
        }
        event = record.get('event')
        if event not in schemas or set(record) != common | schemas[event] or self.terminal:
            raise ValueError('invalid telemetry schema or event after completion')
        for key in ('elapsed_seconds', 'seconds'):
            if key in record and (type(record[key]) not in (int, float) or not math.isfinite(record[key]) or record[key] < 0):
                raise ValueError('invalid telemetry duration')
        for key in ('tests', 'deselected', 'collected', 'finished'):
            if key in record and (type(record[key]) is not int or record[key] < 0):
                raise ValueError('invalid telemetry count')
        if 'module' in record and record['module'] not in self.selected:
            raise ValueError('unexpected telemetry module')
        if 'node_sha256' in record:
            if not re.fullmatch(r'[a-f0-9]{64}', record['node_sha256']) or not re.fullmatch(r'[A-Za-z0-9_:]{1,200}', record['function']):
                raise ValueError('invalid redacted node identity')
        if 'phase' in record and record['phase'] not in {'setup', 'call', 'teardown'}:
            raise ValueError('invalid telemetry phase')
        if 'outcome' in record and record['outcome'] not in {'passed', 'failed', 'skipped'}:
            raise ValueError('invalid telemetry outcome')
        if event == 'collection_node':
            digest = record['node_sha256']
            if digest in self.nodes or len(self.nodes) >= 20000:
                raise ValueError('duplicate or excessive collected nodes')
            self.nodes[digest] = {k: record[k] for k in node}
        elif event == 'collection_finish':
            skipped = record['skipped_modules']
            expected = [hashlib.sha256(n.encode()).hexdigest() for n in self.manifest['selected_deselect']]
            if (self.collection_valid or not self.nodes or record['tests'] != len(self.nodes)
                    or sorted(record['deselected_sha256']) != sorted(expected) or record['deselected'] != len(expected)
                    or not isinstance(skipped, list) or not set(skipped) <= self.selected
                    or {n['module'] for n in self.nodes.values()} | set(skipped) != self.selected):
                raise ValueError('collection metadata mismatch')
            self.collection_valid = True
            write_json(self.directory / 'collected.json', {'source': self.manifest['source'],
                       'nodes': list(self.nodes.values()), 'deselected_sha256': record['deselected_sha256'],
                       'skipped_modules': skipped})
        elif event in {'test_start', 'phase_start', 'phase_end'}:
            digest = record['node_sha256']
            if not self.collection_valid or self.nodes.get(digest) != {k: record[k] for k in node}:
                raise ValueError('phase without matching collection')
            if event == 'phase_end':
                phase = (digest, record['phase'])
                if phase in self.phases:
                    raise ValueError('duplicate phase result')
                self.phases.add(phase)
                key = record['phase'] + ':' + record['outcome']
                self.outcomes[key] = self.outcomes.get(key, 0) + 1
        elif event == 'session_finish':
            finished = sum(phase == 'teardown' for _, phase in self.phases)
            complete = self.collection_valid and len(self.nodes) > 0 and finished == len(self.nodes)
            if (record['source'] != self.manifest['source'] or type(record['exit_code']) is not int
                    or type(record['complete']) is not bool or type(record['successful']) is not bool
                    or record['collected'] != len(self.nodes) or record['finished'] != finished
                    or record['complete'] != complete or record['outcomes'] != self.outcomes
                    or record['successful'] != (complete and record['exit_code'] == 0)
                    or not isinstance(record['modules'], dict) or not set(record['modules']) <= self.selected):
                raise ValueError('completion metadata mismatch')
            for value in record['modules'].values():
                if (not isinstance(value, dict) or set(value) != {'phase_seconds', 'collection_seconds'}
                        or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in value.values())):
                    raise ValueError('invalid module timings')
            self.terminal = True
            write_json(self.directory / 'timings.json', {k: v for k, v in record.items() if k != 'event'})
        encoded = json.dumps(record, sort_keys=True) + '\n'
        self.events += 1
        self.bytes += len(encoded.encode('utf-8'))
        if self.events > MAX_EVENTS or self.bytes > MAX_TELEMETRY_BYTES:
            raise ValueError('telemetry budget exceeded')
        with (self.directory / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(encoded)
        if event in {'collection_start', 'test_start', 'phase_start', 'session_finish'}:
            print('[public-ci] ' + encoded.rstrip(), flush=True)

    def read_output(self, stream):
        continuation = False
        try:
            while True:
                line = stream.readline(MAX_FRAME_BYTES + 1)
                if not line:
                    break
                if not continuation and line.startswith(FRAME_PREFIX):
                    if not self.failed:
                        try:
                            if len(line) > MAX_FRAME_BYTES or not line.endswith(b'\n'):
                                raise ValueError('oversized or truncated telemetry frame')
                            self.accept(json.loads(line[len(FRAME_PREFIX):], object_pairs_hook=unique_object))
                        except Exception:
                            self.failed = True
                            print('[public-ci] metadata channel failed; completion cannot pass', flush=True)
                else:
                    # Preserve primary pytest failures even when metadata has failed.
                    # Raw output is forwarded to the console, never to artifacts.
                    text = line.decode('utf-8', errors='replace')
                    # Redirected Windows stdout may use a narrow code page. Keep
                    # draining the pipe even when a failure report contains CJK.
                    encoding = sys.stdout.encoding or 'utf-8'
                    print(text.encode(encoding, errors='backslashreplace').decode(encoding), end='', flush=True)
                continuation = not line.endswith(b'\n')
        except Exception:
            self.failed = True
            print('[public-ci] output channel failed; completion cannot pass', flush=True)


def run_bounded(command, seconds, diagnostics=None):
    start = time.monotonic()
    receiver = MetadataReceiver(diagnostics) if diagnostics is not None else None
    options = {'stdout': subprocess.PIPE, 'stderr': subprocess.STDOUT} if receiver else {}
    proc = subprocess.Popen(command, cwd=REPO, **options)
    reader = None
    if receiver:
        reader = threading.Thread(target=receiver.read_output, args=(proc.stdout,), daemon=True)
        reader.start()
    code = None
    try:
        while code is None:
            remaining = seconds - (time.monotonic() - start)
            if remaining <= 0:
                stop_process(proc)
                code = 124
                break
            try:
                code = proc.wait(timeout=min(60, remaining))
            except subprocess.TimeoutExpired:
                print(f'[public-ci] heartbeat elapsed_seconds={int(time.monotonic() - start)}', flush=True)
    except BaseException:
        if proc.poll() is None:
            stop_process(proc)
        raise
    finally:
        if reader:
            reader.join(timeout=15)
            if reader.is_alive():
                receiver.failed = True
    if code == 0 and receiver and (receiver.failed or not receiver.terminal):
        return 2
    return code


def selection_manifest(before, buckets, shard, selected, deselect, timeout_seconds, decision=None):
    manifest = {'version': 1, 'source': before, 'shard': shard, 'shards': buckets,
                'selected': selected, 'deselect': deselect, 'selected_deselect': [n for n in deselect if n.split('::', 1)[0] in selected],
                'excluded': {p: v for p, v in load_classification()[0].items() if v['status'] != 'public'},
                'strategy': read_json(SCHEDULE)['strategy'], 'timeout_seconds': timeout_seconds,
                'github': {k: os.environ.get(k, '') for k in ('GITHUB_SHA', 'GITHUB_HEAD_REF', 'GITHUB_BASE_REF')},
                'sha256': {p: hashlib.sha256((REPO / p).read_bytes()).hexdigest() for p in
                           [*selected, 'tests/public_test_classification.json', 'tests/public_shard_schedule.json',
                            'tests/zero_provider_validation.py', 'scripts/run_public_tests.py', 'scripts/public_test_telemetry.py']}}
    if decision is not None:
        from scripts.public_ci import digest
        manifest['qualification_sha256'] = digest(decision)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--shards', type=int, default=1, choices=(1, L3_SHARDS))
    parser.add_argument('--shard', type=int, default=0)
    parser.add_argument('--timeout-seconds', type=int, default=4500)
    parser.add_argument('--diagnostics')
    parser.add_argument('--plan-only', action='store_true')
    parser.add_argument('--decision', help='exact source/run-bound risk decision; omitted means full suite')
    args = parser.parse_args(argv)
    if not 0 <= args.shard < args.shards or not 1 <= args.timeout_seconds <= 4500:
        parser.error('invalid shard or timeout (maximum 4500 seconds)')
    files, deselect = load_classification()
    public = sorted(p for p, meta in files.items() if meta['status'] == 'public')
    buckets = balanced_shards(public, args.shards)
    decision = None
    if args.decision:
        sys.path.insert(0, str(REPO))
        from scripts.public_ci import verified_decision, select
        decision = verified_decision(args.decision)
        buckets, selected = select(decision, args.shards, args.shard)
    else:
        selected = buckets[args.shard]
    selected_deselect = [n for n in deselect if n.split('::', 1)[0] in selected]
    before = source_identity()
    diagnostics = external_directory(args.diagnostics or (Path(tempfile.gettempdir()) / ('nt_public_ci_' + uuid.uuid4().hex)))
    diagnostics.mkdir(parents=True, exist_ok=False)
    manifest = selection_manifest(before, buckets, args.shard, selected, deselect, args.timeout_seconds, decision)
    write_json(diagnostics / 'selection.json', manifest)
    print(f'[public-ci] source={before["commit"]} tree={before["tree"]} shard={args.shard} modules={len(selected)}', flush=True)
    if args.plan_only:
        assert_source_unchanged(before)
        return 0
    basetemp = Path(tempfile.gettempdir()) / ('nt_public_tests_' + uuid.uuid4().hex)
    deselect_args = []
    for node in dict.fromkeys([DPAPI_NODE, *selected_deselect]):
        deselect_args += ['--deselect', node]
    command = [sys.executable, '-u', str(WRAPPER), '--basetemp', str(basetemp), *deselect_args,
               '--', '-q', '-p', 'no:cacheprovider', '-p', 'scripts.public_test_telemetry',
               '--public-ci-diagnostics', str(diagnostics), *selected]
    result = {'status': 'incomplete', 'exit_code': None, 'source_unchanged': False}
    write_json(diagnostics / 'result.json', result)
    start = time.monotonic()
    try:
        code = run_bounded(command, args.timeout_seconds, diagnostics=diagnostics)
        if code == 0:
            completed = read_json(diagnostics / 'timings.json')
            if (completed.get('source') != before or completed.get('successful') is not True
                    or completed.get('complete') is not True or completed.get('exit_code') != 0):
                raise ValueError('missing or incomplete test completion evidence')
        result.update(exit_code=code, status='timeout' if code == 124 else ('passed' if code == 0 else 'failed'))
        return code
    finally:
        result['elapsed_seconds'] = round(time.monotonic() - start, 3)
        try:
            assert_source_unchanged(before)
            result['source_unchanged'] = True
        except Exception:
            result['status'] = 'source_changed'
            raise
        finally:
            write_json(diagnostics / 'result.json', result)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print('public CI refused: ' + type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        raise SystemExit(2)
