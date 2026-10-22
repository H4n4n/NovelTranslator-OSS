"""Public test support helpers (synthetic only; no private data)."""
import ast

from novel_project import ResidualCJKMicroRepairConfig


def enabled_config(**overrides):
    """Synthetic residual-CJK micro-repair configuration used by public tests."""
    values = {
        "enabled": True,
        "max_total_cjk_chars": 12,
        "max_fragments": 3,
        "max_affected_lines": 3,
        "max_attempts_per_pending_version": 1,
    }
    values.update(overrides)
    return ResidualCJKMicroRepairConfig(**values)


def supervised_callback_source(source, function_name, expected):
    """Verify deferred supervisor dispatch, then expose callbacks to old guards.

    Keep the existing safety/order assertions on their original call bodies.
    Only an exact recovery.run(stage, lambda: expected_callback(...)) in the
    named function can be projected; strings, eager calls, duplicate assignments,
    wrong stages/receivers/callbacks and extra keywords cannot satisfy the guard.
    UTF-8 byte offsets preserve both original arguments and surrounding gates.
    """
    functions = [n for n in ast.parse(source).body
                 if isinstance(n, ast.FunctionDef) and n.name == function_name]
    assert len(functions) == 1
    raw = source.encode("utf-8")
    lines = raw.splitlines(keepends=True)
    edits = []
    def local_nodes(node):
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                yield from local_nodes(child)
    nodes = list(local_nodes(functions[0]))
    for target, (stage, callback) in expected.items():
        def matches(node):
            if isinstance(node, ast.Name):
                return node.id
            if isinstance(node, ast.Tuple) and all(isinstance(e, ast.Name) for e in node.elts):
                return tuple(e.id for e in node.elts)
            return None
        assignments = [n for n in nodes if isinstance(n, ast.Assign)
                       and len(n.targets) == 1 and matches(n.targets[0]) == target]
        if stage == "audit_acquisition":
            # The other assignment is the established retained-intent branch,
            # which must not reacquire. Verify it rather than ignoring duplicates.
            assert len(assignments) == 2, target
            saved = [n for n in assignments if isinstance(n.value, ast.Tuple)]
            assert len(saved) == 1
            retained = ast.parse('(intent["audit_result"], intent["audit_stats"], intent["chunking"])', mode="eval").body
            assert ast.dump(saved[0].value) == ast.dump(retained)
            assignments = [n for n in assignments if n is not saved[0]]
        assert len(assignments) == 1, target
        call = assignments[0].value
        assert isinstance(call, ast.Call)
        assert isinstance(call.func, ast.Attribute) and call.func.attr == "run"
        assert isinstance(call.func.value, ast.Name) and call.func.value.id == "recovery"
        assert len(call.args) == 2 and isinstance(call.args[0], ast.Constant)
        assert call.args[0].value == stage
        deferred = call.args[1]
        assert isinstance(deferred, ast.Lambda)
        args = deferred.args
        assert not any((args.posonlyargs, args.args, args.vararg, args.kwonlyargs,
                        args.kwarg, args.defaults, args.kw_defaults))
        body = deferred.body
        assert isinstance(body, ast.Call) and isinstance(body.func, ast.Name)
        assert body.func.id == callback
        if stage == "audit_publication":
            assert len(call.keywords) == 1 and call.keywords[0].arg == "recover_local"
            recovery = call.keywords[0].value
            assert isinstance(recovery, ast.Name) and recovery.id == "reconcile_local_publication"
        else:
            assert not call.keywords
        def offset(line, column):
            return sum(map(len, lines[:line - 1])) + column
        start, end = offset(call.lineno, call.col_offset), offset(call.end_lineno, call.end_col_offset)
        body_start = offset(body.lineno, body.col_offset)
        body_end = offset(body.end_lineno, body.end_col_offset)
        edits.append((start, end, raw[body_start:body_end]))
    for start, end, body in sorted(edits, reverse=True):
        raw = raw[:start] + body + raw[end:]
    return raw.decode("utf-8")
