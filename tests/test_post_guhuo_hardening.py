"""Synthetic continuity prompt regressions; no provider execution."""
import ast
import json
from pathlib import Path
import pytest
from production_reviewed_state import finalize_reviewed_chapter_state
from production_state_extraction import run_state_extraction, validate_state
ROOT = Path(__file__).resolve().parents[1]


def state_prompt():
    # Read the real prompt without loading the production runtime or credentials.
    tree = ast.parse((ROOT / "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py").read_text(encoding="utf-8"))
    return ast.literal_eval(next(n.value for n in tree.body if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "STATE_PROMPT_BASE" for t in n.targets)))


@pytest.mark.parametrize("requirement", [
    "injury / health / condition", "power/progression", "relationship/attitude",
    "allegiance/faction", "possessions/resources/artifacts", "location/residency",
    "persistent knowledge/secrets", "commitments/promises/debts/obligations",
    "unresolved threats/questions", "at most 200 English words",
    "even if not needed in the immediately next scene",
])
def test_durable_continuity_prompt_contract(requirement):
    assert requirement in state_prompt()
    assert "useful only for the NEXT chapter" not in state_prompt()


def test_offer_and_plan_prompt_contract():
    prompt = state_prompt()
    assert "confirmed facts/actions" in prompt
    assert "offers/proposals, intentions/plans" in prompt
    assert "predictions/likely next steps" in prompt
    assert "Never state an offer or intention as a completed fact or a guaranteed next action" in prompt
    assert "acceptance is not shown" in prompt
    assert 'must NOT become "Li Yan stays with Zha and will return to reconcile"' in prompt


@pytest.mark.parametrize("summary", [
    "Her injury persists; she now distrusts her former ally; her new ability remains available. The next scene concerns another character.",
    "Zha offered Li Yan lodging and suggested a later return; acceptance is not shown. Li Yan intends to seek reconciliation; completion remains unresolved.",
])
def test_compliant_synthetic_continuity_survives_extraction_and_finalization(summary):
    # This proves preservation and one-call behavior, not model comprehension.
    state = {"new_terms": [], "continuity_summary": summary, "issues": []}
    calls = []

    def request(operation, ceiling):
        calls.append((operation, ceiling))
        return {"response": {"content": json.dumps(state), "finish_reason": "stop", "stats": {}},
                "replayed": False, "operation_id": "synthetic"}

    def forbidden(*args):
        pytest.fail("No recovery/repair/extra review is needed")

    accepted, _ = run_state_extraction(request=request, repair=forbidden,
        clean_text=lambda text: text, salvage=forbidden, combine_stats=forbidden,
        emit=lambda *args, **kwargs: None)
    validate_state(accepted)
    continuity = {}
    finalize_reviewed_chapter_state(chapter_number=1, eligible_candidates=[],
        review_result={"decisions": [], "issues": []}, prefilter_rejected=[],
        state=accepted, qa={"issues": []}, continuity=continuity,
        issues_data={"issues": []}, apply_term_review=lambda *args: {"rejected": []},
        replace_chapter_issues=lambda existing, chapter, items: items)
    assert continuity == {"latest_chapter": 1, "summary": summary}
    assert len(calls) == 1
