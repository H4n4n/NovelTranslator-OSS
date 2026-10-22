from public_test_support import supervised_callback_source
from pathlib import Path

import pytest

import translate_production_unattended_scheduled_retroactive_dynamic_pricing as production
from chapter_glossary_review import (
    ChapterGlossaryReviewFailure,
    ChapterGlossaryReviewPersistenceError,
)
from production_failure_classification import (
    LocalCallbackError,
    LocalPersistenceError,
    NonProviderProductionError,
    ProductionControlError,
    ProductionIntegrityError,
    ProviderResponseError,
    describe_non_provider_error,
)
from production_paid_request_journal import PaidRequestJournalError


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "error",
    [
        PaidRequestJournalError("journal mismatch"),
        ChapterGlossaryReviewFailure("review validation"),
        ChapterGlossaryReviewPersistenceError("review persistence"),
        LocalCallbackError("callback"),
        LocalPersistenceError("persistence"),
        ProviderResponseError("response"),
        ProductionIntegrityError("integrity"),
        ProductionControlError("control"),
    ],
)
def test_controlled_non_provider_failures_share_typed_base(error):
    assert isinstance(error, NonProviderProductionError)
    assert isinstance(error, RuntimeError)


@pytest.mark.parametrize(
    ("error", "phrase"),
    [
        (
            LocalPersistenceError("disk"),
            "local checkpoint/persistence operation failed",
        ),
        (
            LocalCallbackError("callback"),
            "local processing callback failed",
        ),
        (
            ProviderResponseError("response"),
            "provider request may have completed",
        ),
        (
            ProductionIntegrityError("integrity"),
            "local production integrity check failed",
        ),
        (
            ProductionControlError("control"),
            "local bounded-run/control invariant failed",
        ),
        (
            PaidRequestJournalError("journal"),
            "controlled local/recovery safety check failed",
        ),
    ],
)
def test_non_provider_descriptions_do_not_claim_transport_failure(error, phrase):
    message = describe_non_provider_error(error).lower()

    assert phrase in message
    assert any(
        marker in message
        for marker in (
            "not an api/network failure",
            "do not assume a new paid request is safe",
            "no api/network diagnosis is implied",
            "no provider error is implied",
        )
    )


def test_missing_paid_usage_is_provider_response_failure_not_generic_transport():
    with pytest.raises(
        ProviderResponseError,
        match="paid accounting is unknown",
    ):
        production.usage_and_cost_for_pricing(None, {})


def test_pending_metadata_write_failure_is_typed_local_persistence(monkeypatch):
    failure = OSError("synthetic disk full")

    def fail_save(_path, _value):
        raise failure

    monkeypatch.setattr(production, "save_json", fail_save)

    with pytest.raises(
        LocalPersistenceError,
        match="glossary_review_result pending metadata persistence failed",
    ) as caught:
        production.persist_pending_meta(
            Path("pending.json"),
            {"stage": "reviewed"},
            stage="glossary_review_result",
        )

    assert caught.value.__cause__ is failure


def test_saved_accounting_repricing_failure_is_typed_local_callback(monkeypatch):
    failure = RuntimeError("synthetic repricing bug")

    def fail_reprice(_stats):
        raise failure

    monkeypatch.setattr(production, "reprice_saved_stats_if_needed", fail_reprice)

    with pytest.raises(
        LocalCallbackError,
        match="translation_recovery saved-accounting repricing failed",
    ) as caught:
        production.reprice_saved_stats_or_raise(
            {"prompt_tokens": 1},
            stage="translation_recovery",
        )

    assert caught.value.__cause__ is failure


def test_active_runtime_provider_tuples_exclude_generic_runtime_and_journal_errors():
    source = (
        REPO_ROOT
        / "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
    ).read_text(encoding="utf-8")

    source = supervised_callback_source(source, "process_chapter", {
        'selected': ('translation', 'obtain_translation'),
        'state_checkpoint': ('state', 'run_state_extraction_checkpoint'),
        'review_checkpoint': ('glossary_review', 'run_glossary_review_checkpoint'),
    })
    lifecycle_call = source[source.rindex("return run_unattended_lifecycle("):]
    assert "handled_error_types=(" in lifecycle_call
    handled = lifecycle_call[
        lifecycle_call.index("handled_error_types=("):
        lifecycle_call.index("describe_local_error=", lifecycle_call.index("handled_error_types=("))
    ]
    assert "APIError" in handled
    assert "APIConnectionError" in handled
    assert "APITimeoutError" in handled
    assert "RateLimitError" in handled
    assert "RuntimeError" not in handled
    assert "PaidRequestJournalError" not in handled
    assert "local_error_types=(NonProviderProductionError,)" in lifecycle_call

    process = source[
        source.index("def process_chapter("):
        source.index("def reconcile_startup_partial_commit_if_present(")
    ]
    translation_provider = process[
        process.index("except ("):
        process.index("except NonProviderProductionError as exc:")
    ]
    assert "PaidRequestJournalError" not in translation_provider
    assert "RuntimeError" not in translation_provider

    state_call = process[
        process.index("state_checkpoint = run_state_extraction_checkpoint("):
        process.index('if state_checkpoint["status"] == "failed":')
    ]
    assert "ProviderResponseError" in state_call
    assert "RuntimeError" not in state_call
    assert "except NonProviderProductionError as exc:" in state_call

    review_start = process.index("review_checkpoint = run_glossary_review_checkpoint(")
    review_end = process.index(
        'review_result = review_checkpoint["review_result"]',
        review_start,
    )
    review_block = process[review_start:review_end]
    assert "APIError" in review_block
    assert "RateLimitError" in review_block
    assert "RuntimeError" not in review_block
    assert "except NonProviderProductionError as exc:" in review_block


def test_active_startup_integrity_uses_typed_local_error():
    source = (
        REPO_ROOT
        / "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
    ).read_text(encoding="utf-8")

    start = source.index("def perform_startup_self_check(")
    end = source.index("def log_glossary_candidate_preparation(", start)
    block = source[start:end]

    assert "ProductionIntegrityError(" in block
    assert 'RuntimeError("Startup integrity check failed:' not in block
