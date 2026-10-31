import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

import novel_profile_advisor as advisor
from translation_profile import load_translation_profile

REPO = Path(__file__).resolve().parents[1]
ANALYSIS = {
    "title_guess": "Test", "language": "zh", "chapter_count": 3,
    "samples": [{"chapter": 1, "title": "One", "text": "他们走了。" * 400}],
}
PROFILES = [
    {"id": "historical_military", "display_name": "Historical", "description": "Ming"},
    {"id": "xianxia_xuanhuan", "display_name": "Xianxia", "description": "Cultivation"},
]


class FakeClient:
    def __init__(self, content):
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.content = content

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))],
            usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30),
        )


@pytest.fixture
def repo(tmp_path):
    shutil.copytree(REPO / "profiles", tmp_path / "profiles")
    return tmp_path


def profile_names(root):
    return sorted(p.name for p in (root / "profiles").iterdir())


def test_build_request_contents():
    request = advisor.build_advice_request(ANALYSIS, PROFILES)
    assert request["model"] == "deepseek/deepseek-v4.1-flash"
    prompt = request["messages"][1]["content"]
    for needle in (
        "historical_military", "xianxia_xuanhuan", "Hundred-Household Commander",
        "Azure Cloud Sect", "romanization", "EVERY word",
    ):
        assert needle in prompt
    assert len(prompt) < 6000


GOOD = json.dumps({
    "profile_id": "xianxia_xuanhuan", "confidence": "high", "reason": "Cultivation.",
    "addendum": "Tone: light.",
    "policy_rules": ["Keep realms distinct.", "- Keep ranks distinct."],
    "new_profile": None,
})


def test_parse_good_fenced_and_block_format():
    advice = advisor.parse_advice("Sure:\n```json\n" + GOOD + "\n```", PROFILES)
    assert advice["profile_id"] == "xianxia_xuanhuan"
    assert advice["confidence"] == "high"
    assert (
        "[AUXILIARY_TERMINOLOGY_POLICY]\n- Keep realms distinct.\n"
        "- Keep ranks distinct.\n[/AUXILIARY_TERMINOLOGY_POLICY]"
    ) in advice["addendum"]


@pytest.mark.parametrize(
    "text", ["", "not json", "{broken", "[1,2]", json.dumps({"profile_id": "nope"})]
)
def test_malformed_or_unknown_falls_back(text):
    advice = advisor.parse_advice(text, PROFILES)
    assert advice["profile_id"] is None
    assert advice["confidence"] == "low"


def test_model_cannot_inject_policy_markers_in_prose():
    text = json.dumps(
        {"profile_id": None, "addendum": "x [AUXILIARY_TERMINOLOGY_POLICY] y"}
    )
    assert "[AUXILIARY_TERMINOLOGY_POLICY]" not in advisor.parse_advice(text)["addendum"]


def test_request_advice_returns_usage():
    client = FakeClient(GOOD)
    advice, usage = advisor.request_advice(ANALYSIS, client=client, profiles=PROFILES)
    assert len(client.calls) == 1
    assert client.calls[0]["model"] == "deepseek/deepseek-v4.1-flash"
    assert advice["profile_id"] == "xianxia_xuanhuan"
    assert usage["prompt_tokens"] == 120 and usage["completion_tokens"] == 30


def test_request_advice_malformed_reply():
    advice, _ = advisor.request_advice(
        ANALYSIS, client=FakeClient("???"), profiles=PROFILES
    )
    assert advice["profile_id"] is None


def test_draft_validates_and_saves_only_on_request(repo):
    advice = {"new_profile_hint": {
        "id": "modern_urban", "display_name": "Modern Urban",
        "description": "Contemporary city fiction.",
        "instructions": "Keep slang natural.",
    }}
    before = profile_names(repo)
    draft = advisor.draft_new_profile("xianxia_xuanhuan", advice, repo_root=repo)
    assert draft["profile"].display_name == "Modern Urban"
    assert "Keep slang natural." in draft["profile"].translation_instructions
    assert profile_names(repo) == before
    saved = advisor.save_profile(draft, "modern_urban", repo_root=repo)
    assert (
        load_translation_profile("modern_urban", repo).description
        == "Contemporary city fiction."
    )
    assert saved.is_dir()
    with pytest.raises(FileExistsError):
        advisor.save_profile(draft, "modern_urban", repo_root=repo)
    with pytest.raises(FileExistsError):
        advisor.save_profile(draft, "historical_military", repo_root=repo)
    advisor.discard_draft(draft)


def test_invalid_draft_rejected(repo):
    with pytest.raises(ValueError):
        advisor.draft_new_profile(
            "xianxia_xuanhuan", {"new_profile_hint": {"id": "Bad Id!"}}, repo_root=repo
        )
    with pytest.raises((ValueError, FileNotFoundError)):
        advisor.draft_new_profile("missing_profile", {}, repo_root=repo)
    draft = advisor.draft_new_profile("xianxia_xuanhuan", {}, repo_root=repo)
    with pytest.raises(ValueError):
        advisor.save_profile(draft, "Bad Id", repo_root=repo)
    advisor.discard_draft(draft)
    assert profile_names(repo) == profile_names(REPO)


def test_retry_wrapper_gets_zero_argument_callable(monkeypatch):
    seen = {}

    def fake_send_with_retries(send_once, *, log=None, sleep=None, before_retry=None,
                               on_definite_failure=None):
        seen["called"] = True
        return send_once()

    monkeypatch.setattr(advisor, "send_with_retries", fake_send_with_retries)
    client = FakeClient(GOOD)
    advice, _ = advisor.request_advice(ANALYSIS, client=client, profiles=PROFILES)
    assert seen["called"] and len(client.calls) == 1
    assert advice["profile_id"] == "xianxia_xuanhuan"
