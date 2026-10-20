"""Fail-closed provider model-identity guard for the production launcher.

The configured/requested model is only a request identity. DeepSeek may change
what an alias routes to, so a paid response must also carry the provider's
reported ``response.model`` before production is allowed to persist translation
metadata or continue downstream model-bound checkpoints.

This runtime layer deliberately wraps the already import-safe legacy production
module instead of duplicating its paid-request journal. The response model and
system fingerprint are added to the journal's durable ``response_recorded``
envelope first; only then is exact model identity validated. A mismatch is
therefore replayable evidence and automatic reissue remains blocked by the
existing paid-request journal.

Known retired/rerouted request aliases are authorized while the journal is
still PLANNED, composed with any external local send authorization. The raw
provider call retains its defensive route check. Importing this module performs
no I/O and no provider call.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone

from production_failure_classification import NonProviderProductionError
from translation_model_config import (
    TranslationModelRoutingError,
    assert_translation_request_allowed,
    validate_provider_response_model,
)


PROVIDER_MODEL_IDENTITY_MODE = "provider_model_identity_v1"
_STATE_ATTR = "_noveltranslator_provider_model_identity_runtime_state"


class ProviderModelIdentityError(NonProviderProductionError):
    """Provider identity is missing, routed, or otherwise unsafe to attribute."""

    failure_kind = "local_safety"


def _field(value, name, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _raise_routing_error(exc):
    raise ProviderModelIdentityError(str(exc)) from exc


def install_provider_model_identity_runtime(production_module, *, now_utc=None):
    """Install request-cutoff and durable response-model validation wrappers.

    Route authorization runs while PLANNED, before any external local budget
    authorization and STARTED persistence. The raw send wrapper also checks
    routing immediately before a provider call as defense in depth.
    The journal wrapper enriches the durable normalized response with provider
    identity, lets the existing journal persist/replay it, and then requires an
    exact match to the model in the immutable paid-request identity.

    ``now_utc`` is injectable only for deterministic tests.
    """
    existing = getattr(production_module, _STATE_ATTR, None)
    if isinstance(existing, dict):
        return existing

    original_send = getattr(production_module, "send_api_request_once", None)
    original_journal = getattr(production_module, "run_journaled_paid_request", None)
    if not callable(original_send):
        raise AttributeError("production module has no callable send_api_request_once")
    if not callable(original_journal):
        raise AttributeError("production module has no callable run_journaled_paid_request")

    clock = now_utc or (lambda: datetime.now(timezone.utc))

    def guarded_send_api_request_once(**kwargs):
        requested_model = kwargs.get("model")
        if not isinstance(requested_model, str) or not requested_model.strip():
            raise ProviderModelIdentityError(
                "paid provider request has no explicit model identity"
            )
        try:
            assert_translation_request_allowed(
                requested_model,
                moment=clock(),
            )
        except TranslationModelRoutingError as exc:
            _raise_routing_error(exc)
        return original_send(**kwargs)

    def guarded_run_journaled_paid_request(
        *,
        journal_path,
        identity,
        save_json,
        prepare_attempt,
        send_once,
        normalize_response,
        authorize_send=None,
    ):
        requested_model = identity.get("model") if isinstance(identity, dict) else None
        if not isinstance(requested_model, str) or not requested_model.strip():
            raise ProviderModelIdentityError(
                "paid request journal identity has no explicit model"
            )

        @contextmanager
        def authorize_route_and_send():
            # This scope is entered only for a new/planned attempt. Recorded
            # response replay and historical uncertain blocking happen first.
            try:
                assert_translation_request_allowed(requested_model, moment=clock())
            except TranslationModelRoutingError as exc:
                _raise_routing_error(exc)
            authorization = (
                authorize_send() if authorize_send is not None else nullcontext(send_once)
            )
            with authorization as authorized_send:
                yield authorized_send

        def normalize_with_provider_identity(response, attempt_context):
            normalized = normalize_response(response, attempt_context)
            if not isinstance(normalized, dict):
                raise ProviderModelIdentityError(
                    "paid response normalizer did not return a dictionary"
                )
            enriched = dict(normalized)
            enriched["provider_response_model"] = str(
                _field(response, "model", "") or ""
            )
            enriched["provider_system_fingerprint"] = str(
                _field(response, "system_fingerprint", "") or ""
            )
            if not enriched.get("provider_response_id"):
                enriched["provider_response_id"] = str(
                    _field(response, "id", "") or ""
                )
            return enriched

        result = original_journal(
            journal_path=journal_path,
            identity=identity,
            save_json=save_json,
            prepare_attempt=prepare_attempt,
            send_once=send_once,
            normalize_response=normalize_with_provider_identity,
            authorize_send=authorize_route_and_send,
        )
        if not isinstance(result, dict) or not isinstance(result.get("response"), dict):
            raise ProviderModelIdentityError(
                "paid request journal returned no durable response envelope"
            )

        response = result["response"]
        observed = response.get("provider_response_model")
        try:
            validate_provider_response_model(requested_model, observed)
        except TranslationModelRoutingError as exc:
            replay_state = "replayed" if result.get("replayed") else "newly recorded"
            raise ProviderModelIdentityError(
                f"{exc}; paid response is {replay_state} in the durable request "
                "journal, so automatic provider reissue is forbidden"
            ) from exc
        return result

    production_module.send_api_request_once = guarded_send_api_request_once
    production_module.run_journaled_paid_request = guarded_run_journaled_paid_request

    state = {
        "mode": PROVIDER_MODEL_IDENTITY_MODE,
        "original_send_api_request_once": original_send,
        "guarded_send_api_request_once": guarded_send_api_request_once,
        "original_run_journaled_paid_request": original_journal,
        "guarded_run_journaled_paid_request": guarded_run_journaled_paid_request,
    }
    setattr(production_module, _STATE_ATTR, state)
    return state
