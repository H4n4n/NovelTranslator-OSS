from production_accounting import accounting_report_lines
from production_auxiliary_paid_request import JournaledAuxiliaryRequest
import copy
from dataclasses import replace
import hashlib
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI, APIError, APIConnectionError, APITimeoutError, RateLimitError

from chapter_library import (
    index_chapter_library,
    source_language_prompt_label,
    source_prompt_heading,
)
from noveltranslator_core import (
    api_permission_blocker,
    add_accounting_totals_once,
    atomic_write_json,
    atomic_write_text,
    canonicalize_locked_high_priority_terms,
    calculate_cost_from_token_counts as core_calculate_cost,
    clean_json_text as core_clean_json_text,
    deepseek_pricing_period as core_deepseek_pricing_period,
    format_technique_name as core_format_technique_name,
    glossary_entries_to_text as core_glossary_entries_to_text,
    glossary_strict_qa_errors,
    commit_chapter_accounting,
    is_deepseek_peak_time as core_is_deepseek_peak_time,
    next_off_peak_time as core_next_off_peak_time,
    normalize_category as core_normalize_category,
    obtain_translation,
    pending_meta_after_commit_qa_failure,
    pending_meta_after_translation_repair,
    pending_matches_committed,
    PRICING_VERSION,
    pricing_snapshot as core_pricing_snapshot,
    PRODUCTION_TRANSLATION_MAX_TOKENS,
    PRODUCTION_TRANSLATION_MODEL,
    PRODUCTION_TRANSLATION_THINKING,
    recovered_stats as core_recovered_stats,
    replace_chapter_issues,
    relevant_glossary_entries as core_relevant_glossary_entries,
    retroactive_replacement_is_safe as core_retroactive_replacement_is_safe,
    salvage_state_json_without_issues as core_salvage_state_json_without_issues,
    STRICT_LOCKED_GLOSSARY_CATEGORIES,
    translation_commit_qa,
    V4_PRO_PRICES,
)
from residual_cjk_micro_repair import (
    MICRO_REPAIR_POLICY_VERSION,
    assess_micro_repair_eligibility,
    deterministic_parenthetical_residual_cleanup,
    perform_residual_cjk_micro_repair,
)
from strict_terminology_repair import (
    MAX_ATTEMPTS_PER_POLICY as STRICT_TERM_MAX_ATTEMPTS_PER_POLICY,
    MICRO_REPAIR_POLICY_VERSION as STRICT_TERM_MICRO_REPAIR_POLICY_VERSION,
    assess_strict_terminology_micro_repair_eligibility,
    perform_deterministic_strict_terminology_repair,
    perform_strict_terminology_micro_repair,
    revalidate_recorded_strict_terminology_candidate,
)
from glossary_audit import (
    AuditChunkConfig,
    run_chunked_audit,
)
from glossary_integrity import (
    GlossaryCandidateIdentityError,
    build_source_integrity_audit_plan,
    stable_glossary_candidate_id,
    validate_glossary_review_result,
)
from glossary_candidate_resolution import (
    GlossaryCandidateResolutionError,
    candidate_resolution_path,
    pending_content_identity,
    prepare_glossary_review_candidates,
    state_extraction_identity,
)
from glossary_candidate_auto_resolution import (
    ADJUDICATION_RECOVERY_KEY,
    GlossaryCandidateAutoResolutionError,
    resolve_candidate_conflicts_automatically,
)
from chapter_glossary_review import (
    ChapterGlossaryCandidateIdentityError,
    ChapterGlossaryReviewFailure,
    build_chapter_review_schema_correction_prompt,
    build_chapter_review_semantic_correction_prompt,
    chapter_review_input_hash,
    chapter_review_merge_targets,
    run_chapter_glossary_review,
)
from noveltranslator_observability import (
    SafeRunLogger,
    error_event_fields,
    startup_integrity_errors,
    stats_event_fields,
)
from production_lifecycle import (
    run_due_audits as run_shared_due_audits,
    run_final_audit_if_needed as run_shared_final_audit_if_needed,
    run_unattended_lifecycle,
)
from production_failure_classification import (
    LocalCallbackError,
    LocalPersistenceError,
    NonProviderProductionError,
    ProviderResponseError,
    ProductionIntegrityError,
    describe_non_provider_error,
)
from production_autonomous_recovery import RecoverySupervisor, identity_sha256
from production_commit_qa_checkpoint import run_commit_qa_checkpoint
from production_operation_policy import (
    OperationRequestPolicy,
    ProductionOperationPolicy,
    policy_for_model,
)
from production_translation_recovery import (
    TranslationStageResult, aggregate_translation_stats, run_translation_stage,
    validate_recovery_provenance,
)
from translation_model_config import pending_translation_model_record


from production_state_extraction import (
    DEFAULT_STATE_POLICY,
    run_state_extraction,
    validate_state,
)
from production_state_extraction_checkpoint import (
    run_state_extraction_checkpoint,
)
from production_candidate_preparation import run_candidate_preparation_checkpoint
from production_glossary_review_checkpoint import run_glossary_review_checkpoint
from production_run_lock import production_run_lock
from production_paid_request_journal import (
    PaidRequestJournalError,
    load_matching_journal,
    paid_request_journal_path,
    request_identity,
    run_journaled_paid_request,
)
from production_chapter_commit import (
    commit_reconciled_reviewed_chapter_tail,
    commit_reviewed_chapter,
)
from production_partial_commit_reconciliation import (
    PARTIAL_COMMIT_RECOVERY_MODE,
    PartialCommitReconciliationError,
    inspect_reviewed_partial_commit_evidence,
)
from production_reviewed_state import finalize_reviewed_chapter_state
from production_rolling_continuity import (
    continuity_baseline, saved_continuity_baseline,
    require_continuity_transition, state_extraction_user_prompt,
)
from production_saved_checkpoint_identity import (
    REVIEW_CHECKPOINT_IDENTITY_KEY,
    STATE_CHECKPOINT_IDENTITY_KEY,
    SavedCheckpointIdentityError,
    build_review_checkpoint_identity,
    build_state_checkpoint_identity,
    require_saved_checkpoint_identity,
)
from novel_project import load_novel_config
from translation_profile import (
    compact_auxiliary_terminology_policy,
    effective_translation_instructions,
    load_novel_terminology_policy,
    load_translation_profile,
    profile_audit_instructions,
    profile_glossary_review_instructions,
    select_profile_category,
    profile_state_instructions,
    project_low_value_terms,
)

MODEL = None
ENGINE_ROOT = Path(__file__).resolve().parent
PROJECT_CONFIG_FILE = Path("novel.json")
ACTIVE_NOVEL_CONFIG = None
ACTIVE_PROFILE = None
ACTIVE_NOVEL_POLICY = None
MING_COMPATIBILITY_MODE = None
PROFILE_SYSTEM_PROMPT = None
FINAL_CHAPTER = None
AUDIT_INTERVAL = None

# True = automatically start DeepSeek API requests only during off-peak hours.
# False = ignore DeepSeek peak/off-peak hours and run whenever active.
AUTO_OFF_PEAK_ONLY = True

# Manual pause: create pause.flag while the script is running.
# Current API request may finish; no NEXT request starts until the file is deleted.
MANUAL_PAUSE_FILE = Path("pause.flag")
PAUSE_CHECK_SECONDS = 5
SCHEDULE_CHECK_SECONDS = 30
# Canonical weekday windows and V4 Pro prices come from noveltranslator_core.

CHAPTERS_FILE = Path("data/chapters.json")
GLOSSARY_FILE = Path("data/glossary.json")
PROGRESS_FILE = Path("data/progress.json")
CONTINUITY_FILE = Path("data/continuity.json")
ISSUES_FILE = Path("data/issues.json")
AUDIT_HISTORY_FILE = Path("data/audit_history.json")
TRANSLATION_PROMPT_FILE = Path("production_translation.txt")
OUTPUT_DIR = Path("output/chapters")
PENDING_DIR = Path("output/pending")
QA_DIR = Path("output/qa")
AUDIT_DIR = Path("output/audits")
RETROACTIVE_DIR = Path("output/retroactive_fixes")
BACKUP_DIR = Path("backup")
LOG_DIR = Path("logs")
GLOSSARY_CANDIDATE_RESOLUTION_DIR = Path(
    "data/glossary_candidate_resolutions"
)
PAID_REQUEST_JOURNAL_DIR = Path("data/provider_request_journal")
MAX_AUDIT_GLOSSARY_CHARS = 50000
MAX_AUDIT_CANDIDATES_PER_CHUNK = 20
MAX_AUDIT_CHUNK_SERIALIZED_CHARS = 24000
MAX_AUDIT_CHUNK_OUTPUT_TOKENS = policy_for_model().for_operation("periodic_audit").max_tokens
MAX_TERM_CONTEXTS = 2
TERM_CONTEXT_RADIUS = 180
APPROVED_READER_FACING_CJK_TERMS = ()

RUN_LOGGER = None
api_key = None
client = None
chapters = None
chapter_map = None
glossary = None
strict_qa_configuration_errors = None
progress = None
continuity = None
issues_data = None
audit_history = None
system_prompt = None
_RUNTIME_INITIALIZED = False
_RUNTIME_INITIALIZING = False


def log_event(event, **fields):
    if RUN_LOGGER is None:
        raise RuntimeError("production runtime state has not been initialized")
    RUN_LOGGER.emit(event, **fields)


def log_error(error, chapter=None, stage=None):
    log_event(
        "error",
        chapter=chapter,
        stage=stage,
        **error_event_fields(error),
    )


def load_json(path, default=None):
    if not path.exists():
        if default is None:
            raise FileNotFoundError(path)
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    atomic_write_json(path, data)


def zero_stats():
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 0,
        "cost_usd": 0.0,
        "rate_correct_cost_usd": 0.0,
    }


def combine_stats(*items):
    out = zero_stats()
    for stats in items:
        if not stats:
            continue
        for key in out:
            out[key] += stats.get(key, 0) or 0
    return out


def initialize_production_runtime_state(*, activate_provider=True, provider_backend=None):
    global MODEL
    global ACTIVE_NOVEL_CONFIG
    global ACTIVE_PROFILE
    global ACTIVE_NOVEL_POLICY
    global MING_COMPATIBILITY_MODE, PROFILE_SYSTEM_PROMPT
    global FINAL_CHAPTER, AUDIT_INTERVAL
    global RUN_LOGGER, api_key, client
    global chapters, chapter_map, glossary, strict_qa_configuration_errors
    global progress, continuity, issues_data, audit_history, system_prompt
    global STATE_PROMPT, LOW_VALUE_TERMS
    global TERM_REVIEW_PROMPT, TERM_REVIEW_MODEL_PROMPT
    global AUDIT_PROMPT, AUDIT_MODEL_PROMPT
    global _RUNTIME_INITIALIZED, _RUNTIME_INITIALIZING

    if _RUNTIME_INITIALIZED:
        raise RuntimeError("production runtime state is already initialized")
    if _RUNTIME_INITIALIZING:
        raise RuntimeError("production runtime initialization is already in progress")

    from production_provider_runtime import selected_provider
    from production_provider_contract import PROVIDER_COMMAND_CODE
    if selected_provider(provider_backend) == PROVIDER_COMMAND_CODE and activate_provider:
        raise RuntimeError("GOAT transport requires provider-neutral initialization and explicit activation")
    _RUNTIME_INITIALIZING = True
    try:
        MODEL = PRODUCTION_TRANSLATION_MODEL
        ACTIVE_NOVEL_CONFIG = (
            load_novel_config(PROJECT_CONFIG_FILE)
            if PROJECT_CONFIG_FILE.is_file()
            else None
        )
        ACTIVE_PROFILE = load_translation_profile(
            ACTIVE_NOVEL_CONFIG.profile
            if ACTIVE_NOVEL_CONFIG
            else "historical_military",
            ENGINE_ROOT,
        )
        ACTIVE_NOVEL_POLICY = load_novel_terminology_policy(
            ACTIVE_PROFILE,
            Path("translation_addendum.txt") if ACTIVE_NOVEL_CONFIG else None,
        )
        MING_COMPATIBILITY_MODE = (
            ACTIVE_NOVEL_CONFIG is None
            or ACTIVE_NOVEL_CONFIG.id == "ming_novel"
        )
        PROFILE_SYSTEM_PROMPT = (
            effective_translation_instructions(
                ACTIVE_PROFILE, Path("translation_addendum.txt")
            )
            if ACTIVE_NOVEL_CONFIG
            else None
        )
        FINAL_CHAPTER = (
            ACTIVE_NOVEL_CONFIG.final_chapter
            if ACTIVE_NOVEL_CONFIG
            else 1406
        )
        AUDIT_INTERVAL = (
            ACTIVE_NOVEL_CONFIG.audit_interval
            if ACTIVE_NOVEL_CONFIG
            else 25
        )

        for directory in (
            OUTPUT_DIR,
            PENDING_DIR,
            QA_DIR,
            AUDIT_DIR,
            RETROACTIVE_DIR,
            BACKUP_DIR,
            GLOSSARY_CANDIDATE_RESOLUTION_DIR,
            PAID_REQUEST_JOURNAL_DIR,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        RUN_LOGGER = SafeRunLogger(LOG_DIR)

        if activate_provider:
            load_dotenv()
            api_key = os.getenv("DEEPSEEK_API_KEY")
            if not api_key:
                raise RuntimeError("DEEPSEEK_API_KEY was not found in .env")
            client = OpenAI(
                api_key=api_key,
                base_url="https://api.deepseek.com",
                max_retries=0,
            )
        else:
            api_key = None
            client = None

        with CHAPTERS_FILE.open("r", encoding="utf-8") as f:
            chapters = json.load(f)
        chapter_map = index_chapter_library(
            chapters,
            FINAL_CHAPTER,
            (
                ACTIVE_NOVEL_CONFIG.chapter_numbering
                if ACTIVE_NOVEL_CONFIG
                else "global"
            ),
        )
        glossary = load_json(GLOSSARY_FILE, {"entries": {}})
        strict_qa_configuration_errors = glossary_strict_qa_errors(
            glossary.get("entries", {}),
            (
                ACTIVE_NOVEL_CONFIG.id
                if ACTIVE_NOVEL_CONFIG
                else "legacy_single_novel"
            ),
            ACTIVE_PROFILE.high_priority_categories,
        )
        if strict_qa_configuration_errors:
            raise RuntimeError(
                "Invalid glossary strict-QA configuration: "
                + "; ".join(strict_qa_configuration_errors)
            )
        progress = load_json(PROGRESS_FILE, {})
        continuity = load_json(
            CONTINUITY_FILE,
            {"latest_chapter": 0, "summary": ""},
        )
        issues_data = load_json(ISSUES_FILE, {"issues": []})
        audit_history = load_json(AUDIT_HISTORY_FILE, {"audits": []})
        system_prompt = (
            PROFILE_SYSTEM_PROMPT
            if PROFILE_SYSTEM_PROMPT is not None
            else TRANSLATION_PROMPT_FILE.read_text(encoding="utf-8")
        )

        progress.setdefault("completed", [])
        progress.setdefault("failed", [])
        progress.setdefault("last_completed", 0)
        progress.setdefault(
            "next_chapter",
            progress["last_completed"] + 1,
        )
        progress.setdefault("total_prompt_tokens", 0)
        progress.setdefault("total_completion_tokens", 0)
        progress.setdefault("estimated_cost_usd", 0.0)
        progress.setdefault("total_cache_hit_tokens", 0)
        progress.setdefault("total_cache_miss_tokens", 0)
        progress.setdefault("rate_correct_cost_usd", 0.0)
        progress.setdefault(
            "rate_correct_cost_start_chapter",
            progress.get(
                "next_chapter",
                progress["last_completed"] + 1,
            ),
        )
        progress.setdefault("pricing_version", PRICING_VERSION)
        progress.setdefault(
            "last_audited_chapter",
            progress["last_completed"],
        )
        issues_data.setdefault("issues", [])
        glossary.setdefault("entries", {})
        audit_history.setdefault("audits", [])

        apply_known_glossary_fixes()

        STATE_PROMPT = (
            STATE_PROMPT_BASE
            + profile_state_instructions(
                ACTIVE_PROFILE, ACTIVE_NOVEL_POLICY
            )
        ).strip()
        LOW_VALUE_TERMS = set(
            project_low_value_terms(
                (
                    ACTIVE_NOVEL_CONFIG.id
                    if ACTIVE_NOVEL_CONFIG
                    else "ming_novel"
                )
            )
        )
        TERM_REVIEW_PROMPT = (
            TERM_REVIEW_PROMPT_BASE
            + profile_glossary_review_instructions(
                ACTIVE_PROFILE, ACTIVE_NOVEL_POLICY
            )
        ).strip()
        TERM_REVIEW_MODEL_PROMPT = (
            TERM_REVIEW_PROMPT.replace(
                '"candidate_id":"source:..."',
                '"candidate_id":"c01"',
            )
            + TERM_REVIEW_MODEL_PROMPT_SUFFIX
        )
        AUDIT_PROMPT = (
            AUDIT_PROMPT_BASE
            + profile_audit_instructions(
                ACTIVE_PROFILE, ACTIVE_NOVEL_POLICY
            )
        ).strip()
        AUDIT_MODEL_PROMPT = (
            AUDIT_PROMPT.replace(
                '"candidate_id":"source:..."',
                '"candidate_id":"c01"',
            )
            + AUDIT_MODEL_PROMPT_SUFFIX
        )
        _RUNTIME_INITIALIZED = True
    finally:
        _RUNTIME_INITIALIZING = False

def format_technique_name(text):
    """
    User-preferred style for formal named martial arts / techniques:
    capitalize the initial letter of every space-separated word.

    Example:
    black tiger seizes the heart
    -> Black Tiger Seizes The Heart
    """
    return core_format_technique_name(text)




# ============================================================
# SAFE RETROACTIVE TERMINOLOGY CORRECTIONS
# ============================================================

RETROACTIVE_UNSAFE_GENERIC_ENGLISH = {
    "captain",
    "commander",
    "general",
    "master",
    "lord",
    "chief",
    "officer",
    "soldier",
    "guard",
    "army",
    "city",
    "town",
    "village",
    "fort",
    "island",
    "river",
    "mountain",
    "temple",
    "palace",
    "pavilion",
}


def normalized_category_local(category):
    return core_normalize_category(category)


def retroactive_replacement_is_safe(old_english, new_english, category):
    """
    Initial eligibility only; historical edit authorization belongs to the
    owned-span planner and immutable transaction engine.
    """
    return core_retroactive_replacement_is_safe(
        old_english,
        new_english,
        category,
        ACTIVE_PROFILE.retroactive_safe_categories,
    )


def apply_retroactive_replacements(
    audit_label, through_chapter, replacements, *, staged_glossary=None,
):
    """Plan and reconcile exact owned historical spans locally, with durable evidence."""
    from retroactive_transactions import execute_retroactive_transaction
    from types import SimpleNamespace

    result = execute_retroactive_transaction(
        novel_id=ACTIVE_NOVEL_CONFIG.id if ACTIVE_NOVEL_CONFIG else "legacy_single_novel",
        audit_label=audit_label, through_chapter=through_chapter,
        replacements=replacements, glossary=glossary, staged_glossary=staged_glossary,
        safe_categories=ACTIVE_PROFILE.retroactive_safe_categories,
        committed_chapters=progress.get("completed", []), chapter_map=chapter_map,
        output_dir=OUTPUT_DIR, retroactive_dir=RETROACTIVE_DIR, backup_dir=BACKUP_DIR,
        writer_context=SimpleNamespace(**globals()),
    )
    print(f"Retroactive {audit_label}: rules={result['replacement_rules_considered']}, "
          f"category eligible={result['rules_eligible_by_category']}, "
          f"planned/applied spans={result['replacement_count']}, "
          f"rejected={result['retroactive_replacements_rejected']}, "
          f"resumed={result['transactions_resumed']}, "
          f"already applied={result['already_applied_edits_reconciled']}; "
          f"transaction={result['transaction_id']}")
    return result


def _prepare_known_glossary_fixes(glossary_data, *, profile, ming_compatibility):
    """Pure existing policy, also rederived before completing a saved witness."""
    staged_glossary = copy.deepcopy(glossary_data)
    entries = staged_glossary["entries"]
    changed = []
    removed = []
    retroactive_rules = []

    if ming_compatibility and "蓋州游擊" in entries:
        old_english = entries[
            "蓋州游擊"
        ].get(
            "english",
            "",
        )

        new_english = (
            "Gaizhou Mobile Corps Commander"
        )

        if old_english != new_english:
            retroactive_rules.append(
                {
                    "chinese": "蓋州游擊",
                    "old_english": old_english,
                    "new_english": new_english,
                    "category": entries[
                        "蓋州游擊"
                    ].get(
                        "category",
                        "historical office",
                    ),
                    "reason": (
                        "Known geographic correction: "
                        "蓋州 is Gaizhou, not Ganzhou."
                    ),
                }
            )

            entries[
                "蓋州游擊"
            ][
                "english"
            ] = new_english

            entries[
                "蓋州游擊"
            ][
                "status"
            ] = "provisional"

            entries[
                "蓋州游擊"
            ][
                "notes"
            ] = (
                "Corrected automatically: "
                "蓋州 is Gaizhou, not Ganzhou."
            )

            changed.append(
                "蓋州游擊"
            )

    if ming_compatibility and "三十六計" in entries:
        old_english = entries[
            "三十六計"
        ].get(
            "english",
            "",
        )

        new_english = (
            "Thirty-Six Stratagems"
        )

        if old_english != new_english:
            retroactive_rules.append(
                {
                    "chinese": "三十六計",
                    "old_english": old_english,
                    "new_english": new_english,
                    "category": (
                        entries[
                            "三十六計"
                        ].get(
                            "category",
                            "specialized recurring term",
                        )
                    ),
                    "reason": (
                        "Known semantic correction: "
                        "traditional Thirty-Six Stratagems."
                    ),
                }
            )

        entries[
            "三十六計"
        ][
            "english"
        ] = new_english

        entries[
            "三十六計"
        ][
            "status"
        ] = "locked"

        entries[
            "三十六計"
        ][
            "notes"
        ] = (
            "Traditional Thirty-Six Stratagems; "
            "not 'the thirty-sixth stratagem'."
        )

        if old_english != new_english:
            changed.append(
                "三十六計"
            )

    # Normalize already-saved formal technique names.
    for chinese, entry in entries.items():
        if (
            normalized_category_local(
                entry.get(
                    "category",
                    "",
                )
            )
            not in profile.formal_named_categories
        ):
            continue

        old_english = entry.get(
            "english",
            "",
        )

        if old_english:
            new_english = format_technique_name(
                old_english
            )

            if new_english != old_english:
                retroactive_rules.append(
                    {
                        "chinese": chinese,
                        "old_english": old_english,
                        "new_english": new_english,
                        "category": normalized_category_local(entry.get("category", "")),
                        "reason": (
                            "Normalize formal technique name "
                            "to user-preferred capitalization."
                        ),
                    }
                )

                entry[
                    "english"
                ] = new_english

                changed.append(
                    chinese
                )

        if (
            entry.get(
                "status"
            )
            == "contextual"
        ):
            approved = entry.get(
                "approved_renderings",
                [],
            )

            normalized = [
                format_technique_name(
                    item
                )
                for item in approved
                if isinstance(
                    item,
                    str,
                )
                and item.strip()
            ]

            if normalized != approved:
                # Contextual entries are not automatically retroactively
                # replaced because the correct variant depends on context.
                entry[
                    "approved_renderings"
                ] = normalized

                if chinese not in changed:
                    changed.append(
                        chinese
                    )

    if ming_compatibility and "黑虎掏心" in entries:
        old_english = entries[
            "黑虎掏心"
        ].get(
            "english",
            "",
        )

        new_english = (
            "Black Tiger Seizes The Heart"
        )

        if (
            old_english
            and old_english != new_english
        ):
            retroactive_rules.append(
                {
                    "chinese": "黑虎掏心",
                    "old_english": old_english,
                    "new_english": new_english,
                    "category": "technique",
                    "reason": (
                        "User-preferred formal technique capitalization."
                    ),
                }
            )

        entries[
            "黑虎掏心"
        ][
            "english"
        ] = new_english

        entries[
            "黑虎掏心"
        ][
            "category"
        ] = "technique"

        if "黑虎掏心" not in changed:
            changed.append(
                "黑虎掏心"
            )

    remove_terms = {
        "紅票",
        "红票",
        "負距離",
        "负距离",
        "磚家",
        "砖家",
        "手藝人",
        "手艺人",
        "狼皮座椅",
        "商隊",
        "商队",
        "突進之策",
        "突进之策",
        "白毛風",
        "白毛风",
        "寶豐堂後門",
        "宝丰堂后门",
        "倒夜壺",
        "倒夜壶",
        "西洋鏡",
        "西洋镜",
    } if ming_compatibility else set()

    for term in remove_terms:
        if term in entries:
            del entries[
                term
            ]
            removed.append(
                term
            )

    return staged_glossary, changed, removed, retroactive_rules


def apply_known_glossary_fixes():
    from types import SimpleNamespace
    from production_standalone_lineage import recover_known_fix, apply_known_fix
    recover_known_fix(SimpleNamespace(**globals()))
    staged_glossary, changed, removed, retroactive_rules = _prepare_known_glossary_fixes(
        glossary, profile=ACTIVE_PROFILE, ming_compatibility=MING_COMPATIBILITY_MODE)
    if changed or removed:
        print()
        print("=" * 70)
        print(
            "STARTUP GLOSSARY CLEANUP"
        )
        print("=" * 70)

        for term in changed:
            print(
                f"Corrected: {term}"
            )

        for term in removed:
            print(
                f"Removed low-value term: {term}"
            )

        print("=" * 70)

    # Apply safe exact fixes to previously committed English chapters.
    if retroactive_rules:
        apply_known_fix(SimpleNamespace(**globals()), staged_glossary, retroactive_rules)
    elif changed or removed:
        save_json(GLOSSARY_FILE, staged_glossary)
        glossary.clear()
        glossary.update(staged_glossary)


HANGUL_RE = re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]")


def utc_now():
    return datetime.now(timezone.utc)


def is_deepseek_peak_time(now=None):
    if now is None:
        now = utc_now()
    return core_is_deepseek_peak_time(now)


def next_off_peak_time(now=None):
    if now is None:
        now = utc_now()
    return core_next_off_peak_time(now)


def wait_for_api_permission():
    """Wait locally until an API request is allowed. Waiting uses no API."""
    manual_message_shown = False
    schedule_message_shown = False

    while True:
        now = utc_now()
        blocker = api_permission_blocker(
            MANUAL_PAUSE_FILE.exists(), AUTO_OFF_PEAK_ONLY, now
        )
        if blocker == "manual_pause":
            if not manual_message_shown:
                print("\n" + "=" * 70)
                print("MANUAL PAUSE ACTIVE")
                print("=" * 70)
                print(f"Detected: {MANUAL_PAUSE_FILE}")
                print("No new DeepSeek API request will start while pause.flag exists.")
                print("Delete pause.flag to resume.")
                print("To shut down safely, wait for this message and then press Ctrl+C.")
                print("=" * 70)
                log_event("pause", stage="manual", reason="pause.flag")
                manual_message_shown = True
            time.sleep(PAUSE_CHECK_SECONDS)
            continue

        if manual_message_shown:
            print("Manual pause removed. API work may resume.")
            log_event("resume", stage="manual", reason="pause.flag removed")
            manual_message_shown = False

        if blocker == "peak":
            resume_at = next_off_peak_time(now)
            remaining = max(0, int((resume_at - now).total_seconds()))
            if not schedule_message_shown:
                print("\n" + "=" * 70)
                print("DEEPSEEK PEAK PERIOD — AUTOMATIC PAUSE")
                print("=" * 70)
                print(
                    "AUTO_OFF_PEAK_ONLY = True, so no new API request will start "
                    "during a DeepSeek peak window."
                )
                print("DeepSeek weekday peak windows (UTC):")
                print("  01:00-04:00")
                print("  06:00-10:00")
                print("Weekends are off-peak.")
                print(
                    "Current peak window ends at: "
                    + resume_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                )
                print(f"Approximate wait remaining: {remaining // 60} minutes")
                print("Waiting is local and consumes zero DeepSeek API tokens.")
                print("=" * 70)
                log_event(
                    "pause",
                    stage="schedule",
                    reason="deepseek_peak",
                    resume_at_utc=resume_at.isoformat(),
                )
                schedule_message_shown = True
            time.sleep(min(SCHEDULE_CHECK_SECONDS, max(1, remaining)))
            continue

        if schedule_message_shown:
            print("DeepSeek off-peak period active. API work resuming.")
            log_event("resume", stage="schedule", reason="deepseek_off_peak")
        return



# ============================================================
# DYNAMIC PEAK / OFF-PEAK PRICING
# ============================================================

LAST_SUCCESSFUL_API_PRICING = None


def pricing_period_for_utc(moment=None):
    return core_deepseek_pricing_period(moment or utc_now())


def pricing_snapshot(moment=None):
    """
    Snapshot the rate that applies immediately before an API request starts.
    """
    return core_pricing_snapshot(moment or utc_now())


def send_api_request_once(**kwargs):
    """One raw provider call; callers own scheduling and retry policy."""
    return client.chat.completions.create(**kwargs)


def calculate_cost_from_token_counts(
    prompt_tokens,
    completion_tokens,
    cache_hits,
    cache_misses,
    pricing_info,
):
    return core_calculate_cost(
        prompt_tokens, completion_tokens, cache_hits, cache_misses, pricing_info
    )


def reprice_saved_stats_if_needed(stats):
    """
    Reprice recovered pending work saved by the previous scheduled script.

    If AUTO_OFF_PEAK_ONLY is True, a successfully completed pending request
    must have started during an off-peak window. Its token counts can therefore
    be repriced using the current official V4 Pro off-peak rates.

    Calls that failed before their usage was saved cannot be reconstructed;
    the DeepSeek billing dashboard is authoritative for those.
    """
    if isinstance(stats, dict) and "state_accepted_operations" in stats:
        from noveltranslator_core import validate_stats_record
        if validate_stats_record(stats, "saved state recovery accounting"):
            raise ProductionIntegrityError("saved state recovery accounting failed validation")
        return copy.deepcopy(stats)
    if isinstance(stats, dict) and "translation_accepted_operations" in stats:
        # Each durable accepted operation retains its actual historical price.
        # Repricing the sum at either request's rate would lose mixed-window cost.
        return aggregate_translation_stats(stats["translation_accepted_operations"], combine_stats)
    return core_recovered_stats(stats, AUTO_OFF_PEAK_ONLY, utc_now())



def reprice_saved_stats_or_raise(stats, *, stage):
    """Reprice recovered accounting without disguising local callback failures."""
    try:
        return reprice_saved_stats_if_needed(stats)
    except Exception as exc:
        raise LocalCallbackError(
            f"{stage} saved-accounting repricing failed"
        ) from exc


def call_api_with_retries(**kwargs):
    """Make one application-visible provider attempt after local scheduling.

    The OpenAI-compatible client is configured with max_retries=0. Ambiguous
    paid outcomes are never automatically reissued by this wrapper; higher-level
    durable protocols decide whether a new logical request is safe.
    """
    global LAST_SUCCESSFUL_API_PRICING
    wait_for_api_permission()
    attempt_pricing = pricing_snapshot()
    response = send_api_request_once(**kwargs)
    LAST_SUCCESSFUL_API_PRICING = attempt_pricing
    return response


def usage_and_cost_for_pricing(usage, pricing_info):
    if usage is None:
        raise ProviderResponseError(
            "provider response omitted usage; paid accounting is unknown"
        )
    raw_prompt_tokens = getattr(usage, "prompt_tokens", None)
    raw_completion_tokens = getattr(usage, "completion_tokens", None)
    if raw_prompt_tokens is None or raw_completion_tokens is None:
        raise ProviderResponseError(
            "provider response omitted required token usage; paid accounting is unknown"
        )
    prompt_tokens = int(raw_prompt_tokens)
    completion_tokens = int(raw_completion_tokens)
    cache_hits = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
    cache_misses = getattr(usage, "prompt_cache_miss_tokens", 0) or 0
    cost, cache_hits, cache_misses = calculate_cost_from_token_counts(
        prompt_tokens,
        completion_tokens,
        cache_hits,
        cache_misses,
        pricing_info,
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_hit_tokens": cache_hits,
        "cache_miss_tokens": cache_misses,
        "cost_usd": cost,
        "rate_correct_cost_usd": cost,
        "pricing_version": pricing_info["pricing_version"],
        "pricing_period": pricing_info["pricing_period"],
        "request_started_utc": pricing_info["request_started_utc"],
        "cache_hit_rate_per_million": pricing_info[
            "cache_hit_rate_per_million"
        ],
        "cache_miss_rate_per_million": pricing_info[
            "cache_miss_rate_per_million"
        ],
        "output_rate_per_million": pricing_info[
            "output_rate_per_million"
        ],
    }


STATE_EXTRACTION_POLICY = DEFAULT_STATE_POLICY


def current_production_operation_policy():
    """Snapshot the already-selected runtime controls; no model routing or IO.

    The launcher prepares these model globals before import. Preserve that seam
    for paid-pending recovery and isolated experiment adapters; this layer does
    not select or authorize models. StateExtractionPolicy remains authoritative.
    """
    model = MODEL or PRODUCTION_TRANSLATION_MODEL
    try:
        registered = policy_for_model(model, state=STATE_EXTRACTION_POLICY)
    except ValueError:
        # Isolated historical benchmark adapters are not production selections.
        # Preserve their controls without authorizing V4.1 recovery or inventing
        # a registry version. The production provider guard rejects unknown routes.
        registered = ProductionOperationPolicy(
            translation=OperationRequestPolicy("chapter_translation", PRODUCTION_TRANSLATION_MAX_TOKENS,
                                               PRODUCTION_TRANSLATION_THINKING),
            state=STATE_EXTRACTION_POLICY, model_id=model,
        )
    translation = OperationRequestPolicy("chapter_translation", PRODUCTION_TRANSLATION_MAX_TOKENS,
                                         PRODUCTION_TRANSLATION_THINKING)
    return replace(registered, translation=translation,
                   translation_recovery=(registered.translation_recovery
                       if translation == registered.translation else None))


def operation_request_controls(operation, *, ceiling=None):
    policy = current_production_operation_policy().for_operation(operation)
    if ceiling is not None and ceiling != policy.max_tokens:
        raise ValueError("request ceiling disagrees with production operation policy")
    if RUN_LOGGER is not None:
        log_event("production_operation_policy", **policy.event_fields())
    return policy.request_controls()


def _chat_request_identity(operation_kind, chapter_number, source_text, request_kwargs, *, translation_text=""):
    novel_id = (
        ACTIVE_NOVEL_CONFIG.id
        if ACTIVE_NOVEL_CONFIG is not None
        else "ming_novel"
    )
    profile_id = getattr(ACTIVE_PROFILE, "id", "") or "historical_military"
    source_sha256 = hashlib.sha256(
        source_text.encode("utf-8")
    ).hexdigest()
    translation_sha256 = (
        hashlib.sha256(translation_text.encode("utf-8")).hexdigest()
        if translation_text
        else ""
    )
    return request_identity(
        operation_kind=operation_kind,
        novel_id=novel_id,
        chapter=chapter_number,
        provider="deepseek",
        model=request_kwargs.get("model", ""),
        profile_id=profile_id,
        source_sha256=source_sha256,
        translation_sha256=translation_sha256,
        request_payload=request_kwargs,
    )


def journaled_chat_request(
    operation_kind,
    chapter_number,
    source_text,
    request_kwargs,
    *,
    translation_text="",
    journal_path=None,
):
    """Persist request intent/outcome around exactly one paid provider attempt."""
    # A controlled transport can authorize in the journal's PLANNED phase.
    # Normal production has no such hook and retains the historical send path.
    completions = getattr(getattr(client, "chat", None), "completions", None)
    authorizer = getattr(completions, "authorize_journaled_send", None)
    if callable(authorizer):
        request_kwargs = copy.deepcopy(request_kwargs)
    send_once = lambda: send_api_request_once(**request_kwargs)
    identity = _chat_request_identity(
        operation_kind, chapter_number, source_text, request_kwargs,
        translation_text=translation_text,
    )
    journal_path = journal_path or paid_request_journal_path(
        PAID_REQUEST_JOURNAL_DIR,
        operation_kind,
        chapter_number,
    )

    def prepare_attempt():
        wait_for_api_permission()
        return pricing_snapshot()

    def normalize_response(response, attempt_pricing):
        choice = response.choices[0]
        return {
            "content": choice.message.content or "",
            "finish_reason": choice.finish_reason,
            "stats": usage_and_cost_for_pricing(
                response.usage,
                attempt_pricing,
            ),
            "provider_response_id": getattr(response, "id", "") or "",
        }

    return run_journaled_paid_request(
        journal_path=journal_path,
        identity=identity,
        save_json=lambda path, value: atomic_write_json(path, value),
        prepare_attempt=prepare_attempt,
        send_once=send_once,
        normalize_response=normalize_response,
        **({"authorize_send": lambda: authorizer(send_once=send_once, **request_kwargs)}
           if callable(authorizer) else {}),
    )


def usage_and_cost(usage):
    prompt_tokens = getattr(
        usage,
        "prompt_tokens",
        0,
    ) or 0

    completion_tokens = getattr(
        usage,
        "completion_tokens",
        0,
    ) or 0

    cache_hits = getattr(
        usage,
        "prompt_cache_hit_tokens",
        0,
    ) or 0

    cache_misses = getattr(
        usage,
        "prompt_cache_miss_tokens",
        0,
    ) or 0

    pricing_info = (
        LAST_SUCCESSFUL_API_PRICING
        or pricing_snapshot()
    )

    (
        cost,
        cache_hits,
        cache_misses,
    ) = calculate_cost_from_token_counts(
        prompt_tokens,
        completion_tokens,
        cache_hits,
        cache_misses,
        pricing_info,
    )

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_hit_tokens": cache_hits,
        "cache_miss_tokens": cache_misses,
        "cost_usd": cost,
        "rate_correct_cost_usd": cost,
        "pricing_version": pricing_info["pricing_version"],
        "pricing_period": pricing_info["pricing_period"],
        "request_started_utc": pricing_info["request_started_utc"],
        "cache_hit_rate_per_million": pricing_info[
            "cache_hit_rate_per_million"
        ],
        "cache_miss_rate_per_million": pricing_info[
            "cache_miss_rate_per_million"
        ],
        "output_rate_per_million": pricing_info[
            "output_rate_per_million"
        ],
    }


def clean_json_text(content):
    return core_clean_json_text(content)


GENERIC_JSON_REPAIR_PROMPT = """
You repair malformed JSON.
Return ONLY one valid JSON object.
Do not add commentary. Do not use Markdown fences.
Correct only what is necessary to make the JSON valid.
Preserve important information, but COMPRESS repetitive material.
If an "issues" array exists, return at most 5 short issue strings.
Never enumerate hundreds of corrupted glyphs, character substitutions, or repeated examples.
A repeated encoding-corruption pattern should be summarized in one short issue.
""".strip()


def repair_json_content(label, malformed_content, schema_hint="", *, operation="generic_json_repair"):
    print(f"\n{label} was malformed.")
    print("Attempting one automatic JSON repair...")
    prompt = GENERIC_JSON_REPAIR_PROMPT
    if schema_hint:
        prompt += "\n\nRequired structure:\n" + schema_hint
    response = call_api_with_retries(
        model=MODEL,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": "Repair this malformed JSON:\n\n" + malformed_content},
        ],
        **operation_request_controls(operation),
    )
    repaired_content = clean_json_text(response.choices[0].message.content or "")
    try:
        repaired = json.loads(repaired_content)
    except json.JSONDecodeError as exc:
        raise ProviderResponseError(
            f"Automatic JSON repair also failed for {label}.\n\nReturned text:\n{repaired_content}"
        ) from exc
    stats = usage_and_cost(response.usage)
    print(f"JSON repair succeeded for {label}.")
    return repaired, stats


def relevant_glossary_for_entries(chinese_text, entries):
    strict_categories = (
        set(ACTIVE_PROFILE.high_priority_categories)
        & set(STRICT_LOCKED_GLOSSARY_CATEGORIES)
    )
    return core_relevant_glossary_entries(
        chinese_text,
        entries,
        source_variant_categories=strict_categories,
    )


def relevant_glossary(chinese_text):
    return relevant_glossary_for_entries(
        chinese_text,
        glossary["entries"],
    )


def glossary_to_text(entries, *, chapter_number=None):
    return core_glossary_entries_to_text(
        entries,
        high_priority_categories=ACTIVE_PROFILE.high_priority_categories,
        strict_locked_categories=STRICT_LOCKED_GLOSSARY_CATEGORIES,
        chapter_number=chapter_number,
    )


def current_state_checkpoint_identity(
    chapter_number,
    chinese_text,
    translation,
    *,
    glossary_entries=None,
    legacy_policy=False,
    prior_continuity=None,
):
    entries = (
        glossary["entries"]
        if glossary_entries is None
        else glossary_entries
    )
    return build_state_checkpoint_identity(
        novel_id=(
            ACTIVE_NOVEL_CONFIG.id
            if ACTIVE_NOVEL_CONFIG
            else "ming_novel"
        ),
        chapter=chapter_number,
        source_text=chinese_text,
        translation=translation,
        profile_id=ACTIVE_PROFILE.id,
        model_id=MODEL,
        state_prompt=STATE_PROMPT,
        existing_glossary_keys=entries.keys(),
        prior_continuity=continuity_baseline(
            continuity if prior_continuity is None else prior_continuity
        ),
        max_tokens=3000 if legacy_policy else STATE_EXTRACTION_POLICY.initial_tokens,
        thinking_type="disabled" if legacy_policy else STATE_EXTRACTION_POLICY.thinking_type,
        extraction_policy=None if legacy_policy else STATE_EXTRACTION_POLICY.identity(),
    )


def current_review_checkpoint_identity(
    chapter_number,
    chinese_text,
    translation,
    candidates,
    *,
    glossary_entries=None,
):
    entries = (
        glossary["entries"]
        if glossary_entries is None
        else glossary_entries
    )
    relevant_entries = relevant_glossary_for_entries(
        chinese_text,
        entries,
    )
    relevant_text = glossary_to_text(relevant_entries, chapter_number=chapter_number)
    input_hash = chapter_review_input_hash(
        candidates,
        entries.keys(),
        chinese_text,
        translation,
    )
    return build_review_checkpoint_identity(
        novel_id=(
            ACTIVE_NOVEL_CONFIG.id
            if ACTIVE_NOVEL_CONFIG
            else "ming_novel"
        ),
        chapter=chapter_number,
        source_text=chinese_text,
        translation=translation,
        profile_id=ACTIVE_PROFILE.id,
        model_id=MODEL,
        review_prompt=TERM_REVIEW_MODEL_PROMPT,
        review_schema=TERM_REVIEW_SCHEMA,
        review_input_hash=input_hash,
        relevant_glossary_text=relevant_text,
        max_original_tokens=current_production_operation_policy().for_operation(
            "chapter_glossary_review").max_tokens,
        max_correction_tokens=current_production_operation_policy().for_operation(
            "chapter_glossary_review_semantic_correction").max_tokens,
        thinking_type=current_production_operation_policy().for_operation(
            "chapter_glossary_review").thinking_type,
    )


def translate_chapter(chapter):
    chapter_number = chapter["chapter"]
    chinese_text = chapter["text"]
    print("\n" + "=" * 70)
    print(f"TRANSLATING CHAPTER {chapter_number}")
    print("=" * 70)
    log_event("chapter_translation_start", chapter=chapter_number, stage="translation")
    start = time.perf_counter()
    from production_translation_request import build_translation_request
    request_kwargs = build_translation_request(chapter=chapter, config=ACTIVE_NOVEL_CONFIG,
        profile=ACTIVE_PROFILE, system_prompt=system_prompt,
        rendered_glossary=glossary_to_text(relevant_glossary(chinese_text), chapter_number=chapter_number), continuity=continuity, model=MODEL, controls=operation_request_controls("chapter_translation"))
    policy = current_production_operation_policy()
    result = run_translation_stage(
        policy=policy, initial_kwargs=request_kwargs,
        request=lambda operation, kwargs, binding: journaled_chat_request(
            operation, chapter_number, chinese_text, kwargs,
            **({"translation_text": binding} if binding else {})),
        prove_initial=lambda: load_matching_journal(
            paid_request_journal_path(PAID_REQUEST_JOURNAL_DIR, "chapter_translation", chapter_number),
            _chat_request_identity("chapter_translation", chapter_number, chinese_text, request_kwargs)),
        combine_stats=combine_stats,
        emit=lambda event, **fields: log_event(event, chapter=chapter_number, **fields),
    )
    elapsed = time.perf_counter() - start
    translation, stats, _, finish_reason = result
    journaled = result.recovery or result.initial
    log_event(
        "chapter_translation_end", chapter=chapter_number, stage="translation",
        elapsed_seconds=elapsed, finish_reason=finish_reason,
        replayed_paid_response=journaled["replayed"], paid_operation_id=journaled["operation_id"],
        translation_recovery_used=result.recovery_used, **stats_event_fields(stats),
    )
    # Print the final operation's actual rate; combined cost/usage below includes
    # both accepted responses. Never imply one rate applied to the aggregate.
    displayed_stats = journaled["response"]["stats"]
    print(f"Translation finished in {elapsed:.2f} sec")
    print(f"Input: {stats['prompt_tokens']:,}")
    print(f"Output: {stats['completion_tokens']:,}")
    print(
        f"Pricing period: "
        f"{displayed_stats['pricing_period'].upper().replace('_', '-')}"
    )
    print(
        "V4 Pro rates / 1M: "
        f"cache hit ${displayed_stats['cache_hit_rate_per_million']:.3f}, "
        f"cache miss ${displayed_stats['cache_miss_rate_per_million']:.2f}, "
        f"output ${displayed_stats['output_rate_per_million']:.2f}"
    )
    print(
        f"Calculated API cost: "
        f"${stats['cost_usd']:.6f}"
    )
    return replace(result, elapsed_seconds=elapsed)


def _auxiliary_request_api(operation, chapter_number, source_text, translation_text):
    def request_kwargs(kind, request):
        return {"model": MODEL, "messages": [
            {"role": "system", "content": request["system_prompt"]},
            {"role": "user", "content": request["user_prompt"]},
        ], **operation_request_controls(kind)}
    return JournaledAuxiliaryRequest(operation=operation, chapter=chapter_number,
        source_text=source_text, translation_text=translation_text,
        journal_dir=PAID_REQUEST_JOURNAL_DIR, request_kwargs=request_kwargs,
        identity_factory=_chat_request_identity, run_request=journaled_chat_request)


def _request_micro_repair(operation, request, *, chapter_number, source_text,
                          translation_text, operation_id):
    return _auxiliary_request_api(operation, chapter_number, source_text, translation_text)(
        request, operation_id=operation_id)


def request_residual_cjk_micro_repair(request, **context):
    return _request_micro_repair("residual_cjk_micro_repair", request, **context)


def account_residual_cjk_micro_repair(operation_id, stats):
    """Durably account a paid repair without advancing chapter progress."""
    if add_accounting_totals_once(progress, operation_id, stats):
        save_json(PROGRESS_FILE, progress)
        return True
    return False


def request_strict_terminology_micro_repair(request, **context):
    return _request_micro_repair("strict_terminology_micro_repair", request, **context)


def account_strict_terminology_micro_repair(operation_id, stats):
    """Account terminology repair separately under its idempotent operation ID."""
    return account_residual_cjk_micro_repair(operation_id, stats)


def request_glossary_candidate_conflict_adjudication(request, **context):
    return _request_micro_repair("glossary_candidate_conflict_adjudication", request, **context)


def account_glossary_candidate_conflict_adjudication(operation_id, stats):
    """Durably account one bounded adjudication without advancing progress."""
    return account_residual_cjk_micro_repair(operation_id, stats)


def maybe_revalidate_recorded_strict_terminology(
    chapter_number, chinese_text, translation, meta, pending_file,
    pending_meta_file, finish_reason,
):
    """Recover a matching paid diagnostic response before new local/API work."""
    return revalidate_recorded_strict_terminology_candidate(
        novel_id=ACTIVE_NOVEL_CONFIG.id,
        chapter=chapter_number,
        source_text=chinese_text,
        translation=translation,
        meta=meta,
        glossary_entries=glossary["entries"],
        high_priority_categories=ACTIVE_PROFILE.high_priority_categories,
        persist_meta=lambda value: save_json(pending_meta_file, value),
        persist_translation=lambda value: atomic_write_text(pending_file, value),
        account_api_usage=account_strict_terminology_micro_repair,
        qa_checker=lambda candidate: local_qa(
            chapter_number, chinese_text, candidate, finish_reason
        ),
        approved_reader_facing_terms=APPROVED_READER_FACING_CJK_TERMS,
        policy_version=STRICT_TERM_MICRO_REPAIR_POLICY_VERSION,
        log_event=log_event,
    )


def maybe_repair_strict_terminology(
    chapter_number, chinese_text, translation, meta, pending_file,
    pending_meta_file, finish_reason,
):
    """Try one bounded paid patch for remaining repair-eligible strict failures."""
    eligibility = assess_strict_terminology_micro_repair_eligibility(
        chinese_text,
        translation,
        glossary["entries"],
        ACTIVE_PROFILE.high_priority_categories,
        chapter_number=chapter_number,
    )
    if not eligibility["eligible_failures"]:
        return {"status": "clean", "translation": translation, "meta": meta}

    print("\n" + "=" * 70)
    print("STRICT TERMINOLOGY MISMATCH DETECTED")
    print("=" * 70)
    print(f"Chapter: {chapter_number}")
    for violation in eligibility["eligible_failures"]:
        print(
            f"  {violation['chinese']} requires "
            f"{violation['required_english']}"
        )
    print(
        "Eligible for bounded terminology micro-repair: "
        + ("YES" if eligibility["eligible"] else "NO")
    )
    if eligibility["reasons"]:
        print("Reason: " + "; ".join(eligibility["reasons"]))
    print("=" * 70)

    result = perform_strict_terminology_micro_repair(
        novel_id=ACTIVE_NOVEL_CONFIG.id,
        chapter=chapter_number,
        profile_id=ACTIVE_PROFILE.id,
        target_language=ACTIVE_NOVEL_CONFIG.target_language,
        source_text=chinese_text,
        translation=translation,
        meta=meta,
        glossary_entries=glossary["entries"],
        high_priority_categories=ACTIVE_PROFILE.high_priority_categories,
        auxiliary_terminology_policy=compact_auxiliary_terminology_policy(
            ACTIVE_PROFILE, ACTIVE_NOVEL_POLICY
        ),
        request_api=_auxiliary_request_api("strict_terminology_micro_repair", chapter_number, chinese_text, translation),
        persist_meta=lambda value: save_json(pending_meta_file, value),
        persist_translation=lambda value: atomic_write_text(pending_file, value),
        account_api_usage=account_strict_terminology_micro_repair,
        qa_checker=lambda candidate: local_qa(
            chapter_number, chinese_text, candidate, finish_reason
        ),
        approved_reader_facing_terms=APPROVED_READER_FACING_CJK_TERMS,
        policy_version=STRICT_TERM_MICRO_REPAIR_POLICY_VERSION,
        max_attempts=STRICT_TERM_MAX_ATTEMPTS_PER_POLICY,
        log_event=log_event,
    )
    if result.get("api_called"):
        attempt_stats = result.get("repair_attempt_stats") or [
            result.get("repair_stats") or zero_stats()
        ]
        stats = combine_stats(*attempt_stats)
        print("\n" + "=" * 70)
        print("STRICT TERMINOLOGY MICRO-REPAIR")
        print("=" * 70)
        print("Existing paid translation reused: YES")
        print("Full translation API call repeated: NO")
        if len(attempt_stats) == 1 and attempt_stats[0].get("pricing_period"):
            print(
                "Pricing period: "
                + attempt_stats[0]["pricing_period"].upper().replace("_", "-")
            )
        print(f"Bounded repair API requests: {len(attempt_stats)}")
        print(f"Repair input tokens: {stats.get('prompt_tokens', 0):,}")
        print(f"Repair output tokens: {stats.get('completion_tokens', 0):,}")
        print(f"Repair API cost: ${stats.get('cost_usd', 0):.6f}")
        print("=" * 70)

    if result["status"] in {
        "succeeded",
        "succeeded_from_recorded_candidate",
        "succeeded_from_revalidated_recorded_candidate",
        "succeeded_with_unrelated_blockers",
        "succeeded_from_recorded_candidate_with_unrelated_blockers",
        "succeeded_from_revalidated_recorded_candidate_with_unrelated_blockers",
    }:
        print("\n" + "=" * 70)
        print("AUTOMATIC STRICT TERMINOLOGY REPAIR SUCCEEDED")
        print("=" * 70)
        if result.get("qa_transition", {}).get("partial"):
            print("Targeted strict terminology passed local QA.")
            print("Unchanged unrelated blockers remain for their normal repair stage.")
        else:
            print("Candidate passed strict terminology and mandatory local QA.")
        print("Continuing normal production processing.")
        print("=" * 70)
    elif result["status"] != "clean":
        print("\n" + "=" * 70)
        print("AUTOMATIC STRICT TERMINOLOGY REPAIR STOPPED SAFELY")
        print("=" * 70)
        print(f"Status: {result['status']}")
        if result["status"] == "ineligible":
            print("Eligibility rejection: " + "; ".join(eligibility["reasons"]))
        elif result["status"] == "locally_rejected":
            print("API request completed but proposed patch failed local validation.")
            for error in result.get("validation", {}).get("errors", [])[:10]:
                print(f"  - {error}")
            print("Repair API usage accounted.")
        elif result["status"] == "qa_rejected":
            print("Anchored patch failed mandatory post-repair QA.")
            print("Repair API usage accounted.")
        elif result["status"] == "api_failed":
            print("Terminology repair API/network request failed.")
        elif result["status"] == "attempt_limit_reached":
            print("Paid attempt already consumed for this unchanged failure identity.")
        print("Paid pending translation preserved.")
        print("No production progress advanced.")
        print("=" * 70)
    return result


def maybe_repair_residual_cjk(
    chapter_number, chinese_text, translation, meta, pending_file,
    pending_meta_file, finish_reason,
):
    """Apply at most one opt-in contextual repair to this paid pending version."""
    config = (
        ACTIVE_NOVEL_CONFIG.residual_cjk_micro_repair
        if ACTIVE_NOVEL_CONFIG
        else None
    )
    if config is None:
        return {"status": "disabled", "translation": translation, "meta": meta}
    eligibility = assess_micro_repair_eligibility(
        chinese_text,
        translation,
        config,
        APPROVED_READER_FACING_CJK_TERMS,
        ACTIVE_NOVEL_CONFIG.source_language,
    )
    if not eligibility["remaining_cjk_character_count"]:
        return {"status": "clean", "translation": translation, "meta": meta}
    cleanup_preview = deterministic_parenthetical_residual_cleanup(
        translation,
        source_language=ACTIVE_NOVEL_CONFIG.source_language,
        target_language=ACTIVE_NOVEL_CONFIG.target_language,
        approved_reader_facing_terms=APPROVED_READER_FACING_CJK_TERMS,
    )

    print("\n" + "=" * 70)
    print("RESIDUAL SOURCE SCRIPT DETECTED")
    print("=" * 70)
    print(f"Chapter: {chapter_number}")
    print(
        "Residual source-script characters: "
        f"{eligibility['remaining_cjk_character_count']}"
    )
    print(
        "Fragments: "
        + ", ".join(repr(item) for item in eligibility["residual_cjk_fragments"])
    )
    print(
        "Deterministic parenthetical cleanup candidates: "
        f"{len(cleanup_preview['removals'])}"
    )
    print("=" * 70)

    relevant_locked = [
        entry for entry in relevant_glossary(chinese_text)
        if entry.get("status") == "locked"
    ]
    result = perform_residual_cjk_micro_repair(
        novel_id=ACTIVE_NOVEL_CONFIG.id,
        chapter=chapter_number,
        profile_id=ACTIVE_PROFILE.id,
        target_language=ACTIVE_NOVEL_CONFIG.target_language,
        source_text=chinese_text,
        translation=translation,
        meta=meta,
        config=config,
        relevant_locked_glossary=relevant_locked,
        auxiliary_terminology_policy=compact_auxiliary_terminology_policy(
            ACTIVE_PROFILE, ACTIVE_NOVEL_POLICY
        ),
        request_api=_auxiliary_request_api("residual_cjk_micro_repair", chapter_number, chinese_text, translation),
        persist_meta=lambda value: save_json(pending_meta_file, value),
        persist_translation=lambda value: atomic_write_text(pending_file, value),
        account_api_usage=account_residual_cjk_micro_repair,
        qa_checker=lambda candidate: local_qa(
            chapter_number, chinese_text, candidate, finish_reason
        ),
        approved_reader_facing_terms=APPROVED_READER_FACING_CJK_TERMS,
        policy_version=MICRO_REPAIR_POLICY_VERSION,
        source_language=ACTIVE_NOVEL_CONFIG.source_language,
        log_event=log_event,
        allow_corrective_retry="_production_goat_runtime" not in globals(),
    )

    if result.get("api_called"):
        stats = result.get("repair_stats") or zero_stats()
        repair_eligibility = result.get("eligibility", eligibility)
        print("\n" + "=" * 70)
        print("RESIDUAL SOURCE-SCRIPT MICRO-REPAIR")
        print("=" * 70)
        print("Existing paid translation reused: YES")
        print("Full translation API call repeated: NO")
        print(
            "Affected fragments: "
            + ", ".join(repair_eligibility["residual_cjk_fragments"])
        )
        if stats.get("request_count", 1) > 1:
            print(f"Bounded repair API requests: {stats['request_count']}")
        if stats.get("pricing_period"):
            print("Pricing period: " + stats["pricing_period"].upper().replace("_", "-"))
        print(f"Repair input tokens: {stats.get('prompt_tokens', 0):,}")
        print(f"Repair output tokens: {stats.get('completion_tokens', 0):,}")
        print(f"Repair API cost: ${stats.get('cost_usd', 0):.6f}")
        print("=" * 70)

    if result["status"] == "succeeded_deterministic_cleanup":
        cleanup = result["deterministic_cleanup"]
        print("\n" + "=" * 70)
        print("DETERMINISTIC RESIDUAL ANNOTATION CLEANUP SUCCEEDED")
        print("=" * 70)
        print(f"Annotations removed: {len(cleanup['removals'])}")
        print("Residual source-script characters after cleanup: 0")
        print("API repair called: NO")
        print("Candidate passed full local QA.")
        print("=" * 70)
    elif result["status"] in {
        "succeeded",
        "succeeded_from_recorded_candidate",
        "succeeded_from_revalidated_recorded_candidate",
    }:
        print("\n" + "=" * 70)
        print("AUTOMATIC SOURCE-SCRIPT MICRO-REPAIR SUCCEEDED")
        print("=" * 70)
        print("Residual CJK after repair: 0")
        print("Candidate passed local repair guards.")
        print("Continuing normal QA.")
        print("=" * 70)
    elif result["status"] not in {"clean"}:
        status = result["status"]
        print("\n" + "=" * 70)
        if status == "ineligible":
            print("AUTOMATIC SOURCE-SCRIPT MICRO-REPAIR NOT ATTEMPTED")
            repair_eligibility = result.get("eligibility", eligibility)
            print("Eligibility rejection: " + "; ".join(
                repair_eligibility.get("reasons")
                or ["configured safety guard rejected it"]
            ))
        elif status == "deterministic_cleanup_rejected":
            print("DETERMINISTIC RESIDUAL ANNOTATION CLEANUP REJECTED")
            print("The in-memory candidate did not pass mandatory local QA.")
            print("No pending text was changed and no API request was made.")
        elif status == "attempt_limit_reached":
            print("AUTOMATIC SOURCE-SCRIPT MICRO-REPAIR NOT ATTEMPTED")
            print(
                "Paid-attempt limit already consumed for this pending content "
                f"under policy {result.get('policy_version', 'unknown')}."
            )
        elif status == "api_failed":
            print("AUTOMATIC SOURCE-SCRIPT MICRO-REPAIR API/NETWORK FAILURE")
            print(
                f"{result.get('error_type', 'API error')}: "
                f"{result.get('error', 'request failed')[:300]}"
            )
        elif status == "locally_rejected":
            errors = result.get("validation", {}).get("errors", [])
            initial_errors = result.get("initial_validation_errors", [])
            malformed = any("malformed repair JSON" in item for item in errors)
            print(
                "AUTOMATIC SOURCE-SCRIPT MICRO-REPAIR MALFORMED RESPONSE REJECTED"
                if malformed else "AUTOMATIC SOURCE-SCRIPT MICRO-REPAIR REJECTED"
            )
            print("API request completed but proposed patch failed local validation.")
            if result.get("corrective_operation_id") and initial_errors:
                print("Initial patch validation errors:")
                for error in initial_errors[:10]:
                    print(f"  - {error}")
                print("Corrective retry validation errors:")
            else:
                print("Validation errors:")
            for error in errors[:10]:
                print(f"  - {error}")
            if len(errors) > 10:
                print(f"  - ... and {len(errors) - 10} more")
            print("Repair API usage accounted.")
            print("No production progress advanced.")
        elif status == "qa_rejected":
            print("AUTOMATIC SOURCE-SCRIPT MICRO-REPAIR REJECTED BY MANDATORY QA")
            print("The anchored patch passed validation but downstream QA did not.")
        else:
            print("AUTOMATIC SOURCE-SCRIPT MICRO-REPAIR FAILED")
        print("=" * 70)
        print("Paid pending translation preserved.")
        print("Progress chapter marker not advanced.")
        if result.get("api_called"):
            print(
                "Same pending content and repair-policy version will not "
                "automatically incur another repair call."
            )
        print("Manual review required.")
        print("=" * 70)
    return result


STATE_PROMPT_BASE = """
You maintain terminology and durable continuity for a Chinese webnovel translation.
Analyze the supplied Chinese chapter and final English translation.
Return ONLY valid JSON:
{
  "new_terms": [{"chinese":"...","english":"...","category":"...","status":"provisional","notes":"..."}],
  "continuity_summary":"...",
  "issues":[]
}
Only propose a term if it is genuinely useful for consistency in future chapters.
Within new_terms, each exact "chinese" source key may appear at most once.
Consolidate complementary evidence into that single proposal; never emit
alternative English/category/status proposals for the same exact source key.
The "chinese" field is the immutable source-side identity even for non-Chinese
novels. Copy it as an EXACT literal span from CHINESE SOURCE. Never translate,
transliterate, normalize, or substitute another script in that field.
Do not propose ordinary vocabulary, one-time descriptions, ordinary objects,
generic duties, generic tactical phrases, website/promotional language, or ordinary idioms.
Do not propose a term already present in the supplied glossary list.
All new terms must remain provisional.
CONTINUITY RULES:
Revise PRIOR COMMITTED CONTINUITY using current source evidence and the accepted
translation. Return the complete revised working memory, not a concatenation or
only this chapter's changes. Preserve still-relevant durable facts from the prior
memory when this chapter does not mention them. Replace an old active value when
current source explicitly supersedes it. Remove resolved threats/questions and
fulfilled debts/promises/obligations from unresolved memory; retain only durable
consequences that still matter. Current source evidence outranks stronger wording
accidentally introduced by the English translation. Preserve attribution and
uncertainty for beliefs/predictions. Contradictory or ambiguous source evidence
must remain uncertain, never silently become a confirmed fact.
Keep continuity_summary a compact memory for future chapters, not a chapter recap.
Use at most 200 English words; prioritize durable changes over transient scene detail.
Retain durable changes when present even if not needed in the immediately next scene:
- injury / health / condition;
- power/progression or persistent capability changes;
- relationship/attitude changes;
- allegiance/faction changes;
- possessions/resources/artifacts acquired or lost;
- location/residency changes;
- persistent knowledge/secrets learned;
- commitments/promises/debts/obligations;
- unresolved threats/questions likely to matter later.
Include only source-supported changes; do not invent items to fill these categories.
Distinguish confirmed facts/actions from offers/proposals, intentions/plans, and
predictions/likely next steps. Label unconfirmed items with their actual status.
Never state an offer or intention as a completed fact or a guaranteed next action.
If acceptance or completion is not shown, keep it unresolved; avoid speculative
"Next:" narration. Omit predictions unless useful, and explicitly mark uncertainty.
Examples: retain "Her injury persists; she now distrusts her former ally; her new
ability remains available" even if the next scene concerns a different character.
"Zha offered Li Yan lodging and suggested a later return; acceptance is not shown"
must NOT become "Li Yan stays with Zha and will return to reconcile".

ISSUES RULES:
- The local QA system already counts routine Hangul/encoding corruption.
- Do NOT list individual corrupted glyphs or character substitutions.
- Do NOT enumerate repeated encoding errors.
- Do NOT report routine corruption if the intended meaning was resolved confidently.
- Report an issue only when corruption or ambiguity materially affects translation confidence,
  terminology, continuity, or produces [TRANSLATION REVIEW REQUIRED].
- Maximum 5 issues.
- Each issue must be a short summary, preferably under 250 characters.

Return JSON only.


PROFILE-SPECIFIC TERMINOLOGY POLICY:
"""
STATE_PROMPT = None

STATE_SCHEMA_HINT = '{"new_terms":[],"continuity_summary":"...","issues":[]}'


def salvage_state_json_without_issues(content):
    """
    Local zero-API fallback for a common failure mode: the model returned
    valid new_terms and continuity_summary, then produced an enormous or
    truncated issues array. In that case, discard only the broken issues
    tail and keep the useful state data.
    """
    result = core_salvage_state_json_without_issues(content)
    if result is not None:
        print("Recovered state JSON locally by discarding a malformed/truncated issues array.")
        print("No extra API call was needed for this recovery.")
    return result


def extract_state(chapter_number, chinese_text, translation, *, prior_continuity=None):
    stage_start = time.perf_counter()
    log_event("state_extraction_start", chapter=chapter_number, stage="state_extraction")
    existing_terms = list(glossary["entries"].keys())
    user_prompt = state_extraction_user_prompt(
        chapter=chapter_number, existing_terms=existing_terms,
        prior=continuity if prior_continuity is None else prior_continuity,
        source=chinese_text, translation=translation,
    )
    messages = [
        {"role": "system", "content": STATE_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    def request(operation, ceiling):
        kwargs = {"model": MODEL, "messages": messages,
                  **operation_request_controls(operation, ceiling=ceiling)}
        # Reuse the exact historical 3K request if that slot already exists.
        # Never create a new initial operation to bypass an old uncertain slot.
        if operation == "state_extraction":
            path = paid_request_journal_path(PAID_REQUEST_JOURNAL_DIR, operation, chapter_number)
            if path.exists():
                try:
                    load_matching_journal(path, _chat_request_identity(
                        operation, chapter_number, chinese_text, kwargs,
                        translation_text=translation))
                except PaidRequestJournalError:
                    # Keep the historical ceiling explicit: this is replay
                    # compatibility, not another current-policy definition.
                    legacy = dict(model=MODEL, messages=messages, max_tokens=3000,
                                  extra_body={"thinking": {"type": "disabled"}})
                    load_matching_journal(path, _chat_request_identity(
                        operation, chapter_number, chinese_text, legacy,
                        translation_text=translation))
                    kwargs = legacy
        journaled = journaled_chat_request(operation, chapter_number, chinese_text, kwargs,
                                            translation_text=translation)
        return {**journaled, "request_ceiling": kwargs["max_tokens"]}

    def repair(content, ceiling):
        kwargs = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": GENERIC_JSON_REPAIR_PROMPT +
                 "\n\nRequired structure:\n" + STATE_SCHEMA_HINT},
                {"role": "user", "content": "Repair this malformed JSON:\n\n" + content},
            ],
            **operation_request_controls("state_json_repair", ceiling=ceiling),
        }
        return journaled_chat_request("state_json_repair", chapter_number, chinese_text,
                                      kwargs, translation_text=content)

    result, stats = run_state_extraction(
        request=request, repair=repair, clean_text=clean_json_text,
        salvage=salvage_state_json_without_issues, combine_stats=combine_stats,
        emit=lambda event, **fields: log_event(event, chapter=chapter_number,
                                               stage="state_extraction", **fields),
        policy=STATE_EXTRACTION_POLICY,
    )
    log_event("state_extraction_end", chapter=chapter_number, stage="state_extraction",
              elapsed_seconds=time.perf_counter() - stage_start, **stats_event_fields(stats))
    return result, stats


def local_qa(chapter_number, chinese_text, translation, finish_reason):
    found = []
    if finish_reason != "stop":
        found.append("Unexpected finish reason: " + str(finish_reason))
    if not translation.strip():
        found.append("Translation is empty")
    # Non-whitespace lengths work for both Chinese and Korean sources. A
    # translation below 35% of a meaningful source is severe truncation;
    # short complete prologues have no absolute output-length requirement.
    source_size = sum(not char.isspace() for char in chinese_text)
    translation_size = sum(not char.isspace() for char in translation)
    if translation.strip() and source_size >= 40 and translation_size < source_size * 0.35:
        found.append("Translation suspiciously short")
    commit_qa = translation_commit_qa(
        chinese_text,
        translation,
        glossary["entries"],
        ACTIVE_PROFILE.high_priority_categories,
        approved_reader_facing_cjk_terms=APPROVED_READER_FACING_CJK_TERMS,
        chapter_number=chapter_number,
        source_language=(
            ACTIVE_NOVEL_CONFIG.source_language
            if ACTIVE_NOVEL_CONFIG
            else "zh"
        ),
    )
    remaining_chinese = commit_qa["remaining_cjk_character_count"]
    if remaining_chinese:
        found.append(
            "Residual source script blocks commit: "
            + ", ".join(commit_qa["residual_cjk_fragments"])
        )
    for violation in commit_qa["locked_high_priority_term_violations"]:
        found.append(
            "Locked terminology blocks commit: "
            f"{violation['chinese']} requires {violation['required_english']}"
        )
    if "[TRANSLATION REVIEW REQUIRED]" in translation:
        found.append("Translation contains review marker")
    source_hangul = len(HANGUL_RE.findall(chinese_text))
    return {
        "chapter": chapter_number,
        "source_hangul_characters": source_hangul,
        "remaining_chinese_characters": remaining_chinese,
        "residual_source_script": commit_qa["source_script"],
        "raw_source_script_character_count": commit_qa[
            "raw_source_script_character_count"
        ],
        "raw_source_script_fragments": commit_qa["raw_source_script_fragments"],
        "intentional_reader_facing_character_count": commit_qa[
            "intentional_reader_facing_character_count"
        ],
        "intentional_reader_facing_fragments": commit_qa[
            "intentional_reader_facing_fragments"
        ],
        "blocking_source_script_character_count": commit_qa[
            "blocking_source_script_character_count"
        ],
        "residual_cjk_fragments": commit_qa["residual_cjk_fragments"],
        "locked_high_priority_term_violations": commit_qa[
            "locked_high_priority_term_violations"
        ],
        "commit_allowed": bool(translation.strip()) and commit_qa["commit_allowed"],
        "commit_blockers": commit_qa["commit_blockers"] + (["Translation is empty"] if not translation.strip() else []),
        "translation_characters": len(translation),
        "source_characters": len(chinese_text),
        "issues": found,
    }


LOW_VALUE_TERMS = set()


def normalize_category(category):
    return core_normalize_category(category)


def looks_like_sub_location(chinese, english):
    generic_suffixes = (
        "後門", "后门", "前門", "前门", "後院", "后院", "前院", "大門", "大门",
        "側門", "侧门", "內院", "内院", "外院", "倉庫", "仓库", "廚房", "厨房",
    )
    if chinese.endswith(generic_suffixes):
        return True
    english_lower = english.lower()
    generic_english_parts = (
        "back gate", "front gate", "rear courtyard", "front courtyard",
        "inner courtyard", "outer courtyard", "warehouse", "kitchen",
    )
    return any(part in english_lower for part in generic_english_parts)


def looks_like_generic_object(category, english):
    if category != "artifact":
        return False
    words = ("chair", "table", "mirror", "bowl", "cup", "door", "gate", "box", "basket", "bed", "lamp", "lantern", "pot", "jar")
    return any(word in english.lower() for word in words)


def looks_like_generic_activity(category, english):
    if category != "specialized recurring term":
        return False
    words = ("duty", "patrol", "work", "strategy", "attack", "retreat", "guard duty", "night patrol")
    return any(word in english.lower() for word in words)


def prefilter_candidates(
    new_terms,
    authoritative_source,
    chapter_number,
    *,
    state=None,
    translation="",
    return_preparation=False,
):
    state = dict(state or {"new_terms": list(new_terms)})
    prepared = prepare_glossary_review_candidates(
        novel_id=(
            ACTIVE_NOVEL_CONFIG.id
            if ACTIVE_NOVEL_CONFIG
            else "ming_novel"
        ),
        chapter=chapter_number,
        state=state,
        source_text=authoritative_source,
        translation=translation,
        profile=ACTIVE_PROFILE,
        existing_terms=glossary["entries"],
        resolution_path=candidate_resolution_path(
            Path("data"), chapter_number
        ),
        low_value_terms=LOW_VALUE_TERMS,
    )
    if return_preparation:
        return (
            prepared["candidates"],
            prepared["rejected"],
            prepared["diagnostics"],
        )
    return prepared["candidates"], prepared["rejected"]


TERM_REVIEW_PROMPT_BASE = '\nYou are the conservative terminology reviewer for a Chinese-to-English webnovel.\nReview ONLY new glossary candidates before they enter the persistent glossary.\nProper names normally use established romanization. Descriptive terms should use meaningful English.\nFor each candidate choose exactly one action: accept, correct, reject, contextual, merge.\nIdentify each decision ONLY with the supplied immutable candidate_id. Do not\nrewrite or invent a source key. The engine reattaches the authoritative source key.\naccept = good as proposed.\ncorrect = belongs in glossary but English/romanization is wrong; supply corrected English.\nreject = not worth keeping in master glossary.\ncontextual = needs multiple approved English renderings; supply approved_renderings.\nmerge = variant/alias/damaged-source form of an EXISTING Chinese glossary key; supply merge_with.\nNever contradict a LOCKED existing term. New entries remain provisional unless aliasing a locked entry.\n\nFORMAL NAMED TECHNIQUE / ART STYLE:\nFor categories designated as formal named terms by the selected profile, use the user\'s preferred English capitalization:\ncapitalize the initial letter of EVERY word in the formal technique name.\nExample:\n黑虎掏心 → Black Tiger Seizes The Heart\nDo not return:\nblack tiger seizes the heart\nor:\nBlack Tiger Seizes the Heart\n\nReturn ONLY JSON:\n{"decisions":[{"candidate_id":"source:...","action":"accept","english":"...","category":"...","approved_renderings":[],"merge_with":"","notes":""}],"issues":[]}\n\n\nPROFILE-SPECIFIC REVIEW POLICY:\n'
TERM_REVIEW_PROMPT = None
TERM_REVIEW_MODEL_PROMPT_SUFFIX = '\n\nREQUEST-LOCAL ID TRANSPORT:\nCandidate IDs in this request are short exact aliases such as c01 and c02.\nReturn each supplied short ID exactly. Never return or derive a source: SHA ID.\n'
TERM_REVIEW_MODEL_PROMPT = None
TERM_REVIEW_SCHEMA = '{"decisions":[{"candidate_id":"c01","action":"accept"}],"issues":[]}'


def build_original_glossary_review_request(*, chapter_number, chinese_text, translation,
                                          model_candidates, metadata, existing_relevant,
                                          system_prompt, model, controls):
    user_prompt = f"""
CHAPTER: {chapter_number}
VALID REQUEST-LOCAL CANDIDATE IDS:
{', '.join(metadata['model_candidate_ids'])}
EXISTING RELEVANT GLOSSARY:
{existing_relevant}
NEW CANDIDATES:
{json.dumps(model_candidates, ensure_ascii=False, indent=2)}
CHINESE SOURCE:
{chinese_text}
FINAL ENGLISH:
{translation}
""".strip()
    return dict(model=model, messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}], **controls)


def review_new_terms(
    chapter_number,
    chinese_text,
    translation,
    candidates,
    *,
    recovery_record=None,
    persist_recovery=lambda record: None,
):
    stage_start = time.perf_counter()
    log_event(
        "glossary_review_start",
        chapter=chapter_number,
        stage="glossary_review",
        candidate_count=len(candidates),
    )
    if not candidates:
        stats = zero_stats()
        log_event(
            "glossary_review_end",
            chapter=chapter_number,
            stage="glossary_review",
            elapsed_seconds=time.perf_counter() - stage_start,
            candidate_count=0,
            skipped=True,
            **stats_event_fields(stats),
        )
        return {"decisions": [], "issues": []}, stats
    relevant_entries = relevant_glossary(chinese_text)
    existing_relevant = glossary_to_text(relevant_entries, chapter_number=chapter_number)
    existing_merge_targets = chapter_review_merge_targets(relevant_entries)

    def build_original_request(model_candidates, metadata):
        return build_original_glossary_review_request(
            chapter_number=chapter_number, chinese_text=chinese_text, translation=translation,
            model_candidates=model_candidates, metadata=metadata, existing_relevant=existing_relevant,
            system_prompt=TERM_REVIEW_MODEL_PROMPT, model=MODEL,
            controls=operation_request_controls("chapter_glossary_review"))

    def request_original(model_candidates, metadata):
        kwargs = build_original_request(model_candidates, metadata)
        goat_original = globals().get("goat_original_glossary_review")
        response = (goat_original(kwargs, metadata, translation) if goat_original else
                    call_api_with_retries(**kwargs))
        proof = getattr(response, "_goat_glossary_recovery", None)
        if proof is not None:
            metadata["persist_transport_recovery"](proof)
        content = clean_json_text(response.choices[0].message.content or "")
        stats = usage_and_cost(response.usage)
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            result, repair_stats = repair_json_content(
                f"Glossary review JSON for Chapter {chapter_number}",
                content,
                TERM_REVIEW_SCHEMA,
                operation="chapter_glossary_review_json_repair",
            )
            accepted_operations = stats.get("glossary_review_accepted_operations")
            stats = combine_stats(stats, repair_stats)
            if accepted_operations is not None:
                stats["glossary_review_accepted_operations"] = accepted_operations
        log_event(
            "glossary_review_request_end",
            chapter=chapter_number,
            stage="glossary_review",
            operation_id=metadata["operation_id"],
            request_kind="original",
            **stats_event_fields(stats),
        )
        return {"result": result, "stats": stats}

    def build_review_correction_request(parsed_result, errors, metadata):
        correction_class = metadata.get("correction_class", "semantic")
        if correction_class == "model_schema":
            system_prompt, user_prompt = build_chapter_review_schema_correction_prompt(
                chapter=chapter_number,
                schema=TERM_REVIEW_SCHEMA,
                errors=errors,
                rejected_result=parsed_result,
            )
            correction_label = "schema"
        else:
            system_prompt, user_prompt = build_chapter_review_semantic_correction_prompt(
                chapter=chapter_number,
                schema=TERM_REVIEW_SCHEMA,
                model_candidates=metadata["model_candidates"],
                candidate_sources=metadata["candidate_sources"],
                existing_merge_targets=existing_merge_targets,
                errors=errors,
                rejected_result=parsed_result,
            )
            correction_label = "semantic"
        return dict(
            model=MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            **operation_request_controls(
                "chapter_glossary_review_schema_correction"
                if correction_class == "model_schema" else
                "chapter_glossary_review_semantic_correction_retry"
                if metadata.get("correction_attempt") == 2 else
                "chapter_glossary_review_semantic_correction"
            ),
        )
    def request_review_correction(parsed_result, errors, metadata):
        correction_label = "schema" if metadata.get("correction_class") == "model_schema" else "semantic"
        response = call_api_with_retries(**build_review_correction_request(parsed_result, errors, metadata))
        stats = usage_and_cost(response.usage)
        content = clean_json_text(response.choices[0].message.content or "")
        try:
            result = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ChapterGlossaryReviewFailure(
                f"chapter glossary {correction_label} correction returned malformed JSON"
            ) from exc
        log_event(
            f"glossary_review_{correction_label}_correction_end",
            chapter=chapter_number,
            stage="glossary_review",
            operation_id=metadata["operation_id"],
            request_kind=f"{correction_label}_correction",
            **stats_event_fields(stats),
        )
        return {"result": result, "stats": stats}

    request_original.build_request = build_original_request
    request_review_correction.build_request = build_review_correction_request
    journal_callbacks = globals().get("journal_owned_chapter_review_callbacks")
    if journal_callbacks is not None:
        request_original, request_review_correction = journal_callbacks(request_original, request_review_correction)

    outcome = run_chapter_glossary_review(
        novel_id=ACTIVE_NOVEL_CONFIG.id if ACTIVE_NOVEL_CONFIG else "ming_novel",
        chapter=chapter_number,
        source_text=chinese_text,
        translation=translation,
        candidates=candidates,
        existing_source_keys=glossary["entries"].keys(),
        request_original=request_original,
        request_correction=request_review_correction,
        recovery_record=recovery_record,
        persist_recovery=persist_recovery,
        reconcile_original=request_original if "goat_original_glossary_review" in globals() else None,
    )
    result = outcome["review_result"]
    original_stats = outcome["stats"]
    log_event(
        "glossary_review_end",
        chapter=chapter_number,
        stage="glossary_review",
        elapsed_seconds=time.perf_counter() - stage_start,
        candidate_count=len(candidates),
        recovered_original=outcome["reused_original"],
        recovered_semantic_correction=outcome["reused_correction"],
        semantic_correction_used=len(outcome["stats"].get("operation_ids", [])) > 1,
        **stats_event_fields(original_stats),
    )
    return result, original_stats


def apply_term_review(chapter_number, candidates, review_result):
    added, rejected, corrected, contextualized, merged = [], [], [], [], []
    decisions = {
        item.get("candidate_id", ""): item
        for item in review_result.get("decisions", [])
        if item.get("candidate_id")
    }
    for candidate in candidates:
        chinese = candidate["chinese"]
        candidate_id = candidate.get("candidate_id") or stable_glossary_candidate_id(
            chinese
        )
        decision = decisions.get(candidate_id)
        if decision is None:
            raise RuntimeError(
                f"validated glossary review omitted candidate_id {candidate_id}"
            )
        action = decision.get("action", "accept").strip().lower()
        category = select_profile_category(
            ACTIVE_PROFILE,
            decision.get("category", candidate.get("category", "")),
            candidate.get("category", ""),
        )
        notes = (decision.get("notes", candidate.get("notes", "")) or "").strip()
        provenance = copy.deepcopy(candidate.get("source_provenance", {}))
        if action == "reject":
            rejected.append({"chinese": chinese, "reason": notes or "reviewer rejected"})
            continue
        if action == "merge":
            merge_with = decision.get("merge_with", "").strip()
            if merge_with and merge_with in glossary["entries"]:
                target = glossary["entries"][merge_with]
                glossary["entries"][chinese] = {
                    "english": target.get("english", candidate["english"]),
                    "category": select_profile_category(
                        ACTIVE_PROFILE, target.get("category", category), category
                    ),
                    "status": "locked" if target.get("status") == "locked" else "provisional",
                    "first_chapter": chapter_number,
                    "alias_of": merge_with,
                    "notes": notes or f"Alias/variant of {merge_with}.",
                    "source_provenance": provenance,
                }
                merged.append(f"{chinese} → {merge_with}")
                continue
            action = "accept"
        if action == "contextual":
            approved = [x.strip() for x in decision.get("approved_renderings", []) if isinstance(x, str) and x.strip()]
            if not approved:
                approved = [candidate["english"]]

            if ACTIVE_PROFILE.requires_formal_capitalization(category):
                approved = [
                    format_technique_name(item)
                    for item in approved
                ]

            glossary["entries"][chinese] = {
                "english": "", "approved_renderings": approved, "category": category,
                "status": "contextual", "first_chapter": chapter_number, "notes": notes,
                "source_provenance": provenance,
            }
            contextualized.append(f"{chinese} → {' / '.join(approved)}")
            continue
        english = (decision.get("english", candidate["english"]) or candidate["english"]).strip()

        if ACTIVE_PROFILE.requires_formal_capitalization(category):
            english = format_technique_name(english)

        if action == "correct":
            corrected.append(f"{chinese}: {candidate['english']} → {english}")
        glossary["entries"][chinese] = {
            "english": english, "category": category, "status": "provisional",
            "first_chapter": chapter_number, "notes": notes,
            "source_provenance": provenance,
        }
        added.append(f"{chinese} → {english}")
    return {"added": added, "rejected": rejected, "corrected": corrected, "contextualized": contextualized, "merged": merged}


AUDIT_PROMPT_BASE = '\nYou are performing a conservative periodic audit of a persistent source-to-English webnovel glossary.\nReview ONLY the supplied audit-target entries. LOCKED terms are authoritative and must never be changed.\nFor each target choose exactly one action: keep, correct, reject, contextual, merge.\nCorrect only real semantic, historical, geographic, romanization, consistency, or glossary-value problems.\nIdentify every decision ONLY by its supplied immutable candidate_id. Never\nrewrite, translate, transliterate, normalize, or invent a source-side key.\n\nFORMAL NAMED TECHNIQUE / ART STYLE:\nAny entry in a profile-designated formal named category must use the user\'s preferred capitalization:\ncapitalize the initial letter of EVERY word.\nExample:\nBlack Tiger Seizes The Heart\n\nIf uncertain, choose keep. For merge, merge_with must be an existing Chinese glossary key.\nReturn exactly one concise decision per target. Keep notes under 500 characters.\nReturn at most 5 short issue strings for this chunk.\nReturn ONLY JSON:\n{"decisions":[{"candidate_id":"source:...","action":"keep","english":"...","approved_renderings":[],"merge_with":"","notes":""}],"issues":[]}\n\n\nPROFILE-SPECIFIC AUDIT POLICY:\n'
AUDIT_PROMPT = None
# Transport-only wording is intentionally excluded from the durable audit
# context fingerprint below. Completed checkpoint chunks remain valid because
# their source-backed candidates, semantic policy, and stored internal IDs are
# unchanged.
AUDIT_MODEL_PROMPT_SUFFIX = "\n\nREQUEST-LOCAL ID TRANSPORT:\nCandidate IDs in this request are short exact aliases such as c01 and c02.\nReturn each supplied short ID exactly. Never return or derive a source: SHA ID.\n\nSTRUCTURED ALIAS-FAMILY IDENTITY:\nEach target supplies separate candidate_id, source_key, family_id, family_root,\nand alias_of fields. candidate_id and source_key are the exact member identity.\nfamily_id and the family_root field are grouping context, not merge identities.\nmerge_with MUST be an exact existing source glossary key, MUST differ from the\ncandidate's own source_key, and MUST NOT be an English rendering or family ID.\nText shown as family_root is usable only when that same text independently\nexists as an exact source glossary key; return that exact source key, never a\nfamily identifier or synthesized representative.\nIf an existing alias_of relationship remains correct, choose KEEP; MERGE is\nonly for changing or creating a relationship to another exact source key. If\nuncertain, choose KEEP. Never merge an entry into itself.\n"
AUDIT_MODEL_PROMPT = None
AUDIT_SCHEMA = '{"decisions":[{"candidate_id":"c01","action":"keep"}],"issues":[]}'


def compact_glossary_index(
    max_chars=MAX_AUDIT_GLOSSARY_CHARS,
    allowed_source_keys=None,
    glossary_data=None,
):
    entries = (glossary if glossary_data is None else glossary_data)["entries"]
    allowed = set(entries) if allowed_source_keys is None else set(allowed_source_keys)
    prioritized = sorted(
        (
            (source_key, entry)
            for source_key, entry in entries.items()
            if source_key in allowed
        ),
        key=lambda kv: (
            0 if kv[1].get("status") == "locked" else 1 if kv[1].get("status") == "contextual" else 2,
            kv[1].get("first_chapter", 0),
            kv[0],
        ),
    )
    lines, total = [], 0
    for chinese, entry in prioritized:
        english = entry.get("english", "")
        if entry.get("status") == "contextual":
            english = " / ".join(entry.get("approved_renderings", []))
        line = f"{chinese} → {english} [{entry.get('status','provisional')}; {entry.get('category','')}]"
        if entry.get("alias_of"):
            line += f" [alias_of={entry['alias_of']}]"
        line += "\n"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "".join(lines)


def audit_targets(
    start_chapter,
    end_chapter,
    glossary_data=None,
    *,
    return_integrity=False,
):
    glossary_data = glossary if glossary_data is None else glossary_data
    plan = build_source_integrity_audit_plan(
        glossary_data["entries"],
        chapters,
        start_chapter,
        end_chapter,
        max_contexts=MAX_TERM_CONTEXTS,
        context_radius=TERM_CONTEXT_RADIUS,
    )
    integrity = {
        "counts": plan["counts"],
        "excluded_window_candidates": plan["excluded_window_candidates"],
        "safe_glossary_keys": plan["safe_glossary_keys"],
    }
    return (plan["candidates"], integrity) if return_integrity else plan["candidates"]


def periodic_audit_checkpoint_path(start_chapter, end_chapter):
    return AUDIT_DIR / (
        f"audit_{start_chapter:04d}_{end_chapter:04d}.checkpoint.json"
    )


def build_request_periodic_audit_chunk(start_chapter, end_chapter, glossary_index, targets, metadata):
    user_prompt = f"""
AUDIT RANGE: Chapters {start_chapter}-{end_chapter}
AUDIT CHUNK: {metadata['chunk_index']} of {metadata['total_chunks']}
Return exactly one decision for every supplied target in this chunk, identified
only by its exact request-local candidate_id. Valid IDs for this request are:
{', '.join(metadata['model_candidate_ids'])}
Do not return source: SHA IDs or modify the short IDs.
CURRENT GLOSSARY INDEX:
{glossary_index}
AUDIT TARGETS:
{json.dumps(targets, ensure_ascii=False, indent=2)}
STRUCTURED FAMILY CONTEXT:
{json.dumps(metadata['family_context'], ensure_ascii=False, indent=2)}
""".strip()
    return dict(
        model=MODEL,
        messages=[
            {"role": "system", "content": AUDIT_MODEL_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        **operation_request_controls("periodic_audit"),
    )


def request_periodic_audit_chunk(
    start_chapter,
    end_chapter,
    glossary_index,
    targets,
    metadata,
):
    print(
        f"Audit chunk {metadata['chunk_index']}/{metadata['total_chunks']} "
        f"({len(targets)} candidates; id {metadata['chunk_id']})"
    )
    log_event(
        "audit_chunk_start",
        stage="glossary_audit",
        audit_start_chapter=start_chapter,
        audit_end_chapter=end_chapter,
        chunk_id=metadata["chunk_id"],
        chunk_index=metadata["chunk_index"],
        total_chunks=metadata["total_chunks"],
        candidate_count=len(targets),
    )
    response = call_api_with_retries(**build_request_periodic_audit_chunk(start_chapter, end_chapter, glossary_index, targets, metadata))
    choice = response.choices[0]
    stats = usage_and_cost(response.usage)
    log_event(
        "audit_chunk_end",
        stage="glossary_audit",
        audit_start_chapter=start_chapter,
        audit_end_chapter=end_chapter,
        chunk_id=metadata["chunk_id"],
        chunk_index=metadata["chunk_index"],
        total_chunks=metadata["total_chunks"],
        candidate_count=len(targets),
        finish_reason=choice.finish_reason,
        **stats_event_fields(stats),
    )
    if str(choice.finish_reason or "").lower() == "length":
        print(
            "TRUNCATED AUDIT RESPONSE: output limit reached; "
            "this candidate chunk will be subdivided without JSON repair."
        )
    return {
        "content": choice.message.content or "",
        "finish_reason": choice.finish_reason,
        "stats": stats,
    }


def build_repair_periodic_audit_chunk(malformed_content, metadata):
    prompt = (
        GENERIC_JSON_REPAIR_PROMPT
        + "\n\nRequired structure:\n"
        + AUDIT_SCHEMA
        + "\nUse every request-local candidate ID exactly once: "
        + ", ".join(metadata["model_candidate_ids"])
        + "\nDo not return source: SHA IDs or alter the short IDs."
    )
    return dict(
        model=MODEL,
        messages=[
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": "Repair this single audit chunk JSON:\n\n" + malformed_content,
            },
        ],
        **operation_request_controls("periodic_audit_json_repair"),
    )


def repair_periodic_audit_chunk(malformed_content, metadata):
    print(
        f"Audit chunk {metadata['chunk_id']} returned malformed JSON. "
        "Attempting one bounded repair for this chunk only..."
    )
    response = call_api_with_retries(**build_repair_periodic_audit_chunk(malformed_content, metadata))
    choice = response.choices[0]
    stats = usage_and_cost(response.usage)
    log_event(
        "audit_chunk_json_repair",
        stage="glossary_audit",
        chunk_id=metadata["chunk_id"],
        finish_reason=choice.finish_reason,
        **stats_event_fields(stats),
    )
    return {
        "content": choice.message.content or "",
        "finish_reason": choice.finish_reason,
        "stats": stats,
    }


def build_correct_periodic_audit_chunk_semantics(parsed_result, errors, metadata):
    system_prompt = f"""
You correct only locally identified schema/semantic FIELD errors in the
supplied invalid periodic glossary-audit decision(s). Return a sparse decision
patch containing exactly those supplied candidate IDs and only their explicitly
authorized fields.

Allowed actions: keep, correct, reject, contextual, merge.
Candidate IDs are request-local short aliases such as c01. Use every supplied
candidate_id exactly once. Do not add, omit, replace, normalize, or rewrite
them, and never return a source: SHA ID. Do not return unrelated decisions.
For missing_contextual_renderings, approved_renderings is the ONLY authorized
field. Do NOT return or change action, english, notes, merge_with, category, or
any source identity field. Return only candidate_id plus approved_renderings,
and approved_renderings must be a non-empty array of non-empty strings.
merge_with must be an exact allowed source glossary key and must not equal the
candidate's source_key. Never return a family ID, synthesized representative,
or English rendering. A family_root string is usable only if it independently
appears in the exact allowed source-key list. For an authorized merge-field
error only, if the existing alias relationship is already correct, change
action to keep only when action is explicitly authorized. Return ONLY JSON
shaped like:
{{"decisions":[{{"candidate_id":"c01","approved_renderings":["Current Rendering"]}}]}}
""".strip()
    user_prompt = f"""
AUDIT WINDOW: {metadata['audit_window']}
CHUNK ID: {metadata['chunk_id']}
REQUEST-LOCAL CANDIDATE IDS AND SOURCE KEYS:
{json.dumps(metadata['candidate_sources'], ensure_ascii=False, indent=2)}
STRUCTURED FAMILY/ALIAS CONTEXT:
{json.dumps(metadata['family_context'], ensure_ascii=False, indent=2)}
AUTHORIZED FIELDS BY CANDIDATE:
{json.dumps(metadata['authorized_fields'], ensure_ascii=False, indent=2)}
EXACT ALLOWED MERGE TARGET SOURCE KEYS:
{json.dumps(metadata['allowed_merge_targets'], ensure_ascii=False, indent=2)}
VALIDATOR ERRORS:
{json.dumps(list(errors), ensure_ascii=False, indent=2)}
REJECTED PARSED CHUNK:
{json.dumps(parsed_result, ensure_ascii=False, indent=2)}
""".strip()
    return dict(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        **operation_request_controls("periodic_audit_semantic_correction"),
    )


def correct_periodic_audit_chunk_semantics(parsed_result, errors, metadata):
    print(
        f"Audit chunk {metadata['chunk_id']} has a bounded semantic field error. "
        "Attempting one corrective retry for this chunk only..."
    )
    response = call_api_with_retries(**build_correct_periodic_audit_chunk_semantics(parsed_result, errors, metadata))
    choice = response.choices[0]
    stats = usage_and_cost(response.usage)
    log_event(
        "audit_chunk_semantic_correction",
        stage="glossary_audit",
        chunk_id=metadata["chunk_id"],
        operation_id=metadata["operation_id"],
        finish_reason=choice.finish_reason,
        **stats_event_fields(stats),
    )
    return {
        "content": choice.message.content or "",
        "finish_reason": choice.finish_reason,
        "stats": stats,
    }


def call_periodic_audit(start_chapter, end_chapter):
    targets, source_integrity = audit_targets(
        start_chapter,
        end_chapter,
        return_integrity=True,
    )
    if not targets:
        return (
            {"decisions": [], "issues": []},
            zero_stats(),
            {
                "candidate_set_hash": "",
                "candidate_count": 0,
                "total_chunks": 0,
                "completed_chunks": [],
                "resumed": False,
                "source_integrity": source_integrity,
            },
        )
    glossary_index = compact_glossary_index(
        allowed_source_keys=source_integrity["safe_glossary_keys"]
    )
    config = AuditChunkConfig(
        max_candidates=MAX_AUDIT_CANDIDATES_PER_CHUNK,
        max_serialized_chars=MAX_AUDIT_CHUNK_SERIALIZED_CHARS,
    )
    context_fingerprint = hashlib.sha256(
        (AUDIT_PROMPT + "\n" + glossary_index).encode("utf-8")
    ).hexdigest()
    request_callback = lambda chunk_targets, metadata: request_periodic_audit_chunk(
        start_chapter, end_chapter, glossary_index, chunk_targets, metadata)
    request_callback.build_request = lambda chunk_targets, metadata: build_request_periodic_audit_chunk(
        start_chapter, end_chapter, glossary_index, chunk_targets, metadata)
    repair_periodic_audit_chunk.build_request = build_repair_periodic_audit_chunk
    correct_periodic_audit_chunk_semantics.build_request = build_correct_periodic_audit_chunk_semantics
    run = run_chunked_audit(
        candidates=targets,
        all_glossary_keys=source_integrity["safe_glossary_keys"],
        start_chapter=start_chapter,
        end_chapter=end_chapter,
        checkpoint_path=periodic_audit_checkpoint_path(start_chapter, end_chapter),
        request_chunk=request_callback,
        repair_chunk=repair_periodic_audit_chunk,
        correct_semantic_chunk=correct_periodic_audit_chunk_semantics,
        config=config,
        context_fingerprint=context_fingerprint,
        supersede_incompatible_policy=True,
        glossary_entries=glossary["entries"],
    )
    leaf_chunks = [
        chunk
        for chunk in run["checkpoint"]["chunks"]
        if chunk.get("status") != "split"
    ]
    chunking = {
        "policy_version": config.policy_version,
        "candidate_set_hash": run["candidate_set_hash"],
        "candidate_count": len(targets),
        "total_chunks": len(leaf_chunks),
        "completed_chunks": [
            chunk["chunk_id"]
            for chunk in leaf_chunks
            if chunk.get("status") == "completed"
        ],
        "api_request_count": run["stats"].get("api_request_count", 0),
        "resumed": run["resumed"],
        "checkpoint_file": str(periodic_audit_checkpoint_path(start_chapter, end_chapter)),
        "source_integrity": source_integrity,
        "superseded_checkpoint": run["checkpoint"].get("superseded_checkpoint", ""),
    }
    from production_periodic_audit_finalization import authoritative_audit_stats
    # Isolated historical adapters own their accounting policy and already
    # reconstruct their aggregate after this provider-neutral return boundary.
    provider = None if globals().get("_experimental_runtime") is not None else progress.get("provider")
    stats = authoritative_audit_stats(run, combine_stats, provider=provider)
    return run["audit_result"], stats, chunking


def apply_periodic_audit(
    start_chapter,
    end_chapter,
    audit_result,
    glossary_data=None,
    targets=None,
):
    glossary_data = glossary if glossary_data is None else glossary_data
    entries = glossary_data["entries"]

    kept = []
    corrected = []
    rejected = []
    contextualized = []
    merged = []
    retroactive_replacements = []

    decisions = {
        item.get(
            "chinese",
            "",
        ): item
        for item in audit_result.get(
            "decisions",
            [],
        )
        if item.get(
            "chinese"
        )
    }

    targets = targets or audit_targets(start_chapter, end_chapter, glossary_data)
    for target in targets:
        chinese = target[
            "chinese"
        ]

        if chinese not in entries:
            continue

        entry = entries[
            chinese
        ]

        if entry.get(
            "status"
        ) == "locked":
            continue

        decision = decisions.get(
            chinese,
            {
                "action": "keep",
                "notes": (
                    "Auditor omitted entry; "
                    "kept conservatively."
                ),
            },
        )

        action = (
            decision.get(
                "action",
                "keep",
            )
            .strip()
            .lower()
        )

        notes = (
            decision.get(
                "notes",
                "",
            )
            or ""
        ).strip()

        if action == "reject":
            del entries[
                chinese
            ]

            rejected.append(
                chinese
            )

            # Removing a glossary entry does NOT rewrite previous prose.
            # Rejection means "do not enforce globally in future", not that
            # every previous English phrase was necessarily wrong.
            continue

        if action == "merge":
            merge_with = (
                decision.get(
                    "merge_with",
                    "",
                )
                .strip()
            )

            if (
                merge_with
                and merge_with in entries
                and merge_with != chinese
            ):
                target_entry = entries[
                    merge_with
                ]

                old_english = entry.get(
                    "english",
                    "",
                )

                new_english = target_entry.get(
                    "english",
                    old_english,
                )

                category = target_entry.get(
                    "category",
                    entry.get(
                        "category",
                        "",
                    ),
                )

                entry[
                    "english"
                ] = new_english

                entry[
                    "category"
                ] = category

                entry[
                    "alias_of"
                ] = merge_with

                entry[
                    "status"
                ] = (
                    "locked"
                    if target_entry.get(
                        "status"
                    )
                    == "locked"
                    else "provisional"
                )

                if notes:
                    entry[
                        "notes"
                    ] = notes

                merged.append(
                    f"{chinese} → {merge_with}"
                )

                if (
                    old_english
                    and new_english
                    and old_english != new_english
                ):
                    retroactive_replacements.append(
                        {
                            "chinese": chinese,
                            "old_english": old_english,
                            "new_english": new_english,
                            "category": category,
                            "reason": (
                                f"Periodic audit merged {chinese} "
                                f"into {merge_with}."
                            ),
                        }
                    )

                continue

            action = "keep"

        if action == "contextual":
            approved = [
                x.strip()
                for x in decision.get(
                    "approved_renderings",
                    [],
                )
                if isinstance(
                    x,
                    str,
                )
                and x.strip()
            ]

            if ACTIVE_PROFILE.requires_formal_capitalization(
                entry.get("category")
            ):
                approved = [
                    format_technique_name(
                        item
                    )
                    for item in approved
                ]

            if approved:
                entry[
                    "english"
                ] = ""

                entry[
                    "approved_renderings"
                ] = approved

                entry[
                    "status"
                ] = "contextual"

                if notes:
                    entry[
                        "notes"
                    ] = notes

                contextualized.append(
                    f"{chinese} → "
                    f"{' / '.join(approved)}"
                )

                # Do not auto-rewrite old chapters for contextual terms.
                # Picking among multiple approved renderings requires context.
                continue

            action = "keep"

        if action == "correct":
            new_english = (
                decision.get(
                    "english",
                    "",
                )
                or ""
            ).strip()

            if ACTIVE_PROFILE.requires_formal_capitalization(
                entry.get("category")
            ):
                new_english = (
                    format_technique_name(
                        new_english
                    )
                )

            if new_english:
                old_english = entry.get(
                    "english",
                    "",
                )

                category = entry.get(
                    "category",
                    "",
                )

                entry[
                    "english"
                ] = new_english

                entry[
                    "status"
                ] = "provisional"

                if notes:
                    entry[
                        "notes"
                    ] = notes

                corrected.append(
                    f"{chinese}: "
                    f"{old_english} → {new_english}"
                )

                if (
                    old_english
                    and old_english != new_english
                ):
                    retroactive_replacements.append(
                        {
                            "chinese": chinese,
                            "old_english": old_english,
                            "new_english": new_english,
                            "category": category,
                            "reason": (
                                "Periodic glossary audit correction."
                            ),
                        }
                    )

                continue

            action = "keep"

        kept.append(
            chinese
        )

    return {
        "kept": kept,
        "corrected": corrected,
        "rejected": rejected,
        "contextualized": contextualized,
        "merged": merged,
        "retroactive_replacements": retroactive_replacements,
    }


def create_checkpoint(chapter_number):
    checkpoint_dir = BACKUP_DIR / f"checkpoint_{chapter_number:04d}"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    for path in (GLOSSARY_FILE, PROGRESS_FILE, CONTINUITY_FILE, ISSUES_FILE, AUDIT_HISTORY_FILE):
        if path.exists():
            shutil.copy2(path, checkpoint_dir / path.name)


def finalize_periodic_audit(start_chapter, end_chapter, audit_result, audit_stats, chunking):
    """Local-only publication seam shared with completed-audit recovery."""
    from types import SimpleNamespace
    from production_periodic_audit_finalization import finalize_periodic_audit as finalize
    return finalize(SimpleNamespace(**globals()), start_chapter, end_chapter, audit_result, audit_stats, chunking)


def run_periodic_audit(
    start_chapter,
    end_chapter,
):
    audit_started = time.perf_counter()
    log_event(
        "audit_start",
        stage="glossary_audit",
        audit_start_chapter=start_chapter,
        audit_end_chapter=end_chapter,
    )
    print()
    print("=" * 70)
    print(
        f"AUTOMATIC GLOSSARY AUDIT — "
        f"CHAPTERS {start_chapter}-{end_chapter}"
    )
    print("=" * 70)

    from production_periodic_audit_finalization import load_intent
    from types import SimpleNamespace
    recovery = autonomous_audit_recovery_supervisor(start_chapter, end_chapter)
    intent = load_intent(SimpleNamespace(**globals()), start_chapter, end_chapter)
    if intent is not None:
        audit_result, audit_stats, chunking = (intent["audit_result"], intent["audit_stats"], intent["chunking"])
    else:
        audit_result, audit_stats, chunking = recovery.run("audit_acquisition",
            lambda: call_periodic_audit(start_chapter, end_chapter))

    def reconcile_local_publication():
        saved = load_intent(SimpleNamespace(**globals()), start_chapter, end_chapter)
        if saved is None:
            raise ProductionIntegrityError("AUT-1 audit finalization has no durable intent")
        # The finalizer proves exact novel/range, old/new state, accounting,
        # policy and transaction prefixes before any local write.
        return finalize_periodic_audit(start_chapter, end_chapter,
            saved["audit_result"], saved["audit_stats"], saved["chunking"])

    result = recovery.run("audit_publication",
        lambda: finalize_periodic_audit(start_chapter, end_chapter, audit_result, audit_stats, chunking),
        recover_local=reconcile_local_publication)
    changes = result["audit_record"]["changes"]
    retroactive_result = result["audit_record"]["retroactive"]

    print(
        f"Kept: "
        f"{len(changes['kept'])}"
    )

    print(
        f"Corrected: "
        f"{len(changes['corrected'])}"
    )

    print(
        f"Rejected/removed: "
        f"{len(changes['rejected'])}"
    )

    print(
        f"Contextualized: "
        f"{len(changes['contextualized'])}"
    )

    print(
        f"Merged aliases: "
        f"{len(changes['merged'])}"
    )

    print(
        f"Retroactive chapters changed: "
        f"{len(retroactive_result['chapters_changed'])}"
    )

    print(
        f"Retroactive exact replacements: "
        f"{retroactive_result['replacement_count']}"
    )

    print(
        f"Audit cost: "
        f"${audit_stats['cost_usd']:.6f}"
    )

    print(
        f"Checkpoint saved: "
        f"{BACKUP_DIR / f'checkpoint_{end_chapter:04d}'}"
    )

    print("=" * 70)
    log_event(
        "audit_end",
        stage="glossary_audit",
        audit_start_chapter=start_chapter,
        audit_end_chapter=end_chapter,
        elapsed_seconds=time.perf_counter() - audit_started,
        **stats_event_fields(audit_stats),
    )


def describe_api_error(exc):
    text = str(exc).lower()
    if any(x in text for x in ("balance", "credit", "billing", "insufficient")):
        return "The API may have run out of balance/credit. Add credit and run the same script again."
    return "Check the API error above. After resolving it, run the same script again."


def pending_paths(chapter_number):
    return (
        PENDING_DIR / f"{chapter_number:04d}.md",
        PENDING_DIR / f"{chapter_number:04d}.json",
    )


def persist_pending_meta(meta_path, value, *, stage):
    """Persist pending metadata with truthful local-failure classification."""
    try:
        save_json(meta_path, value)
    except Exception as exc:
        raise LocalPersistenceError(
            f"{stage} pending metadata persistence failed: {meta_path}"
        ) from exc


def reconcile_stale_completed_pending():
    """Remove only exact pending duplicates left after a durable commit."""
    completed = set(progress.get("completed", []))
    for pending_file in PENDING_DIR.glob("*.md"):
        if not pending_file.stem.isdigit():
            continue
        chapter_number = int(pending_file.stem)
        if chapter_number not in completed:
            continue
        if not pending_matches_committed(chapter_number, PENDING_DIR, OUTPUT_DIR):
            continue
        _, pending_meta_file = pending_paths(chapter_number)
        pending_file.unlink()
        if pending_meta_file.exists():
            pending_meta_file.unlink()
        print(f"Removed stale pending copy for committed Chapter {chapter_number}.")
        log_event(
            "recovered_pending_stage",
            chapter=chapter_number,
            stage="stale_committed_cleanup",
        )


def perform_startup_self_check():
    errors = startup_integrity_errors(progress, OUTPUT_DIR, PENDING_DIR)
    if errors:
        message = "; ".join(errors)
        error = ProductionIntegrityError(
            "Startup integrity check failed: " + message
        )
        log_error(error, stage="startup_self_check")
        raise error
    log_event("startup_self_check", stage="startup", status="passed")


def log_glossary_candidate_preparation(chapter_number, diagnostics):
    """Record bounded candidate identity diagnostics without chapter prose."""
    log_event(
        "glossary_candidate_preparation",
        chapter=chapter_number,
        stage="glossary_review",
        preparation_version=diagnostics.get("preparation_version", ""),
        raw_candidate_count=diagnostics.get("raw_candidate_count", 0),
        canonical_candidate_count=diagnostics.get("canonical_candidate_count", 0),
        exact_duplicates_removed=diagnostics.get("exact_duplicates_removed", 0),
        normalized_duplicates_removed=diagnostics.get(
            "semantic_duplicates_removed", 0
        ),
        conflict_count=diagnostics.get("conflict_count", 0),
        candidate_ids=list(diagnostics.get("candidate_ids", [])),
        candidates=list(diagnostics.get("candidates", [])),
        collapsed_groups=list(diagnostics.get("collapsed_groups", [])),
        original_conflict_count=diagnostics.get("original_conflict_count", 0),
        resolved_conflict_count=diagnostics.get("resolved_conflict_count", 0),
        applied_resolutions=list(diagnostics.get("applied_resolutions", [])),
        resolution_record_sha256=diagnostics.get(
            "resolution_record_sha256", ""
        ),
    )


def report_glossary_candidate_resolutions(chapter_number, diagnostics):
    """Render bounded, auditable details for explicit human approvals."""
    applied = list(diagnostics.get("applied_resolutions", []))
    if not applied:
        return
    print("\n" + "=" * 70)
    print("GLOSSARY CANDIDATE CONFLICT RESOLUTION APPLIED")
    print("=" * 70)
    print(f"Chapter: {chapter_number}")
    for resolution in applied:
        approved = dict(resolution.get("approved_resolution", {}))
        print(f"Source: {resolution.get('source_key', '')}")
        print(
            "Conflict fingerprint: "
            f"{resolution.get('conflict_fingerprint', '')}"
        )
        print("Selected:")
        print(f"  English: {approved.get('english', '')}")
        print(f"  Category: {approved.get('category', '')}")
        print(f"  Status: {approved.get('status', '')}")
    print(
        "Resolution record: "
        f"{candidate_resolution_path(Path('data'), chapter_number)}"
    )
    print(
        "Original candidates: "
        f"{diagnostics.get('raw_candidate_count', 0)}"
    )
    print(
        "Canonical candidates after resolution: "
        f"{diagnostics.get('canonical_candidate_count', 0)}"
    )
    print("=" * 70)


def attempt_automatic_glossary_candidate_resolution(
    chapter_number,
    chinese_text,
    translation,
    state,
    meta,
    pending_meta_file,
    identity_error,
):
    """Run the opt-in tiered selector while retaining pending state verbatim."""
    config = (
        ACTIVE_NOVEL_CONFIG.glossary_candidate_auto_resolution
        if ACTIVE_NOVEL_CONFIG
        else None
    )

    def persist_recovery(record):
        meta[ADJUDICATION_RECOVERY_KEY] = copy.deepcopy(dict(record))
        save_json(pending_meta_file, meta)

    result = resolve_candidate_conflicts_automatically(
        novel_id=(
            ACTIVE_NOVEL_CONFIG.id if ACTIVE_NOVEL_CONFIG else "ming_novel"
        ),
        chapter=chapter_number,
        pending_identity=pending_content_identity(translation),
        state_identity=state_extraction_identity(state),
        conflicts=list(identity_error.diagnostics.get("conflicts", [])),
        source_text=chinese_text,
        translation=translation,
        existing_glossary=glossary["entries"],
        config=config,
        resolution_path=candidate_resolution_path(Path("data"), chapter_number),
        recovery_record=meta.get(ADJUDICATION_RECOVERY_KEY),
        persist_recovery=persist_recovery,
        request_api=_auxiliary_request_api("glossary_candidate_conflict_adjudication", chapter_number, chinese_text, translation),
        is_operation_accounted=lambda operation_id: operation_id in progress.get(
            "accounted_api_operation_ids", []
        ),
        account_api_usage=account_glossary_candidate_conflict_adjudication,
    )
    if result.get("status") == "resolved":
        print("\n" + "=" * 70)
        print("GLOSSARY CANDIDATE CONFLICT AUTO-RESOLVED")
        print("=" * 70)
        print(f"Chapter: {chapter_number}")
        print(f"Method: {result.get('method', '')}")
        print(
            "Bounded adjudication API calls: "
            f"{1 if result.get('api_called') else 0}"
        )
        record = result.get("created", {}).get("record", {})
        for entry in record.get("resolutions", []):
            print(
                "Selected existing proposal: "
                f"{entry.get('selected_semantic_fingerprint', '')}"
            )
            print(f"Reason: {entry.get('rationale', '')}")
        recovery = result.get("recovery_record", {})
        stats = recovery.get("stats", {}) if isinstance(recovery, dict) else {}
        if result.get("method") == "model_adjudicated":
            print(f"Adjudication cost: ${stats.get('cost_usd', 0):.6f}")
        print("=" * 70)
        log_event(
            "glossary_candidate_conflict_auto_resolved",
            chapter=chapter_number,
            stage="glossary_review",
            method=result.get("method", ""),
            api_called=bool(result.get("api_called")),
            operation_id=result.get("operation_id", ""),
            resolution_record_sha256=record.get("record_sha256", ""),
            **stats_event_fields(stats),
        )
    return result


def stop_for_unresolved_automatic_glossary_candidate_conflict(
    chapter_number, result, error, pending_file
):
    print("\n" + "=" * 70)
    print("GLOSSARY CANDIDATE CONFLICT REQUIRES MANUAL RESOLUTION")
    print("=" * 70)
    print(f"Chapter: {chapter_number}")
    print("Automatic deterministic resolution: insufficient evidence")
    reason = str(result.get("reason", result.get("status", "unavailable")))
    print(f"Bounded adjudication: {reason}")
    print(f"Translation/state preserved at: {pending_file}")
    print("Progress unchanged")
    print("Manual resolution command remains available.")
    print("=" * 70)
    log_event(
        "glossary_candidate_conflict_auto_unresolved",
        chapter=chapter_number,
        stage="glossary_review",
        reason=reason,
        api_called=bool(result.get("api_called")),
        operation_id=result.get("operation_id", ""),
        diagnostics=dict(getattr(error, "diagnostics", {}) or {}),
    )
    return False


def stop_for_glossary_candidate_resolution_error(
    chapter_number, error, pending_file
):
    """Fail closed on a malformed, stale, or inapplicable approval record."""
    diagnostics = dict(getattr(error, "diagnostics", {}) or {})
    print("\n" + "=" * 70)
    print("GLOSSARY CANDIDATE CONFLICT RESOLUTION INVALID")
    print("=" * 70)
    print(f"Chapter: {chapter_number}")
    print(f"Code: {getattr(error, 'code', 'resolution_invalid')}")
    print(f"Reason: {error}")
    print(
        "Resolution record: "
        f"{candidate_resolution_path(Path('data'), chapter_number)}"
    )
    print(f"Paid translation preserved at: {pending_file}")
    print("No new glossary-review API call was made.")
    print("No progress advanced.")
    print("=" * 70)
    log_event(
        "glossary_candidate_resolution_invalid",
        chapter=chapter_number,
        stage="glossary_review",
        error_type=type(error).__name__,
        error_code=str(getattr(error, "code", "") or ""),
        error_message=str(error),
        diagnostics=diagnostics,
        resolution_record=str(
            candidate_resolution_path(Path("data"), chapter_number)
        ),
    )
    return False


def stop_for_glossary_candidate_identity_conflict(
    chapter_number, error, pending_file
):
    """Render one known integrity failure as a controlled production stop."""
    diagnostics = dict(getattr(error, "diagnostics", {}) or {})
    conflicts = list(diagnostics.get("conflicts", []))
    print("\n" + "=" * 70)
    print("GLOSSARY CANDIDATE IDENTITY CONFLICT")
    print("=" * 70)
    print(f"Chapter: {chapter_number}")
    print(f"Original candidates: {diagnostics.get('raw_candidate_count', 0)}")
    print(
        "Canonical candidates: "
        f"{diagnostics.get('canonical_candidate_count', 0)}"
    )
    print(f"Conflicting identities: {len(conflicts)}")
    for conflict in conflicts:
        source_term = str(conflict.get("source_term", "") or "")
        code = str(conflict.get("code", "candidate_identity_conflict") or "")
        positions = conflict.get("raw_positions", [])
        detail = f"- {code}"
        if source_term:
            detail += f": {source_term}"
        if positions:
            detail += f" (candidate positions: {positions})"
        print(detail)
        differing_fields = list(
            conflict.get("differing_semantic_fields", [])
        )
        if differing_fields:
            print(
                "  Differing semantic fields: "
                + ", ".join(str(value)[:80] for value in differing_fields)
            )
        for proposal in list(conflict.get("proposals", []))[:6]:
            if not isinstance(proposal, dict):
                continue
            print(
                "  Proposal "
                f"{proposal.get('raw_position', '?')}: "
                f"english={str(proposal.get('english', ''))[:120]!r}; "
                f"category={str(proposal.get('category', ''))[:80]!r}; "
                f"status={str(proposal.get('status', ''))[:40]!r}"
            )
    print(f"Paid translation preserved at: {pending_file}")
    print("No new glossary-review API call was made.")
    print("No progress advanced.")
    print("=" * 70)
    log_event(
        "glossary_candidate_identity_conflict",
        chapter=chapter_number,
        stage="glossary_review",
        error_type=type(error).__name__,
        error_code=str(getattr(error, "code", "") or ""),
        error_message=str(error),
        diagnostics=diagnostics,
    )
    return False


def reconcile_reviewed_partial_commit_tail(chapter_number):
    """Finish only the durable commit tail for a proven reviewed partial commit."""

    global glossary, continuity, issues_data

    final_file = OUTPUT_DIR / f"{chapter_number:04d}.md"
    pending_file, pending_meta_file = pending_paths(chapter_number)
    qa_file = QA_DIR / f"{chapter_number:04d}.json"
    meta = load_json(pending_meta_file)

    evidence = inspect_reviewed_partial_commit_evidence(
        chapter_number=chapter_number,
        pending_file=pending_file,
        pending_meta_file=pending_meta_file,
        final_file=final_file,
        qa_file=qa_file,
        progress=progress,
        meta=meta,
    )
    if evidence["mode"] != PARTIAL_COMMIT_RECOVERY_MODE:
        raise PartialCommitReconciliationError(
            "partial commit reconciler requires proven reviewed commit-tail evidence"
        )

    eligible_candidates = copy.deepcopy(meta["eligible_candidates"])
    prefilter_rejected = copy.deepcopy(meta["prefilter_rejected"])
    review_result = copy.deepcopy(meta["review_result"])
    state = copy.deepcopy(meta["state"])
    qa = copy.deepcopy(meta["local_qa"])

    candidate_keys = []
    seen_candidate_keys = set()
    for candidate in eligible_candidates:
        if not isinstance(candidate, dict):
            raise PartialCommitReconciliationError(
                "reviewed partial commit contains a malformed eligible candidate"
            )
        source_key = str(candidate.get("chinese", "") or "").strip()
        if not source_key:
            raise PartialCommitReconciliationError(
                "reviewed partial commit candidate has no source key"
            )
        if source_key in seen_candidate_keys:
            raise PartialCommitReconciliationError(
                f"reviewed partial commit repeats candidate source key {source_key!r}"
            )
        seen_candidate_keys.add(source_key)
        candidate_keys.append(source_key)

    current_glossary = copy.deepcopy(glossary)
    if not isinstance(current_glossary.get("entries"), dict):
        raise PartialCommitReconciliationError(
            "current glossary entries are not a JSON object"
        )

    # Reconstruct the deterministic post-review state on scratch copies.
    # Eligible candidates were new when reviewed; remove only those source keys
    # from the scratch baseline so a prior partial glossary write cannot change
    # candidate preparation semantics during reconciliation.
    scratch_glossary = copy.deepcopy(current_glossary)
    for source_key in candidate_keys:
        scratch_glossary["entries"].pop(source_key, None)
    scratch_issues = copy.deepcopy(issues_data)

    source_chapter = chapter_map.get(chapter_number)
    if not isinstance(source_chapter, dict):
        raise PartialCommitReconciliationError(
            "reviewed partial commit source chapter is unavailable"
        )
    chinese_text = str(source_chapter.get("text", "") or "")
    translation = pending_file.read_text(encoding="utf-8")

    try:
        prior_continuity = saved_continuity_baseline(meta.get(STATE_CHECKPOINT_IDENTITY_KEY))
        if prior_continuity["latest_chapter"] != progress.get("last_completed"):
            raise SavedCheckpointIdentityError("prior continuity does not match pre-commit progress")
        scratch_continuity = copy.deepcopy(prior_continuity)
        require_saved_checkpoint_identity(
            saved=meta.get(STATE_CHECKPOINT_IDENTITY_KEY),
            expected=current_state_checkpoint_identity(
                chapter_number,
                chinese_text,
                translation,
                glossary_entries=scratch_glossary["entries"],
                prior_continuity=prior_continuity,
            ),
            label="reviewed partial commit saved state extraction",
        )
        require_saved_checkpoint_identity(
            saved=meta.get(REVIEW_CHECKPOINT_IDENTITY_KEY),
            expected=current_review_checkpoint_identity(
                chapter_number,
                chinese_text,
                translation,
                eligible_candidates,
                glossary_entries=scratch_glossary["entries"],
            ),
            label="reviewed partial commit saved glossary review",
        )
    except SavedCheckpointIdentityError as exc:
        raise PartialCommitReconciliationError(
            "reviewed partial commit saved checkpoint identity is stale or unbound: "
            f"{exc}"
        ) from exc

    # Revalidate the durable reviewed decision set against the reconstructed
    # pre-commit glossary baseline before using it to derive any expected state.
    try:
        review_result = validate_glossary_review_result(
            review_result,
            eligible_candidates,
            scratch_glossary["entries"].keys(),
        )
    except ValueError as exc:
        raise PartialCommitReconciliationError(
            "saved reviewed glossary decisions no longer validate against "
            "the reconstructed pre-commit glossary"
        ) from exc

    original_glossary = glossary
    try:
        glossary = scratch_glossary
        glossary_changes = finalize_reviewed_chapter_state(
            chapter_number=chapter_number,
            eligible_candidates=eligible_candidates,
            review_result=review_result,
            prefilter_rejected=prefilter_rejected,
            state=state,
            qa=qa,
            continuity=scratch_continuity,
            issues_data=scratch_issues,
            apply_term_review=apply_term_review,
            replace_chapter_issues=replace_chapter_issues,
        )
        expected_glossary = copy.deepcopy(glossary)
    finally:
        glossary = original_glossary

    current_entries = current_glossary["entries"]
    expected_entries = expected_glossary["entries"]
    for source_key in candidate_keys:
        actual_present = source_key in current_entries
        expected_present = source_key in expected_entries
        if actual_present:
            if not expected_present or current_entries[source_key] != expected_entries[source_key]:
                raise PartialCommitReconciliationError(
                    "current glossary contains divergent partially committed "
                    f"entry for {source_key!r}"
                )

    try:
        require_continuity_transition(current=continuity, prior=prior_continuity,
            chapter=chapter_number, summary=state["continuity_summary"])
    except SavedCheckpointIdentityError as exc:
        raise PartialCommitReconciliationError(str(exc)) from exc
    if continuity == scratch_continuity and current_glossary != expected_glossary:
        raise PartialCommitReconciliationError("advanced continuity has no exact preceding glossary commit")

    actual_target_issues = [
        item
        for item in issues_data.get("issues", [])
        if isinstance(item, dict) and item.get("chapter") == chapter_number
    ]
    expected_target_issues = [
        item
        for item in scratch_issues.get("issues", [])
        if isinstance(item, dict) and item.get("chapter") == chapter_number
    ]
    if actual_target_issues and actual_target_issues != expected_target_issues:
        raise PartialCommitReconciliationError(
            "current target-chapter issues diverge from the reviewed pending state"
        )
    if actual_target_issues and continuity != scratch_continuity:
        raise PartialCommitReconciliationError("advanced issues have no preceding continuity commit")

    translation_stats = reprice_saved_stats_if_needed(
        meta["translation_stats"]
    )
    state_stats = reprice_saved_stats_if_needed(meta["state_stats"])
    review_stats = reprice_saved_stats_if_needed(meta["review_stats"])

    # All proof checks above are read-only. Only after they succeed do we
    # publish the deterministic expected in-memory state used by the commit tail.
    glossary.clear()
    glossary.update(expected_glossary)
    continuity.clear()
    continuity.update(scratch_continuity)
    issues_data.clear()
    issues_data.update(scratch_issues)

    chapter_stats = commit_reconciled_reviewed_chapter_tail(
        chapter_number=chapter_number,
        translation_stats=translation_stats,
        state_stats=state_stats,
        review_stats=review_stats,
        progress=progress,
        glossary=glossary,
        continuity=continuity,
        issues_data=issues_data,
        audit_history=audit_history,
        final_file=final_file,
        pending_file=pending_file,
        pending_meta_file=pending_meta_file,
        glossary_file=GLOSSARY_FILE,
        continuity_file=CONTINUITY_FILE,
        issues_file=ISSUES_FILE,
        audit_history_file=AUDIT_HISTORY_FILE,
        progress_file=PROGRESS_FILE,
        combine_stats=combine_stats,
        save_json=save_json,
        commit_chapter_accounting=commit_chapter_accounting,
    )

    print(f"\nChapter {chapter_number} partial commit tail reconciled.")
    print(f"Glossary accepted: {len(glossary_changes['added'])}")
    print(f"Glossary corrected: {len(glossary_changes['corrected'])}")
    print(f"Glossary contextualized: {len(glossary_changes['contextualized'])}")
    print(f"Glossary merged aliases: {len(glossary_changes['merged'])}")
    print(f"Glossary rejected: {len(glossary_changes['rejected'])}")
    log_event(
        "partial_commit_reconciled",
        chapter=chapter_number,
        stage="committed",
        recovery_mode=PARTIAL_COMMIT_RECOVERY_MODE,
        final_sha256=evidence["final_sha256"],
        qa_sha256=evidence["qa_sha256"],
        **stats_event_fields(chapter_stats),
    )
    return True


def autonomous_audit_recovery_supervisor(start_chapter, end_chapter):
    # Diagnostic range identity is not a source proof or a grant to publish.
    # Historical ranges need not start in the current chapter map. Acquisition
    # and the durable finalizer retain exact source/intent/transaction proofs.
    return RecoverySupervisor(
        identity={
            "scope": "audit_range_diagnostics",
            "novel": ACTIVE_NOVEL_CONFIG.id if ACTIVE_NOVEL_CONFIG else "legacy_single_novel",
            "start_chapter": start_chapter, "end_chapter": end_chapter,
        },
        emit=lambda event, **fields: log_event(event, audit_start_chapter=start_chapter,
            audit_end_chapter=end_chapter, last_completed=progress.get("last_completed"),
            next_chapter=progress.get("next_chapter"), **fields),
    )


def autonomous_recovery_supervisor(chapter_number):
    source = chapter_map[chapter_number]["text"]
    return RecoverySupervisor(
        identity={
            "novel": ACTIVE_NOVEL_CONFIG.id if ACTIVE_NOVEL_CONFIG else "legacy_single_novel",
            "chapter": chapter_number, "end_chapter": None,
            "source_sha256": identity_sha256(source),
            "model": MODEL, "profile": ACTIVE_PROFILE.id,
            "policy_sha256": current_production_operation_policy().identity_sha256(),
        },
        emit=lambda event, **fields: log_event(event, chapter=chapter_number,
            last_completed=progress.get("last_completed"),
            next_chapter=progress.get("next_chapter"), **fields),
    )


def process_chapter(chapter_number):
    chapter_started = time.perf_counter()
    chapter = chapter_map[chapter_number]
    chinese_text = chapter["text"]
    final_file = OUTPUT_DIR / f"{chapter_number:04d}.md"
    pending_file, pending_meta_file = pending_paths(chapter_number)
    if chapter_number in progress["completed"]:
        return True
    if final_file.exists():
        # Startup permits only byte-identical final+pending as a potentially
        # recoverable ahead-of-progress state. Never rerun translation/state/
        # review work in that state; the reviewed commit-tail reconciler proves
        # the stronger metadata/QA/associated-state contract before any write.
        return reconcile_reviewed_partial_commit_tail(chapter_number)
    recovery = autonomous_recovery_supervisor(chapter_number)
    try:
        selected = recovery.run("translation", lambda: obtain_translation(
            chapter_number,
            PENDING_DIR,
            lambda: translate_chapter(chapter),
        ))
    except (
        APIError,
        APIConnectionError,
        APITimeoutError,
        RateLimitError,
    ) as exc:
        log_error(exc, chapter=chapter_number, stage="translation_provider")
        print("\n" + "=" * 70)
        print("PROVIDER/REQUEST ERROR DURING TRANSLATION")
        print("=" * 70)
        print(f"Chapter: {chapter_number}\nError: {exc}\n")
        print(describe_api_error(exc))
        return False
    except NonProviderProductionError as exc:
        log_error(exc, chapter=chapter_number, stage="translation_local")
        print("\n" + "=" * 70)
        print("LOCAL/RECOVERY ERROR DURING TRANSLATION")
        print("=" * 70)
        print(f"Chapter: {chapter_number}\nError: {exc}\n")
        print(describe_non_provider_error(exc))
        return False

    meta = selected["meta"]
    if selected["source"] == "pending":
        translation = selected["translation"]
        translation_stats = reprice_saved_stats_or_raise(
            meta.get(
                "translation_stats",
                zero_stats(),
            ),
            stage="translation_recovery",
        )
        elapsed = meta.get("elapsed_seconds", 0.0)
        finish_reason = meta.get("finish_reason", "stop")
        print(f"\nRecovered pending translation for Chapter {chapter_number}.")
        print("The translation will NOT be generated again.")
        log_event(
            "recovered_pending_stage",
            chapter=chapter_number,
            stage=meta.get("stage", "translated"),
            **stats_event_fields(translation_stats),
        )
    else:
        generated = selected["generated_result"]
        translation, translation_stats, elapsed, finish_reason = generated
        if isinstance(generated, TranslationStageResult) and generated.failure:
            print(f"Chapter {chapter_number}: {generated.failure}. Stopping safely.")
            return False
        if not translation.strip():
            print(f"Chapter {chapter_number}: empty translation. Stopping safely.")
            return False
        if finish_reason != "stop":
            print(f"Chapter {chapter_number}: unexpected finish reason {finish_reason}. Stopping safely.")
            return False
        meta = {
            "chapter": chapter_number,
            "stage": "translated",
            "translation_stats": translation_stats,
            "elapsed_seconds": elapsed,
            "finish_reason": finish_reason,
        }
        if isinstance(generated, TranslationStageResult) and generated.recovery_used:
            meta.update(pending_translation_model_record(MODEL))
            meta.update(generated.pending_provenance())
            validate_recovery_provenance(meta)
            meta["translation_max_tokens"] = meta["translation_recovery_max_tokens"]
            meta["stage"] = "translation_recovery_recorded"
            # Metadata first for recovered text: a crash after Markdown appears
            # must not lose the initial spend, chain identity or model pin.
            save_json(pending_meta_file, meta)
        atomic_write_text(pending_file, translation)
        meta["stage"] = "translated"
        save_json(pending_meta_file, meta)
        print(f"Pending translation saved to: {pending_file}")

    canonicalization = canonicalize_locked_high_priority_terms(
        chinese_text,
        translation,
        glossary["entries"],
        ACTIVE_PROFILE.high_priority_categories,
        chapter_number=chapter_number,
    )
    if canonicalization["repairs_applied"]:
        # Invalidate derived metadata first. If the following atomic text write
        # is interrupted, recovery safely repeats this deterministic repair
        # instead of replaying state extracted from the old pending text.
        meta = pending_meta_after_translation_repair(
            meta, canonicalization["repairs_applied"]
        )
        save_json(pending_meta_file, meta)
        translation = canonicalization["repaired_text"]
        atomic_write_text(pending_file, translation)
        for repair in canonicalization["repairs_applied"]:
            print(
                "Local locked-term repair: "
                f"{repair['old_variant']} â†’ {repair['canonical_english']}"
            )
            log_event(
                "locked_term_canonicalization",
                chapter=chapter_number,
                stage="local_qa",
                chinese_term=repair["chinese_term"],
                old_variant=repair["old_variant"],
                canonical_english=repair["canonical_english"],
                replacement_count=repair["replacement_count"],
                repair_type=repair["repair_type"],
                api_cost=0,
            )

    strict_repair = recovery.run("strict_deterministic", lambda: perform_deterministic_strict_terminology_repair(
        chapter_number=chapter_number,
        source_text=chinese_text,
        translation=translation,
        meta=meta,
        glossary_entries=glossary["entries"],
        high_priority_categories=ACTIVE_PROFILE.high_priority_categories,
        persist_meta=lambda value: save_json(pending_meta_file, value),
        persist_translation=lambda value: atomic_write_text(pending_file, value),
        qa_checker=lambda candidate: local_qa(
            chapter_number, chinese_text, candidate, finish_reason
        ),
        approved_reader_facing_terms=APPROVED_READER_FACING_CJK_TERMS,
        log_event=lambda event, **fields: log_event(
            event, chapter=chapter_number, stage="local_qa", **fields
        ),
    ))
    if strict_repair["repairs_applied"]:
        meta = strict_repair["meta"]
        translation = strict_repair["translation"]
        for repair in strict_repair["repairs_applied"]:
            print(
                "Automatic strict-term repair: "
                f"{repair['old_variant']} â†’ {repair['canonical_english']}"
            )

    recorded_strict_recovery = recovery.run("strict_recorded", lambda: maybe_revalidate_recorded_strict_terminology(
        chapter_number,
        chinese_text,
        translation,
        meta,
        pending_file,
        pending_meta_file,
        finish_reason,
    ))
    if recorded_strict_recovery["status"].startswith(
        "succeeded_from_revalidated_recorded_candidate"
    ):
        translation = recorded_strict_recovery["translation"]
        meta = recorded_strict_recovery["meta"]
        print("Recovered and revalidated the recorded strict-term patches locally.")

    strict_micro_repair = recovery.run("strict_micro", lambda: maybe_repair_strict_terminology(
        chapter_number,
        chinese_text,
        translation,
        meta,
        pending_file,
        pending_meta_file,
        finish_reason,
    ))
    translation = strict_micro_repair["translation"]
    meta = strict_micro_repair["meta"]

    if strict_micro_repair["status"] in {
        "clean",
        "succeeded",
        "succeeded_from_recorded_candidate",
        "succeeded_from_revalidated_recorded_candidate",
        "succeeded_with_unrelated_blockers",
        "succeeded_from_recorded_candidate_with_unrelated_blockers",
        "succeeded_from_revalidated_recorded_candidate_with_unrelated_blockers",
    }:
        micro_repair = recovery.run("residual_cjk", lambda: maybe_repair_residual_cjk(
            chapter_number,
            chinese_text,
            translation,
            meta,
            pending_file,
            pending_meta_file,
            finish_reason,
        ))
        translation = micro_repair["translation"]
        meta = micro_repair["meta"]

    qa_checkpoint = recovery.run("commit_qa", lambda: run_commit_qa_checkpoint(
        chapter_number=chapter_number,
        source_text=chinese_text,
        translation=translation,
        finish_reason=finish_reason,
        meta=meta,
        qa_checker=local_qa,
        reset_failed_meta=pending_meta_after_commit_qa_failure,
        persist_meta=lambda value: save_json(pending_meta_file, value),
    ))
    qa = qa_checkpoint["qa"]
    meta = qa_checkpoint["meta"]
    if not qa_checkpoint["commit_allowed"]:
        # Any downstream pending work belongs to the failed text and must not be
        # replayed after an operator repairs that text. Preserve translation and
        # translation accounting, but restart auxiliary processing from this gate.
        print("\n" + "=" * 70)
        print("HARD LOCAL QA BLOCK — CHAPTER NOT COMMITTED")
        print("=" * 70)
        print(f"Chapter: {chapter_number}")
        if qa["remaining_chinese_characters"]:
            print(
                "Residual source-script characters: "
                f"{qa['remaining_chinese_characters']}"
            )
            print(
                "Detected fragments: "
                + ", ".join(repr(item) for item in qa["residual_cjk_fragments"])
            )
        for violation in qa["locked_high_priority_term_violations"]:
            print(
                "Missing locked rendering: "
                f"{violation['chinese']} → {violation['required_english']}"
            )
        print(f"Paid translation preserved at: {pending_file}")
        print("Progress, continuity, glossary, and audit state were not advanced.")
        print("Correct the pending translation, then restart; it will be reused and rechecked.")
        print("=" * 70)
        log_event(
            "error",
            chapter=chapter_number,
            stage="local_qa",
            error_type="TranslationCommitQABlocker",
            error_message="; ".join(qa["issues"]),
            remaining_cjk_characters=qa["remaining_chinese_characters"],
            residual_cjk_fragments=qa["residual_cjk_fragments"],
            locked_term_violations=qa["locked_high_priority_term_violations"],
        )
        return False

    try:
        state_checkpoint_identity = current_state_checkpoint_identity(
            chapter_number, chinese_text, translation,
            legacy_policy=(meta.get("stage") in {"state_extracted", "reviewed"}
                           and isinstance(meta.get("state_checkpoint_identity"), dict)
                           and meta.get("state_checkpoint_identity", {}).get("max_tokens") == 3000
                           and "extraction_policy" not in meta.get("state_checkpoint_identity", {})),
        )
        if state_checkpoint_identity["prior_continuity"]["latest_chapter"] != progress.get("last_completed"):
            raise SavedCheckpointIdentityError("prior continuity does not match committed progress")
        state_checkpoint = recovery.run("state", lambda: run_state_extraction_checkpoint(
            meta=meta,
            validate_saved_state=validate_state,
            extract_state=lambda: extract_state(
                chapter_number,
                chinese_text,
                translation,
                prior_continuity=state_checkpoint_identity["prior_continuity"],
            ),
            reprice_saved_stats=reprice_saved_stats_if_needed,
            zero_stats=zero_stats,
            persist_meta=lambda value: save_json(pending_meta_file, value),
            checkpoint_identity=state_checkpoint_identity,
            recoverable_exceptions=(
                APIError,
                APIConnectionError,
                APITimeoutError,
                RateLimitError,
                ProviderResponseError,
            ),
        ))
    except SavedCheckpointIdentityError as exc:
        log_error(
            exc,
            chapter=chapter_number,
            stage="state_extraction_recovery",
        )
        print("\n" + "=" * 70)
        print("SAVED STATE CHECKPOINT IDENTITY BLOCK")
        print("=" * 70)
        print(f"Chapter: {chapter_number}")
        print(f"Error: {exc}")
        print("The saved state will NOT be reused and no replacement provider call was made.")
        print("=" * 70)
        return False
    except NonProviderProductionError as exc:
        log_error(
            exc,
            chapter=chapter_number,
            stage="state_extraction_local",
        )
        print("\n" + "=" * 70)
        print("LOCAL/RECOVERY ERROR DURING STATE EXTRACTION")
        print("=" * 70)
        print(f"Chapter: {chapter_number}\nError: {exc}\n")
        print(describe_non_provider_error(exc))
        print(f"\nThe paid translation remains safely saved in:\n{pending_file}")
        return False
    if state_checkpoint["status"] == "failed":
        exc = state_checkpoint["error"]
        if isinstance(exc, ProviderResponseError):
            log_error(
                exc,
                chapter=chapter_number,
                stage="state_extraction_response",
            )
            print("\n" + "=" * 70)
            print("PROVIDER RESPONSE/ACCOUNTING FAILURE DURING STATE EXTRACTION")
            print("=" * 70)
            print(f"Chapter: {chapter_number}\nError: {exc}\n")
            print(describe_non_provider_error(exc))
        else:
            log_error(
                exc,
                chapter=chapter_number,
                stage="state_extraction_provider",
            )
            print("\n" + "=" * 70)
            print("PROVIDER/REQUEST ERROR DURING STATE EXTRACTION")
            print("=" * 70)
            print(f"Chapter: {chapter_number}\nError: {exc}\n")
            print(describe_api_error(exc))
        print(f"\nThe paid translation remains safely saved in:\n{pending_file}")
        return False

    state = state_checkpoint["state"]
    state_stats = state_checkpoint["state_stats"]
    meta = state_checkpoint["meta"]

    if state_checkpoint["status"] == "recovered":
        print(f"Recovered saved state extraction for Chapter {chapter_number}.")
        log_event(
            "recovered_pending_stage",
            chapter=chapter_number,
            stage="state_extracted",
            **stats_event_fields(state_stats),
        )

    candidate_checkpoint = recovery.run("candidates", lambda: run_candidate_preparation_checkpoint(
        prepare_candidates=lambda: prefilter_candidates(
            state.get("new_terms", []),
            chinese_text,
            chapter_number,
            state=state,
            translation=translation,
            return_preparation=True,
        ),
        resolve_conflict=lambda conflict: attempt_automatic_glossary_candidate_resolution(
            chapter_number,
            chinese_text,
            translation,
            state,
            meta,
            pending_meta_file,
            conflict,
        ),
    ))
    if candidate_checkpoint["status"] == "resolution_failed":
        return stop_for_glossary_candidate_resolution_error(
            chapter_number, candidate_checkpoint["error"], pending_file
        )
    if candidate_checkpoint["status"] == "identity_failed":
        return stop_for_glossary_candidate_identity_conflict(
            chapter_number, candidate_checkpoint["error"], pending_file
        )
    if candidate_checkpoint["status"] == "unresolved":
        return stop_for_unresolved_automatic_glossary_candidate_conflict(
            chapter_number,
            candidate_checkpoint["automatic"],
            candidate_checkpoint["error"],
            pending_file,
        )
    eligible_candidates = candidate_checkpoint["eligible_candidates"]
    prefilter_rejected = candidate_checkpoint["prefilter_rejected"]
    candidate_preparation = candidate_checkpoint["candidate_preparation"]
    log_glossary_candidate_preparation(chapter_number, candidate_preparation)
    report_glossary_candidate_resolutions(
        chapter_number, candidate_preparation
    )

    review_checkpoint_identity = current_review_checkpoint_identity(
        chapter_number,
        chinese_text,
        translation,
        eligible_candidates,
    )

    try:
        review_checkpoint = recovery.run("glossary_review", lambda: run_glossary_review_checkpoint(
            meta=meta,
            eligible_candidates=eligible_candidates,
            candidate_preparation=candidate_preparation,
            prefilter_rejected=prefilter_rejected,
            checkpoint_identity=review_checkpoint_identity,
            validate_saved_review=lambda saved: validate_glossary_review_result(
                saved,
                eligible_candidates,
                glossary["entries"].keys(),
            ),
            reprice_saved_stats=lambda saved: reprice_saved_stats_or_raise(
                saved,
                stage="glossary_review_recovery",
            ),
            zero_stats=zero_stats,
            run_review=lambda recovery_record, persist_recovery: review_new_terms(
                chapter_number,
                chinese_text,
                translation,
                eligible_candidates,
                recovery_record=recovery_record,
                persist_recovery=persist_recovery,
            ),
            persist_checkpoint_meta=lambda value, *, stage: persist_pending_meta(
                pending_meta_file,
                value,
                stage=stage,
            ),
            persist_recovery_meta=lambda value: save_json(
                pending_meta_file,
                value,
            ),
        ))
    except SavedCheckpointIdentityError as exc:
        log_error(
            exc,
            chapter=chapter_number,
            stage="glossary_review_recovery",
        )
        print("\n" + "=" * 70)
        print("SAVED GLOSSARY REVIEW CHECKPOINT IDENTITY BLOCK")
        print("=" * 70)
        print(f"Chapter: {chapter_number}")
        print(f"Error: {exc}")
        print(
            "The saved review will NOT be reused and no replacement "
            "provider call was made."
        )
        print("=" * 70)
        return False
    except ChapterGlossaryCandidateIdentityError as exc:
        return stop_for_glossary_candidate_identity_conflict(
            chapter_number, exc, pending_file
        )
    except (
        APIError,
        APIConnectionError,
        APITimeoutError,
        RateLimitError,
    ) as exc:
        log_error(
            exc,
            chapter=chapter_number,
            stage="glossary_review_provider",
        )
        print("\n" + "=" * 70)
        print("PROVIDER/REQUEST ERROR DURING GLOSSARY REVIEW")
        print("=" * 70)
        print(f"Chapter: {chapter_number}\nError: {exc}\n")
        print(describe_api_error(exc))
        print("\nTranslation and state extraction remain saved for recovery.")
        return False
    except LocalPersistenceError as exc:
        log_error(
            exc,
            chapter=chapter_number,
            stage="glossary_review_local",
        )
        message = str(exc)
        if "glossary_review_preparation" in message:
            heading = "LOCAL PERSISTENCE ERROR BEFORE GLOSSARY REVIEW"
        elif "glossary_review_result" in message:
            heading = "LOCAL PERSISTENCE ERROR AFTER GLOSSARY REVIEW"
        else:
            heading = "LOCAL PERSISTENCE ERROR DURING GLOSSARY REVIEW"
        print("\n" + "=" * 70)
        print(heading)
        print("=" * 70)
        print(f"Chapter: {chapter_number}\nError: {exc}\n")
        print(describe_non_provider_error(exc))
        print("\nTranslation and state extraction remain saved for recovery.")
        return False
    except NonProviderProductionError as exc:
        log_error(
            exc,
            chapter=chapter_number,
            stage="glossary_review_local",
        )
        print("\n" + "=" * 70)
        print("LOCAL/RECOVERY ERROR DURING GLOSSARY REVIEW")
        print("=" * 70)
        print(f"Chapter: {chapter_number}\nError: {exc}\n")
        print(describe_non_provider_error(exc))
        print("\nTranslation and state extraction remain saved for recovery.")
        return False

    review_result = review_checkpoint["review_result"]
    review_stats = review_checkpoint["review_stats"]
    meta = review_checkpoint["meta"]
    if review_checkpoint["status"] == "recovered":
        print(f"Recovered saved glossary review for Chapter {chapter_number}.")
        log_event(
            "recovered_pending_stage",
            chapter=chapter_number,
            stage="reviewed",
            **stats_event_fields(review_stats),
        )

    glossary_changes = recovery.run("reviewed_state", lambda: finalize_reviewed_chapter_state(
        chapter_number=chapter_number,
        eligible_candidates=eligible_candidates,
        review_result=review_result,
        prefilter_rejected=prefilter_rejected,
        state=state,
        qa=qa,
        continuity=continuity,
        issues_data=issues_data,
        apply_term_review=apply_term_review,
        replace_chapter_issues=replace_chapter_issues,
    ))
    chapter_stats = recovery.run("chapter_commit", lambda: commit_reviewed_chapter(
        chapter_number=chapter_number,
        translation=translation,
        qa=qa,
        translation_stats=translation_stats,
        state_stats=state_stats,
        review_stats=review_stats,
        progress=progress,
        glossary=glossary,
        continuity=continuity,
        issues_data=issues_data,
        audit_history=audit_history,
        final_file=final_file,
        qa_file=QA_DIR / f"{chapter_number:04d}.json",
        glossary_file=GLOSSARY_FILE,
        continuity_file=CONTINUITY_FILE,
        issues_file=ISSUES_FILE,
        audit_history_file=AUDIT_HISTORY_FILE,
        progress_file=PROGRESS_FILE,
        pending_file=pending_file,
        pending_meta_file=pending_meta_file,
        combine_stats=combine_stats,
        atomic_write_text=atomic_write_text,
        save_json=save_json,
        commit_chapter_accounting=commit_chapter_accounting,
    ))

    print(f"\nChapter {chapter_number} fully committed.")
    print(f"Glossary accepted: {len(glossary_changes['added'])}")
    print(f"Glossary corrected: {len(glossary_changes['corrected'])}")
    print(f"Glossary contextualized: {len(glossary_changes['contextualized'])}")
    print(f"Glossary merged aliases: {len(glossary_changes['merged'])}")
    print(f"Glossary rejected: {len(glossary_changes['rejected'])}")
    print(
        f"Chapter total API cost "
        + ("(credit-equivalent; provider_invoice_claimed=false): "
           if chapter_stats.get("provider") == "command_code_provider"
           else "(current DeepSeek rates): ") +
        f"${chapter_stats['cost_usd']:.6f}"
    )
    accounting_lines = accounting_report_lines(progress)
    if accounting_lines:
        for line in accounting_lines:
            print(line)
    else:
        print(
            f"Rate-correct cost tracked since "
            f"Chapter {progress['rate_correct_cost_start_chapter']}: "
            f"${progress['rate_correct_cost_usd']:.6f}"
        )
        print(
            f"Legacy cumulative project counter: "
            f"${progress['estimated_cost_usd']:.6f}"
        )
        print(
            "Legacy counter note: earlier chapters were recorded with the "
            "old local price constants; newly processed calls use current rates."
        )
    print(f"Next chapter: {progress['next_chapter']}")
    log_event(
        "chapter_commit",
        chapter=chapter_number,
        stage="committed",
        elapsed_seconds=time.perf_counter() - chapter_started,
        **stats_event_fields(chapter_stats),
    )
    return True


def reconcile_startup_partial_commit_if_present():
    """Finish a proven next-chapter partial commit before any due audits."""

    target = progress.get("next_chapter")
    if (
        not isinstance(target, int)
        or isinstance(target, bool)
        or target < 1
        or target > FINAL_CHAPTER
    ):
        return False
    final_file = OUTPUT_DIR / f"{target:04d}.md"
    if not final_file.is_file():
        return False
    return reconcile_reviewed_partial_commit_tail(target)


def run_due_audits():
    return run_shared_due_audits(
        progress=progress,
        audit_interval=AUDIT_INTERVAL,
        run_periodic_audit=run_periodic_audit,
    )


def run_final_audit_if_needed():
    return run_shared_final_audit_if_needed(
        progress=progress,
        final_chapter=FINAL_CHAPTER,
        run_periodic_audit=run_periodic_audit,
    )


def run_whole_history_consistency():
    from types import SimpleNamespace
    from historical_reconciliation import save_final_consistency
    return save_final_consistency(SimpleNamespace(**globals()))


def run_unattended_production(*, target_chapter=None):
    """Compatibility callable backed by shared lifecycle sequencing."""

    return run_unattended_lifecycle(
        progress=progress,
        glossary=glossary,
        final_chapter=FINAL_CHAPTER,
        auto_off_peak_only=AUTO_OFF_PEAK_ONLY,
        manual_pause_file=MANUAL_PAUSE_FILE,
        prices=V4_PRO_PRICES,
        run_log_path=(
            str(RUN_LOGGER.path)
            if RUN_LOGGER.path
            else None
        ),
        reconcile_stale_completed_pending=(
            reconcile_stale_completed_pending
        ),
        perform_startup_self_check=perform_startup_self_check,
        reconcile_partial_commit_before_audits=(
            reconcile_startup_partial_commit_if_present
        ),
        run_due_audits_callback=run_due_audits,
        run_final_audit_if_needed_callback=(
            run_final_audit_if_needed
        ),
        process_chapter=process_chapter,
        log_event=log_event,
        log_error=log_error,
        describe_api_error=describe_api_error,
        handled_error_types=(
            APIError,
            APIConnectionError,
            APITimeoutError,
            RateLimitError,
        ),
        describe_local_error=describe_non_provider_error,
        local_error_types=(NonProviderProductionError,),
        print_fn=print,
        target_chapter=target_chapter,
        run_whole_history_consistency=(run_whole_history_consistency if target_chapter is None else None),
    )


def main():
    with production_run_lock(Path.cwd()):
        initialize_production_runtime_state()
        return run_unattended_production()


if __name__ == "__main__":
    main()
