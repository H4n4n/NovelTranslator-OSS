"""Import-safe provider-backend contract for production NovelTranslator inference.

This module names the immutable provider backends a production translation
lifecycle may bind and resolves them fail-closed. It performs no IO, reads no
credentials, and constructs no client.

Two transport backends exist:

* ``direct_deepseek``  — the legacy/direct DeepSeek platform API
  (``https://api.deepseek.com``), authenticated by ``DEEPSEEK_API_KEY``.
* ``command_code_provider`` — the GOAT Command Code Provider API
  (``https://api.commandcode.ai/provider/v1``), authenticated by a DPAPI secret.

The Command Code coding-agent conversation is never an inference transport here.
A requested Command Code Provider route that cannot initialize MUST fail closed;
there is no silent fallback to ``api.deepseek.com``.
"""
from dataclasses import dataclass

PROVIDER_DIRECT_DEEPSEEK = "direct_deepseek"
PROVIDER_COMMAND_CODE = "command_code_provider"
PROVIDER_BACKENDS = (PROVIDER_DIRECT_DEEPSEEK, PROVIDER_COMMAND_CODE)

DIRECT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
COMMAND_CODE_BASE_URL = "https://api.commandcode.ai/provider/v1"
CHAT_COMPLETIONS_PATH = "/chat/completions"

# The GOAT DeepSeek wire model id. Distinct from direct DeepSeek's wire model
# (e.g. "deepseek-flash"); the two are NOT interchangeable identities.
COMMAND_CODE_DEEPSEEK_V41_FLASH = "deepseek/deepseek-v4.1-flash"
COMMAND_CODE_TRANSLATION_REASONING_EFFORT = "low"
from production_goat_policy import TRANSLATION_MAX_COMPLETION_TOKENS
COMMAND_CODE_TRANSLATION_COMPLETION_TOKENS = TRANSLATION_MAX_COMPLETION_TOKENS
COMMAND_CODE_AUXILIARY_COMPLETION_TOKENS = None

# Direct DeepSeek legacy production budgets (12k normal, 3k auxiliary).
DIRECT_DEEPSEEK_TRANSLATION_COMPLETION_TOKENS = 12_000
DIRECT_DEEPSEEK_AUXILIARY_COMPLETION_TOKENS = 3_000

CREDENTIAL_COMMAND_CODE_DPAPI_FILENAME = "command_code_api_key.dpapi"

ACCOUNTING_DEEPSEEK_USD = "deepseek_usd_peak_offpeak"
ACCOUNTING_COMMAND_CODE_CREDIT = "command_code_credit_equivalent"
PRICING_UNIT_DEEPSEEK = "usd_per_1m_tokens"
PRICING_UNIT_COMMAND_CODE = "usd_credit_equivalent_per_1m_tokens"


class ProviderBackendError(ValueError):
    """A requested production provider backend cannot be resolved safely."""


@dataclass(frozen=True)
class ProviderBackend:
    provider: str
    base_url: str
    chat_path: str
    wire_model: str
    credential_source: str
    reasoning_effort: str | None
    translation_completion_tokens: int
    auxiliary_completion_tokens: int | None
    accounting: str
    pricing_unit: str

    def chat_completions_url(self) -> str:
        return self.base_url.rstrip("/") + self.chat_path

    def identity(self) -> dict:
        result = {
            "provider": self.provider,
            "base_url": self.base_url,
            "chat_path": self.chat_path,
            "wire_model": self.wire_model,
            "credential_source": self.credential_source,
            "reasoning_effort": self.reasoning_effort,
            "translation_completion_tokens": self.translation_completion_tokens,
            "auxiliary_completion_tokens": self.auxiliary_completion_tokens,
            "accounting": self.accounting,
            "pricing_unit": self.pricing_unit,
        }
        if self.provider == PROVIDER_COMMAND_CODE:
            result["auxiliary_completion_policy"] = "provider_unbounded"
        return result


def resolve_provider_backend(provider, *, wire_model) -> ProviderBackend:
    """Resolve one production provider backend, fail-closed.

    Unknown providers are refused. A Command Code Provider request whose wire
    model is not the exact GOAT DeepSeek identity is refused rather than
    silently re-routed to the direct DeepSeek endpoint.
    """
    if provider == PROVIDER_DIRECT_DEEPSEEK:
        if not isinstance(wire_model, str) or not wire_model.strip():
            raise ProviderBackendError(
                "direct_deepseek requires an explicit non-empty wire model"
            )
        return ProviderBackend(
            provider=PROVIDER_DIRECT_DEEPSEEK,
            base_url=DIRECT_DEEPSEEK_BASE_URL,
            chat_path=CHAT_COMPLETIONS_PATH,
            wire_model=wire_model.strip(),
            credential_source="deepseek_env",
            reasoning_effort=None,
            translation_completion_tokens=DIRECT_DEEPSEEK_TRANSLATION_COMPLETION_TOKENS,
            auxiliary_completion_tokens=DIRECT_DEEPSEEK_AUXILIARY_COMPLETION_TOKENS,
            accounting=ACCOUNTING_DEEPSEEK_USD,
            pricing_unit=PRICING_UNIT_DEEPSEEK,
        )
    if provider == PROVIDER_COMMAND_CODE:
        if wire_model != COMMAND_CODE_DEEPSEEK_V41_FLASH:
            raise ProviderBackendError(
                "command_code_provider DeepSeek wire model must be "
                f"{COMMAND_CODE_DEEPSEEK_V41_FLASH!r}, got {wire_model!r}; "
                "no fallback is permitted"
            )
        return ProviderBackend(
            provider=PROVIDER_COMMAND_CODE,
            base_url=COMMAND_CODE_BASE_URL,
            chat_path=CHAT_COMPLETIONS_PATH,
            wire_model=COMMAND_CODE_DEEPSEEK_V41_FLASH,
            credential_source="command_code_dpapi",
            reasoning_effort=COMMAND_CODE_TRANSLATION_REASONING_EFFORT,
            translation_completion_tokens=COMMAND_CODE_TRANSLATION_COMPLETION_TOKENS,
            auxiliary_completion_tokens=COMMAND_CODE_AUXILIARY_COMPLETION_TOKENS,
            accounting=ACCOUNTING_COMMAND_CODE_CREDIT,
            pricing_unit=PRICING_UNIT_COMMAND_CODE,
        )
    raise ProviderBackendError(
        f"unknown production provider backend {provider!r}; "
        "no silent fallback is permitted"
    )


def assert_single_provider_identity(left: ProviderBackend, right: ProviderBackend) -> None:
    """Refuse mixing two different provider identities in one authorization."""
    if left.identity() != right.identity():
        raise ProviderBackendError(
            "provider backend identity drift detected; authorization is invalid"
        )
