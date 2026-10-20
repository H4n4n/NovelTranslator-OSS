import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from full_pipeline_batch_acceptance import ACCEPTANCE_FIELD
from full_pipeline_batch_wave import (
    BATCH_MODEL,
    SUBMIT_AUTHORIZATION,
    collect_wave,
    submit_wave,
)
from full_pipeline_experiment_harness import (
    PROCESSING_FILENAME,
    FullPipelineExperimentNotCommitted,
    capture_experiment_lineage,
    capture_production_guard,
    create_experiment,
    prepare_experiment_wave,
    process_collected_wave,
)
from novel_project import NovelConfig


REPO_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_SCRIPT = (
    REPO_ROOT
    / "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
)
CONFIGURED_FINAL_CHAPTER = 26


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _chapter_zh(number, *, long_source=False):
    body = (
        "姚雲走進城門，與守衛交談，記住了今日的約定。" * 55
        if long_source
        else "姚雲走進城門，與守衛交談，並記住了今日的約定。"
    )
    return {
        "chapter": number,
        "production_index": number,
        "heading": f"第{number}章 開始",
        "source_file": f"Text/{number:04d}.xhtml",
        "paragraph_count": 2,
        "character_count": len(body),
        "text": f"第{number}章 開始\n\n{body}",
    }


def _chapter_ko(number):
    body = "아렌은 성문으로 걸어가 경비병과 이야기를 나누고 오늘의 약속을 기억했다."
    return {
        "chapter": number,
        "production_index": number,
        "unit_kind": "chapter",
        "volume_index": 1,
        "heading": f"{number}화 시작",
        "local_chapter_number": number,
        "local_chapter_start": number,
        "local_chapter_end": number,
        "source_file": f"chapter-{number:04d}.txt",
        "paragraph_count": 2,
        "character_count": len(body),
        "text": f"{number}화 시작\n\n{body}",
    }


def _source_project(repo, novel_id, *, long_source=False):
    root = repo / "novels" / novel_id
    (root / "data").mkdir(parents=True)
    (root / "output").mkdir(parents=True)
    (root / "translation_addendum.txt").write_text("", encoding="utf-8")

    if novel_id == "great_king_zhuxia":
        config = NovelConfig.from_dict(
            {
                "id": novel_id,
                "title": "Great King Fixture",
                "source_language": "zh",
                "target_language": "en",
                "profile": "xianxia_xuanhuan",
                "final_chapter": CONFIGURED_FINAL_CHAPTER,
                "audit_interval": 25,
                "chapter_numbering": "global",
                "residual_cjk_micro_repair": {
                    "enabled": True,
                    "max_total_cjk_chars": 12,
                    "max_fragments": 6,
                    "max_affected_lines": 3,
                    "max_attempts_per_pending_version": 1,
                },
            }
        )
        chapters = [
            _chapter_zh(number, long_source=long_source and number == 1)
            for number in range(1, CONFIGURED_FINAL_CHAPTER + 1)
        ]
    else:
        config = NovelConfig.from_dict(
            {
                "id": novel_id,
                "title": "Rogue Prince Fixture",
                "source_language": "ko",
                "target_language": "en",
                "profile": "korean_fantasy",
                "final_chapter": CONFIGURED_FINAL_CHAPTER,
                "audit_interval": 25,
                "chapter_numbering": "spine_sequential",
                "source_type": "txt",
                "residual_cjk_micro_repair": {
                    "enabled": True,
                    "max_total_cjk_chars": 12,
                    "max_fragments": 6,
                    "max_affected_lines": 3,
                    "max_attempts_per_pending_version": 1,
                },
            }
        )
        chapters = [_chapter_ko(number) for number in range(1, CONFIGURED_FINAL_CHAPTER + 1)]

    _write_json(root / "novel.json", config.to_dict())
    _write_json(root / "data" / "chapters.json", chapters)
    return root


def _synthetic_repo(tmp_path, *, long_source=False, only_zh=False):
    repo = tmp_path / "synthetic_repo"
    shutil.copytree(REPO_ROOT / "profiles", repo / "profiles")
    shutil.copyfile(PRODUCTION_SCRIPT, repo / PRODUCTION_SCRIPT.name)
    _source_project(repo, "great_king_zhuxia", long_source=long_source)
    if not only_zh:
        _source_project(repo, "rogue_first_prince")
    return repo


class BatchFiles:
    def __init__(self, label):
        self.label = label
        self.contents = {}
        self.create_calls = []
        self.content_calls = []

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return SimpleNamespace(
            id=f"file-input-{self.label}",
            purpose="batch",
            filename="batch_input.jsonl",
            bytes=100,
        )

    def content(self, file_id):
        self.content_calls.append(file_id)
        return SimpleNamespace(text=self.contents[file_id])


class BatchBatches:
    def __init__(self, label):
        self.create_calls = []
        self.retrieve_calls = []
        self.list_calls = []
        self.batch = SimpleNamespace(
            id=f"batch-{label}",
            status="validating",
            endpoint="/v1/chat/completions",
            input_file_id=f"file-input-{label}",
            output_file_id=None,
            error_file_id=None,
            completion_window="24h",
            created_at=1,
            completed_at=None,
            expires_at=None,
            request_counts=SimpleNamespace(total=0, completed=0, failed=0),
            metadata={},
        )

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        self.batch.input_file_id = kwargs["input_file_id"]
        self.batch.metadata = kwargs["metadata"]
        return self.batch

    def retrieve(self, batch_id):
        self.retrieve_calls.append(batch_id)
        return self.batch

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return SimpleNamespace(data=[self.batch])


class BatchClient:
    def __init__(self, label):
        self.files = BatchFiles(label)
        self.batches = BatchBatches(label)


def _batch_row(custom_id, translation):
    return {
        "id": f"req-{custom_id}",
        "custom_id": custom_id,
        "response": {
            "status_code": 200,
            "request_id": f"http-{custom_id}",
            "body": {
                "id": f"chat-{custom_id}",
                "model": BATCH_MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": translation},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 350,
                    "total_tokens": 1350,
                    "prompt_tokens_details": {"cached_tokens": 100},
                },
            },
        },
        "error": None,
    }


def _translation(novel_id, chapter):
    name = "Yao Yun" if novel_id == "great_king_zhuxia" else "Aren"
    return (
        f"Chapter {chapter}: Beginning\n\n"
        f"{name} walked through the city gate and spoke with the guards. "
        "The conversation was clear, deliberate, and complete. He remembered "
        "the promise made that day and continued onward without hesitation. "
        "The surrounding streets were busy, but the purpose of the meeting "
        "remained unchanged."
    )


def _collect(experiment, repo, wave_number, translations=None):
    wave_manifest, target = prepare_experiment_wave(experiment, repo, wave_number)
    experiment_manifest = json.loads(
        (experiment / "experiment_manifest.json").read_text(encoding="utf-8")
    )
    capture = lambda: capture_production_guard(
        repo, experiment_manifest["novel_ids"]
    )
    client = BatchClient(f"wave-{wave_number}")
    submitted = submit_wave(
        target,
        client,
        SUBMIT_AUTHORIZATION,
        capture_protected_state=capture,
    )
    client.batches.batch.status = "completed"
    client.batches.batch.output_file_id = f"file-output-wave-{wave_number}"
    client.batches.batch.request_counts = SimpleNamespace(
        total=len(submitted["requests"]),
        completed=len(submitted["requests"]),
        failed=0,
    )
    rows = []
    for item in reversed(submitted["requests"]):
        text = (
            translations[item["novel_id"]]
            if translations and item["novel_id"] in translations
            else _translation(item["novel_id"], wave_number)
        )
        rows.append(_batch_row(item["custom_id"], text))
    client.files.contents[client.batches.batch.output_file_id] = "\n".join(
        json.dumps(row, ensure_ascii=False) for row in rows
    ) + "\n"
    document, _collected = collect_wave(
        target,
        client,
        capture_protected_state=capture,
    )
    assert document["status"] == "complete"
    assert wave_manifest["wave_number"] == wave_number
    return document, target


class AuxiliaryCompletions:
    def __init__(self, novel_id, chapter):
        self.novel_id = novel_id
        self.chapter = chapter
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        messages = kwargs["messages"]
        system = messages[0]["content"]
        user = messages[-1]["content"]

        if "continuity_summary" in system and "new_terms" in system:
            terms = []
            if self.chapter == 1:
                terms = [
                    {
                        "chinese": (
                            "姚雲" if self.novel_id == "great_king_zhuxia" else "아렌"
                        ),
                        "english": (
                            "Yao Yun" if self.novel_id == "great_king_zhuxia" else "Aren"
                        ),
                        "category": "character",
                        "status": "provisional",
                        "notes": "Recurring named character.",
                    }
                ]
            payload = {
                "new_terms": terms,
                "continuity_summary": (
                    f"Accepted continuity after Chapter {self.chapter} "
                    f"for {self.novel_id}."
                ),
                "issues": [],
            }
        elif "periodic audit" in system.lower():
            ids = sorted(set(re.findall(r"\bc\d{2}\b", user)))
            payload = {
                "decisions": [
                    {"candidate_id": item, "action": "keep", "notes": ""}
                    for item in ids
                ],
                "issues": [],
            }
        else:
            # Request-local IDs live in the user payload; system examples such
            # as c01/c02 are not actual review candidates.
            ids = sorted(set(re.findall(r"\bc\d{2}\b", user)))
            english = "Yao Yun" if self.novel_id == "great_king_zhuxia" else "Aren"
            payload = {
                "decisions": [
                    {
                        "candidate_id": item,
                        "action": "accept",
                        "english": english,
                        "category": "character",
                        "notes": "",
                    }
                    for item in ids
                ],
                "issues": [],
            }

        return SimpleNamespace(
            id=f"aux-{self.novel_id}-{self.chapter}-{len(self.calls)}",
            model=BATCH_MODEL,
            system_fingerprint="synthetic-full-pipeline",
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content=json.dumps(payload, ensure_ascii=False)
                    ),
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=300,
                completion_tokens=80,
                prompt_tokens_details=SimpleNamespace(cached_tokens=30),
            ),
        )


class AuxiliaryClient:
    def __init__(self, novel_id, chapter):
        self.completions = AuxiliaryCompletions(novel_id, chapter)
        self.chat = SimpleNamespace(completions=self.completions)


def _client_factory_registry():
    created = []

    def factory(novel_id, chapter):
        client = AuxiliaryClient(novel_id, chapter)
        created.append(client)
        return client

    return created, factory


def _make_experiment(tmp_path, *, long_source=False, target_chapters=2, only_zh=False):
    repo = _synthetic_repo(tmp_path, long_source=long_source, only_zh=only_zh)
    experiment = tmp_path / "experiment"
    novel_ids = (
        ("great_king_zhuxia",)
        if only_zh
        else ("great_king_zhuxia", "rogue_first_prince")
    )
    manifest = create_experiment(
        experiment,
        repo,
        repo_anchor="synthetic-phase60f-anchor",
        novel_ids=novel_ids,
        target_chapters=target_chapters,
        experiment_id="phase60f-zero-provider",
    )
    return repo, experiment, manifest


def test_external_s0_and_two_waves_use_real_state_growth_for_next_prompts(tmp_path):
    repo, experiment, manifest = _make_experiment(tmp_path)
    production_before = capture_production_guard(repo, manifest["novel_ids"])

    for novel_id in manifest["novel_ids"]:
        project = experiment / "projects" / novel_id
        assert json.loads(
            (project / "data" / "glossary.json").read_text(encoding="utf-8")
        ) == {"entries": {}}
        assert json.loads(
            (project / "data" / "continuity.json").read_text(encoding="utf-8")
        ) == {"latest_chapter": 0, "summary": ""}
        progress = json.loads(
            (project / "data" / "progress.json").read_text(encoding="utf-8")
        )
        assert progress["last_completed"] == 0
        assert progress["next_chapter"] == 1

    _collect(experiment, repo, 1)
    clients1, factory1 = _client_factory_registry()
    assert process_collected_wave(
        experiment, repo, 1, client_factory=factory1
    )["status"] == "complete"
    assert len(clients1) == 2

    chinese = experiment / "projects" / "great_king_zhuxia"
    korean = experiment / "projects" / "rogue_first_prince"
    assert json.loads(
        (chinese / "data" / "glossary.json").read_text(encoding="utf-8")
    )["entries"]["姚雲"]["english"] == "Yao Yun"
    assert json.loads(
        (korean / "data" / "glossary.json").read_text(encoding="utf-8")
    )["entries"]["아렌"]["english"] == "Aren"

    wave2_manifest, wave2_dir = prepare_experiment_wave(experiment, repo, 2)
    lines = [
        json.loads(line)
        for line in (wave2_dir / "batch_input.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    prompts = [line["body"]["messages"][1]["content"] for line in lines]
    assert len(lines) == 2
    assert any("姚雲" in prompt and "Yao Yun" in prompt for prompt in prompts)
    assert any("아렌" in prompt and "Aren" in prompt for prompt in prompts)
    assert any(
        "Accepted continuity after Chapter 1 for great_king_zhuxia." in prompt
        for prompt in prompts
    )
    assert any(
        "Accepted continuity after Chapter 1 for rogue_first_prince." in prompt
        for prompt in prompts
    )
    assert wave2_manifest["wave_number"] == 2

    experiment_manifest = json.loads(
        (experiment / "experiment_manifest.json").read_text(encoding="utf-8")
    )
    capture = lambda: capture_production_guard(
        repo, experiment_manifest["novel_ids"]
    )
    batch = BatchClient("wave-2-manual")
    submitted = submit_wave(
        wave2_dir,
        batch,
        SUBMIT_AUTHORIZATION,
        capture_protected_state=capture,
    )
    batch.batches.batch.status = "completed"
    batch.batches.batch.output_file_id = "file-output-wave-2-manual"
    batch.batches.batch.request_counts = SimpleNamespace(
        total=2, completed=2, failed=0
    )
    batch.files.contents[batch.batches.batch.output_file_id] = "\n".join(
        json.dumps(
            _batch_row(item["custom_id"], _translation(item["novel_id"], 2)),
            ensure_ascii=False,
        )
        for item in reversed(submitted["requests"])
    ) + "\n"
    collect_wave(wave2_dir, batch, capture_protected_state=capture)

    clients2, factory2 = _client_factory_registry()
    assert process_collected_wave(
        experiment, repo, 2, client_factory=factory2
    )["status"] == "complete"
    assert len(clients2) == 2
    for project in (chinese, korean):
        progress = json.loads(
            (project / "data" / "progress.json").read_text(encoding="utf-8")
        )
        assert progress["last_completed"] == 2
        assert progress["next_chapter"] == 3
        assert progress["last_audited_chapter"] == 0
        assert json.loads(
            (project / "data" / "audit_history.json").read_text(encoding="utf-8")
        ) == {"audits": []}

    assert capture_production_guard(repo, manifest["novel_ids"]) == production_before


def test_restart_after_pending_files_reuses_exact_pending_without_batch_recollection(tmp_path):
    repo, experiment, _manifest = _make_experiment(tmp_path)
    _collect(experiment, repo, 1)
    clients, factory = _client_factory_registry()

    def crash(stage, context):
        if stage == "after_pending_files" and context["novel_id"] == "great_king_zhuxia":
            raise KeyboardInterrupt("synthetic crash after pending durability")

    with pytest.raises(KeyboardInterrupt):
        process_collected_wave(
            experiment,
            repo,
            1,
            client_factory=factory,
            crash_hook=crash,
        )
    assert clients == []
    pending = experiment / "projects" / "great_king_zhuxia" / "output" / "pending"
    assert (pending / "0001.md").is_file()
    assert (pending / "0001.json").is_file()

    assert process_collected_wave(
        experiment, repo, 1, client_factory=factory
    )["status"] == "complete"
    assert len(clients) == 2


def test_restart_after_engine_commit_reconciles_exact_qa_without_second_aux_call(tmp_path):
    repo, experiment, _manifest = _make_experiment(tmp_path, only_zh=True)
    _collect(experiment, repo, 1)
    clients, factory = _client_factory_registry()

    def crash(stage, _context):
        if stage == "after_engine_run":
            raise KeyboardInterrupt("synthetic crash after real engine commit")

    with pytest.raises(KeyboardInterrupt):
        process_collected_wave(
            experiment,
            repo,
            1,
            client_factory=factory,
            crash_hook=crash,
        )
    assert len(clients) == 1
    qa_path = (
        experiment
        / "projects"
        / "great_king_zhuxia"
        / "output"
        / "qa"
        / "0001.json"
    )
    qa = json.loads(qa_path.read_text(encoding="utf-8"))
    assert qa[ACCEPTANCE_FIELD]["batch_id"] == "batch-wave-1"

    def forbidden_factory(_novel_id, _chapter):
        pytest.fail("exact committed QA must reconcile without a second auxiliary call")

    assert process_collected_wave(
        experiment, repo, 1, client_factory=forbidden_factory
    )["status"] == "complete"
    assert len(clients) == 1


def test_catastrophic_heading_only_batch_result_never_reaches_state_or_glossary(tmp_path):
    repo, experiment, manifest = _make_experiment(
        tmp_path,
        long_source=True,
        target_chapters=1,
        only_zh=True,
    )
    project = experiment / "projects" / "great_king_zhuxia"
    state_paths = [
        project / "data" / name
        for name in (
            "glossary.json",
            "continuity.json",
            "issues.json",
            "audit_history.json",
        )
    ]
    before = {path.name: path.read_bytes() for path in state_paths}
    _collect(
        experiment,
        repo,
        1,
        translations={
            "great_king_zhuxia": "Chapter Eighteen: Buried Three Inches Deep!"
        },
    )
    clients, factory = _client_factory_registry()

    with pytest.raises(FullPipelineExperimentNotCommitted):
        process_collected_wave(
            experiment, repo, 1, client_factory=factory
        )

    # The in-memory client may be installed under the real engine, but the hard
    # gate must stop before any auxiliary provider request is attempted.
    assert len(clients) == 1
    assert clients[0].completions.calls == []
    assert {path.name: path.read_bytes() for path in state_paths} == before
    progress = json.loads(
        (project / "data" / "progress.json").read_text(encoding="utf-8")
    )
    assert progress["last_completed"] == 0
    assert progress["next_chapter"] == 1
    pending_meta = json.loads(
        (project / "output" / "pending" / "0001.json").read_text(encoding="utf-8")
    )
    assert any(
        item["type"] == "catastrophic_incompleteness"
        for item in pending_meta["local_qa"]["commit_blockers"]
    )
    processing = json.loads(
        (
            experiment
            / "waves"
            / "wave_0001"
            / PROCESSING_FILENAME
        ).read_text(encoding="utf-8")
    )
    assert processing["status"] == "processing"
    assert processing["requests"][0]["status"] == "pending_installed"
    assert capture_production_guard(repo, manifest["novel_ids"]) == manifest["production_guard"]


def test_lineage_changes_only_after_accepted_real_engine_commit(tmp_path):
    repo, experiment, manifest = _make_experiment(tmp_path, only_zh=True)
    project = experiment / "projects" / "great_king_zhuxia"
    before = capture_experiment_lineage(project)
    _collect(experiment, repo, 1)
    clients, factory = _client_factory_registry()
    process_collected_wave(experiment, repo, 1, client_factory=factory)
    after = capture_experiment_lineage(project)
    assert before["last_completed"] == 0
    assert after["last_completed"] == 1
    assert before["glossary_sha256"] != after["glossary_sha256"]
    assert before["continuity_sha256"] != after["continuity_sha256"]
    assert after["lineage_sha256"] != before["lineage_sha256"]
    assert manifest["initial_lineages"]["great_king_zhuxia"] == before
