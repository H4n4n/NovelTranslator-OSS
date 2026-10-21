#!/usr/bin/env python3
"""Deterministic PUBLIC qualification and fail-closed evidence reconciliation."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import run_public_tests as runner

POLICY = 'scripts/public_ci_policy.json'
SMOKE = ['tests/test_public_ci_reliability.py', 'tests/test_runtime_hardening_smoke.py']
JOBS = {'L1': 'l1', 'L2': 'l2', 'L3': 'test'}
L2_MODULES = ['tests/test_audit_semantic_completeness_all.py',
              'tests/test_audit_semantic_completeness_candidates.py',
              'tests/test_audit_semantic_kinship.py']
SHA = re.compile(r'[a-f0-9]{40}')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def identity(event, env):
    source = runner.source_identity()
    require(source['tree'] == source['index_tree'], 'index/tree mismatch')
    require(env.get('GITHUB_SHA') == source['commit'], 'event/checkout commit mismatch')
    kind = env.get('GITHUB_EVENT_NAME')
    require(kind in {'pull_request', 'push', 'workflow_dispatch'}, 'unsupported event')
    parents = runner.git('rev-list', '--parents', '-n', '1', source['commit']).split()[1:]
    if kind == 'pull_request':
        pr = event['pull_request']
        head, base = pr['head']['sha'], pr['base']['sha']
        require(all(isinstance(s, str) and SHA.fullmatch(s) for s in (head, base)), 'invalid PR identity')
        require(parents == [base, head], 'merge parents differ from exact PR base/head')
        require(env.get('GITHUB_REF') == f"refs/pull/{event['number']}/merge", 'not a PR merge ref')
        require(pr['base']['ref'] == 'main', 'unexpected PR base branch')
    else:
        head, base = source['commit'], parents[0] if parents else source['commit']
        if kind == 'push':
            require(event.get('after') == head and env.get('GITHUB_REF') == 'refs/heads/main', 'invalid main push')
    run = {k: env.get(k) for k in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_REPOSITORY')}
    require(all(isinstance(run[k], str) and run[k].isdigit() and int(run[k]) > 0
                for k in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT')), 'missing run identity')
    require(run['GITHUB_REPOSITORY'] == event['repository']['full_name'], 'repository mismatch')
    return dict(source, head=head, base=base, event=kind,
                pr=event['number'] if kind == 'pull_request' else None, run=run)


def changed_paths(base, commit):
    fields = runner.git('diff', '--name-status', '-z', '--find-renames', base, commit).split('\0')
    changes = []
    while fields and fields[0]:
        status = fields.pop(0)
        paths = [fields.pop(0)]
        if status.startswith(('R', 'C')):
            paths.append(fields.pop(0))
        require(all(p and '\n' not in p and '\\' not in p for p in paths), 'malformed diff path')
        changes.append({'status': status, 'paths': paths})
    return changes


def load_policy(files):
    policy = runner.read_json(runner.REPO / POLICY)
    require(set(policy) == {'version', 'non_normative_docs', 'smoke', 'impact'}, 'invalid policy fields')
    require(type(policy['version']) is int and policy['version'] == 1 and policy['smoke'] == SMOKE, 'invalid policy version/smoke')
    docs, impact = policy['non_normative_docs'], policy['impact']
    require(docs == ['docs/DEVELOPMENT_TIPS.md'], 'unreviewed docs policy')
    require(all(isinstance(p, str) and p.startswith('docs/') and p.endswith('.md')
                and (runner.REPO / p).is_file() for p in docs), 'invalid docs policy')
    require(isinstance(impact, dict) and impact, 'empty impact mapping')
    for path, entry in impact.items():
        require(path == 'source_language_filter.py', 'unreviewed L2 source')
        require(set(entry) == {'modules', 'reason'} and isinstance(entry['reason'], str)
                and entry['reason'].strip(), 'missing impact rationale')
        modules = entry['modules']
        require(isinstance(modules, list) and sorted(modules) == L2_MODULES, 'inexact reviewed impact coverage')
        require(all(files.get(p, {}).get('status') == 'public' for p in modules + SMOKE), 'non-public impact/smoke')
        require((runner.REPO / path).is_file(), 'stale source mapping')
    return policy


def classify(changes, kind, policy, policy_unchanged=True):
    if kind != 'pull_request':
        return 'L3', 'main push or manual qualification requires full suite'
    if not policy or not policy_unchanged:
        return 'L3', 'missing, invalid, or changed impact policy'
    if not changes or any(c['status'] != 'M' or len(c['paths']) != 1 for c in changes):
        return 'L3', 'empty delta or added/renamed/deleted/type-changed path'
    paths = {c['paths'][0] for c in changes}
    if paths <= set(policy['non_normative_docs']):
        return 'L1', 'only explicitly reviewed non-normative documentation'
    if paths <= set(policy['impact']):
        return 'L2', 'only explicitly mapped ordinary source'
    return 'L3', 'unknown, mixed, normative, or safety/infrastructure change'


def decide(event, env):
    bound = identity(event, env)
    files, deselect = runner.load_classification()
    public = sorted(p for p, meta in files.items() if meta['status'] == 'public')
    shards = runner.balanced_shards(public, 3)
    changes = changed_paths(bound['base'], bound['commit'])
    policy = None
    policy_sha = None
    try:
        policy = load_policy(files)
        policy_sha = runner.git('rev-parse', f"HEAD:{POLICY}")
        unchanged = policy_sha == runner.git('rev-parse', f"{bound['base']}:{POLICY}")
    except (ValueError, OSError, runner.subprocess.SubprocessError, TypeError, KeyError):
        unchanged = False
    tier, reason = classify(changes, bound['event'], policy, unchanged)
    if tier == 'L3':
        selected = public
    else:
        selected = sorted(set(SMOKE) | {module for c in changes for path in c['paths']
                                      for module in policy['impact'].get(path, {}).get('modules', [])})
    require(selected, 'empty test selection')
    runner.assert_source_unchanged({k: bound[k] for k in ('commit', 'tree', 'index_tree')})
    return {'version': 1, 'tier': tier, 'reason': reason, 'identity': bound, 'changes': changes,
            'policy_sha': policy_sha, 'policy_sha256': digest(policy) if policy else None,
            'selected': selected, 'shards': shards if tier == 'L3' else [selected],
            'deselect': deselect, 'selected_deselect': [n for n in deselect if n.split('::')[0] in selected],
            'excluded': {p: v for p, v in files.items() if v['status'] != 'public'},
            'l4': 'NOT VERIFIED: separately authorized off-public evidence or owner exception required'}


def environment_decision():
    event = runner.read_json(Path(os.environ['GITHUB_EVENT_PATH']))
    return decide(event, os.environ)


def verified_decision(path):
    actual = runner.read_json(Path(path))
    require(actual == environment_decision(), 'missing, stale, or tampered decision')
    return actual


def select(decision, shards, shard):
    require(shards == (3 if decision['tier'] == 'L3' else 1), 'incorrect qualification shard count')
    require(0 <= shard < shards, 'incorrect qualification shard')
    return decision['shards'], decision['shards'][shard]


def verify_jobs(tier, needs):
    require(tier in JOBS and set(needs) == {'guard', 'l1', 'l2', 'test'}, 'missing/unexpected jobs')
    for job, value in needs.items():
        expected = 'success' if job in {'guard', JOBS[tier]} else 'skipped'
        require(isinstance(value, dict) and value.get('result') == expected, f'required job result mismatch: {job}')


def verify_live(bound, live):
    if bound['event'] != 'pull_request':
        require(live == {}, 'unexpected PR snapshot')
        return
    require(live.get('state') == 'open' and live.get('number') == bound['pr'], 'PR no longer open/current')
    require(live.get('head', {}).get('sha') == bound['head']
            and live.get('base', {}).get('sha') == bound['base']
            and live.get('merge_commit_sha') == bound['commit'], 'stale live head/base/merge identity')


def verify_evidence(directory, expected, tier):
    """Replay the existing bounded receiver; independently reconcile persisted files."""
    require(runner.read_json(directory / 'selection.json') == expected, 'selection manifest mismatch')
    result = runner.read_json(directory / 'result.json')
    require(result.get('status') == 'passed' and type(result.get('exit_code')) is int
            and result['exit_code'] == 0 and result.get('source_unchanged') is True, 'incomplete test result')
    events = directory / 'events.jsonl'
    require(0 < events.stat().st_size <= runner.MAX_TELEMETRY_BYTES, 'missing/excessive telemetry')
    with tempfile.TemporaryDirectory(prefix='nt_gate_replay_') as temp:
        replay = Path(temp)
        runner.write_json(replay / 'selection.json', expected)
        receiver = runner.MetadataReceiver(replay)
        passed = set()
        phases = {}
        with events.open(encoding='utf-8') as stream, open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink):
            while line := stream.readline(runner.MAX_FRAME_BYTES + 1):
                require(len(line.encode()) <= runner.MAX_FRAME_BYTES and line.endswith('\n'), 'truncated/oversized event')
                record = json.loads(line, object_pairs_hook=runner.unique_object)
                receiver.accept(record)
                if record['event'] == 'phase_end':
                    phases.setdefault(record['node_sha256'], {})[record['phase']] = record['outcome']
                    if record['phase'] == 'call' and record['outcome'] == 'passed':
                        passed.add(record['module'])
        require(receiver.terminal and receiver.collection_valid and not receiver.failed, 'missing terminal telemetry')
        for name in ('collected.json', 'timings.json'):
            require(runner.read_json(directory / name) == runner.read_json(replay / name), 'tampered ' + name)
        times = runner.read_json(replay / 'timings.json')
        require(times['complete'] is True and times['successful'] is True and times['exit_code'] == 0, 'failed/incomplete session')
        require(set(phases) == set(receiver.nodes), 'missing node execution')
        for outcomes in phases.values():
            require(outcomes.get('teardown') == 'passed' and outcomes.get('setup') in {'passed', 'skipped'}
                    and (outcomes.get('call') in {'passed', 'skipped'} if outcomes.get('setup') == 'passed'
                         else 'call' not in outcomes), 'failed or incomplete phase evidence')
        require(passed and (tier == 'L3' or passed == set(expected['selected'])), 'inadequate nonempty mapped/smoke coverage')
    return set(expected['selected']), set(expected['selected_deselect'])


def gate(decision, guard, needs, live, evidence):
    before = runner.source_identity()
    require(before == {k: decision['identity'][k] for k in ('commit', 'tree', 'index_tree')}, 'gate source identity mismatch')
    verify_jobs(decision['tier'], needs)
    require(guard == {'version': 1, 'decision_sha256': digest(decision), 'identity': decision['identity']}, 'guard identity mismatch')
    verify_live(decision['identity'], live)
    count = len(decision['shards'])
    run = decision['identity']['run']
    prefix = f"public-ci-{run['GITHUB_RUN_ID']}-{run['GITHUB_RUN_ATTEMPT']}-"
    require({p.name for p in evidence.iterdir()} == {prefix + str(i) for i in range(count)}, 'missing/extra shard evidence')
    modules, nodes = set(), set()
    for shard in range(count):
        selected = decision['shards'][shard]
        expected = runner.selection_manifest(before, decision['shards'], shard, selected,
                                             decision['deselect'], 4500, decision)
        found, omitted = verify_evidence(evidence / (prefix + str(shard)), expected, decision['tier'])
        require(not modules & found and not nodes & omitted, 'duplicate module/deselection coverage')
        modules |= found
        nodes |= omitted
    require(modules == set(decision['selected']) and nodes == set(decision['selected_deselect']), 'inexact total coverage')
    runner.assert_source_unchanged(before)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['decide', 'gate'])
    parser.add_argument('--directory', required=True)
    parser.add_argument('--needs')
    parser.add_argument('--live')
    parser.add_argument('--evidence')
    args = parser.parse_args(argv)
    directory = runner.external_directory(args.directory)
    if args.command == 'decide':
        directory.mkdir(parents=True, exist_ok=False)
        decision = environment_decision()
        runner.write_json(directory / 'decision.json', decision)
        runner.write_json(directory / 'guard.json', {'version': 1, 'decision_sha256': digest(decision), 'identity': decision['identity']})
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
            stream.write(f"tier={decision['tier']}\n")
        print(f"Public CI {decision['tier']}: {decision['reason']}")
    else:
        decision = verified_decision(directory / 'decision.json')
        gate(decision, runner.read_json(directory / 'guard.json'),
             json.loads(args.needs, object_pairs_hook=runner.unique_object), runner.read_json(Path(args.live)),
             runner.external_directory(args.evidence))
        print('Public CI gate: PASS; ' + decision['l4'])
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError, IndexError, runner.subprocess.SubprocessError) as exc:
        print('Public CI refused: ' + type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        raise SystemExit(2)
