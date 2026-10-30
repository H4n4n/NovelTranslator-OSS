"""What to do when a provider request fails. All retry decisions live here.

Command Code documents 429 and 5xx as retry-with-backoff, and 502 and 403
refusals as not charged. A connection that never opened can't have been charged.
A timeout or dropped response is the only truly ambiguous case, so it gets one
resend. Exception text is read only to spot usage limits and is never logged,
because provider errors can echo request details.
"""
import re
import time
from datetime import datetime, timezone

import httpx
import openai

RETRY = "retry"
WAIT_FOR_LIMIT = "wait_for_limit"
WAIT_FOR_NETWORK = "wait_for_network"
RESEND_ONCE = "resend_once"
STOP = "stop"

BACKOFF_SECONDS = (5, 15, 45, 120, 300, 600, 900, 900)
LIMIT_POLL_SECONDS = 900
LIMIT_MAX_POLLS = 800  # A little over eight days, which outlasts the weekly window.
NETWORK_POLL_SECONDS = 60
NETWORK_MAX_POLLS = 720  # Twelve hours.
RESEND_DELAY_SECONDS = 30
MAX_AMBIGUOUS_RESENDS = 1

_OUT_OF_CREDIT = re.compile(r"insufficient|out of credit|no credits|payment required|top.?up", re.I)
_USAGE_LIMIT = re.compile(r"usage limit|limit for your plan|your limit resets|resets at|quota", re.I)
_NEVER_CONNECTED = (httpx.ConnectError, httpx.ConnectTimeout)


class DefiniteProviderFailure(RuntimeError):
    """The provider returned no billable result, so this slot may be sent again."""
    definite_no_response = True

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _chain(exc):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _status(exc):
    status = getattr(exc, "status_code", None)
    return status if type(status) is int else None


def _body_text(exc):
    return f"{getattr(exc, 'message', '')} {getattr(exc, 'body', '')}"


def classify(exc):
    status = _status(exc)
    if status is not None:
        text = _body_text(exc)
        if _OUT_OF_CREDIT.search(text) or status == 402:
            return STOP
        if status in {403, 429} and _USAGE_LIMIT.search(text):
            return WAIT_FOR_LIMIT
        if status == 429 or status >= 500:
            return RETRY
        return STOP
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)) or isinstance(exc, httpx.TransportError):
        if any(isinstance(item, _NEVER_CONNECTED) for item in _chain(exc)):
            return WAIT_FOR_NETWORK
        return RESEND_ONCE
    return None


def stop_reason(exc):
    status = _status(exc)
    if status in {401, 402} or _OUT_OF_CREDIT.search(_body_text(exc)):
        return "provider_key_or_credit"
    if status == 403:
        return "provider_refused"
    return "provider_rejected" if status is not None else "provider_unavailable"


def send_with_retries(send_once, *, log=None, sleep=time.sleep, before_retry=None, on_definite_failure=None):
    """Run one logical send. Raises DefiniteProviderFailure when nothing was billed.

    An unclassified error, or a second ambiguous failure, propagates unchanged
    so the journal records it as uncertain.
    """
    transient = limit_polls = network_polls = resends = 0

    def record(action, exc):
        if log is not None:
            log(dict(action=action, status=_status(exc), error_type=type(exc).__name__,
                     utc=datetime.now(timezone.utc).isoformat()))

    def wait(seconds):
        try:
            sleep(seconds)
            if before_retry is not None:
                before_retry()
        except BaseException as exc:
            # No request is outstanding during a wait, so the slot stays reusable.
            exc.definite_no_response = True
            raise

    while True:
        try:
            return send_once()
        except Exception as exc:
            action = classify(exc)
            if action is None:
                raise
            record(action, exc)
            if action == STOP:
                if on_definite_failure is not None:
                    on_definite_failure()
                raise DefiniteProviderFailure(stop_reason(exc)) from None
            if action == RESEND_ONCE:
                if resends >= MAX_AMBIGUOUS_RESENDS:
                    raise
                resends += 1
                wait(RESEND_DELAY_SECONDS)
                continue
            if on_definite_failure is not None:
                on_definite_failure()
            if action == WAIT_FOR_LIMIT:
                if limit_polls >= LIMIT_MAX_POLLS:
                    raise DefiniteProviderFailure("provider_usage_limit") from None
                limit_polls += 1
                wait(LIMIT_POLL_SECONDS)
            elif action == WAIT_FOR_NETWORK:
                if network_polls >= NETWORK_MAX_POLLS:
                    raise DefiniteProviderFailure("network_unavailable") from None
                network_polls += 1
                wait(NETWORK_POLL_SECONDS)
            else:
                if transient >= len(BACKOFF_SECONDS):
                    raise DefiniteProviderFailure("provider_unavailable") from None
                wait(BACKOFF_SECONDS[transient])
                transient += 1
