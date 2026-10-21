"""Emit bounded public metadata to a pipe; only the supervisor persists files."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import time
from collections import Counter, defaultdict
from functools import wraps

import pytest

from scripts.run_public_tests import FRAME_PREFIX, MAX_FRAME_BYTES, external_directory, read_json

# Explicit -p loading happens before pytest starts descriptor capture. Duplicate
# the reporting pipe now, not a filesystem handle or a cached file-open bypass.
# The descriptor is non-inheritable: descendants cannot hold the pipe open.
try:
    _REPORT_PIPE = os.dup(sys.stdout.fileno()) if stat.S_ISFIFO(os.fstat(sys.stdout.fileno()).st_mode) else None
except (OSError, ValueError, AttributeError):
    _REPORT_PIPE = None
_WRITE_PIPE = os.write
_CLOCK = time.monotonic
_ENCODE_JSON = json.dumps


def send_record(record):
    if _REPORT_PIPE is None:
        raise RuntimeError('public telemetry reporting pipe unavailable')
    frame = b'\n' + FRAME_PREFIX + _ENCODE_JSON(record, sort_keys=True).encode('ascii') + b'\n'
    if len(frame) > MAX_FRAME_BYTES:
        raise ValueError('public telemetry frame exceeds limit')
    while frame:
        written = _WRITE_PIPE(_REPORT_PIPE, frame)
        if written <= 0:
            raise OSError('public telemetry pipe closed')
        frame = frame[written:]


def node_identity(nodeid):
    module, _, name = nodeid.partition('::')
    function = name.split('[', 1)[0]
    if not re.fullmatch(r'[A-Za-z0-9_:]{1,200}', function):
        function = 'redacted'
    return {'module': module, 'function': function,
            'node_sha256': hashlib.sha256(nodeid.encode('utf-8')).hexdigest()}


def phase_hook(function):
    """A reporting defect must not interrupt fixture cleanup or mask a failure."""
    @wraps(function)
    def guarded(self, *args, **kwargs):
        try:
            return function(self, *args, **kwargs)
        except Exception:
            self.failed = True
    return guarded


class Telemetry:
    def __init__(self, directory, config=None, send=None):
        self.directory = external_directory(directory)
        self.manifest = read_json(self.directory / 'selection.json')
        self.send = send or send_record
        self.failed = False
        self.selected = set(self.manifest['selected'])
        self.deselected = []
        self.collection_valid = False
        self.collected = 0
        self.finished = 0
        self.started = {}
        self.skipped_modules = set()
        self.modules = defaultdict(lambda: {'phase_seconds': 0.0, 'collection_seconds': 0.0})
        self.outcomes = Counter()
        self.begin = _CLOCK()

    def emit(self, event, **fields):
        if self.failed:
            return
        try:
            self.send({'event': event, 'elapsed_seconds': round(_CLOCK() - self.begin, 6), **fields})
        except Exception:
            self.failed = True

    def safe_node(self, nodeid):
        record = node_identity(nodeid)
        if record['module'] not in self.selected:
            raise pytest.UsageError('unexpected test module in public shard')
        return record

    def pytest_collectstart(self, collector):
        if collector.nodeid in self.selected:
            self.started[collector.nodeid] = _CLOCK()
            self.emit('collection_start', module=collector.nodeid)

    def pytest_collectreport(self, report):
        if report.nodeid in self.selected:
            duration = _CLOCK() - self.started.pop(report.nodeid, _CLOCK())
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
        for item in session.items:
            self.emit('collection_node', **self.safe_node(item.nodeid))
        self.emit('collection_finish', tests=self.collected, deselected=len(self.deselected),
                  deselected_sha256=[node_identity(n)['node_sha256'] for n in self.deselected],
                  skipped_modules=sorted(self.skipped_modules))

    @phase_hook
    def pytest_runtest_logstart(self, nodeid, location):
        self.emit('test_start', **self.safe_node(nodeid))

    @pytest.hookimpl(tryfirst=True)
    @phase_hook
    def pytest_runtest_setup(self, item):
        self.emit('phase_start', phase='setup', **self.safe_node(item.nodeid))

    @pytest.hookimpl(tryfirst=True)
    @phase_hook
    def pytest_runtest_call(self, item):
        self.emit('phase_start', phase='call', **self.safe_node(item.nodeid))

    @pytest.hookimpl(tryfirst=True)
    @phase_hook
    def pytest_runtest_teardown(self, item):
        self.emit('phase_start', phase='teardown', **self.safe_node(item.nodeid))

    @phase_hook
    def pytest_runtest_logreport(self, report):
        node = self.safe_node(report.nodeid)
        self.modules[node['module']]['phase_seconds'] += report.duration
        self.outcomes[report.when + ':' + report.outcome] += 1
        if report.when == 'teardown':
            self.finished += 1
        self.emit('phase_end', **node, phase=report.when, seconds=report.duration, outcome=report.outcome)

    def pytest_sessionfinish(self, session, exitstatus):
        try:
            complete = self.collection_valid and self.collected > 0 and self.finished == self.collected and not self.failed
            if exitstatus == 0 and not complete:
                session.exitstatus = pytest.ExitCode.TESTS_FAILED
            self.emit('session_finish', source=self.manifest['source'], complete=complete,
                      successful=exitstatus == 0 and complete, collected=self.collected, finished=self.finished,
                      exit_code=int(session.exitstatus), outcomes=dict(self.outcomes), modules=dict(self.modules))
        except Exception:
            self.failed = True
        if self.failed and session.exitstatus == 0:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_addoption(parser):
    parser.addoption('--public-ci-diagnostics', required=True)


def pytest_configure(config):
    config.pluginmanager.register(Telemetry(config.getoption('--public-ci-diagnostics')), 'public-ci-telemetry')
