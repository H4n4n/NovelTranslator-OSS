"""Bounded public-test metadata only: no captured output, parameter text or locals."""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter, defaultdict

import pytest

from scripts.run_public_tests import external_directory, read_json, write_json


def node_identity(nodeid):
    module, _, name = nodeid.partition('::')
    # Parameter ids may contain fixture text. Only an opaque full-id hash escapes.
    function = name.split('[', 1)[0]
    if not re.fullmatch(r'[A-Za-z0-9_:]+', function):
        function = 'redacted'
    return {'module': module, 'function': function,
            'node_sha256': hashlib.sha256(nodeid.encode('utf-8')).hexdigest()}


class Telemetry:
    def __init__(self, directory, config=None):
        self.config = config
        self.directory = external_directory(directory)
        self.manifest = read_json(self.directory / 'selection.json')
        self.selected = set(self.manifest['selected'])
        self.deselected = []
        self.collection_valid = False
        self.collected = 0
        self.finished = 0
        self.started = {}
        self.skipped_modules = set()
        self.modules = defaultdict(lambda: {'phase_seconds': 0.0, 'collection_seconds': 0.0})
        self.outcomes = Counter()
        self.begin = time.monotonic()

    def emit(self, event, **fields):
        record = {'event': event, 'elapsed_seconds': round(time.monotonic() - self.begin, 6), **fields}
        # Open/close on every record: complete events survive process termination.
        with (self.directory / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, sort_keys=True) + '\n')
        if event in {'collection_start', 'test_start', 'phase_start', 'session_finish'}:
            # Keep telemetry out of capsys/capfd fixture buffers and assertions.
            if self.config is not None:
                capture = self.config.pluginmanager.getplugin('capturemanager')
                with capture.global_and_fixture_disabled():
                    print('[public-ci] ' + json.dumps(record, sort_keys=True), flush=True)

    def safe_node(self, nodeid):
        record = node_identity(nodeid)
        if record['module'] not in self.selected:
            raise pytest.UsageError('unexpected test module in public shard')
        return record

    def pytest_collectstart(self, collector):
        if collector.nodeid in self.selected:
            self.started[collector.nodeid] = time.monotonic()
            self.emit('collection_start', module=collector.nodeid)

    def pytest_collectreport(self, report):
        if report.nodeid in self.selected:
            duration = time.monotonic() - self.started.pop(report.nodeid, time.monotonic())
            self.modules[report.nodeid]['collection_seconds'] += duration
            if report.skipped:
                self.skipped_modules.add(report.nodeid)
            self.emit('collection_end', module=report.nodeid, seconds=duration, outcome=report.outcome)

    def pytest_deselected(self, items):
        self.deselected.extend(item.nodeid for item in items)

    def pytest_collection_finish(self, session):
        actual_modules = {self.safe_node(item.nodeid)['module'] for item in session.items}
        expected = self.manifest['selected_deselect']
        if (not session.items or actual_modules | self.skipped_modules != self.selected
                or sorted(self.deselected) != sorted(expected)):
            raise pytest.UsageError('public collection/deselection coverage mismatch')
        self.collection_valid = True
        self.collected = len(session.items)
        # No raw parameter ids in the collected-node manifest.
        write_json(self.directory / 'collected.json', {
            'source': self.manifest['source'], 'nodes': [self.safe_node(i.nodeid) for i in session.items],
            'deselected_sha256': [node_identity(n)['node_sha256'] for n in self.deselected],
            'skipped_modules': sorted(self.skipped_modules)})
        self.emit('collection_finish', tests=self.collected, deselected=len(self.deselected))

    def pytest_runtest_logstart(self, nodeid, location):
        self.emit('test_start', **self.safe_node(nodeid))

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item):
        self.emit('phase_start', phase='setup', **self.safe_node(item.nodeid))

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_call(self, item):
        self.emit('phase_start', phase='call', **self.safe_node(item.nodeid))

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_teardown(self, item):
        self.emit('phase_start', phase='teardown', **self.safe_node(item.nodeid))

    def pytest_runtest_logreport(self, report):
        node = self.safe_node(report.nodeid)
        self.modules[node['module']]['phase_seconds'] += report.duration
        self.outcomes[report.when + ':' + report.outcome] += 1
        if report.when == 'teardown':
            self.finished += 1
        self.emit('phase_end', **node, phase=report.when, seconds=report.duration, outcome=report.outcome)

    def pytest_sessionfinish(self, session, exitstatus):
        complete = self.collection_valid and self.collected > 0 and self.finished == self.collected
        if exitstatus == 0 and not complete:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        successful = exitstatus == 0 and complete
        write_json(self.directory / 'timings.json', {
            'source': self.manifest['source'], 'complete': complete, 'successful': successful,
            'collected': self.collected, 'finished': self.finished,
            'exit_code': int(session.exitstatus), 'outcomes': dict(self.outcomes),
            'modules': dict(self.modules), 'elapsed_seconds': time.monotonic() - self.begin})
        self.emit('session_finish', complete=complete, successful=successful, exit_code=int(session.exitstatus))


def pytest_addoption(parser):
    parser.addoption('--public-ci-diagnostics', required=True)


def pytest_configure(config):
    config.pluginmanager.register(Telemetry(config.getoption('--public-ci-diagnostics'), config), 'public-ci-telemetry')
