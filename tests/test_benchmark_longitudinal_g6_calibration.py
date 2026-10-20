import benchmark_longitudinal_g6_calibration as g6


def test_g6a_evidence_and_source_hashes_are_frozen():
    assert g6.G6A_SELECTION_SEMANTIC_SHA256 == (
        "d5fa68513570c11feae996fcc1e956c8d98823c974b4449c14a1e02f304bc3a5"
    )
    assert g6.G6A_SELECTION_FILE_SHA256 == (
        "d40c03014bdbe471c53a078bc63f560a1e74c77d65176f07be29adfd8797ea59"
    )
    assert g6.G6A_MANIFEST_SHA256 == (
        "4c1158e9a80997d9945a6837fd9e432b95df511581930cbc1df2e76ce89b2718"
    )
    assert g6.ACE_SOURCE_SHA256 == (
        "b43ab411a4e33a616a0353a8f6c8b4682c75d36dc96090915c91c4fc0d9dbc1f"
    )
    assert g6.ROGUE_SOURCE_SHA256 == (
        "ce939a2167c0eb11700bd1b5a0f463c891da232d1bbe84265e2c5e718714015e"
    )


def test_exact_four_g6_source_cases_are_frozen():
    observed = [
        (case.novel_id, case.stress_axis, case.chapter, case.text_sha256)
        for case in g6.G6_SOURCE_CASES
    ]

    assert observed == [
        (
            "ace_evolution",
            "dialogue_stress",
            18,
            "e1b141fbd64069d5208d8a1d73076afa0a25bb159efd2d2907638ac9c5e0798a",
        ),
        (
            "ace_evolution",
            "information_density_stress",
            2,
            "769310fa78921a78c4e148d0282818bc7ffc1f11d0ae6b83b1695e0e7d817596",
        ),
        (
            "rogue_first_prince",
            "dialogue_stress",
            25,
            "a42ecf2525be7c2c3bf59adddf9135d89d8e911fbc6fc70cdd8c140398e2d2cb",
        ),
        (
            "rogue_first_prince",
            "information_density_stress",
            14,
            "4b07c5c545fcf8683d47eeb7521518c913efaa3751c89e7f698aefc79555d5f3",
        ),
    ]


def test_g6_plan_has_exactly_forty_calls():
    assert g6.G6_PRIMARY_CALL_COUNT == 40
    assert len(g6.G6_CALIBRATION_CASES) == 40
    assert len({case.case_id for case in g6.G6_CALIBRATION_CASES}) == 40


def test_adjustable_and_default_model_call_counts_are_exact():
    counts = {}
    for case in g6.G6_CALIBRATION_CASES:
        counts[case.benchmark_model_id] = counts.get(case.benchmark_model_id, 0) + 1

    assert counts == {
        "deepseek/deepseek-v4.1-flash": 8,
        "z-ai/glm-5.3-flash": 8,
        "qwen/qwen3.8-max-0902": 8,
        "google/gemini-3.8-flash": 8,
        "tencent/hy3-paid": 4,
        "xiaomi/mimo-v2.5-pro": 4,
    }


def test_exact_reasoning_efforts_are_bound_to_live_verified_controls():
    by_model = {}
    for case in g6.G6_CALIBRATION_CASES:
        by_model.setdefault(case.benchmark_model_id, set()).add(case.effort)

    assert by_model == {
        "deepseek/deepseek-v4.1-flash": {"low", "high"},
        "z-ai/glm-5.3-flash": {"low", "high"},
        "qwen/qwen3.8-max-0902": {"low", "medium"},
        "google/gemini-3.8-flash": {"low", "medium"},
        "tencent/hy3-paid": {None},
        "xiaomi/mimo-v2.5-pro": {None},
    }


def test_calibration_context_and_execution_policy_are_frozen():
    assert g6.TRANSLATION_CONTEXT_MODE == "production_profile_empty_glossary_no_continuity"
    assert g6.MAX_COMPLETION_TOKENS == 12_288
    assert g6.AUTOMATIC_RETRIES == 0
    assert g6.ZDR_ENABLED is False
    assert g6.LITERARY_PROVIDER_CALLS_AUTHORIZED is False


def test_calibration_freeze_is_deterministic():
    assert g6.G6_CALIBRATION_FREEZE_SHA256 == g6._sha256_json(
        g6.g6_calibration_identity()
    )
    assert len(g6.G6_CALIBRATION_FREEZE_SHA256) == 64
