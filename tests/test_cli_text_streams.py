import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(code):
    env = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONUTF8="0")
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, check=False)


def test_legacy_code_page_cannot_print_source_text_without_it():
    assert _run("print('殘留 한글')").returncode != 0


def test_configured_streams_print_source_text_as_utf8():
    done = _run("from cli_text_streams import configure_cli_text_streams\n"
                "configure_cli_text_streams()\nprint('殘留 한글')")
    assert done.returncode == 0, done.stderr
    assert done.stdout.decode("utf-8").strip() == "殘留 한글"


def test_entry_points_configure_streams_before_work():
    for name in ("translate.py", "chapter_review.py", "extract_epub.py",
                 "goat_periodic_audit_finalize_plan.py", "goat_periodic_audit_finalize_execute.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        main = text[text.index("def main(argv=None):"):]
        assert "configure_cli_text_streams()" in main.split("\ndef ", 1)[0], name
