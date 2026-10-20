#!/usr/bin/env python3
"""Run a complete reviewed public shard, with bounded, source-bound diagnostics."""
from __future__ import annotations

import argparse
import hashlib
import json
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


def balanced_shards(public, shards):
    """Frozen interim allocation; no source-size or invented duration weights.

    Keep the canonical baseline membership until complete per-module measurements
    on Windows justify a reviewed rebalance. A single-shard local run remains
    available; CI always runs all three buckets.
    """
    if shards not in (1, 3) or not public or len(set(public)) != len(public):
        raise ValueError('invalid or empty public partition')
    schedule = read_json(SCHEDULE)
    buckets = schedule.get('shards')
    if schedule.get('version') != 1 or not isinstance(buckets, list) or len(buckets) != 3:
        raise ValueError('invalid schedule schema')
    if not all(isinstance(b, list) and b and all(isinstance(p, str) for p in b) for b in buckets):
        raise ValueError('empty or malformed scheduled shard')
    flat = [p for bucket in buckets for p in bucket]
    if len(flat) != len(set(flat)) or set(flat) != set(public):
        raise ValueError('schedule must partition every public module exactly once')
    return buckets if shards == 3 else [sorted(public)]


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


def run_bounded(command, seconds):
    start = time.monotonic()
    proc = subprocess.Popen(command, cwd=REPO)
    try:
        while True:
            remaining = seconds - (time.monotonic() - start)
            if remaining <= 0:
                stop_process(proc)
                return 124
            try:
                return proc.wait(timeout=min(60, remaining))
            except subprocess.TimeoutExpired:
                print(f'[public-ci] heartbeat elapsed_seconds={int(time.monotonic() - start)}', flush=True)
    except BaseException:
        if proc.poll() is None:
            stop_process(proc)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--shards', type=int, default=1, choices=(1, 3))
    parser.add_argument('--shard', type=int, default=0)
    parser.add_argument('--timeout-seconds', type=int, default=4500)
    parser.add_argument('--diagnostics')
    parser.add_argument('--plan-only', action='store_true')
    args = parser.parse_args(argv)
    if not 0 <= args.shard < args.shards or not 1 <= args.timeout_seconds <= 4500:
        parser.error('invalid shard or timeout (maximum 4500 seconds)')
    files, deselect = load_classification()
    public = sorted(p for p, meta in files.items() if meta['status'] == 'public')
    buckets = balanced_shards(public, args.shards)
    selected = buckets[args.shard]
    selected_deselect = [n for n in deselect if n.split('::', 1)[0] in selected]
    before = source_identity()
    diagnostics = external_directory(args.diagnostics or (Path(tempfile.gettempdir()) / ('nt_public_ci_' + uuid.uuid4().hex)))
    diagnostics.mkdir(parents=True, exist_ok=False)
    manifest = {'version': 1, 'source': before, 'shard': args.shard, 'shards': buckets,
                'selected': selected, 'deselect': deselect, 'selected_deselect': selected_deselect,
                'excluded': {p: v for p, v in files.items() if v['status'] != 'public'},
                'strategy': read_json(SCHEDULE)['strategy'], 'timeout_seconds': args.timeout_seconds,
                'github': {k: os.environ.get(k, '') for k in ('GITHUB_SHA', 'GITHUB_HEAD_REF', 'GITHUB_BASE_REF')},
                'sha256': {p: hashlib.sha256((REPO / p).read_bytes()).hexdigest() for p in
                           [*selected, 'tests/public_test_classification.json', 'tests/public_shard_schedule.json',
                            'tests/zero_provider_validation.py', 'scripts/run_public_tests.py', 'scripts/public_test_telemetry.py']}}
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
        code = run_bounded(command, args.timeout_seconds)
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
