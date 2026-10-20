"""Exact, path-scoped SHA-256 hashes of reviewed synthetic credential fixtures.

Test-only literals confirmed synthetic by independent audit. These are not real
secrets; no rotation is indicated. Keep exceptions exact and path-scoped.
"""

EXACT_SYNTHETIC = {
    "tests/test_benchmark_longitudinal_capability_probe.py": {
        "aff83f8fa7cec55532dcbfe48d91cf080f65da6dde9904ce7e6ce50365292879",
        "b748f95127cdfd837329de2beb804d7f30a9cb6399fe58a7aefdb9524b022fd4",
    },
    "tests/test_benchmark_longitudinal_g10f_matched_executor.py": {
        "4fdd29f90eaf3f7cbb29c843dcf038d926c356a9ca6beb1237ff34702642ed46",
    },
    "tests/test_benchmark_longitudinal_g10g_chronological_executor.py": {
        "4fdd29f90eaf3f7cbb29c843dcf038d926c356a9ca6beb1237ff34702642ed46",
    },
    "tests/test_benchmark_longitudinal_g11_runner.py": {
        "4fdd29f90eaf3f7cbb29c843dcf038d926c356a9ca6beb1237ff34702642ed46",
    },
    "tests/test_benchmark_longitudinal_g6_execution.py": {
        "0d43123772654e6b38fd5d23c1e31b2f136d6708bdb73e8c35238028b24d0fd8",
    },
    "tests/test_benchmark_longitudinal_g6_mimo_recovery.py": {
        "0d43123772654e6b38fd5d23c1e31b2f136d6708bdb73e8c35238028b24d0fd8",
    },
    "tests/test_benchmark_longitudinal_g6_recovery.py": {
        "0d43123772654e6b38fd5d23c1e31b2f136d6708bdb73e8c35238028b24d0fd8",
    },
    "tests/test_benchmark_longitudinal_g7f_stage1_paid_executor.py": {
        "da0b0b59e6d17b5903e0a1558baa04e8fefb2324b4aab4bf601f4c5eeac8764f",
    },
    "tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py": {
        "5ef5e5fcfadf6f02900fa0184b793ae621074b493622c968837828d6712d576f",
        "c90c64b665704858a028680b447dcbd7184672ad8060ca30518694b3fb17f5e6",
    },
    "tests/test_benchmark_longitudinal_g7h_readiness_doctor.py": {
        "388e00b8877deeec77492034f5b7dcd496fc04ed5ee696b55b8694425f84176e",
        "bf4e5548778a6ef886124b8b8d332c2826c1412bd2f467cd0f5f6d09d75becdb",
        "d53cf468fee6bb53e40753e0143f3e5260f9cde5671823e9198acca9f8c1048a",
    },
    "tests/test_benchmark_longitudinal_g7r_stage1_ch3_24_wave_controller.py": {
        "5ef5e5fcfadf6f02900fa0184b793ae621074b493622c968837828d6712d576f",
        "d0802efe69dd2b6a790a470c487ed75aca7ac409804446e1b299038fe1d2f37c",
    },
    "tests/test_benchmark_longitudinal_provider.py": {
        "fd30930c2efaf67c0b7a5e00da3b7e86e1c7f718c62539a9fc540634f8fc8d49",
        "fff59c369b4de1643dafe106505db900d7b0be1cff6d3e890e112d6846a32a58",
    },
    "tests/test_command_code_provider_transport.py": {
        "9f63a86a0c18c17cee77960725729d5463301e71a697fd82e6f3a9ea8e51060a",
    },
    "tests/test_controlled_trial_execute.py": {
        "ae6c648412e2c968f6efec30475741588bdea0a3cb24fde5a52c2957653af7f2",
        "cd5162a9161128637d4a780fa3ba1934afcee9e115c6a4e04f8e713956c3dc64",
    },
    "tests/test_experimental_goat.py": {
        "5a0ab0052044628fac7f658adbdf26ae7609bb12a3cf4ef21ea0a670713b6f74",
        "88927aca622ffa3de17446ff501957e2f98770c4cf3aa23855ffbf834bda60e1",
    },
    "tests/test_pending_recovery_execute.py": {
        "fed27cfe56d4713893a41f2f218df2a44746e91823b0b97d1400542e4b397dff",
    },
}
