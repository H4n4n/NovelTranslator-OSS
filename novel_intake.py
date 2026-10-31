"""Local, zero-API intake: raw EPUB or TXT to a created novel project.

analyze_source() is read-only. create_from_analysis() creates the project,
installs the source, runs the existing extraction and writes the addendum.
"""

import hashlib
from pathlib import Path
import re
import shutil
import tempfile
import unicodedata

from create_novel import create_novel_project
from extract_epub import (
    ExtractionError,
    dry_run_extract_epub,
    extract_source_epub,
    install_extraction as install_epub_extraction,
)
from extract_txt import (
    TXTExtractionError,
    extract_source_txt,
    extract_txt_text,
    install_extraction as install_txt_extraction,
)
from novel_project import (
    CHAPTER_NUMBERING_GLOBAL,
    CHAPTER_NUMBERING_SPINE_SEQUENTIAL,
    NOVEL_ID_RE,
    NovelConfig,
    SOURCE_TYPE_EPUB,
    SOURCE_TYPE_TXT,
    load_novel_project,
    novels_root_for,
    repository_root,
)
from translation_profile import _novel_policy_rules, load_translation_profile


ENCODING_CANDIDATES = ("gb18030", "big5", "cp949")
SAMPLE_CHARACTERS = 1200
NARRATIVE_PROFILE = "historical_military"

_HANGUL = re.compile(r"[ᄀ-ᇿ㄰-㆏가-힯]")
_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
# Characters that are frequent in running text and identical in Simplified and
# Traditional Chinese, plus common Korean particles. Text decoded with the wrong
# codec produces rare characters instead, so these separate near-ties.
_COMMON = frozenset("的一不人在有中大上我他你是了也就都要可以이가은는을를의에다고")
_TRADITIONAL = frozenset("們這個來說國時為會學對從與發經電長開關後還過門東車書體讓應點")
_SIMPLIFIED = frozenset("们这个来说国时为会学对从与发经电长开关后还过门东车书体让应点")
# Characters present in both sets above are not evidence either way.
_TRAD_ONLY = _TRADITIONAL - _SIMPLIFIED
_SIMP_ONLY = _SIMPLIFIED - _TRADITIONAL


def _script_share(text):
    visible = [c for c in text if not c.isspace()]
    if not visible:
        return 0.0
    hits = sum(1 for c in visible if _CJK.match(c) or _HANGUL.match(c))
    common = sum(1 for c in visible if c in _COMMON)
    return hits / len(visible) + 2.0 * common / len(visible)


def detect_encoding_and_decode(raw_bytes):
    """Return (text, encoding, confidence) for raw TXT bytes."""
    raw_bytes = bytes(raw_bytes)
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = None
    if text is not None:
        encoding = "utf-8-sig" if raw_bytes.startswith(b"\xef\xbb\xbf") else "utf-8"
        return text, encoding, "high"
    scored = []
    for name in ENCODING_CANDIDATES:
        try:
            decoded = raw_bytes.decode(name)
        except UnicodeDecodeError:
            continue
        scored.append((_script_share(decoded), name, decoded))
    if not scored:
        # Last resort so the caller can show something; confidence says it all.
        return raw_bytes.decode("utf-8", errors="replace"), "utf-8", "low"
    scored.sort(key=lambda item: -item[0])
    best = scored[0]
    margin = best[0] - scored[1][0] if len(scored) > 1 else best[0]
    visible_share = best[0]
    confidence = "high" if visible_share >= 0.6 and margin >= 0.1 else "low"
    return best[2], best[1], confidence


def normalize_txt_source(path, dest):
    """Write a UTF-8 copy of a TXT source for extract_txt; original untouched."""
    path, dest = Path(path), Path(dest)
    text, encoding, confidence = detect_encoding_and_decode(path.read_bytes())
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(text.encode("utf-8"))
    return {"path": dest, "encoding": encoding, "confidence": confidence}


def detect_language(text):
    """Return "ko", "zh" (Simplified) or "zh-Hant" (Traditional)."""
    hangul = len(_HANGUL.findall(text))
    cjk = len(_CJK.findall(text))
    if hangul and hangul >= 0.3 * (hangul + cjk):
        return "ko"
    trad = sum(1 for c in text if c in _TRAD_ONLY)
    simp = sum(1 for c in text if c in _SIMP_ONLY)
    return "zh-Hant" if trad > simp else "zh"


def slugify_title(title, *, repo_root=None, taken=None):
    """Lowercase ASCII id from a title; hash fallback for CJK-only titles."""
    ascii_title = (
        unicodedata.normalize("NFKD", title or "")
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title).strip("-")[:48].strip("-")
    if not slug:
        digest = hashlib.sha1((title or "").encode("utf-8")).hexdigest()[:8]
        slug = f"novel-{digest}"
    novels_root = novels_root_for(repo_root or repository_root())
    taken = set(taken or ())
    candidate, counter = slug, 2
    while candidate in taken or (novels_root / candidate).exists():
        candidate = f"{slug}-{counter}"
        counter += 1
    assert NOVEL_ID_RE.fullmatch(candidate)
    return candidate


def _truncate(text, limit=SAMPLE_CHARACTERS):
    return text if len(text) <= limit else text[:limit]


def _samples(chapters):
    count = len(chapters)
    if not count:
        return []
    picks = list(range(min(3, count)))
    for index in (count // 2, count // 2 + 1):
        if index < count and index not in picks:
            picks.append(index)
    return [
        {
            "chapter": chapters[i].get("chapter"),
            "title": chapters[i].get("title") or chapters[i].get("heading") or "",
            "text": _truncate(chapters[i].get("text", "")),
        }
        for i in picks
    ]


def _junk_from_lengths(chapters):
    sizes = sorted(len(c.get("text", "")) for c in chapters)
    if len(sizes) < 5:
        return []
    median = sizes[len(sizes) // 2]
    junk = []
    for chapter in chapters:
        size = len(chapter.get("text", ""))
        if median and size < 0.1 * median:
            junk.append({
                "kind": "very_short_chapter",
                "chapter": chapter.get("chapter"),
                "title": chapter.get("title") or chapter.get("heading") or "",
                "characters": size,
            })
    return junk


def _clean_title(value):
    value = (value or "").strip().strip("《》<>「」『』").strip()
    return value


def _empty_analysis(path, source_type):
    return {
        "source_path": str(path),
        "source_type": source_type,
        "encoding": None,
        "encoding_confidence": None,
        "language": None,
        "title_guess": _clean_title(Path(path).stem),
        "author_guess": None,
        "chapter_count": 0,
        "final_chapter": None,
        "gaps": [],
        "duplicates": [],
        "suspected_junk": [],
        "chapter_numbering": None,
        "samples": [],
        "warnings": [],
        "error": None,
    }


def _analyze_txt(path, analysis):
    raw = Path(path).read_bytes()
    text, encoding, confidence = detect_encoding_and_decode(raw)
    analysis["encoding"] = encoding
    analysis["encoding_confidence"] = confidence
    analysis["language"] = detect_language(text)
    if confidence == "low":
        analysis["warnings"].append(
            "encoding could not be determined with confidence; check the sample text"
        )
    utf8_bytes = text.encode("utf-8")
    try:
        result = extract_txt_text(
            text, hashlib.sha256(utf8_bytes).hexdigest(), source_file="novel.txt"
        )
    except TXTExtractionError as exc:
        analysis["error"] = str(exc)
        return analysis
    chapters, report = result["chapters"], result["report"]
    lines = text.splitlines()
    first_line = chapters[0]["source_line_start"]
    for line in lines[: first_line - 1]:
        if line.strip() and len(line.strip()) <= 40:
            analysis["title_guess"] = _clean_title(line)
            break
    analysis["chapter_count"] = len(chapters)
    analysis["final_chapter"] = len(chapters)
    analysis["chapter_numbering"] = CHAPTER_NUMBERING_SPINE_SEQUENTIAL
    analysis["gaps"] = list(report["source_number_missing"])
    analysis["duplicates"] = list(report["source_number_duplicates"])
    analysis["suspected_junk"] = [
        {"kind": item["classification"], "line": item["line_start"]}
        for item in report["excluded_sections"]
        if item["classification"] not in ("heading",)
    ] + _junk_from_lengths(chapters)
    analysis["samples"] = _samples(chapters)
    if analysis["gaps"] or analysis["duplicates"]:
        analysis["warnings"].append(
            "source chapter numbers have gaps or duplicates; review before translating"
        )
    return analysis


def _epub_metadata(path):
    from ebooklib import epub

    try:
        book = epub.read_epub(str(path))
        title = book.get_metadata("DC", "title")
        author = book.get_metadata("DC", "creator")
    except Exception:
        return None, None
    return (
        title[0][0] if title else None,
        author[0][0] if author else None,
    )


def _epub_clean(result):
    # The placeholder final_chapter makes only final_chapter errors meaningless.
    return [e for e in result.validation_errors if "final_chapter" not in e]


def _analyze_epub(path, analysis):
    title, author = _epub_metadata(path)
    if title:
        analysis["title_guess"] = _clean_title(title)
    analysis["author_guess"] = author
    analysis["encoding"] = "epub"
    chosen = None
    first_errors = None
    try:
        for mode in (CHAPTER_NUMBERING_GLOBAL, CHAPTER_NUMBERING_SPINE_SEQUENTIAL):
            result = dry_run_extract_epub(path, chapter_numbering=mode)
            errors = _epub_clean(result)
            if first_errors is None:
                first_errors, first_result = errors, result
            if not errors:
                chosen = (mode, result)
                break
    except (ExtractionError, OSError, ValueError) as exc:
        analysis["error"] = str(exc)
        return analysis
    if chosen is None:
        mode, result = CHAPTER_NUMBERING_GLOBAL, first_result
        analysis["error"] = "; ".join(first_errors) or "no chapters extracted"
    else:
        mode, result = chosen
    chapters = list(result.chapters)
    if chapters:
        analysis["language"] = detect_language(
            "\n".join(c.get("text", "") for c in chapters[:20])
        )
        analysis["final_chapter"] = max(c["chapter"] for c in chapters)
    analysis["chapter_count"] = len(chapters)
    analysis["chapter_numbering"] = mode
    # Gaps and duplicates always describe the source's own numbering, even when
    # spine_sequential indexing makes the library installable.
    analysis["gaps"] = list(first_result.missing_chapters)
    analysis["duplicates"] = list(first_result.duplicate_chapters)
    if chosen is not None and mode != CHAPTER_NUMBERING_GLOBAL:
        analysis["warnings"].append(
            "visible chapter numbers are irregular; using spine_sequential numbering"
        )
    junk = []
    for label, items in (
        ("meta_document", result.meta_documents),
        ("extra_document", result.extra_documents),
        ("unmatched_document", result.unmatched_documents),
    ):
        for item in items:
            junk.append({"kind": label, "detail": _short(item)})
    if result.suspicious_hangul_characters:
        junk.append({
            "kind": "unexpected_hangul",
            "characters": result.suspicious_hangul_characters,
        })
    analysis["suspected_junk"] = junk + _junk_from_lengths(chapters)
    analysis["samples"] = _samples(chapters)
    analysis["warnings"].extend(str(w) for w in result.local_numbering_warnings)
    return analysis


def _short(item):
    if isinstance(item, dict):
        return {
            key: value for key, value in item.items()
            if isinstance(value, (str, int, float)) and key != "text"
        }
    return str(item)[:200]


def analyze_source(path):
    """Read-only analysis of an EPUB or TXT source; never raises on bad input."""
    path = Path(path)
    suffix = path.suffix.lower()
    source_type = {".epub": SOURCE_TYPE_EPUB, ".txt": SOURCE_TYPE_TXT}.get(suffix)
    analysis = _empty_analysis(path, source_type)
    if source_type is None:
        analysis["error"] = f"unsupported source type {suffix!r}; use .epub or .txt"
        return analysis
    if not path.is_file():
        analysis["error"] = f"source file does not exist: {path}"
        return analysis
    try:
        if source_type == SOURCE_TYPE_TXT:
            return _analyze_txt(path, analysis)
        return _analyze_epub(path, analysis)
    except Exception as exc:  # analysis must never crash the UI
        analysis["error"] = f"{type(exc).__name__}: {exc}"
        return analysis


def create_from_analysis(
    analysis, *, novel_id, title, profile, addendum="", repo_root=None
):
    """Create the project from a clean analysis and run extraction."""
    if analysis.get("error"):
        raise ValueError("cannot create from an analysis with an error: " + analysis["error"])
    for key in ("source_type", "language", "final_chapter", "chapter_numbering"):
        if not analysis.get(key):
            raise ValueError(f"analysis is missing {key}")
    repo_root = Path(repo_root or repository_root()).resolve()
    load_translation_profile(profile, repo_root)
    addendum = (addendum or "").strip()
    if addendum:
        _novel_policy_rules(addendum)  # reject a malformed policy block early
    source_type = analysis["source_type"]
    config = NovelConfig(
        id=novel_id,
        title=title,
        source_language=analysis["language"],
        target_language="en",
        profile=profile,
        final_chapter=int(analysis["final_chapter"]),
        chapter_numbering=analysis["chapter_numbering"],
        source_type=source_type,
        author=analysis.get("author_guess") or None,
    )
    with tempfile.TemporaryDirectory() as scratch:
        source = Path(analysis["source_path"])
        if source_type == SOURCE_TYPE_TXT:
            source = normalize_txt_source(source, Path(scratch) / "novel.txt")["path"]
        project_root = create_novel_project(config, repo_root, source_path=source)
    try:
        project = load_novel_project(novel_id, repo_root)
        if source_type == SOURCE_TYPE_TXT:
            result = extract_source_txt(project)
            install_txt_extraction(project, result)
        else:
            result = extract_source_epub(project)
            install_epub_extraction(project, result)
        if addendum:
            project.translation_addendum_file.write_text(
                addendum + "\n", encoding="utf-8", newline="\n"
            )
    except Exception:
        shutil.rmtree(project_root, ignore_errors=True)
        raise
    return project
