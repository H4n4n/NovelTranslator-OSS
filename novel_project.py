"""Import-safe configuration and path resolution for novel projects.

This module performs no I/O at import time.  A selected project owns every
mutable production path; repository-level Python files remain shared.
"""

from dataclasses import dataclass
import json
import os
import re
from pathlib import Path


NOVELS_ROOT_ENV = "NOVELTRANSLATOR_NOVELS_ROOT"
DEFAULT_FINAL_CHAPTER = 1406
DEFAULT_AUDIT_INTERVAL = 25
CHAPTER_NUMBERING_GLOBAL = "global"
CHAPTER_NUMBERING_SPINE_SEQUENTIAL = "spine_sequential"
CHAPTER_NUMBERING_MODES = frozenset((
    CHAPTER_NUMBERING_GLOBAL,
    CHAPTER_NUMBERING_SPINE_SEQUENTIAL,
))
SOURCE_TYPE_EPUB = "epub"
SOURCE_TYPE_TXT = "txt"
SOURCE_TYPES = frozenset((SOURCE_TYPE_EPUB, SOURCE_TYPE_TXT))
NOVEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

DEFAULT_RESIDUAL_CJK_MICRO_REPAIR_LIMITS = {
    "enabled": False,
    "max_total_cjk_chars": 12,
    "max_fragments": 3,
    "max_affected_lines": 3,
    "max_attempts_per_pending_version": 1,
}

DEFAULT_GLOSSARY_CANDIDATE_AUTO_RESOLUTION = {
    "enabled": False,
    "allow_model_adjudication": False,
    "max_model_requests_per_chapter": 1,
    "category_normalizations": {},
}


def _normalized_category(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return " ".join(value.strip().lower().replace("_", " ").replace("-", " ").split())


@dataclass(frozen=True)
class GlossaryCandidateAutoResolutionConfig:
    """Per-novel opt-in policy for selection-only candidate adjudication."""

    enabled: bool = False
    allow_model_adjudication: bool = False
    max_model_requests_per_chapter: int = 1
    category_normalizations: tuple = ()

    @classmethod
    def from_dict(cls, value):
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise ValueError(
                "novel.json glossary_candidate_auto_resolution must be a JSON object"
            )
        unknown = sorted(
            set(value) - set(DEFAULT_GLOSSARY_CANDIDATE_AUTO_RESOLUTION)
        )
        if unknown:
            raise ValueError(
                "novel.json glossary_candidate_auto_resolution contains "
                "unsupported fields: " + ", ".join(unknown)
            )
        merged = {**DEFAULT_GLOSSARY_CANDIDATE_AUTO_RESOLUTION, **value}
        for key in ("enabled", "allow_model_adjudication"):
            if not isinstance(merged[key], bool):
                raise ValueError(
                    f"novel.json glossary_candidate_auto_resolution.{key} "
                    "must be boolean"
                )
        maximum = merged["max_model_requests_per_chapter"]
        if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum != 1:
            raise ValueError(
                "novel.json glossary_candidate_auto_resolution."
                "max_model_requests_per_chapter must be exactly 1"
            )
        if merged["allow_model_adjudication"] and not merged["enabled"]:
            raise ValueError(
                "novel.json glossary_candidate_auto_resolution cannot allow model "
                "adjudication while automatic resolution is disabled"
            )
        raw_normalizations = merged["category_normalizations"]
        if not isinstance(raw_normalizations, dict):
            raise ValueError(
                "novel.json glossary_candidate_auto_resolution."
                "category_normalizations must be a JSON object"
            )
        normalizations = []
        seen_sources = set()
        for raw_source, raw_target in raw_normalizations.items():
            source = _normalized_category(
                raw_source,
                "novel.json glossary_candidate_auto_resolution."
                "category_normalizations key",
            )
            target = _normalized_category(
                raw_target,
                "novel.json glossary_candidate_auto_resolution."
                f"category_normalizations.{raw_source}",
            )
            if source in seen_sources:
                raise ValueError(
                    "novel.json glossary_candidate_auto_resolution."
                    "category_normalizations contains duplicate normalized keys"
                )
            if source == target:
                raise ValueError(
                    "novel.json glossary_candidate_auto_resolution."
                    "category_normalizations cannot contain identity mappings"
                )
            seen_sources.add(source)
            normalizations.append((source, target))
        mapping = dict(normalizations)
        for source, target in normalizations:
            if target in mapping:
                raise ValueError(
                    "novel.json glossary_candidate_auto_resolution."
                    "category_normalizations cannot contain chains or cycles"
                )
        normalizations.sort()
        return cls(
            enabled=merged["enabled"],
            allow_model_adjudication=merged["allow_model_adjudication"],
            max_model_requests_per_chapter=maximum,
            category_normalizations=tuple(normalizations),
        )

    def category_normalization_map(self):
        return dict(self.category_normalizations)

    def to_dict(self):
        return {
            "enabled": self.enabled,
            "allow_model_adjudication": self.allow_model_adjudication,
            "max_model_requests_per_chapter": self.max_model_requests_per_chapter,
            "category_normalizations": dict(self.category_normalizations),
        }


@dataclass(frozen=True)
class ResidualCJKMicroRepairConfig:
    """Per-novel opt-in limits for one bounded paid repair request."""

    enabled: bool = False
    max_total_cjk_chars: int = 12
    max_fragments: int = 3
    max_affected_lines: int = 3
    max_attempts_per_pending_version: int = 1

    @classmethod
    def from_dict(cls, value):
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise ValueError(
                "novel.json residual_cjk_micro_repair must be a JSON object"
            )
        unknown = sorted(
            set(value) - set(DEFAULT_RESIDUAL_CJK_MICRO_REPAIR_LIMITS)
        )
        if unknown:
            raise ValueError(
                "novel.json residual_cjk_micro_repair contains unsupported fields: "
                + ", ".join(unknown)
            )
        merged = {**DEFAULT_RESIDUAL_CJK_MICRO_REPAIR_LIMITS, **value}
        if not isinstance(merged["enabled"], bool):
            raise ValueError(
                "novel.json residual_cjk_micro_repair.enabled must be boolean"
            )
        for key in (
            "max_total_cjk_chars",
            "max_fragments",
            "max_affected_lines",
            "max_attempts_per_pending_version",
        ):
            item = merged[key]
            if not isinstance(item, int) or isinstance(item, bool) or item < 1:
                raise ValueError(
                    f"novel.json residual_cjk_micro_repair.{key} "
                    "must be a positive integer"
                )
        return cls(**merged)

    def to_dict(self):
        return {
            "enabled": self.enabled,
            "max_total_cjk_chars": self.max_total_cjk_chars,
            "max_fragments": self.max_fragments,
            "max_affected_lines": self.max_affected_lines,
            "max_attempts_per_pending_version": (
                self.max_attempts_per_pending_version
            ),
        }


@dataclass(frozen=True)
class NovelConfig:
    id: str
    title: str
    source_language: str
    target_language: str
    profile: str
    final_chapter: int
    audit_interval: int = DEFAULT_AUDIT_INTERVAL
    author: str | None = None
    chapter_numbering: str = CHAPTER_NUMBERING_GLOBAL
    source_type: str = SOURCE_TYPE_EPUB
    residual_cjk_micro_repair: ResidualCJKMicroRepairConfig = (
        ResidualCJKMicroRepairConfig()
    )
    glossary_candidate_auto_resolution: GlossaryCandidateAutoResolutionConfig = (
        GlossaryCandidateAutoResolutionConfig()
    )

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ValueError("novel.json must contain a JSON object")
        required = (
            "id", "title", "source_language", "target_language", "profile",
            "final_chapter",
        )
        missing = [key for key in required if key not in value]
        if missing:
            raise ValueError("novel.json is missing: " + ", ".join(missing))
        novel_id = validate_novel_id(value["id"])
        strings = {}
        for key in ("title", "source_language", "target_language", "profile"):
            item = value[key]
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"novel.json {key} must be a non-empty string")
            strings[key] = item.strip()
        final_chapter = value["final_chapter"]
        if not isinstance(final_chapter, int) or isinstance(final_chapter, bool) or final_chapter < 1:
            raise ValueError("novel.json final_chapter must be a positive integer")
        audit_interval = value.get("audit_interval", DEFAULT_AUDIT_INTERVAL)
        if not isinstance(audit_interval, int) or isinstance(audit_interval, bool) or audit_interval < 1:
            raise ValueError("novel.json audit_interval must be a positive integer")
        author = value.get("author")
        if author is not None:
            if not isinstance(author, str) or not author.strip():
                raise ValueError("novel.json author must be a non-empty string when supplied")
            author = author.strip()
        chapter_numbering = value.get(
            "chapter_numbering", CHAPTER_NUMBERING_GLOBAL
        )
        if chapter_numbering not in CHAPTER_NUMBERING_MODES:
            choices = ", ".join(sorted(CHAPTER_NUMBERING_MODES))
            raise ValueError(
                f"novel.json chapter_numbering must be one of: {choices}"
            )
        source_type = value.get("source_type", SOURCE_TYPE_EPUB)
        if source_type not in SOURCE_TYPES:
            choices = ", ".join(sorted(SOURCE_TYPES))
            raise ValueError(
                f"novel.json source_type must be one of: {choices}"
            )
        residual_cjk_micro_repair = ResidualCJKMicroRepairConfig.from_dict(
            value.get("residual_cjk_micro_repair")
        )
        glossary_candidate_auto_resolution = (
            GlossaryCandidateAutoResolutionConfig.from_dict(
                value.get("glossary_candidate_auto_resolution")
            )
        )
        return cls(
            id=novel_id,
            final_chapter=final_chapter,
            audit_interval=audit_interval,
            author=author,
            chapter_numbering=chapter_numbering,
            source_type=source_type,
            residual_cjk_micro_repair=residual_cjk_micro_repair,
            glossary_candidate_auto_resolution=(
                glossary_candidate_auto_resolution
            ),
            **strings,
        )

    def to_dict(self):
        result = {
            "id": self.id,
            "title": self.title,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "profile": self.profile,
            "final_chapter": self.final_chapter,
            "audit_interval": self.audit_interval,
            "chapter_numbering": self.chapter_numbering,
        }
        if self.author is not None:
            result["author"] = self.author
        # EPUB is the backward-compatible default, so old projects need no
        # migration and newly serialized EPUB projects retain their old shape.
        if self.source_type != SOURCE_TYPE_EPUB:
            result["source_type"] = self.source_type
        if self.residual_cjk_micro_repair.enabled:
            result["residual_cjk_micro_repair"] = (
                self.residual_cjk_micro_repair.to_dict()
            )
        if self.glossary_candidate_auto_resolution.enabled:
            result["glossary_candidate_auto_resolution"] = (
                self.glossary_candidate_auto_resolution.to_dict()
            )
        return result


@dataclass(frozen=True)
class NovelProject:
    repository_root: Path
    root: Path
    config: NovelConfig
    is_legacy: bool = False

    @property
    def config_file(self):
        return self.root / "novel.json"

    @property
    def source_dir(self):
        return self.root / "source"

    @property
    def data_dir(self):
        return self.root / "data"

    @property
    def output_root(self):
        return self.root / "output"

    @property
    def chapters_file(self):
        return self.data_dir / "chapters.json"

    @property
    def glossary_file(self):
        return self.data_dir / "glossary.json"

    @property
    def progress_file(self):
        return self.data_dir / "progress.json"

    @property
    def continuity_file(self):
        return self.data_dir / "continuity.json"

    @property
    def issues_file(self):
        return self.data_dir / "issues.json"

    @property
    def audit_history_file(self):
        return self.data_dir / "audit_history.json"

    @property
    def glossary_candidate_resolution_dir(self):
        return self.data_dir / "glossary_candidate_resolutions"

    @property
    def chapter_dir(self):
        return self.output_root / "chapters"

    @property
    def pending_dir(self):
        return self.output_root / "pending"

    @property
    def qa_dir(self):
        return self.output_root / "qa"

    @property
    def audit_dir(self):
        return self.output_root / "audits"

    @property
    def retroactive_dir(self):
        return self.output_root / "retroactive_fixes"

    @property
    def epub_dir(self):
        return self.output_root / "epub"

    @property
    def benchmark_dir(self):
        return self.output_root / "benchmark"

    @property
    def backup_dir(self):
        return self.root / "backup"

    @property
    def logs_dir(self):
        return self.root / "logs"

    @property
    def pause_file(self):
        return self.root / "pause.flag"

    @property
    def source_epub(self):
        return self.source_dir / "novel.epub"

    @property
    def source_txt(self):
        return self.source_dir / "novel.txt"

    @property
    def source_file(self):
        """Return the configured authoritative source without probing by guess."""
        if self.config.source_type == SOURCE_TYPE_TXT:
            return self.source_txt
        return self.source_epub

    @property
    def source_overrides_file(self):
        """Fixed novel-owned exceptions for confirmed malformed source headings."""
        return self.root / "source_overrides.json"

    @property
    def translation_prompt_file(self):
        return self.root / "production_translation.txt"

    @property
    def translation_addendum_file(self):
        """Fixed optional novel-owned instructions; never controlled by a profile."""
        return self.root / "translation_addendum.txt"


def validate_novel_id(novel_id):
    if not isinstance(novel_id, str) or not NOVEL_ID_RE.fullmatch(novel_id):
        raise ValueError(
            "novel identifier must contain only lowercase letters, digits, '-' or '_', "
            "and must start with a letter or digit"
        )
    return novel_id


def repository_root():
    return Path(__file__).resolve().parent


def novels_root_for(repo_root):
    """Novel data location: NOVELTRANSLATOR_NOVELS_ROOT, else <repo>/novels."""
    configured = os.environ.get(NOVELS_ROOT_ENV)
    return Path(configured).resolve() if configured else (Path(repo_root) / "novels").resolve()


def load_novel_config(path):
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        return NovelConfig.from_dict(json.load(handle))


def load_novel_project(novel_id, repo_root=None):
    novel_id = validate_novel_id(novel_id)
    repo_root = Path(repo_root or repository_root()).resolve()
    novels_root = novels_root_for(repo_root)
    project_root = (novels_root / novel_id).resolve()
    if project_root.parent != novels_root:
        raise ValueError("novel path escapes the novels directory")
    config_file = project_root / "novel.json"
    if not config_file.is_file():
        raise FileNotFoundError(f"novel project does not exist: {config_file}")
    config = load_novel_config(config_file)
    if config.id != novel_id:
        raise ValueError(
            f"novel.json id {config.id!r} does not match selected novel {novel_id!r}"
        )
    return NovelProject(repo_root, project_root, config)


def legacy_project(repo_root=None):
    """Return a compatibility view of the original repository-level project."""
    repo_root = Path(repo_root or repository_root()).resolve()
    config = NovelConfig(
        id="legacy",
        title="Checkpoint English Translation",
        source_language="zh",
        target_language="en",
        profile="historical_military",
        final_chapter=DEFAULT_FINAL_CHAPTER,
    )
    return NovelProject(repo_root, repo_root, config, is_legacy=True)


def select_project(novel_id=None, repo_root=None):
    return (
        load_novel_project(novel_id, repo_root)
        if novel_id
        else legacy_project(repo_root)
    )
