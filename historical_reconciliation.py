"""AUT-2 local evidence: full-range discovery and conservative freshness.

This module never authorizes a prose edit, provider request, or regeneration.
The existing audit intent and owned-span transaction remain the only writers.
Reports contain opaque identities/counts/hashes, never source or English prose.
"""
import copy
import json
import math
import unicodedata
from pathlib import Path

from noveltranslator_core import atomic_write_json, strict_term_valid_targets
from production_failure_classification import ProductionIntegrityError
from retroactive_span_planning import canonical_hash, digest, target_spans, ownership_policy, plan_chapter, normalized_rules
import retroactive_transactions as tx
from contextlib import contextmanager
from functools import wraps

REVISION = "aut2-history-evidence-v1"
PREFLIGHT_REVISION = "aut2-impact-preflight-v2"
MAX_DISCOVERY_CHAPTERS = 10000
MAX_DISCOVERY_CHANGES = 64
MAX_DISCOVERY_ROWS = 100000


def require(condition, reason):
    if not condition:
        raise ProductionIntegrityError("AUT2: " + reason)


def _proof_plain(path, checked=None):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if checked is not None and part in checked:
            continue
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ProductionIntegrityError("AUT2: LINKED_EVIDENCE")
        if checked is not None:
            checked.add(part)
    return path


class _ProofInputs:
    def __init__(self, module):
        # Parent link observations are shared only inside this non-writing
        # traversal and checked afresh at exit, alongside actual file bytes.
        self.checked = set()
        self.root = _proof_plain(module.OUTPUT_DIR, self.checked).parent.parent
        self.config = copy.deepcopy(module.ACTIVE_NOVEL_CONFIG)
        self.source = copy.deepcopy(module.chapter_map)
        self.profile = copy.deepcopy(module.ACTIVE_PROFILE)
        # Read-only publication consumers deliberately supply a narrower
        # context. Missing convenience paths are derived from this novel root;
        # writer_inventory still independently requires its full writer context.
        relative = dict(OUTPUT_DIR="output/chapters", CHAPTERS_FILE="data/chapters.json",
            GLOSSARY_FILE="data/glossary.json", PROGRESS_FILE="data/progress.json",
            ISSUES_FILE="data/issues.json", AUDIT_HISTORY_FILE="data/audit_history.json",
            CONTINUITY_FILE="data/continuity.json", BACKUP_DIR="backup",
            AUDIT_DIR="output/audits", RETROACTIVE_DIR="output/retroactive_fixes")
        self.paths = {name: Path(getattr(module, name, self.root / path)).absolute()
                      for name, path in relative.items()}
        self.files = {}
        self.directories = {}
        self.plans = {}
        self.active = True
        # Namespace observations are revalidated as well as captured bytes.
        # No negative file lookup can become a reusable proof input.
        for path in (self.root, self.root / "data", module.OUTPUT_DIR,
                     self.paths["AUDIT_DIR"], self.paths["RETROACTIVE_DIR"],
                     self.paths["RETROACTIVE_DIR"] / "transactions", self.paths["BACKUP_DIR"]):
            self._watch(path)
        if self.paths["BACKUP_DIR"].is_dir():
            for path in self.paths["BACKUP_DIR"].rglob("*"):
                if path.is_dir():
                    self._watch(path)
        for name in ("CHAPTERS_FILE", "GLOSSARY_FILE", "PROGRESS_FILE", "ISSUES_FILE",
                     "AUDIT_HISTORY_FILE", "CONTINUITY_FILE"):
            path = self.paths[name]
            if path.is_file():
                self.read(path)
        config = self.root / "novel.json"
        if config.is_file():
            self.read(config)
        # local_module may have loaded its source before this traversal began.
        # Bind that context to the ACTUAL captured library, not just its path.
        source_path = self.paths["CHAPTERS_FILE"]
        if source_path in self.files:
            from chapter_library import index_chapter_library
            from novel_project import NovelConfig, legacy_project
            policy = (NovelConfig.from_dict(json.loads(self.read(config))) if config.is_file()
                      else legacy_project(self.root).config)
            if self.config is not None and policy.id != self.config.id:
                raise ProductionIntegrityError("AUT2: FOREIGN_PROOF_INPUT")
            source = index_chapter_library(json.loads(self.read(source_path)),
                policy.final_chapter, policy.chapter_numbering)
            if source != self.source:
                raise ProductionIntegrityError("AUT2: SOURCE_INVENTORY_DRIFT")

    def check_module(self, module):
        if (not self.active or self.config != module.ACTIVE_NOVEL_CONFIG
                or self.source != module.chapter_map or self.profile != module.ACTIVE_PROFILE
                or any(Path(getattr(module, name, path)).absolute() != path
                       for name, path in self.paths.items())):
            raise ProductionIntegrityError("AUT2: PROOF_INPUT_SCOPE_DRIFT")

    def _watch(self, path):
        path = Path(path).absolute()
        if path in self.directories:
            return
        path = _proof_plain(path, self.checked)
        # An absent directory is observed afresh; its nearest existing parent
        # detects creation during this traversal. Absence is never memoized.
        while not path.is_dir() and path != path.parent:
            path = _proof_plain(path.parent, self.checked)
        if path not in self.directories:
            self.directories[path] = frozenset(p.name for p in path.iterdir())

    def _entry(self, path):
        if not self.active:
            raise ProductionIntegrityError("AUT2: CLOSED_PROOF_INPUTS")
        path = Path(path).absolute()
        if not path.is_relative_to(self.root):
            raise ProductionIntegrityError("AUT2: FOREIGN_PROOF_INPUT")
        if path not in self.files:
            path = _proof_plain(path, self.checked)
            self._watch(path.parent)
            raw = path.read_bytes()
            from hashlib import sha256
            self.files[path] = (raw, sha256(raw).hexdigest())
        return self.files[path]

    def read(self, path):
        return self._entry(path)[0]

    def sha(self, path):
        # Observe existence on each call; recheck links and bytes before return.
        # Missing evidence is not stored. Revalidation detects removed inputs.
        path = Path(path)
        if not path.is_file():
            _proof_plain(path)
            return ""
        return self._entry(path)[1]

    def read_json(self, path):
        return json.loads(self.read(path))

    def read_object(self, path):
        from historical_reconciliation import read_object
        return read_object(path, _inputs=self)

    def build_plan(self, request, chapters, chapter_map):
        # Exact request values, original bytes and source text qualify a PURE
        # computation. Neither path nor metadata nor a previous verdict is a key.
        import retroactive_transactions as tx
        key = (tx.SCHEMA_VERSION, tx.POLICY_VERSION, tx.build_plan,
               json.dumps(request, ensure_ascii=False, sort_keys=True, allow_nan=False),
               tuple((n, raw, chapter_map.get(n, {}).get("text"))
                     for n, raw in sorted(chapters.items())))
        if key not in self.plans:
            self.plans[key] = copy.deepcopy(tx.build_plan(request, chapters, chapter_map))
        return copy.deepcopy(self.plans[key])

    def revalidate(self, module):
        self.check_module(module)
        from hashlib import sha256
        checked = set()  # Fresh link observations; no metadata crosses boundaries.
        for path, (raw, digest) in self.files.items():
            fresh = _proof_plain(path, checked).read_bytes()
            if fresh != raw or sha256(fresh).hexdigest() != digest:
                raise ProductionIntegrityError("AUT2: PROOF_INPUT_BYTES_CHANGED")
        for path, names in self.directories.items():
            if frozenset(p.name for p in _proof_plain(path).iterdir()) != names:
                raise ProductionIntegrityError("AUT2: PROOF_INPUT_NAMESPACE_CHANGED")
        self.check_module(module)

    def close(self):
        self.active = False
        self.files.clear()
        self.directories.clear()
        self.plans.clear()
        self.checked.clear()


@contextmanager
def proof_input_scope(module):
    """One non-writing traversal, with fresh physical revalidation on success."""
    inputs = _ProofInputs(module)
    try:
        yield inputs
        inputs.revalidate(module)
    finally:
        inputs.close()


def proof_traversal(function=None, *, fresh=False):
    """Explicitly shared only by nested pure validators; fresh at every caller."""
    if function is None:
        return lambda owner: proof_traversal(owner, fresh=fresh)
    @wraps(function)
    def wrapped(module, *args, _inputs=None, **kwargs):
        if fresh and _inputs is not None:
            raise ProductionIntegrityError("AUT2: INDEPENDENT_PROOF_BOUNDARY_REQUIRED")
        if _inputs is not None:
            _inputs.check_module(module)
            return function(module, *args, _inputs=_inputs, **kwargs)
        with proof_input_scope(module) as inputs:
            return function(module, *args, _inputs=inputs, **kwargs)
    return wrapped



def plain(path):
    path = Path(path)
    require(not any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction())
                    for p in (path, *path.parents)), "LINKED_EVIDENCE")
    return path


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate evidence key")
        result[key] = value
    return result


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite evidence number")
    return number


def _invalid_constant(value):
    raise ValueError("nonfinite evidence constant")


def read_value(path, *, _inputs=None):
    try:
        # Decode explicitly: json.loads(bytes) also accepts UTF-16/32. Existing
        # UTF-8/BOM, whitespace, CRLF and pretty printing remain valid.
        raw = plain(path).read_bytes() if _inputs is None else _inputs.read(path)
        value = json.loads(raw.decode("utf-8-sig"),
                           object_pairs_hook=_unique_object, parse_float=_finite_float,
                           parse_constant=_invalid_constant)
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, OSError, RecursionError) as exc:
        raise ProductionIntegrityError("AUT2: UNREADABLE_EVIDENCE") from exc
    return value


def read_object(path, *, _inputs=None):
    value = read_value(path, _inputs=_inputs)
    require(isinstance(value, dict), "INVALID_EVIDENCE")
    return value



def audit_intent(module, path, *, _inputs=None):
    from production_periodic_audit_finalization import load_intent
    value = read_object(path, _inputs=_inputs)
    require(type(value.get("start")) is int and type(value.get("end")) is int
            and 1 <= value["start"] <= value["end"], "INVALID_AUDIT_BOUNDARY")
    try:
        intent = load_intent(module, value["start"], value["end"], _inputs=_inputs)
        transaction = intent["transaction_plan"]["transaction_id"]
        require(isinstance(transaction, str) and len(transaction) == 64
                and all(c in "0123456789abcdef" for c in transaction), "INVALID_TRANSACTION_ID")
        require(path.name == f"audit_{intent['start']:04d}_{intent['end']:04d}.finalization.json",
                "AUDIT_PATH_MISMATCH")
        require(isinstance(intent["record"], dict), "INVALID_AUDIT_RECORD")
    except (KeyError, TypeError) as exc:
        raise ProductionIntegrityError("AUT2: INVALID_AUDIT_INTENT") from exc
    return intent


def source_inventory(chapter_map):
    require(all(type(n) is int and n > 0 and isinstance(unit, dict)
                and isinstance(unit.get("text"), str) for n, unit in chapter_map.items()),
            "INVALID_SOURCE_INVENTORY")
    return [{"chapter": n, "unit_sha256": canonical_hash(unit),
             "source_sha256": digest(unit["text"].encode("utf-8"))}
            for n, unit in sorted(chapter_map.items())]


def _discovery_intent(module, start, end, expected_sha):
    """Reprove the existing finalizer's intent; never acquire or finalize it.

    This adapter supports the current audit publication prefix only. Older
    superseded state needs the separate lineage owner, not a guessed baseline.
    """
    from types import SimpleNamespace
    from production_periodic_audit_finalization import (
        intent_path, state_paths, verify_publication, transaction_proof,
        prepare_finalization,
    )
    path = plain(intent_path(module, start, end))
    require(path.is_file(), "MISSING_ACCEPTED_CHANGE_PROVENANCE")
    intent = audit_intent(module, path)
    require(intent["intent_sha256"] == expected_sha, "DISCOVERY_INTENT_DRIFT")
    novel = module.ACTIVE_NOVEL_CONFIG.id if module.ACTIVE_NOVEL_CONFIG else "legacy_single_novel"
    request = intent["transaction_plan"]["request"]
    require(request["novel_id"] == novel and request["audit_label"] == f"{start:04d}_{end:04d}"
            and request["through_chapter"] == end, "FOREIGN_DISCOVERY_INTENT")
    require("historical_proof" in intent and "historical_reconciliation" in intent["record"],
            "DISCOVERY_SOURCE_UNIT_WITNESS_MISSING")
    require(module.progress["last_completed"] == end, "SUPERSEDED_AUDIT_REQUIRES_LINEAGE_REVIEW")
    verify_publication(module, intent)
    for key, name in (("glossary", "glossary"), ("progress", "progress"),
                      ("issues", "issues_data"), ("history", "audit_history")):
        require(read_object(state_paths(module)[key]) == getattr(module, name), "DISCOVERY_STATE_DRIFT")
    # Reject ambiguous JSON even when the older transaction reader would accept it.
    for evidence in plain(module.RETROACTIVE_DIR / "transactions").glob("*.json"):
        read_object(evidence)
    detached = SimpleNamespace(**vars(module))
    for key, name in (("glossary", "glossary"), ("progress", "progress"),
                      ("issues", "issues_data"), ("history", "audit_history")):
        setattr(detached, name, copy.deepcopy(intent["baseline"][key]))
    if detached.progress.get("provider") == "command_code_provider" and intent["chunking"].get("candidate_count", 0):
        from goat_periodic_audit_finalize_plan import checkpoint_proof
        paid = checkpoint_proof(detached, start, end)
        require(all(paid[k] == intent[k] for k in ("audit_result", "audit_stats")),
                "PAID_AUDIT_EVIDENCE_DRIFT")
    proof = transaction_proof(detached, start, end, intent["baseline"]["glossary"],
                              intent["post"]["glossary"], intent["record"]["changes"])
    prepared = prepare_finalization(detached, start, end, intent["audit_result"], intent["audit_stats"],
                                   intent["chunking"], timestamp=intent["record"]["timestamp_unix"])
    for record in (prepared["record"], prepared["post"]["history"]["audits"][-1]):
        record["retroactive"] = intent["record"]["retroactive"]
    require(prepared == {k: v for k, v in intent.items() if k != "intent_sha256"},
            "DISCOVERY_AUDIT_AUTHORIZATION_DRIFT")
    return intent, proof



@proof_traversal
def _historical_reservations(module, selected, *, unpublished=False, writer_zero_send=False, chapter_bytes=None, _events=None, _inputs=None):
    """Prove prior immutable publications before reserving their ownership.

    Only a veto inventory: never feed it into positive edit authorization.
    A history marker, hash-consistent intent alone, or incomplete lineage is
    insufficient. Older transactions are reconstructed from verified BEFORE
    backups/current bytes, then reproved by their original finalizer.
    """
    from types import SimpleNamespace
    from production_periodic_audit_finalization import (
        transaction_proof, prepare_finalization, state_paths,
    )
    read_bytes = _inputs.read
    read_object = _inputs.read_object
    try:
        records = selected["baseline"]["history"]["audits"]
        paths = list(plain(module.AUDIT_DIR).glob("*.finalization.json"))
        intents = [audit_intent(module, path, _inputs=_inputs) for path in paths]
        if unpublished:
            require(not any(i["start"] == selected["start"] for i in intents),
                    "COMPETING_UNPUBLISHED_OWNERSHIP_INTENT")
            intents.append(selected)
        prior = sorted((i for i in intents if i != selected and i["end"] < selected["start"]),
                       key=lambda i: (i["start"], i["end"]))
        require(len(intents) == len(prior) + 1 and selected in intents,
                "UNRELATED_OR_COMPETING_OWNERSHIP_INTENT")
        require([i["record"] for i in prior] == records,
                "PRIOR_RECORD_WITHOUT_VALIDATING_INTENT")
        blobs = {digest(raw): raw for raw in (
            chapter_bytes[n] if chapter_bytes is not None else
            read_bytes(module.OUTPUT_DIR / f"{n:04d}.md")
            for n in module.progress["completed"])}
        for intent in intents:
            for item in intent["transaction_plan"]["chapters"]:
                backup = plain(module.BACKUP_DIR / f"retroactive_{intent['transaction_plan']['transaction_id']}" /
                               "chapters_before" / f"{item['chapter']:04d}.md")
                if item["edits"] and backup.exists():
                    raw = read_bytes(backup)
                    require(digest(raw) == item["before_sha256"], "HISTORICAL_BACKUP_DRIFT")
                    blobs[digest(raw)] = raw
        from production_standalone_lineage import witnesses, add_blobs, prove_gap
        standalone = witnesses(module, active_plan=selected["transaction_plan"])
        add_blobs(module, standalone, blobs)
        policy, evidence, latest, previous_end = {}, [], {}, 0
        consumed = set()
        novel = selected["transaction_plan"]["request"]["novel_id"]
        for index, intent in enumerate(prior):
            prove_gap(module, standalone, previous_end, records[:index], blobs,
                      latest, policy, evidence, consumed, events=_events)
            start, end = intent["start"], intent["end"]
            request = intent["transaction_plan"]["request"]
            require(start == previous_end + 1 and request["novel_id"] == novel
                    and request["audit_label"] == f"{start:04d}_{end:04d}"
                    and request["through_chapter"] == end, "HISTORICAL_OWNERSHIP_LINEAGE_GAP")
            require("historical_proof" in intent and "historical_reconciliation" in intent["record"],
                    "HISTORICAL_OWNERSHIP_SOURCE_WITNESS_MISSING")
            require(intent["baseline"]["history"]["audits"] == records[:index]
                    and intent["post"]["history"]["audits"] == records[:index + 1],
                    "HISTORICAL_OWNERSHIP_HISTORY_FORK")
            require(request["committed_chapters"] == list(range(1, end + 1)),
                    "HISTORICAL_OWNERSHIP_INVENTORY_INCOMPLETE")
            detached = SimpleNamespace(**vars(module))
            for key, name in (("glossary", "glossary"), ("progress", "progress"),
                              ("issues", "issues_data"), ("history", "audit_history")):
                setattr(detached, name, copy.deepcopy(intent["baseline"][key]))
                require(read_object(plain(module.BACKUP_DIR / f"checkpoint_{end:04d}" /
                                          state_paths(module)[key].name)) == intent["post"][key],
                        "HISTORICAL_OWNERSHIP_CHECKPOINT_DRIFT")
            checkpoint = plain(module.periodic_audit_checkpoint_path(start, end))
            if writer_zero_send:
                _writer_checkpoint(module, intent)
            else:
                require(digest(read_bytes(checkpoint)) == intent["checkpoint_sha256"],
                        "HISTORICAL_OWNERSHIP_CHECKPOINT_DRIFT")
            require(digest(read_bytes(module.BACKUP_DIR / f"checkpoint_{end:04d}" /
                                 module.CONTINUITY_FILE.name)) == intent["continuity_sha256"],
                    "HISTORICAL_OWNERSHIP_CONTINUITY_DRIFT")
            if detached.progress.get("provider") == "command_code_provider" and intent["chunking"].get("candidate_count", 0):
                from goat_periodic_audit_finalize_plan import checkpoint_proof
                paid = checkpoint_proof(detached, start, end)
                require(all(paid[k] == intent[k] for k in ("audit_result", "audit_stats")),
                        "HISTORICAL_OWNERSHIP_PAID_EVIDENCE_DRIFT")
            before, after = {}, {}
            for item in intent["transaction_plan"]["chapters"]:
                n = item["chapter"]
                raw = blobs.get(item["before_sha256"])
                require(raw is not None and (n not in latest or latest[n] == item["before_sha256"]),
                        "HISTORICAL_OWNERSHIP_BYTE_CHAIN_UNPROVEN")
                before[n] = raw
                after[n] = tx.transform_bytes(raw, item["edits"])
                latest[n] = item["after_sha256"]
            proof = transaction_proof(detached, start, end, intent["baseline"]["glossary"],
                intent["post"]["glossary"], intent["record"]["changes"], chapter_bytes=after, _inputs=_inputs)
            require(proof["state"] and proof["state"]["state"] == "FINALIZED",
                    "HISTORICAL_OWNERSHIP_NOT_FINALIZED")
            prepared = prepare_finalization(detached, start, end, intent["audit_result"],
                intent["audit_stats"], intent["chunking"], timestamp=intent["record"]["timestamp_unix"],
                chapter_bytes=after, _inputs=_inputs)
            for record in (prepared["record"], prepared["post"]["history"]["audits"][-1]):
                record["retroactive"] = intent["record"]["retroactive"]
            require(all(prepared[k] == intent[k] for k in ("post", "record", "transaction_plan", "operation_id")),
                    "HISTORICAL_OWNERSHIP_AUTHORIZATION_DRIFT")
            require(read_object(module.AUDIT_DIR / f"audit_{start:04d}_{end:04d}.json") == intent["record"],
                    "HISTORICAL_OWNERSHIP_RECORD_DRIFT")
            _reserve_policy(policy, ownership_policy(intent["baseline"]["glossary"], intent["post"]["glossary"]))
            evidence.append(intent["intent_sha256"])
            if _events is not None:
                _events.append((intent["transaction_plan"], before, after))
            previous_end = end
        require(previous_end == selected["start"] - 1, "HISTORICAL_OWNERSHIP_LINEAGE_GAP")
        prove_gap(module, standalone, previous_end, records, blobs,
                  latest, policy, evidence, consumed, events=_events)
        require(len(consumed) == len(standalone), "STANDALONE_CHRONOLOGY_UNPROVEN")
        selected_before = {i["chapter"]: i["before_sha256"] for i in selected["transaction_plan"]["chapters"]}
        for n, sha in latest.items():
            current = (chapter_bytes[n] if chapter_bytes is not None else
                       read_bytes(module.OUTPUT_DIR / f"{n:04d}.md"))
            require(selected_before.get(n, digest(current)) == sha,
                    "HISTORICAL_OWNERSHIP_BYTE_CHAIN_UNPROVEN")
        return policy, evidence
    except (ProductionIntegrityError, OSError, KeyError, TypeError, ValueError) as exc:
        raise ProductionIntegrityError("AUT2: HISTORICAL_OWNERSHIP_UNAVAILABLE_UNPROVEN") from exc


def _reserve_policy(policy, extra):
    for key, value in extra.items():
        item = policy.setdefault(key, {"targets": [], "source_forms": [], "alias_of": [], "contextual": False})
        item["contextual"] |= value["contextual"]
        for field in ("targets", "source_forms", "alias_of"):
            item[field] = sorted(set(item[field]) | set(value[field]))



WRITER_AUTHORIZATION_REVISION = "historical-writer-authorization-v1"


def _writer_checkpoint(module, intent):
    """No-send audit absence is an explicit local fact, never a paid witness."""
    path = plain(module.periodic_audit_checkpoint_path(intent["start"], intent["end"]))
    if path.is_file():
        require(digest(path.read_bytes()) == intent["checkpoint_sha256"], "WRITER_ACCEPTED_CHANGE_UNPROVEN")
        read_object(path)
        return
    from production_periodic_audit_finalization import STAT_TOTAL_FIELDS
    require(intent["checkpoint_sha256"] == "" and intent["chunking"].get("candidate_count") == 0
            and intent["chunking"].get("api_request_count", 0) == 0
            and intent["audit_result"].get("decisions") == []
            and intent["audit_stats"].get("api_request_count", 0) == 0
            and not intent["audit_stats"].get("accounting_components")
            and all(intent["audit_stats"].get(k, 0) == 0 for k in STAT_TOTAL_FIELDS)
            and intent["post"]["glossary"] == intent["baseline"]["glossary"]
            and not intent["transaction_plan"]["request"]["rules"]
            and not any(item["edits"] for item in intent["transaction_plan"]["chapters"]),
            "WRITER_ACCEPTED_CHANGE_UNPROVEN")


def writer_module(output_dir, novel_id):
    """Derive context from canonical novel files, never a caller's report."""
    from novel_project import NovelProject, NovelConfig, repository_root, legacy_project
    from goat_periodic_audit_finalize_plan import local_module
    output = plain(Path(output_dir).absolute())
    root = output.parent.parent
    require(output == root / "output/chapters", "AUTHORITATIVE_WRITER_CONTEXT_UNAVAILABLE")
    config_path = plain(root / "novel.json")
    if config_path.is_file():
        config = NovelConfig.from_dict(read_object(config_path))
        require(config.id == novel_id, "FOREIGN_WRITER_NOVEL")
        project = NovelProject(repository_root(), root, config)
    else:
        require(novel_id == "legacy_single_novel", "AUTHORITATIVE_WRITER_CONTEXT_UNAVAILABLE")
        project = legacy_project(root)
    from production_run_lock import held_production_run_lock, ProductionRunLockedError
    try:
        held_production_run_lock(root)
    except RuntimeError as exc:
        raise ProductionRunLockedError("Actual caller-held novel production lock required") from exc
    module = local_module(project)
    if project.is_legacy:
        module.ACTIVE_NOVEL_CONFIG = None
    return module


def writer_inventory(module, supplied, through):
    """Independent disk inventory under the actual caller-held novel lock."""
    from production_run_lock import held_production_run_lock, ProductionRunLockedError
    root = plain(Path(module.OUTPUT_DIR).absolute()).parent.parent
    try:
        held_production_run_lock(root)  # Registry/descriptor proof, not file existence.
    except RuntimeError as exc:
        raise ProductionRunLockedError("Actual caller-held novel production lock required") from exc
    return read_only_inventory(module, supplied, through)


def read_only_inventory(module, supplied, through):
    """Same authoritative inventory for proof readers; grants no writer lease."""
    from chapter_library import index_chapter_library
    root = plain(Path(module.OUTPUT_DIR).absolute()).parent.parent
    expected_paths = dict(OUTPUT_DIR="output/chapters", RETROACTIVE_DIR="output/retroactive_fixes",
        BACKUP_DIR="backup", AUDIT_DIR="output/audits", CHAPTERS_FILE="data/chapters.json",
        PROGRESS_FILE="data/progress.json", GLOSSARY_FILE="data/glossary.json",
        AUDIT_HISTORY_FILE="data/audit_history.json", ISSUES_FILE="data/issues.json",
        CONTINUITY_FILE="data/continuity.json")
    for name, relative in expected_paths.items():
        require(plain(Path(getattr(module, name)).absolute()) == root / relative,
                "FOREIGN_WRITER_ROOT")
    progress = read_object(module.PROGRESS_FILE)
    last, numbers = progress.get("last_completed"), progress.get("completed")
    require(type(last) is int and last > 0 and isinstance(numbers, list)
            and all(type(n) is int for n in numbers) and numbers == list(range(1, last + 1))
            and type(progress.get("next_chapter")) is int and progress["next_chapter"] == last + 1,
            "NONSEQUENTIAL_COMMITTED_INVENTORY")
    require(type(through) is int and 1 <= through <= last, "COMMITTED_INVENTORY_DRIFT")
    require(isinstance(supplied, list) and all(type(n) is int for n in supplied)
            and supplied == sorted(set(supplied)), "NONSEQUENTIAL_COMMITTED_INVENTORY")
    require(supplied in (numbers, list(range(1, through + 1))), "COMMITTED_INVENTORY_INCOMPLETE")
    require(module.progress.get("completed") == numbers and module.progress.get("last_completed") == last
            and module.progress.get("next_chapter") == last + 1, "COMMITTED_INVENTORY_DRIFT")
    library = read_value(module.CHAPTERS_FILE)
    require(isinstance(library, list) and library, "SOURCE_INVENTORY_UNPROVEN")
    from novel_project import NovelConfig, legacy_project
    if (root / "novel.json").is_file():
        config = NovelConfig.from_dict(read_object(root / "novel.json"))
        require(module.ACTIVE_NOVEL_CONFIG is not None and module.ACTIVE_NOVEL_CONFIG.id == config.id,
                "FOREIGN_WRITER_NOVEL")
    else:
        require(module.ACTIVE_NOVEL_CONFIG is None, "FOREIGN_WRITER_NOVEL")
        config = legacy_project(root).config
    try:
        source_map = index_chapter_library(library, config.final_chapter, config.chapter_numbering)
    except ValueError as exc:
        raise ProductionIntegrityError("AUT2: SOURCE_INVENTORY_DRIFT") from exc
    require(len(source_map) == len(library) and list(source_map) == list(range(1, len(library) + 1))
            and set(numbers) <= source_map.keys() and source_map == module.chapter_map,
            "SOURCE_INVENTORY_DRIFT")
    source_inventory(source_map)
    files = {p.relative_to(module.OUTPUT_DIR).as_posix(): plain(p)
             for p in plain(module.OUTPUT_DIR).rglob("*.md")}
    expected = {f"{n:04d}.md" for n in numbers}
    require(expected <= files.keys(), "COMMITTED_INVENTORY_INCOMPLETE")
    extra = files.keys() - expected
    # The existing startup reconciler owns its exact reviewed-pending exception
    # BEFORE due audits. A historical writer cannot certify or consume it.
    require(not extra, "COMMITTED_INVENTORY_EXTRA")
    from translation_profile import load_translation_profile
    from novel_project import repository_root
    profile = load_translation_profile(config.profile, repository_root())
    require(set(module.ACTIVE_PROFILE.retroactive_safe_categories) == set(profile.retroactive_safe_categories),
            "WRITER_POLICY_DRIFT")
    return dict(novel_id=(module.ACTIVE_NOVEL_CONFIG.id if module.ACTIVE_NOVEL_CONFIG else "legacy_single_novel"),
                config_sha256=canonical_hash(config.to_dict()), committed=numbers, through=through, source_inventory=source_inventory(source_map),
                pending_exception={name: digest(files[name].read_bytes()) for name in extra})


def writer_evidence_files(module, lineage, *, _inputs=None):
    historical_files = {}
    # Bind physical immutable evidence as well as semantic intent identities.
    # Current transaction/publication prefixes are deliberately excluded; their
    # exact immutable plan and BEFORE inventory are bound separately below.
    for path in plain(module.AUDIT_DIR).glob("*.finalization.json"):
        prior = audit_intent(module, path, _inputs=_inputs)
        if prior["intent_sha256"] not in lineage:
            continue
        prior_request = prior["transaction_plan"]["request"]
        prior_slot = canonical_hash({k: prior_request[k] for k in ("novel_id", "audit_label", "through_chapter")})
        files = [path, module.AUDIT_DIR / f"audit_{prior['start']:04d}_{prior['end']:04d}.json"]
        checkpoint = module.periodic_audit_checkpoint_path(prior["start"], prior["end"])
        if checkpoint.is_file():
            files.append(checkpoint)
        else:
            # _writer_checkpoint has already proved the exact no-send shape.
            root = Path(module.OUTPUT_DIR).absolute().parent.parent
            historical_files[Path(checkpoint).absolute().relative_to(root).as_posix()] = "ABSENT_ZERO_SEND"
        files += list((module.RETROACTIVE_DIR / "transactions").glob(f"{prior_slot}.*.json"))
        files += list((module.BACKUP_DIR / f"checkpoint_{prior['end']:04d}").glob("*"))
        files += list((module.BACKUP_DIR / f"retroactive_{prior['transaction_plan']['transaction_id']}" /
                       "chapters_before").glob("*.md"))
        root = Path(module.OUTPUT_DIR).absolute().parent.parent
        for evidence in files:
            require(plain(evidence).is_file(), "HISTORICAL_OWNERSHIP_UNAVAILABLE_UNPROVEN")
            historical_files[Path(evidence).absolute().relative_to(root).as_posix()] = digest(evidence.read_bytes() if _inputs is None else _inputs.read(evidence))
    from production_standalone_lineage import evidence_files
    historical_files.update(evidence_files(module, lineage))
    return historical_files


@proof_traversal(fresh=True)
def writer_authorization(module, plan, *, selected=None, unpublished=False, recovery_before=None, _inputs=None):
    """Fresh veto proof for this exact immutable plan; never edit authority alone.

    This is not the discovery report. Both callers rederive disk inventory and
    prior-intent proofs before persistence; the receipt binds their versioned
    hashes to the original plan without rewriting that plan's schema or bytes.
    """
    from production_periodic_audit_finalization import load_intent, verify_publication
    read_bytes, read_object = _inputs.read, _inputs.read_object
    request = plan["request"]
    inventory = writer_inventory(module, request["committed_chapters"], request["through_chapter"])
    require(request["novel_id"] == inventory["novel_id"], "FOREIGN_WRITER_NOVEL")
    require(request["safe_categories"] == sorted(module.ACTIVE_PROFILE.retroactive_safe_categories),
            "WRITER_POLICY_DRIFT")
    tx.validate_plan(plan, request, module.chapter_map)
    if recovery_before is not None:
        # Read-only reproof of a complete immutable pre-audit snapshot. This
        # path cannot prove an edit and is never used by transaction execution.
        require(not request["rules"] and not any(i["edits"] for i in plan["chapters"])
                and request["committed_chapters"] == list(range(1, request["through_chapter"] + 1))
                and set(recovery_before) == set(inventory["committed"]),
                "RECOVERY_BASELINE_NOT_ZERO_EDIT")
    originals = {}
    for item in plan["chapters"]:
        n = item["chapter"]
        path = module.OUTPUT_DIR / f"{n:04d}.md"
        raw = recovery_before[n] if recovery_before is not None else read_bytes(path)
        require(digest(raw) in {item["before_sha256"], item["after_sha256"]},
                f"Chapter {n}: current SHA neither BEFORE nor AFTER")
        backup = plain(module.BACKUP_DIR / f"retroactive_{plan['transaction_id']}" / "chapters_before" / path.name)
        if backup.exists():
            require(digest(read_bytes(backup)) == item["before_sha256"], f"Chapter {n}: backup hash mismatch")
        if item["edits"] and digest(raw) == item["after_sha256"]:
            require(backup.is_file(), f"Chapter {n}: AFTER lacks BEFORE backup")
            raw = read_bytes(backup)
        originals[n] = raw
    require(_inputs.build_plan(request, originals, module.chapter_map) == plan, "IMMUTABLE_PLAN_POLICY_CONFLICT")
    if selected is None:
        import re
        match = re.fullmatch(r"(\d{4,})_(\d{4,})", request["audit_label"])
        if match:
            start, end = map(int, match.groups())
            require(end == request["through_chapter"], "WRITER_ACCEPTED_CHANGE_UNPROVEN")
            selected = load_intent(module, start, end, _inputs=_inputs)
            if selected is None and request["rules"]:
                # The legacy GOAT direct-before-intent prefix has a transport-
                # free accepted-checkpoint owner. Derive its candidate afresh.
                # Other periodic callers need an immutable accepted intent.
                require(module.progress.get("provider") == "command_code_provider",
                        "WRITER_ACCEPTED_CHANGE_UNPROVEN")
                from goat_periodic_audit_finalize_plan import checkpoint_proof
                from production_periodic_audit_finalization import prepare_finalization, publication_timestamp
                paid = checkpoint_proof(module, start, end)
                selected = prepare_finalization(module, start, end, paid["audit_result"], paid["audit_stats"],
                    paid["chunking"], timestamp=publication_timestamp(module, start, end), _inputs=_inputs)
                require(selected["transaction_plan"] == plan, "WRITER_ACCEPTED_CHANGE_UNPROVEN")
                unpublished = True
        if selected is None:
            history = module.audit_history if recovery_before is not None else read_object(module.AUDIT_HISTORY_FILE)
            last_audit = (module.progress if recovery_before is not None else
                          read_object(module.PROGRESS_FILE)).get("last_audited_chapter", 0)
            require(type(last_audit) is int and 0 <= last_audit <= request["through_chapter"],
                    "HISTORICAL_OWNERSHIP_UNAVAILABLE_UNPROVEN")
            selected = dict(start=last_audit + 1, end=request["through_chapter"],
                baseline=dict(history=history), transaction_plan=plan)
            unpublished = True
        else:
            require(selected["transaction_plan"] == plan, "IMMUTABLE_PLAN_POLICY_CONFLICT")
            verify_publication(module, selected, _inputs=_inputs)
            # Direct recovery must reprove the current accepted audit too, not
            # merely trust a self-consistent stored intent/hash.
            from types import SimpleNamespace
            from production_periodic_audit_finalization import prepare_finalization, transaction_proof
            detached = SimpleNamespace(**vars(module))
            for key, name in (("glossary", "glossary"), ("progress", "progress"),
                              ("issues", "issues_data"), ("history", "audit_history")):
                setattr(detached, name, copy.deepcopy(selected["baseline"][key]))
            if detached.progress.get("provider") == "command_code_provider" and selected["chunking"].get("candidate_count", 0):
                from goat_periodic_audit_finalize_plan import checkpoint_proof
                paid = checkpoint_proof(detached, selected["start"], selected["end"])
                require(all(paid[k] == selected[k] for k in ("audit_result", "audit_stats")),
                        "WRITER_ACCEPTED_CHANGE_UNPROVEN")
            transaction_proof(detached, selected["start"], selected["end"], selected["baseline"]["glossary"],
                              selected["post"]["glossary"], selected["record"]["changes"], _inputs=_inputs)
            prepared = prepare_finalization(detached, selected["start"], selected["end"], selected["audit_result"],
                selected["audit_stats"], selected["chunking"], timestamp=selected["record"]["timestamp_unix"], _inputs=_inputs)
            for value in (prepared["record"], prepared["post"]["history"]["audits"][-1]):
                value["retroactive"] = selected["record"]["retroactive"]
            require(all(prepared[k] == selected[k] for k in ("post", "record", "transaction_plan", "operation_id")),
                    "WRITER_ACCEPTED_CHANGE_UNPROVEN")
    slot = canonical_hash({k: request[k] for k in ("novel_id", "audit_label", "through_chapter")})
    directory = module.RETROACTIVE_DIR / "transactions"
    if (directory / f"{slot}.plan.json").exists() and not (directory / f"{slot}.authorization.json").exists():
        require(not unpublished and "historical_proof" in selected
                and "historical_reconciliation" in selected.get("record", {}),
                "IMMUTABLE_PLAN_POLICY_CONFLICT: HISTORICAL_OWNERSHIP_UNAVAILABLE_UNPROVEN")
    if "checkpoint_sha256" in selected:
        require("historical_proof" in selected and "historical_reconciliation" in selected.get("record", {}),
                "HISTORICAL_OWNERSHIP_UNAVAILABLE_UNPROVEN")
        _writer_checkpoint(module, selected)
    reservations, lineage = _historical_reservations(module, selected, unpublished=unpublished,
        writer_zero_send=True, chapter_bytes=recovery_before, _inputs=_inputs)
    reserved = copy.deepcopy(request["ownership_policy"])
    _reserve_policy(reserved, reservations)
    for item in plan["chapters"]:
        edits, _ = plan_chapter(module.chapter_map[item["chapter"]]["text"], originals[item["chapter"]],
                               request["rules"], reserved, request["safe_categories"])
        # A reservation is a veto, never a replacement plan or new source anchor.
        require(all(edit in edits for edit in item["edits"]), "HISTORICAL_OWNERSHIP_RESERVATION")
    known_plans = [plan]
    for path in plain(module.AUDIT_DIR).glob("*.finalization.json"):
        known_plans.append(audit_intent(module, path, _inputs=_inputs)["transaction_plan"])
    from production_standalone_lineage import witnesses, verify_transaction_inventory
    known_plans.extend(w["plan"] for w in witnesses(module, active_plan=plan))
    verify_transaction_inventory(module, known_plans)
    before_manifest = {f"{n:04d}.md": digest(recovery_before[n] if recovery_before is not None else
                       read_bytes(module.OUTPUT_DIR / f"{n:04d}.md")) for n in inventory["committed"]}
    before_manifest.update({f"{i['chapter']:04d}.md": i["before_sha256"] for i in plan["chapters"]})
    historical_files = writer_evidence_files(module, lineage, _inputs=_inputs)
    accepted = ({k: selected[k] for k in ("start", "end", "baseline", "audit_result", "audit_stats",
                 "checkpoint_sha256", "continuity_sha256")} if "checkpoint_sha256" in selected else
                {"standalone_exact_policy": request["ownership_policy"]})
    if "checkpoint_sha256" in selected:
        accepted["post_glossary"] = selected["post"]["glossary"]
        accepted["source_witness"] = selected["record"]["historical_reconciliation"]
    proof = dict(revision=WRITER_AUTHORIZATION_REVISION, plan_sha256=canonical_hash(plan),
        accepted_change_sha256=canonical_hash(accepted), historical_evidence_sha256=canonical_hash(historical_files),
                 inventory_sha256=canonical_hash({**inventory, "before_chapters": before_manifest}),
                 reservations_sha256=canonical_hash(reserved), prior_intents_sha256=canonical_hash(lineage))
    proof["authorization_sha256"] = canonical_hash(proof)
    from production_standalone_lineage import bind_authorization
    proof = bind_authorization(module, plan, proof)
    slot = canonical_hash({k: request[k] for k in ("novel_id", "audit_label", "through_chapter")})
    receipt = plain(module.RETROACTIVE_DIR / "transactions" / f"{slot}.authorization.json")
    require(not receipt.exists() or read_object(receipt) == proof,
            "IMMUTABLE_PLAN_POLICY_CONFLICT: WRITER_AUTHORIZATION_DRIFT")
    require(writer_inventory(module, request["committed_chapters"], request["through_chapter"]) == inventory,
            "WRITER_INVENTORY_CHANGED_DURING_PROOF")
    return proof


def _unowned_source_script_count(source, policy):
    # Literal forms only. Unknown script is uncertainty, never referent evidence.
    from noveltranslator_core import _claim_longest_non_overlapping_occurrences
    claims = _claim_longest_non_overlapping_occurrences(
        source, [(key, item["source_forms"]) for key, item in policy.items()])
    covered = set()
    for anchors in claims.values():
        for anchor in anchors:
            covered.update(range(anchor["start"], anchor["end"]))
    return sum(index not in covered and (
        "CJK UNIFIED IDEOGRAPH" in unicodedata.name(char, "") or
        "CJK COMPATIBILITY IDEOGRAPH" in unicodedata.name(char, "") or
        "HANGUL" in unicodedata.name(char, "")) for index, char in enumerate(source))


def historical_impact_preflight(module, changes, *, through_chapter,
                               audit_start=None, audit_end=None, expected_intent_sha256=None):
    """Bounded read-only discovery over 1..through_chapter, with no cache.

    ``changes`` uses the existing retroactive rule shape (including ``chinese``
    for Korean source identities). Supply the independently selected immutable
    periodic intent hash and window to reprove accepted provenance. Unwitnessed
    proposals remain deferred. A missing/drifted *supplied* witness blocks.

    Caller supplies the existing initialized local module under the cooperative
    novel writer exclusion; this API does not load a provider, take a writing
    lock, persist a report, or expand transaction authority. Eligibility means
    only that CURRENT plan_chapter policy can prove the current bytes and
    verified historical reservations do not veto it. Unclaimed source script
    prevents a literal-inventory absence finding; it never proves a referent.
    """
    from noveltranslator_core import _claim_longest_non_overlapping_occurrences
    from retroactive_span_planning import POLICY_VERSION
    from production_periodic_audit_finalization import state_paths

    require(type(through_chapter) is int and 1 <= through_chapter <= MAX_DISCOVERY_CHAPTERS,
            "DISCOVERY_BOUNDARY_LIMIT")
    require(isinstance(changes, list) and 0 < len(changes) <= MAX_DISCOVERY_CHANGES,
            "DISCOVERY_CHANGE_LIMIT")
    require(all(isinstance(change, dict) and set(change) <= {
        "chinese", "old_english", "new_english", "category", "reason", "change_kind"
    } for change in changes), "INVALID_DISCOVERY_CHANGE_FIELDS")
    require(through_chapter * len(changes) <= MAX_DISCOVERY_ROWS, "DISCOVERY_ROW_LIMIT")
    rules = normalized_rules(changes)
    require(len(rules) == len(changes), "DUPLICATE_DISCOVERY_CHANGE")
    completed = module.progress["completed"]
    last = module.progress["last_completed"]
    require(type(last) is int and through_chapter <= last <= MAX_DISCOVERY_CHAPTERS
            and all(type(n) is int for n in completed)
            and completed == list(range(1, last + 1)), "NONSEQUENTIAL_COMMITTED_INVENTORY")
    numbers = list(range(1, through_chapter + 1))
    require(set(completed) <= module.chapter_map.keys(), "MISSING_SOURCE")
    inventory = source_inventory({n: module.chapter_map[n] for n in completed})
    require({p.relative_to(module.OUTPUT_DIR).as_posix() for p in plain(module.OUTPUT_DIR).rglob("*.md")} ==
            {f"{n:04d}.md" for n in completed}, "COMMITTED_FILE_INVENTORY_DRIFT")

    # Bind all evidence read by the owners and recheck after computation. No
    # directory is created, no stale cache or report is trusted, no prose leaks.
    def snapshot():
        paths = set(state_paths(module).values()) | {module.CONTINUITY_FILE}
        for folder, pattern in ((module.OUTPUT_DIR, "*.md"), (module.AUDIT_DIR, "*.json"),
                                (module.RETROACTIVE_DIR / "transactions", "*.json"),
                                (module.BACKUP_DIR, "retroactive_*/chapters_before/*.md"),
                                (module.BACKUP_DIR, "checkpoint_*/*")):
            paths.update(p for p in plain(folder).glob(pattern) if p.is_file())
        require(len(paths) <= MAX_DISCOVERY_ROWS, "DISCOVERY_EVIDENCE_FILE_LIMIT")
        return {str(p): digest(plain(p).read_bytes()) for p in paths}

    output_root = plain(module.OUTPUT_DIR).resolve().parent
    require(plain(module.AUDIT_DIR).resolve().parent == output_root
            and plain(module.RETROACTIVE_DIR).resolve().parent == output_root
            and plain(module.BACKUP_DIR).resolve().parent == output_root.parent
            and all(plain(p).resolve().is_relative_to(output_root.parent)
                    for p in state_paths(module).values()), "FOREIGN_DISCOVERY_ROOT")
    witness = snapshot()
    for key, name in (("glossary", "glossary"), ("progress", "progress"),
                      ("issues", "issues_data"), ("history", "audit_history")):
        require(read_object(state_paths(module)[key]) == getattr(module, name), "DISCOVERY_STATE_DRIFT")
    supplied = expected_intent_sha256 is not None
    require(supplied or (audit_start is None and audit_end is None), "INCOMPLETE_DISCOVERY_PROVENANCE")
    intent, proof = None, None
    if supplied:
        require(type(audit_start) is int and type(audit_end) is int
                and 1 <= audit_start <= audit_end and through_chapter <= audit_end
                and isinstance(expected_intent_sha256, str) and len(expected_intent_sha256) == 64
                and all(c in "0123456789abcdef" for c in expected_intent_sha256),
                "INVALID_DISCOVERY_PROVENANCE")
        intent, proof = _discovery_intent(module, audit_start, audit_end, expected_intent_sha256)
    baseline = intent["baseline"]["glossary"] if intent else module.glossary
    staged = intent["post"]["glossary"] if intent else module.glossary
    policy = ownership_policy(baseline, staged)
    reservations, historical_intents = _historical_reservations(module, intent) if intent else ({}, [])
    reserved_policy = copy.deepcopy(policy)
    _reserve_policy(reserved_policy, reservations)
    categories = sorted(module.ACTIVE_PROFILE.retroactive_safe_categories)
    accepted = normalized_rules(intent["record"]["changes"]["retroactive_replacements"]) if intent else []
    provenance = []
    for rule in rules:
        reason = "MISSING_ACCEPTED_CHANGE_PROVENANCE"
        original = next(item for item in changes if normalized_rules([item])[0] == rule)
        kind = original.get("change_kind", "canonical")
        require(kind in {"canonical", "alias_merge", "alias_split", "contextual"}, "INVALID_DISCOVERY_CHANGE_KIND")
        if intent:
            before = baseline["entries"].get(rule["chinese"], {})
            after = staged["entries"].get(rule["chinese"], {})
            if rule in accepted:
                reason = ""
            elif (before != after and rule["old_english"] == before.get("english", "")
                  and rule["new_english"] == after.get("english", "")
                  and rule["category"] == after.get("category", before.get("category", ""))
                  and after.get("status") == "contextual"):
                reason = ""  # Accepted contextualization still never permits a global edit.
            else:
                reason = "CHANGE_NOT_IN_VERIFIED_AUDIT"
        if kind == "alias_split":
            reason = "ALIAS_SPLIT_PROVENANCE_UNAVAILABLE"
        provenance.append(reason)

    rows = []
    for n in numbers:
        raw = plain(module.OUTPUT_DIR / f"{n:04d}.md").read_bytes()
        require(len(raw) <= 16 * 1024 * 1024, "DISCOVERY_CHAPTER_BYTE_LIMIT")
        source, text = module.chapter_map[n]["text"], raw.decode("utf-8")
        claims = _claim_longest_non_overlapping_occurrences(
            source, [(key, item["source_forms"]) for key, item in policy.items()])
        _, decisions = plan_chapter(source, raw, rules, policy, categories)
        _, reserved_decisions = plan_chapter(source, raw, rules, reserved_policy, categories)
        unowned_script = _unowned_source_script_count(source, reserved_policy)
        findings = []
        for rule, decision, reserved, missing in zip(rules, decisions, reserved_decisions, provenance):
            anchors = claims.get(rule["chinese"], [])
            old_count = decision["target_candidate_count"]
            new_count = len(target_spans(text, rule["new_english"])) if rule["new_english"] else 0
            entry = policy.get(rule["chinese"], {})
            potential = bool(anchors or rule["chinese"] in source or old_count or new_count)
            reasons = []
            if missing:
                reasons.append(missing)
            if entry.get("contextual"):
                reasons.append("contextual_identity_requires_manual_review")
            if entry.get("alias_of"):
                reasons.append("alias_identity_requires_manual_review")
            if len(anchors) > 1:
                reasons.append("repeated_source_occurrences_require_independent_pairing")
            if old_count > 1 or new_count > 1:
                reasons.append("repeated_target_occurrences_require_independent_pairing")
            outcome = "deferred"
            if not missing and decision["status"] == "planned":
                if reserved["status"] == "planned":
                    outcome, reasons = "proven_eligible", ["OWNED_SINGLE_SPAN"]
                else:
                    reasons.extend(["HISTORICAL_OWNERSHIP_RESERVATION", reserved["skip_reason"]])
            elif not missing and not potential:
                if unowned_script:
                    outcome, reasons = "coverage_unproven", ["SOURCE_IDENTITY_COVERAGE_INCOMPLETE"]
                else:
                    outcome, reasons = "unaffected", ["NO_SOURCE_OR_BOUNDED_TARGET_CANDIDATE"]
            elif not missing and not old_count and new_count:
                reverse = {**rule, "old_english": rule["new_english"], "new_english": rule["old_english"]}
                owned, reverse_decisions = plan_chapter(source, raw, [reverse], policy, categories)
                reserved_owned, reserved_reverse = plan_chapter(source, raw, [reverse], reserved_policy, categories)
                if owned and reserved_owned:
                    outcome, reasons = "already_current", ["OWNED_APPROVED_RENDERING"]
                elif owned:
                    reasons.extend(["HISTORICAL_OWNERSHIP_RESERVATION", reserved_reverse[0]["skip_reason"]])
                else:
                    reasons.append(reverse_decisions[0]["skip_reason"])
            else:
                reasons.append(decision.get("skip_reason", "OWNERSHIP_REQUIRES_REVIEW"))
            findings.append({"term_id": canonical_hash(rule["chinese"]), "change_id": canonical_hash(rule),
                "potential_impact": potential, "outcome": outcome,
                "reason_codes": sorted(set(reasons)), "source_owned_count": len(anchors),
                "source_claims_sha256": canonical_hash(anchors),
                "english_candidate_count": old_count, "approved_candidate_count": new_count})
        rows.append({"chapter": n, "unit_sha256": canonical_hash(module.chapter_map[n]),
                     "source_sha256": digest(source.encode("utf-8")), "english_sha256": digest(raw),
                     "source_identity_coverage": "incomplete" if unowned_script else "literal_inventory_covered",
                     "unowned_source_script_count": unowned_script, "decisions": findings})
    require(snapshot() == witness and source_inventory({n: module.chapter_map[n] for n in completed}) == inventory,
            "DISCOVERY_INPUT_DRIFT")
    report = {"revision": PREFLIGHT_REVISION, "policy_version": POLICY_VERSION,
        "phase": "DISCOVERY_NOT_COMMIT_AUTHORITY", "scope": "whole_committed_history",
        "novel_id_sha256": canonical_hash(module.ACTIVE_NOVEL_CONFIG.id if module.ACTIVE_NOVEL_CONFIG else "legacy_single_novel"),
        "audit_boundary": through_chapter, "examined_chapters": len(rows),
        "source_inventory_sha256": canonical_hash(inventory), "evidence_sha256": canonical_hash(witness),
        "baseline_glossary_sha256": canonical_hash(baseline), "staged_glossary_sha256": canonical_hash(staged),
        "current_glossary_sha256": canonical_hash(module.glossary), "ownership_policy_sha256": canonical_hash(policy),
        "historical_reservation_policy_sha256": canonical_hash(reserved_policy),
        "historical_intents_sha256": canonical_hash(historical_intents),
        "source_identity_coverage_scope": "LITERAL_INVENTORY_ONLY_NO_SEMANTIC_COMPLETENESS",
        "safe_categories_sha256": canonical_hash(categories), "selected_changes_sha256": canonical_hash(
            sorted(changes, key=canonical_hash)),
        "intent_sha256": intent["intent_sha256"] if intent else None,
        "transaction_id": proof["plan"]["transaction_id"] if proof else None,
        "chapters": rows, "corrected_chapters": 0, "provider_calls": 0}
    for outcome in ("proven_eligible", "deferred", "already_current", "unaffected", "coverage_unproven"):
        report[outcome + "_chapters"] = sum(any(d["outcome"] == outcome for d in row["decisions"]) for row in rows)
    report["potential_impact_chapters"] = sum(any(d["potential_impact"] for d in row["decisions"]) for row in rows)
    report["report_sha256"] = canonical_hash(report)
    return report


def impact_report(plan, chapter_map, staged_glossary, chapter_bytes=None):
    """Read-only discovery from the exact plan the transaction must reprove.

    A potential occurrence is not edit authority. Cardinality/alias/contextual
    ambiguity stays deferred under the unchanged owned-single-span-v1 policy.
    """
    tx.validate_plan(plan, plan["request"], chapter_map)
    # Later source acquisition must not change an immutable earlier audit.
    # validate_plan proves every committed source hash; retain full unit identity
    # in the inventory so in-scope provenance drift still changes the proof.
    audited_sources = {n: chapter_map[n] for n in plan["request"]["committed_chapters"]}
    rules = plan["request"]["rules"]
    rows = []
    for item in plan["chapters"]:
        decisions = []
        for rule, decision in zip(rules, item["decisions"]):
            if decision["status"] == "planned":
                outcome = "proven_eligible"
            elif decision["target_candidate_count"]:
                outcome = "deferred"
            elif decision["source_owned_count"]:
                outcome = "needs_further_review"
                if chapter_bytes is not None:
                    raw = chapter_bytes[item["chapter"]]
                    require(digest(raw) == item["before_sha256"], "DISCOVERY_BEFORE_DRIFT")
                    reverse = {**rule, "old_english": rule["new_english"], "new_english": rule["old_english"]}
                    owned, _ = plan_chapter(chapter_map[item["chapter"]]["text"], raw, [reverse],
                                            plan["request"]["ownership_policy"], plan["request"]["safe_categories"])
                    if owned:
                        outcome = "already_current"
            else:
                outcome = "unchanged"
            decisions.append({"term_id": canonical_hash(rule["chinese"]),
                              "rule_id": canonical_hash(rule), "outcome": outcome,
                              "reason_code": "OWNED_APPROVED_RENDERING" if outcome == "already_current" else decision.get("skip_reason", "OWNED_SINGLE_SPAN"),
                              "source_owned_count": decision["source_owned_count"],
                              "english_candidate_count": decision["target_candidate_count"]})
        rows.append({"chapter": item["chapter"], "source_sha256": item["source_sha256"],
                     "before_sha256": item["before_sha256"], "after_sha256": item["after_sha256"],
                     "decisions": decisions})
    counts = {kind + "_chapters": sum(any(d["outcome"] == kind for d in row["decisions"])
                                     for row in rows)
              for kind in ("proven_eligible", "deferred", "needs_further_review", "unchanged", "already_current")}
    if not rules:
        counts["unchanged_chapters"] = len(rows)
    return {"revision": REVISION, "scope": "whole_committed_history",
            "audit_boundary": plan["request"]["through_chapter"],
            "transaction_id": plan["transaction_id"],
            "source_inventory_sha256": canonical_hash(source_inventory(audited_sources)),
            "glossary_revision_sha256": canonical_hash(staged_glossary),
            "examined_chapters": len(rows), **counts, "chapters": rows,
            "phase": "DISCOVERY_NOT_COMMIT_AUTHORITY", "provider_calls": 0}


def registered_publication_generations(root):
    """Inventory claims/references, not unrelated files or directories.

    An immutable input-hash intent registers even an interrupted generation.
    Published EPUBs additionally require their matching provenance and archive
    proof. Historical snapshots and the pointer retain missing-root obligations.
    No readiness claim, cleanup, registration or evidence repair occurs here.
    """
    from publication_regeneration import (
        GENERATIONS, POINTER, APPROVALS, REVISION as generation_revision, _sha, _plain_tree,
        PublicationWorkspace, _verify_epub, _validate_previous_pointer,
    )
    from novel_project import NovelConfig, NovelProject, repository_root
    root = plain(root)
    directory = plain(root / GENERATIONS)
    _plain_tree(directory)  # Links are refused even in otherwise unrelated residue.
    references = set()
    pointer = plain(root / POINTER)
    if pointer.exists():
        references.add(_sha(read_object(pointer).get("generation")))
    # An old captured target must not disappear merely because registration or
    # the current pointer was removed. Readers never repair those obligations.
    evidence = list(plain(root / "output/audits").glob("*.finalization.json"))
    evidence += list(plain(root / "output/publication_freshness").glob("*.json"))
    standalone = plain(root / "output/retroactive_fixes/standalone_lineage")
    evidence += list(standalone.glob("*.prepared.json"))
    for path in evidence:
        try:
            value = read_object(path)
        except ProductionIntegrityError as exc:
            if path.parent != standalone:
                raise
            # Snapshot discovery must preserve the historical ownership contract.
            raise ProductionIntegrityError("AUT2: HISTORICAL_OWNERSHIP_UNAVAILABLE_UNPROVEN") from exc
        captured = value.get("publication_inputs_before", {})
        require(isinstance(captured, dict), "INVALID_DOWNSTREAM_INVENTORY")
        for target in captured:
            relative = Path(target)
            parts = relative.parts
            require(not relative.is_absolute() and not relative.drive and ".." not in parts
                    and ":" not in target, "INVALID_GENERATION_REFERENCE")
            if tuple(part.casefold() for part in parts[:2]) == ("output", "publication_generations"):
                require(len(parts) >= 4 and parts[3] == "final" and ".." not in parts,
                        "INVALID_GENERATION_REFERENCE")
                references.add(_sha(parts[2]))
    candidates = set(references)
    reserved = {"intent.json", "source_bound_provenance.json", "publication_current.json"}
    for entry in sorted(directory.iterdir()) if directory.is_dir() else ():
        if entry.is_dir():
            claims = (entry / "final").exists() or any(p.name.casefold() in reserved for p in entry.rglob("*"))
            if claims:
                candidates.add(_sha(entry.name))
        else:
            require(entry.name.casefold() not in reserved and entry.name not in references,
                    "UNREGISTERED_GENERATION_CLAIM")
    if not candidates and not pointer.exists():
        return []
    config = NovelConfig.from_dict(read_object(root / "novel.json"))
    project = NovelProject(repository_root(), root, config)
    result = []
    for identity in sorted(candidates):
        generation = plain(directory / identity)
        require(generation.is_dir(), "MISSING_REFERENCED_GENERATION")
        original = read_object(generation / "intent.json")
        require(set(original) == {"revision", "source", "approvals", "authorization_sha256", "runtime"}
                and original["revision"] == generation_revision and canonical_hash(original) == identity
                and original["source"]["novel_id"] == config.id
                and original["source"]["config"]["id"] == config.id,
                "INVALID_OR_FOREIGN_GENERATION_REGISTRATION")
        require(isinstance(original["approvals"], dict) and isinstance(original["runtime"], dict)
                and bool(original["runtime"]) and original["source"].get("revision") == generation_revision
                and set(APPROVALS) <= set(original["approvals"]) <= set(APPROVALS) | {"normalization_inventory"}
                and set(original["source"]) == {"revision", "novel_id", "config", "source_inventory_sha256",
                    "chapters", "evidence", "profile", "whole_history_sha256", "transactions", "novel_policy"},
                "INVALID_GENERATION_REGISTRATION_SCHEMA")
        _sha(original["authorization_sha256"])
        for hashes in (original["approvals"], original["runtime"]):
            for value in hashes.values():
                _sha(value)
        # Partial stage JSON remains unverified, but malformed evidence cannot
        # hide behind an otherwise valid registration or unrelated residue.
        for path in generation.rglob("*.json"):
            read_object(path)
        proof = plain(generation / "final/reports/source_bound_provenance.json")
        if proof.exists():
            require(read_object(proof) == original, "GENERATION_SOURCE_PROVENANCE_STALE")
        final = generation / "final"
        if any(plain(final / "epub").glob("*.epub")):
            require(proof.is_file(), "MISSING_PUBLISHED_GENERATION_PROOF")
            _verify_epub(PublicationWorkspace(project, final))
        result.append(generation)
    if pointer.exists():
        _validate_previous_pointer(project)  # Exact novel/root/EPUB hash; no CURRENT promotion.
    return result


def publication_input_snapshot(root):
    """Hashes only: inventory known local downstream stages before invalidation."""
    root = plain(root)
    folders = ("output/qa", "output/epub", "final/chapters", "final/normalized_chapters",
               "final/titled_chapters", "final/publication_chapters", "final/editorial_chapters",
               "final/reports", "final/epub")
    result = {}
    for name in folders:
        directory = plain(root / name)
        result[name] = {p.name: digest(plain(p).read_bytes()) for p in sorted(directory.glob("*"))
                        if p.is_file() and p.suffix in {".md", ".json", ".epub"}}
    # New intents capture actual state/QA and generation targets before edits.
    # Existing immutable snapshots retain their original replay contract.
    result["data"] = {name: digest(plain(root / "data" / name).read_bytes())
                      for name in ("continuity.json", "issues.json")
                      if plain(root / "data" / name).is_file()}
    for generation in registered_publication_generations(root):
        for folder in ("chapters", "normalized_chapters", "titled_chapters", "publication_chapters",
                       "editorial_chapters", "reports", "epub"):
            directory = plain(generation / "final" / folder)
            result[directory.relative_to(root).as_posix()] = {
                p.name: digest(plain(p).read_bytes()) for p in sorted(directory.glob("*")) if p.is_file()}
        for name in ("manifest.json", "glossary.json"):
            path = plain(generation / "final" / name)
            if path.is_file():
                result[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    glossary = plain(root / "final/glossary.json")
    result["final/glossary.json"] = digest(glossary.read_bytes()) if glossary.is_file() else None
    manifest = plain(root / "final/manifest.json")
    result["final/manifest.json"] = digest(manifest.read_bytes()) if manifest.is_file() else None
    return result


def invalidation_path(module, intent):
    try:
        transaction = intent["transaction_plan"]["transaction_id"]
        require(isinstance(transaction, str) and len(transaction) == 64
                and all(c in "0123456789abcdef" for c in transaction), "INVALID_TRANSACTION_ID")
        root = plain(module.OUTPUT_DIR).resolve().parent
        require(plain(module.RETROACTIVE_DIR).resolve().parent == root
                and plain(module.AUDIT_DIR).resolve().parent == root, "FOREIGN_INVALIDATION_ROOT")
        directory = plain(root / "publication_freshness")
        path = plain(directory / (transaction + ".json"))
        require(path.resolve().parent == directory.resolve()
                and path.resolve().is_relative_to(root), "INVALIDATION_PATH_ESCAPE")
        return path
    except (KeyError, TypeError) as exc:
        raise ProductionIntegrityError("AUT2: INVALID_INVALIDATION_IDENTITY") from exc


def validate_invalidation_identity(module, intent, start, end):
    """Read-only bindings; a recomputed checksum alone grants no authority."""
    from production_periodic_audit_finalization import intent_path
    try:
        path = invalidation_path(module, intent)  # Check before any ID-derived lookup.
        plan = intent["transaction_plan"]
        request = plan["request"]
        novel = module.ACTIVE_NOVEL_CONFIG.id if module.ACTIVE_NOVEL_CONFIG else "legacy_single_novel"
        require(type(start) is int and type(end) is int and 1 <= start <= end
                and type(intent["start"]) is int and type(intent["end"]) is int
                and (intent["start"], intent["end"]) == (start, end), "INVALID_AUDIT_BOUNDARY")
        require(request["novel_id"] == novel and request["audit_label"] == f"{start:04d}_{end:04d}"
                and type(request["through_chapter"]) is int and request["through_chapter"] == end,
                "INVALIDATION_OPERATION_MISMATCH")
        require(type(intent["record"]["start_chapter"]) is int and type(intent["record"]["end_chapter"]) is int
                and (intent["record"]["start_chapter"], intent["record"]["end_chapter"]) == (start, end),
                "INVALIDATION_RECORD_MISMATCH")
        report = intent["record"].get("historical_reconciliation")
        if report is not None:
            require(report["transaction_id"] == plan["transaction_id"]
                    and type(report["audit_boundary"]) is int and report["audit_boundary"] == end,
                    "INVALIDATION_REPORT_MISMATCH")
        tx.validate_plan(plan, request, module.chapter_map)
        require(audit_intent(module, intent_path(module, start, end)) == intent, "DURABLE_INTENT_MISMATCH")
        return path
    except (KeyError, TypeError) as exc:
        raise ProductionIntegrityError("AUT2: INVALID_INVALIDATION_IDENTITY") from exc


def invalidation_record(intent):
    plan = intent["transaction_plan"]
    return {"revision": REVISION, "novel_id": plan["request"]["novel_id"],
            "audit_boundary": intent["end"], "audit_intent_sha256": intent["intent_sha256"],
            "transaction_id": plan["transaction_id"],
            "glossary_revision_sha256": canonical_hash(intent["post"]["glossary"]),
            "chapters": [{k: item[k] for k in ("chapter", "before_sha256", "after_sha256")}
                         for item in plan["chapters"]],
            "publication_inputs_before": intent.get("publication_inputs_before", {}),
            "impacted_chapters": [item["chapter"] for item in plan["chapters"] if item["edits"]],
            "dependent_stages": ["checkpoint_epub", "finalization", "normalization", "titles",
                                 "publication_chapters", "editorial", "publication_epub"],
            "status": "STALE" if any(i["edits"] for i in plan["chapters"]) else "NOT_VERIFIED",
            "reason_code": "DOWNSTREAM_PROVENANCE_REQUIRES_REVALIDATION",
            "next_safe_action": "Verify revised production inventory and approved downstream plans; preserve existing outputs.",
            "automatic_rebuild": "DEFERRED_REQUIRES_DOWNSTREAM_POLICY", "provider_calls": 0}


def ensure_invalidation(module, intent):
    """Durable stale intent BEFORE historical edits, audit/progress markers last.

    A missing derived manifest can be reconstructed from immutable intent.
    Corrupt or contradictory evidence is never overwritten.
    Caller owns the existing novel writer lock and must finish all read-only
    finalization proofs before invoking this writer.
    """
    path = validate_invalidation_identity(module, intent, intent["start"], intent["end"])
    if "historical_reconciliation" not in intent["record"]:
        return  # Legacy intents retain their original recovery contract.
    expected = invalidation_record(intent)
    if path.exists():
        require(read_object(path) == expected, "FRESHNESS_MANIFEST_DRIFT")
        return
    # The finalizer has already reproved publication prefixes, paid evidence,
    # deterministic authorization and the transaction. Only now may this
    # conservative receipt become durable, before any transaction mutation.
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, expected)


def publication_revisions(project):
    """Reprove immutable invalidation receipts; receipts grant no write authority."""
    from types import SimpleNamespace
    module = SimpleNamespace(AUDIT_DIR=project.audit_dir)
    revisions = []
    for path in sorted(plain(project.audit_dir).glob("*.finalization.json")):
        intent = audit_intent(module, path)
        require(intent["transaction_plan"]["request"]["novel_id"] == ("legacy_single_novel" if project.is_legacy else project.config.id),
                "FOREIGN_NOVEL_INTENT")
        if "historical_reconciliation" not in intent["record"]:
            continue
        expected = invalidation_record(intent)
        receipt = plain(project.root / "output/publication_freshness" / (expected["transaction_id"] + ".json"))
        require(read_object(receipt) == expected, "FRESHNESS_MANIFEST_DRIFT")
        revisions.append(expected)
    from production_standalone_lineage import publication_revisions as standalone_revisions
    standalone = standalone_revisions(project)
    known = {r["transaction_id"] + ".json" for r in revisions}
    directory = plain(project.root / "output/publication_freshness")
    require({p.name for p in directory.glob("*.json")} == known, "ORPHAN_FRESHNESS_MANIFEST")
    return revisions + standalone


def publication_freshness(project):
    """Recompute freshness; a source-bound generation must reprove its inputs."""
    revisions = publication_revisions(project)
    artifacts = []
    for directory in (project.epub_dir, project.root / "final/epub"):
        for path in sorted(plain(directory).glob("*.epub")):
            artifacts.append({"path": path.relative_to(project.root).as_posix(),
                              "sha256": digest(plain(path).read_bytes()), "status": "NOT_VERIFIED"})
    actual_paths = {item["path"] for item in artifacts}
    for revision in revisions:
        for folder in ("output/epub", "final/epub"):
            for name, sha in revision["publication_inputs_before"].get(folder, {}).items():
                relative = folder + "/" + name
                if relative not in actual_paths:
                    artifacts.append({"path": relative, "before_sha256": sha, "status": "MISSING"})
                    actual_paths.add(relative)
    dependencies = []
    for revision in revisions:
        for folder, files in revision["publication_inputs_before"].items():
            parts = Path(folder).parts
            generated = (len(parts) >= 4 and parts[:2] == ("output", "publication_generations")
                         and len(parts[2]) == 64 and all(c in "0123456789abcdef" for c in parts[2])
                         and parts[3] == "final")
            if folder in {"final/manifest.json", "final/glossary.json"} or (
                    generated and len(parts) == 5 and parts[4] in {"manifest.json", "glossary.json"}):
                files = {parts[-1]: files} if files else {}
                folder = Path(folder).parent.as_posix()
                parts = Path(folder).parts
            allowed_generation = generated and (len(parts) == 4 or len(parts) == 5 and parts[4] in {
                "chapters", "normalized_chapters", "titled_chapters", "publication_chapters",
                "editorial_chapters", "reports", "epub"})
            require((allowed_generation or folder in {"final", "data", "output/qa", "output/epub", "final/chapters", "final/normalized_chapters",
                               "final/titled_chapters", "final/publication_chapters", "final/editorial_chapters",
                               "final/reports", "final/epub"}) and isinstance(files, dict), "INVALID_DOWNSTREAM_INVENTORY")
            for name, before_sha in files.items():
                require(isinstance(name, str) and Path(name).name == name and "/" not in name and "\\" not in name,
                        "INVALID_DOWNSTREAM_NAME")
                path = plain(project.root / folder / name)
                after_sha = digest(path.read_bytes()) if path.is_file() else None
                status = ("MISSING" if after_sha is None else "STALE" if revision["impacted_chapters"]
                          and after_sha == before_sha else "NOT_VERIFIED")
                dependencies.append({"path": folder + "/" + name, "before_sha256": before_sha,
                                     "sha256": after_sha, "status": status, "transaction_id": revision["transaction_id"]})
    from publication_regeneration import current_generation
    generation = current_generation(project)
    if "artifact" in generation:
        artifacts.append(generation["artifact"])
    return {"revision": REVISION, "status": generation["status"] if generation["status"] != "NOT_VERIFIED" else ("STALE" if revisions else "NOT_VERIFIED"),
            "transaction_ids": [r["transaction_id"] for r in revisions],
            "impacted_chapters": sorted({n for r in revisions for n in r["impacted_chapters"]}),
            "artifacts": artifacts, "rebuilt_epubs": int(generation["status"] == "CURRENT"),
            "generation": generation, "downstream_outputs": dependencies,
            "reason_code": "SOURCE_BOUND_GENERATION_VERIFIED" if generation["status"] == "CURRENT" else ("DOWNSTREAM_PROVENANCE_REQUIRES_REVALIDATION" if revisions else "NO_CURRENT_OUTPUT_PROOF"),
            "provider_calls": 0}


def require_publication_freshness(project):
    """No successful publication claim from an old self-consistent editorial copy."""
    revisions = publication_revisions(project)
    final_dir = getattr(project, "publication_final_dir", project.root / "final")
    proof = plain(final_dir / "reports/source_bound_provenance.json")
    if proof.exists():
        from publication_regeneration import verify_generation_corpus
        verify_generation_corpus(project, final_dir)
    else:
        require(not revisions, "PUBLICATION_STALE_REVALIDATE_DOWNSTREAM_PLANS")
        require(False, "PUBLICATION_MISSING_SOURCE_PROOF_REVALIDATE_DOWNSTREAM")
    from publication_readiness import require_publication_ready
    require_publication_ready(project)


@proof_traversal
def final_consistency_report(module, *, _inputs=None):
    """Whole-book local consistency pass, distinct from acquisition's tail audit.

    Reprove original audit transactions and their byte chain, then examine every
    glossary identity in every committed source unit. Semantic re-review and
    expanded repeated-span eligibility are intentionally not claimed here.
    """
    from production_periodic_audit_finalization import (
        transaction_proof, prepare_finalization, file_sha, state_paths,
    )
    file_sha, read_object = _inputs.sha, _inputs.read_object
    from types import SimpleNamespace
    numbers = module.progress["completed"]
    boundary = module.progress["last_completed"]
    require(numbers == list(range(1, boundary + 1)), "NONSEQUENTIAL_COMMITTED_INVENTORY")
    require(module.progress["last_audited_chapter"] == boundary, "UNFINISHED_WINDOW_AUDIT")
    inventory = source_inventory(module.chapter_map)
    require(set(numbers) <= set(module.chapter_map), "MISSING_SOURCE")
    files = {p.name for p in plain(module.OUTPUT_DIR).glob("*.md")}
    require(files == {f"{n:04d}.md" for n in numbers}, "COMMITTED_FILE_INVENTORY_DRIFT")
    current = {n: _inputs.read(module.OUTPUT_DIR / f"{n:04d}.md") for n in numbers}
    # Later BEFORE backups witness unchanged bytes in earlier audit plans.
    blobs = {digest(raw): raw for raw in current.values()}
    intents = []
    for path in sorted(plain(module.AUDIT_DIR).glob("*.finalization.json")):
        intent = audit_intent(module, path, _inputs=_inputs)
        require(intent["end"] <= boundary, "UNFINISHED_AUDIT_INTENT")
        for item in intent["transaction_plan"]["chapters"]:
            if item["edits"]:
                backup = plain(module.BACKUP_DIR / f"retroactive_{intent['transaction_plan']['transaction_id']}" /
                               "chapters_before" / f"{item['chapter']:04d}.md")
                require(backup.is_file() and digest(_inputs.read(backup)) == item["before_sha256"],
                        "MISSING_OR_CORRUPT_BEFORE_BACKUP")
                blobs[item["before_sha256"]] = _inputs.read(backup)
        intents.append(intent)
    from production_standalone_lineage import witnesses, add_blobs, prove_gap, verified_events
    standalone = witnesses(module)
    add_blobs(module, standalone, blobs)
    reservations, lineage, consumed = {}, [], set()
    latest, history_rules, previous_end, evidence = {}, [], 0, []
    for intent in intents:
        if standalone:
            prove_gap(module, standalone, previous_end, intent["baseline"]["history"]["audits"],
                      blobs, latest, reservations, lineage, consumed)
        require(intent["start"] == previous_end + 1, "MISSING_OR_COMPETING_AUDIT_HISTORY")
        previous_end = intent["end"]
        detached = SimpleNamespace(**vars(module))
        for key, name in (("glossary", "glossary"), ("progress", "progress"),
                          ("issues", "issues_data"), ("history", "audit_history")):
            setattr(detached, name, copy.deepcopy(intent["baseline"][key]))
        require(file_sha(module.periodic_audit_checkpoint_path(intent["start"], intent["end"])) == intent["checkpoint_sha256"],
                "AUDIT_CHECKPOINT_DRIFT")
        if detached.progress.get("provider") == "command_code_provider" and intent["chunking"].get("candidate_count", 0):
            from goat_periodic_audit_finalize_plan import checkpoint_proof
            paid = checkpoint_proof(detached, intent["start"], intent["end"])
            require(paid["audit_result"] == intent["audit_result"] and paid["audit_stats"] == intent["audit_stats"],
                    "PAID_AUDIT_EVIDENCE_DRIFT")
        checkpoint = module.BACKUP_DIR / f"checkpoint_{intent['end']:04d}"
        for key, path in state_paths(module).items():
            require(read_object(checkpoint / path.name) == intent["post"][key], "AUDIT_CHECKPOINT_BACKUP_DRIFT")
        require(file_sha(checkpoint / module.CONTINUITY_FILE.name) == intent["continuity_sha256"],
                "AUDIT_CONTINUITY_BACKUP_DRIFT")
        plan = intent["transaction_plan"]
        after = {}
        for item in plan["chapters"]:
            raw = blobs.get(item["before_sha256"])
            require(raw is not None, "HISTORICAL_BEFORE_WITNESS_MISSING")
            require(item["chapter"] not in latest or latest[item["chapter"]] == item["before_sha256"],
                    "DISCONNECTED_HISTORICAL_BYTE_CHAIN")
            after[item["chapter"]] = tx.transform_bytes(raw, item["edits"])
            latest[item["chapter"]] = item["after_sha256"]
        proof = transaction_proof(detached, intent["start"], intent["end"], intent["baseline"]["glossary"],
                                  intent["post"]["glossary"], intent["record"]["changes"], chapter_bytes=after, _inputs=_inputs)
        require(proof["state"] and proof["state"]["state"] == "FINALIZED", "TRANSACTION_NOT_FINALIZED")
        prepared = prepare_finalization(detached, intent["start"], intent["end"], intent["audit_result"],
            intent["audit_stats"], intent["chunking"], timestamp=intent["record"]["timestamp_unix"], chapter_bytes=after, _inputs=_inputs)
        for value in (prepared["record"], prepared["post"]["history"]["audits"][-1]):
            value["retroactive"] = intent["record"]["retroactive"]
        require(all(prepared[k] == intent[k] for k in ("post", "record", "transaction_plan", "operation_id")),
                "AUDIT_AUTHORIZATION_DRIFT")
        require(read_object(module.AUDIT_DIR / f"audit_{intent['start']:04d}_{intent['end']:04d}.json") == intent["record"],
                "OUTER_AUDIT_RECORD_DRIFT")
        require(module.audit_history["audits"][:len(intent["post"]["history"]["audits"])] == intent["post"]["history"]["audits"],
                "OUTER_AUDIT_HISTORY_DRIFT")
        history_rules.extend(plan["request"]["rules"])
        if standalone:
            _reserve_policy(reservations, plan["request"]["ownership_policy"])
            lineage.append(intent["intent_sha256"])
        evidence.append({"transaction_id": plan["transaction_id"], "intent_sha256": intent["intent_sha256"]})
    require(intents and previous_end == boundary, "MISSING_FINAL_AUDIT_INTENT")
    final_glossary = intents[-1]["post"]["glossary"]
    if standalone:
        prove_gap(module, standalone, previous_end, module.audit_history["audits"],
                  blobs, latest, reservations, lineage, consumed)
        require(len(consumed) == len(standalone), "STANDALONE_CHRONOLOGY_UNPROVEN")
        events = verified_events(module)
        by_transaction = {w["plan"]["transaction_id"]: w for w in standalone}
        last = by_transaction.get(events[-1][0]["transaction_id"])
        if last:
            final_glossary = last["staged_glossary"]
        for value in standalone:
            history_rules.extend(value["plan"]["request"]["rules"])
            evidence.append({"transaction_id": value["plan"]["transaction_id"], "witness_sha256": value["witness_sha256"]})
    require(all(digest(current[n]) == sha for n, sha in latest.items()), "CURRENT_PROSE_OUTSIDE_AUDIT_CHAIN")
    for key, name in (("glossary", "glossary"), ("progress", "progress"), ("issues", "issues_data"), ("history", "audit_history")):
        require(getattr(module, name) == (final_glossary if key == "glossary" else intents[-1]["post"][key]),
                "FINAL_STATE_OUTSIDE_AUDIT_CHAIN")
    entries = module.glossary["entries"]
    rows = []
    from noveltranslator_core import _claim_longest_non_overlapping_occurrences
    policy = ownership_policy(module.glossary)
    for n, raw in current.items():
        source = module.chapter_map[n]["text"]
        text = raw.decode("utf-8")
        decisions = []
        # Claims use the exact same longest-source machinery as the span owner.
        claims = _claim_longest_non_overlapping_occurrences(source,
            [(key, item["source_forms"]) for key, item in policy.items()])
        for key, anchors in sorted(claims.items()):
            entry = entries[key]
            targets = strict_term_valid_targets(entry)
            old = {r["old_english"] for r in history_rules if r["chinese"] == key
                   and r["old_english"] not in targets}
            old_count = sum(len(target_spans(text, value)) for value in old)
            present = sum(len(target_spans(text, value)) for value in set(targets) if value)
            canonical = entry.get("english", "")
            owned = []
            if canonical and present and not old_count:
                probe = {"chinese": key, "old_english": canonical, "new_english": "Ownership Probe",
                         "category": entry.get("category", ""), "reason": "local ownership check only"}
                owned, _ = plan_chapter(source, raw, normalized_rules([probe]), policy,
                                        module.ACTIVE_PROFILE.retroactive_safe_categories)
            reason = ("HISTORICAL_RENDERING_REQUIRES_OWNERSHIP_REVIEW" if old_count else
                      "APPROVED_RENDERING_NOT_FOUND" if not present else
                      "APPROVED_RENDERING_OWNERSHIP_UNPROVEN" if not owned else
                      "OWNED_APPROVED_RENDERING_PRESENT")
            decisions.append({"term_id": canonical_hash(key), "source_owned_count": len(anchors),
                              "old_candidate_count": old_count, "approved_candidate_count": present,
                              "outcome": "deferred" if old_count or not owned else "already_current",
                              "reason_code": reason})
        for key in sorted((set(entries) | {r["chinese"] for r in history_rules}) - claims.keys()):
            prior = [r for r in history_rules if r["chinese"] == key]
            if key in source or any(target_spans(text, r["old_english"]) for r in prior):
                decisions.append({"term_id": canonical_hash(key), "outcome": "deferred",
                                  "reason_code": "REMOVED_IDENTITY_REQUIRES_REVIEW" if key not in entries else
                                                 "HISTORICAL_SOURCE_OWNERSHIP_UNPROVEN"})
        rows.append({"chapter": n, "english_sha256": digest(raw),
                     "source_sha256": digest(source.encode("utf-8")), "decisions": decisions})
    deferred = sum(any(d["outcome"] == "deferred" for d in row["decisions"]) for row in rows)
    return {"revision": REVISION, "scope": "final_whole_committed_history",
            "audit_boundary": boundary, "novel_id": (module.ACTIVE_NOVEL_CONFIG.id if module.ACTIVE_NOVEL_CONFIG else "legacy_single_novel"),
            "glossary_revision_sha256": canonical_hash(module.glossary),
            "source_inventory_sha256": canonical_hash(inventory),
            "examined_chapters": len(rows), "deferred_chapters": deferred,
            "corrected_chapters": 0,
            "historically_corrected_chapters": len({i["chapter"] for intent in intents
                                                     for i in intent["transaction_plan"]["chapters"] if i["edits"]}
                                                    | {i["chapter"] for w in standalone for i in w["plan"]["chapters"] if i["edits"]}),
            "already_current_chapters": sum(bool(row["decisions"]) and all(d["outcome"] == "already_current"
                                                for d in row["decisions"]) for row in rows),
            "unchanged_chapters": sum(not row["decisions"] for row in rows),
            "transactions": evidence, "chapters": rows,
            "status": "CHECKED_WITH_DEFERRALS" if deferred else "CHECKED",
            "semantic_rereview": "NOT_PERFORMED", "publication_status": "NOT_VERIFIED",
            "provider_calls": 0}


def save_final_consistency(module):
    """Immutable derived report; replay produces zero additional writes."""
    report = final_consistency_report(module)
    directory = plain(module.AUDIT_DIR / "whole_history")
    path = plain(directory / (canonical_hash(report) + ".json"))
    if path.exists():
        require(read_object(path) == report, "FINAL_REPORT_DRIFT")
    else:
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, report)
    return report
