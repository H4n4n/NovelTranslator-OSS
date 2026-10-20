"""Fresh S0, immutable plans, evidence-gated probes and sequential real lifecycle."""
import copy
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import uuid
from experimental_goat_policy import (ExperimentError, ModelPolicy, OPERATIONS, canonical,
    decimal, digest, external_path, local_path, positive_int, text_sha)
from experimental_goat_budget import ExperimentBudget
from experimental_goat_runtime import ExperimentalRuntime, accept_response, install_experimental_runtime
from noveltranslator_core import atomic_write_json
from production_paid_request_journal import load_matching_journal, inspect_paid_request_journals
from production_run_lock import production_run_lock

SCRIPT = "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
_ENGINE_CONTEXT_LOCK = threading.Lock()


def read_json(path):
    return json.loads(local_path(path).read_text(encoding="utf-8-sig"))


def file_inventory(root, paths):
    result = {}
    for relative in sorted(paths):
        path = local_path(Path(root) / relative)
        if Path(root).resolve() not in path.parents:
            raise ExperimentError("immutable input outside root")
        if not path.is_file():
            raise ExperimentError("missing immutable input")
        result[str(relative).replace("\\", "/")] = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    return result


def code_inventory(engine):
    paths = [p.relative_to(engine) for p in engine.glob("*.py")]
    paths += [p.relative_to(engine) for p in (engine / "profiles").rglob("*") if p.is_file()]
    return file_inventory(engine, paths)


def immutable_inputs(production, track):
    from novel_project import load_novel_config, validate_novel_id
    validate_novel_id(track["novel_id"])
    root = local_path(production / "novels" / track["novel_id"])
    config = load_novel_config(root / "novel.json")
    if (config.id != track["novel_id"] or config.profile != track["profile"]
        or config.source_language != track["source_language"] or track["last_chapter"] > config.final_chapter):
        raise ExperimentError("track immutable policy mismatch")
    positive_int(track["last_chapter"])
    paths = [Path("novel.json"), Path("data/chapters.json"), Path("translation_addendum.txt")]
    paths += [p.relative_to(root) for p in (root / "source").rglob("*") if p.is_file()]
    if len(paths) == 3:
        raise ExperimentError("track source missing")
    from chapter_library import index_chapter_library
    library = index_chapter_library(read_json(root / "data/chapters.json"), config.final_chapter, config.chapter_numbering)
    if any(ch not in library for ch in range(1, track["last_chapter"]+1)):
        raise ExperimentError("canonical source range incomplete")
    return root, file_inventory(root, paths)


def interleaved_slots(tracks):
    result = []
    for chapter in range(1, max(t["last_chapter"] for t in tracks)+1):
        for track in tracks:
            if chapter <= track["last_chapter"]:
                result.append({"track":track["novel_id"], "chapter":chapter})
    return result


def build_plan(*, production_root, output_root, config, engine_root=None, frozen_roots=()):
    """Read immutable inputs only. Planning never creates state or loads credentials."""
    production = local_path(production_root)
    engine = local_path(engine_root or Path(__file__).parent)
    model = ModelPolicy.from_dict(config["model"])
    if set(config) != {"model", "limits", "tracks"}:
        raise ExperimentError("unknown experiment configuration")
    tracks = copy.deepcopy(config["tracks"])
    if (not tracks or len({t["novel_id"] for t in tracks}) != len(tracks)
        or any(set(t) != {"novel_id", "profile", "source_language", "last_chapter"} for t in tracks)):
        raise ExperimentError("explicit unique tracks required")
    # All existing sibling NovelTranslator roots are protected discovery/frozen evidence.
    siblings = [p for p in production.parent.glob("NovelTranslator*") if p.is_dir()]
    protected = sorted({str(local_path(p)) for p in (production, engine, *siblings, *frozen_roots)})
    output = external_path(output_root, protected)
    if output.exists():
        raise ExperimentError("S0 destination already exists")
    inputs = {t["novel_id"]: immutable_inputs(production, t)[1] for t in tracks}
    plan = dict(revision="experimental-goat-plan-v1", production_root=str(production), engine_root=str(engine),
        output_root=str(output), protected_roots=protected, model=model.to_dict(), limits=copy.deepcopy(config["limits"]),
        tracks=tracks, slots=interleaved_slots(tracks), inputs=inputs, engine_inputs=code_inventory(engine),
        provider_calls=0, credential_reads=0, live_capability_status="catalog_declared")
    plan["plan_sha256"] = digest(plan)
    ExperimentBudget(output, plan_sha256=plan["plan_sha256"], limits=plan["limits"], model=model)
    return plan


def validate_plan(plan):
    if (plan.get("revision") != "experimental-goat-plan-v1" or plan.get("live_capability_status") != "catalog_declared"
        or plan["plan_sha256"] != digest({k:v for k,v in plan.items() if k != "plan_sha256"})
        or plan["slots"] != interleaved_slots(plan["tracks"])):
        raise ExperimentError("invalid frozen experiment plan")
    ModelPolicy.from_dict(plan["model"])
    external_path(plan["output_root"], plan["protected_roots"])
    if code_inventory(Path(plan["engine_root"])) != plan["engine_inputs"]:
        raise ExperimentError("engine/profile input drift")
    return plan


def create_s0(plan):
    """Create once, preserve staging on interruption; never copy mutable history."""
    validate_plan(plan)
    from create_novel import PROJECT_DIRECTORIES, initial_project_state
    root = external_path(plan["output_root"], plan["protected_roots"])
    stage = external_path(root.with_name(root.name + ".staging"), plan["protected_roots"])
    if root.exists() or stage.exists():
        raise ExperimentError("existing S0/staging must be preserved")
    # Verify every production input before any output write.
    for track in plan["tracks"]:
        _, actual = immutable_inputs(Path(plan["production_root"]), track)
        if actual != plan["inputs"][track["novel_id"]]:
            raise ExperimentError("production immutable input drift")
    stage.mkdir(parents=True)
    for track in plan["tracks"]:
        novel = track["novel_id"]
        destination = stage / "tracks" / novel
        source = Path(plan["production_root"]) / "novels" / novel
        for folder in (*PROJECT_DIRECTORIES, "data/provider_request_journal", "data/glossary_candidate_resolutions"):
            (destination / folder).mkdir(parents=True, exist_ok=True)
        for name, value in initial_project_state().items():
            if name != "chapters.json":
                if name == "progress.json":
                    from experimental_goat_accounting import accounting_identity
                    value.update(accounting_identity(ModelPolicy.from_dict(plan["model"]),plan["plan_sha256"]))
                    value["experiment_accepted_ce"] = "0"
                atomic_write_json(destination / "data" / name, value)
        for relative in plan["inputs"][novel]:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(local_path(source / relative), target)
        if file_inventory(destination, plan["inputs"][novel]) != plan["inputs"][novel]:
            raise ExperimentError("staged immutable copy drift")
    atomic_write_json(stage / "plan.json", plan)
    save_results(stage / "results.json", {"plan_sha256":plan["plan_sha256"], "quarantine":{},
        "slots":[{**slot, "status":"planned"} for slot in plan["slots"]]})
    # Recheck originals and code before publishing; no broad cleanup of interrupted stage.
    validate_plan(plan)
    for track in plan["tracks"]:
        if immutable_inputs(Path(plan["production_root"]), track)[1] != plan["inputs"][track["novel_id"]]:
            raise ExperimentError("immutable source changed during S0 copy")
    stage.rename(root)
    return root


def validate_root(root):
    root = local_path(root)
    plan = validate_plan(read_json(root / "plan.json"))
    if root != Path(plan["output_root"]):
        raise ExperimentError("experiment root identity drift")
    for track in plan["tracks"]:
        novel = track["novel_id"]
        if file_inventory(root / "tracks" / novel, plan["inputs"][novel]) != plan["inputs"][novel]:
            raise ExperimentError("experiment immutable input drift")
    return plan


def authorization_token(plan, action):
    if action not in {"probe", "run"}:
        raise ExperimentError("invalid authorization action")
    return digest({"plan_sha256":plan["plan_sha256"], "action":action,
                   "authorization":"explicit-operator-paid-experimental-goat-v1"})


def require_authorization(plan, action, token, authorize_paid):
    if authorize_paid is not True or token != authorization_token(plan, action):
        raise ExperimentError("explicit matching paid authorization required")


def runtime_for(plan, client):
    model = ModelPolicy.from_dict(plan["model"])
    root = Path(plan["output_root"])
    budget = ExperimentBudget(root, plan_sha256=plan["plan_sha256"], limits=plan["limits"], model=model)
    return ExperimentalRuntime(root=root, model=model, budget=budget, client=client, plan_sha256=plan["plan_sha256"])


def probe_efforts(model):
    return list(dict.fromkeys([model.effort("chapter_translation"),
                              *sorted(set(dict(model.reasoning_by_operation).values()))]))


def validate_capability_probe_evidence(plan, runtime, effort, index):
    """Mechanical acceptance plus exact durable settlement; text/finish are diagnostics."""
    kwargs = {"model":runtime.model.provider_model,
              "messages":[{"role":"user", "content":"Reply with OK."}], "max_tokens":64}
    identity, wire = runtime.identity("capability_probe", "capability_probes", index,
        "experiment_capability_probe", "Reply with OK.", kwargs, probe_effort=effort)
    path = Path(plan["output_root"]) / "probes" / f"{effort}.json"
    with runtime.budget.locked():
        journal = load_matching_journal(path, identity)
        if not journal or journal["status"] != "response_recorded":
            raise ExperimentError("live verification journal missing")
        reserve = runtime.budget.require(identity, path)
        env = accept_response(runtime.model, journal["response"], wire, prompt_upper=reserve["prompt_upper"])
        if (reserve["status"] != "accepted" or reserve["response_sha256"] != journal["response_sha256"]
            or decimal(reserve["actual_ce"]) != decimal(env["stats"]["cost_credit_equivalent"])):
            raise ExperimentError("live verification settlement mismatch")
    stats = env["stats"]
    return {"effort":effort, "operation_id":journal["operation_id"],
        "response_sha256":journal["response_sha256"], "returned_model":env["provider_response_model"],
        "finish_reason":env["finish_reason"], "visible_content_sha256":text_sha(env["content"]),
        "prompt_tokens":stats["prompt_tokens"], "completion_tokens":stats["completion_tokens"],
        "cached_tokens_exposed":stats["cached_tokens_exposed"],
        "cache_tokens":stats["cache_hit_tokens"] if stats["cached_tokens_exposed"] else None}


def run_probes(root, *, client, token, authorize_paid=False):
    plan = validate_root(root)
    require_authorization(plan, "probe", token, authorize_paid)
    runtime = runtime_for(plan, client)
    evidence = []
    for index, effort in enumerate(probe_efforts(runtime.model), 1):
        path = Path(root) / "probes" / f"{effort}.json"
        runtime.request("capability_probe", "capability_probes", index, "experiment_capability_probe",
            "Reply with OK.", {"model":runtime.model.provider_model,
                "messages":[{"role":"user", "content":"Reply with OK."}], "max_tokens":64},
            journal_path=path, probe_effort=effort)
        evidence.append(validate_capability_probe_evidence(plan, runtime, effort, index))
    record = {"status":"live_verified", "plan_sha256":plan["plan_sha256"],
              "model_policy_sha256":runtime.model.sha256, "probe_evidence":evidence,
              "scope":"exact serialized reasoning control accepted; not proof of internal reasoning"}
    atomic_write_json(Path(root) / "capability.json", record)
    return record


def require_live_verification(plan, runtime):
    """Rebuild the same mechanical evidence; a status flag alone never grants eligibility."""
    stored = read_json(Path(plan["output_root"]) / "capability.json")
    evidence = [validate_capability_probe_evidence(plan, runtime, effort, index)
                for index, effort in enumerate(probe_efforts(runtime.model), 1)]
    expected = {"status":"live_verified", "plan_sha256":plan["plan_sha256"],
        "model_policy_sha256":runtime.model.sha256, "probe_evidence":evidence,
        "scope":"exact serialized reasoning control accepted; not proof of internal reasoning"}
    if stored != expected:
        raise ExperimentError("capability provenance mismatch")
    return expected


@contextmanager
def real_engine(plan, track_root, runtime):
    """Fresh module, provider-neutral initializer, restore shared core and cwd."""
    require_live_verification(plan, runtime)
    import noveltranslator_core as core
    from catastrophic_completeness_runtime import install_catastrophic_completeness_qa
    from semantic_shadow_runtime import install_semantic_shadow_qa
    if not _ENGINE_CONTEXT_LOCK.acquire(blocking=False):
        raise ExperimentError("another in-process engine context owns shared core/cwd")
    try:
        before = dict(vars(core))
        cwd = Path.cwd()
        name = "_experimental_goat_engine_" + uuid.uuid4().hex
    except BaseException:
        _ENGINE_CONTEXT_LOCK.release()
        raise
    module = None
    try:
        install_catastrophic_completeness_qa(core)
        install_semantic_shadow_qa(core, reporter=None, include_experimental=False)
        spec = importlib.util.spec_from_file_location(name, Path(plan["engine_root"]) / SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        os.chdir(track_root)
        module.initialize_production_runtime_state(activate_provider=False)
        install_experimental_runtime(module, runtime, track_root)
        yield module
    finally:
        runtime.before_send = None
        if module is not None:
            module.client = None
        os.chdir(cwd)
        sys.modules.pop(name, None)
        for key in set(vars(core)) - set(before):
            delattr(core, key)
        for key, value in before.items():
            setattr(core, key, value)
        _ENGINE_CONTEXT_LOCK.release()


def save_results(path, report):
    report.pop("record_sha256",None)
    report["record_sha256"] = digest(report)
    atomic_write_json(path,report)


def verify_committed_slot(plan, slot):
    track_root = Path(plan["output_root"]) / "tracks" / slot["track"]
    chapter = slot["chapter"]
    progress = read_json(track_root / "data/progress.json")
    qa = read_json(track_root / "output/qa" / f"{chapter:04d}.json")
    model = ModelPolicy.from_dict(plan["model"])
    track = next(t for t in plan["tracks"] if t["novel_id"] == slot["track"])
    expected = {"plan_sha256":plan["plan_sha256"], "model_policy_sha256":model.sha256,
        "provider_model":model.provider_model, "profile":track["profile"], "novel_id":slot["track"]}
    prose = local_path(track_root / "output/chapters" / f"{chapter:04d}.md").read_text(encoding="utf-8")
    chapters = read_json(track_root / "data/chapters.json")
    source = next(x["text"] for x in chapters if x["chapter"] == chapter)
    if (chapter not in progress["completed"] or qa.get("experimental_provenance") != expected
        or qa.get("experimental_translation_sha256") != text_sha(prose)
        or qa.get("experimental_source_sha256") != text_sha(source)):
        raise ExperimentError("committed slot proof mismatch")


def run_chronological(root, *, client, token, authorize_paid=False):
    plan = validate_root(root)
    require_authorization(plan, "run", token, authorize_paid)
    runtime = runtime_for(plan, client)
    require_live_verification(plan, runtime)
    # Separate controller rendezvous; the ledger retains its own request-lifetime lock.
    control = Path(root) / "controller"
    control.mkdir(exist_ok=True)
    with production_run_lock(control):
        report_path = Path(root) / "results.json"
        report = read_json(report_path)
        if (report.get("record_sha256") != digest({k:v for k,v in report.items() if k != "record_sha256"})
            or any(s.get("status") not in {"planned","committed","stopped","quarantined_unattempted"} for s in report["slots"])
            or report["plan_sha256"] != plan["plan_sha256"] or
            [{"track":s["track"], "chapter":s["chapter"]} for s in report["slots"]] != plan["slots"]):
            raise ExperimentError("chronological result slot identity drift")
        for slot in report["slots"]:
            track, chapter = slot["track"], slot["chapter"]
            if slot["status"] == "committed":
                verify_committed_slot(plan,slot)
                continue
            if track in report["quarantine"]:
                slot["status"] = "quarantined_unattempted" if slot["status"] == "planned" else slot["status"]
                save_results(report_path, report)
                continue
            track_root = Path(root) / "tracks" / track
            try:
                with production_run_lock(track_root):
                    inspection = inspect_paid_request_journals(track_root / "data/provider_request_journal")
                    if inspection["errors"] or inspection["uncertain"]:
                        raise ExperimentError("track has ambiguous/corrupt paid journals")
                    with real_engine(plan, track_root, runtime) as engine:
                        if engine.progress["next_chapter"] != chapter:
                            raise ExperimentError("canonical next chapter mismatch")
                        engine.run_due_audits()
                        if not engine.process_chapter(chapter):
                            raise ExperimentError("real chapter lifecycle stopped")
                        if engine.progress["last_completed"] != chapter:
                            raise ExperimentError("chapter lacks durable commit")
                        engine.run_due_audits()
                slot["status"] = "committed"
            except Exception as exc:
                slot.update(status="stopped", error_type=type(exc).__name__)
                report["quarantine"][track] = {"chapter":chapter, "error_type":type(exc).__name__}
            save_results(report_path, report)
        with runtime.budget.locked():
            ce, sends = runtime.budget.totals()
        report["credit_equivalent"] = str(ce)
        report["physical_sends_and_unresolved_reservations"] = sends
        save_results(report_path, report)
    return report
