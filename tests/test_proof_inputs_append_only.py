"""Files a batch prepared ahead adds during an audit proof, on a synthetic novel folder."""
from types import SimpleNamespace

import pytest

from historical_reconciliation import proof_input_scope
from production_failure_classification import ProductionIntegrityError


@pytest.fixture
def novel(tmp_path):
    root = tmp_path / "novel"
    for folder in ("output/chapters", "output/audits", "output/retroactive_fixes/transactions",
                   "data/provider_request_journal", "backup/superseded_translations/batch_0001"):
        (root / folder).mkdir(parents=True)
    (root / "output/chapters/0001.md").write_text("Chapter one.", encoding="utf-8")
    (root / "data/provider_request_journal/0001.chapter_translation.json").write_text("{}", encoding="utf-8")
    (root / "data/provider_request_journal/0002.chapter_translation.json").write_text("{}", encoding="utf-8")
    module = SimpleNamespace(OUTPUT_DIR=root / "output/chapters", ACTIVE_NOVEL_CONFIG=None, chapter_map={},
                             ACTIVE_PROFILE=None)
    return root, module


def prove(module, during, read=None):
    with proof_input_scope(module) as inputs:
        inputs.read(module.OUTPUT_DIR / "0001.md")
        if read is not None:
            read(inputs)
        during()


def test_requests_a_batch_ahead_adds_to_the_journal_do_not_stop_the_proof(novel):
    root, module = novel
    journal = root / "data/provider_request_journal"
    prove(module, lambda: (journal / "0060.chapter_translation.json").write_text("{}", encoding="utf-8"),
          read=lambda inputs: inputs.read(journal / "0001.chapter_translation.json"))


def test_backups_of_chapters_translated_again_do_not_stop_the_proof(novel):
    root, module = novel
    superseded = root / "backup/superseded_translations"
    prove(module, lambda: ((superseded / "batch_0051").mkdir(),
                           (superseded / "batch_0001" / "0003.md").write_text("old", encoding="utf-8")))


def test_the_first_superseded_backup_folder_may_appear(novel):
    root, module = novel
    import shutil
    shutil.rmtree(root / "backup/superseded_translations")
    prove(module, lambda: (root / "backup/superseded_translations/batch_0051").mkdir(parents=True))


@pytest.mark.parametrize("change", ["journal_file_removed", "looked_for_then_added", "audit_added", "chapter_added",
                                    "other_backup_added"])
def test_any_other_change_still_stops_the_proof(novel, change):
    root, module = novel
    journal = root / "data/provider_request_journal"
    read = lambda inputs: inputs.read(journal / "0001.chapter_translation.json")
    if change == "journal_file_removed":
        during = lambda: (journal / "0002.chapter_translation.json").unlink()  # One the proof never read.
    elif change == "looked_for_then_added":
        read = lambda inputs: (inputs.read(journal / "0001.chapter_translation.json"),
                               inputs.sha(journal / "0050.periodic_audit.json"))
        during = lambda: (journal / "0050.periodic_audit.json").write_text("{}", encoding="utf-8")
    elif change == "audit_added":
        during = lambda: (root / "output/audits/audit_0001_0050.json").write_text("{}", encoding="utf-8")
    elif change == "chapter_added":
        during = lambda: (root / "output/chapters/0002.md").write_text("Two.", encoding="utf-8")
    else:
        during = lambda: (root / "backup/checkpoint_0050").mkdir()
    with pytest.raises(ProductionIntegrityError, match="PROOF_INPUT_NAMESPACE_CHANGED"):
        prove(module, during, read=read)
