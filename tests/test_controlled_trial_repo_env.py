import os

import controlled_trial_repo_env as bridge


def test_repository_env_key_is_temporarily_loaded_and_restored(tmp_path, monkeypatch):
    monkeypatch.delenv(bridge.API_KEY_ENV, raising=False)
    (tmp_path / ".env").write_text(
        "OTHER=value\nDEEPSEEK_API_KEY=test-secret-from-repo\n",
        encoding="utf-8",
    )

    with bridge.production_api_key_environment(tmp_path) as state:
        assert state["source"] == "repository_env_file"
        assert os.environ[bridge.API_KEY_ENV] == "test-secret-from-repo"
        assert "OTHER" not in os.environ

    assert bridge.API_KEY_ENV not in os.environ


def test_existing_process_key_wins_without_reading_repo_secret(tmp_path, monkeypatch):
    monkeypatch.setenv(bridge.API_KEY_ENV, "existing-process-secret")
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=repo-secret-that-must-not-override\n",
        encoding="utf-8",
    )

    with bridge.production_api_key_environment(tmp_path) as state:
        assert state["source"] == "process_environment"
        assert os.environ[bridge.API_KEY_ENV] == "existing-process-secret"

    assert os.environ[bridge.API_KEY_ENV] == "existing-process-secret"


def test_missing_repository_key_refuses_before_delegate(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(bridge.API_KEY_ENV, raising=False)
    (tmp_path / ".env").write_text("OTHER=value\n", encoding="utf-8")
    delegated = []
    monkeypatch.setattr(bridge.executor, "main", lambda argv=None: delegated.append(argv) or 0)

    result = bridge.main([
        "--repo-root", str(tmp_path),
        "--authorize-paid-trial",
    ])

    output = capsys.readouterr().out
    assert result == 2
    assert delegated == []
    assert "REFUSED_BEFORE_PROVIDER" in output
    assert "API calls: 0" in output
    assert "test-secret" not in output


def test_unpaid_rehearsal_does_not_require_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv(bridge.API_KEY_ENV, raising=False)
    delegated = []
    monkeypatch.setattr(bridge.executor, "main", lambda argv=None: delegated.append(argv) or 0)
    argv = ["--repo-root", str(tmp_path)]

    result = bridge.main(argv)

    assert result == 0
    assert delegated == [argv]
    assert bridge.API_KEY_ENV not in os.environ
