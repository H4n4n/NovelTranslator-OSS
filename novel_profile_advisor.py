"""One cheap model request that suggests a profile and drafts an addendum.

The module never reads credentials. The caller injects an OpenAI-compatible
client. Nothing is written to the repository unless save_profile() is called.
"""

import json
from pathlib import Path
import re
import shutil
import tempfile

from translation_profile import (
    MAX_NOVEL_POLICY_RULE_CHARS,
    MAX_NOVEL_POLICY_RULES,
    NOVEL_POLICY_END,
    NOVEL_POLICY_START,
    _novel_policy_rules,
    load_translation_profile,
    validate_profile_id,
)

try:  # present on branches that carry the retry policy
    from provider_retry_policy import send_with_retries
except ImportError:  # pragma: no cover - depends on branch
    send_with_retries = None


ADVICE_MODEL = "deepseek/deepseek-v4.1-flash"
MAX_PROMPT_SAMPLE_CHARACTERS = 900

OWNER_TERMINOLOGY_PREFERENCES = (
    "Keep Chinese historical and cultivation terminology conceptually Chinese, "
    "not Westernized: prefer `Hundred-Household Commander` over `Centurion`.",
    "Prefer semantic English for sect and institution names, such as "
    "`Azure Cloud Sect`, over pinyin-only forms.",
    "Personal names use standard romanization (pinyin for Chinese).",
    "Capitalize the first letter of EVERY word in formal technique, formation "
    "and secret-art names, for example `Black Tiger Seizes The Heart`.",
)

_SYSTEM = (
    "You help set up a Chinese or Korean web-novel for English translation. "
    "Pick the closest existing translation profile and draft novel-specific "
    "terminology rules. Reply with one JSON object only."
)


def _profile_fields(profile):
    if isinstance(profile, dict):
        return profile.get("id"), profile.get("display_name", ""), profile.get("description", "")
    return profile.id, profile.display_name, profile.description


def list_profiles(repo_root=None):
    """Load every installed profile as (id, display_name, description) dicts."""
    root = Path(repo_root or Path(__file__).resolve().parent) / "profiles"
    found = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        try:
            profile = load_translation_profile(folder.name, root.parent)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        found.append({
            "id": profile.id,
            "display_name": profile.display_name,
            "description": profile.description,
        })
    return found


def build_advice_request(analysis, profiles):
    """Return one OpenAI-style chat completion request body."""
    profile_lines = []
    for profile in profiles:
        pid, name, description = _profile_fields(profile)
        profile_lines.append(f"- {pid}: {name}. {description}")
    sample_blocks = []
    for sample in analysis.get("samples", []):
        text = (sample.get("text") or "")[:MAX_PROMPT_SAMPLE_CHARACTERS]
        sample_blocks.append(f"[Chapter {sample.get('chapter')}: {sample.get('title', '')}]\n{text}")
    preferences = "\n".join(f"- {line}" for line in OWNER_TERMINOLOGY_PREFERENCES)
    user = (
        f"Novel title guess: {analysis.get('title_guess', '')}\n"
        f"Source language: {analysis.get('language')}\n"
        f"Chapters: {analysis.get('chapter_count')}\n\n"
        "Available profiles:\n" + "\n".join(profile_lines) + "\n\n"
        "Owner terminology preferences:\n" + preferences + "\n\n"
        "Sample chapters:\n" + "\n\n".join(sample_blocks) + "\n\n"
        "Return JSON with keys: profile_id (one of the available ids, or null "
        "if none fits), confidence (high, medium or low), reason (one or two "
        "sentences), addendum (short prose: world, tone, title conventions, "
        "recurring names), policy_rules (list of up to "
        f"{MAX_NOVEL_POLICY_RULES} short plain rule strings about "
        "terminology distinctions), new_profile (null, or an object with id, "
        "display_name, description, instructions when no profile fits)."
    )
    return {
        "model": ADVICE_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }


def _safe_result(reason):
    return {
        "profile_id": None,
        "confidence": "low",
        "reason": reason,
        "addendum": "",
        "new_profile_hint": None,
    }


def _extract_json(text):
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            value = json.loads(text[start:end + 1])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            return None
    return None


def _build_addendum(prose, rules):
    clean = []
    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, str):
            continue
        rule = rule.strip().lstrip("-").strip()
        if rule and len(rule) <= MAX_NOVEL_POLICY_RULE_CHARS and rule not in clean:
            clean.append(rule)
    clean = clean[:MAX_NOVEL_POLICY_RULES]
    prose = prose.strip() if isinstance(prose, str) else ""
    # The model must not smuggle its own markers into the prose.
    prose = prose.replace(NOVEL_POLICY_START, "").replace(NOVEL_POLICY_END, "").strip()
    parts = [prose] if prose else []
    if clean:
        block = (
            NOVEL_POLICY_START + "\n"
            + "\n".join(f"- {rule}" for rule in clean) + "\n"
            + NOVEL_POLICY_END
        )
        _novel_policy_rules(block)
        parts.append(block)
    return "\n\n".join(parts)


def parse_advice(text, profiles=None):
    """Tolerant parse. Unknown profile or bad output gives profile_id=None.

    profiles: optional iterable used to validate the suggested id; when
    omitted, only the id syntax is checked.
    """
    data = _extract_json(text)
    if data is None:
        return _safe_result("the model reply could not be parsed")
    known = None
    if profiles is not None:
        known = {_profile_fields(p)[0] for p in profiles}
    profile_id = data.get("profile_id")
    try:
        validate_profile_id(profile_id)
        if known is not None and profile_id not in known:
            raise ValueError("unknown profile")
    except ValueError:
        profile_id = None
    confidence = str(data.get("confidence", "low")).strip().lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "low"
    if profile_id is None:
        confidence = "low"
    reason = data.get("reason")
    reason = reason.strip() if isinstance(reason, str) else ""
    try:
        addendum = _build_addendum(data.get("addendum"), data.get("policy_rules"))
    except ValueError:
        addendum = _build_addendum(data.get("addendum"), [])
    hint = data.get("new_profile")
    if not isinstance(hint, dict):
        hint = None
    else:
        hint = {
            key: hint[key].strip()
            for key in ("id", "display_name", "description", "instructions")
            if isinstance(hint.get(key), str) and hint[key].strip()
        } or None
    return {
        "profile_id": profile_id,
        "confidence": confidence,
        "reason": reason,
        "addendum": addendum,
        "new_profile_hint": hint,
    }


def request_advice(analysis, *, client, profiles):
    """One call through the injected client. Returns (advice, usage)."""
    request = build_advice_request(analysis, profiles)

    def call():
        return client.chat.completions.create(**request)

    response = send_with_retries(call) if send_with_retries else call()
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        content = ""
    usage = getattr(response, "usage", None)
    token_usage = {
        "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
        "model": ADVICE_MODEL,
    }
    return parse_advice(content, profiles), token_usage


def draft_new_profile(base_profile_id, advice, *, repo_root):
    """Copy the closest profile into a temp folder with changed text.

    Returns a draft dict. Raises ValueError/OSError when the result does not
    pass load_translation_profile. The repository is not modified.
    """
    repo_root = Path(repo_root).resolve()
    base = load_translation_profile(base_profile_id, repo_root)
    hint = (advice or {}).get("new_profile_hint") or {}
    draft_id = hint.get("id") or f"{base_profile_id}-draft"
    validate_profile_id(draft_id)
    temp_root = Path(tempfile.mkdtemp(prefix="nt_profile_draft_"))
    try:
        target = temp_root / "profiles" / draft_id
        shutil.copytree(repo_root / "profiles" / base_profile_id, target)
        config_path = target / "profile.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["id"] = draft_id
        config["display_name"] = hint.get("display_name") or base.display_name + " (draft)"
        config["description"] = hint.get("description") or base.description
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        instructions = base.translation_instructions.rstrip()
        if hint.get("instructions"):
            instructions += "\n\n" + hint["instructions"]
        (target / "translation_instructions.txt").write_text(
            instructions + "\n", encoding="utf-8", newline="\n"
        )
        profile = load_translation_profile(draft_id, temp_root)
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise
    return {
        "profile_id": draft_id,
        "base_profile_id": base_profile_id,
        "temp_root": temp_root,
        "path": target,
        "profile": profile,
    }


def discard_draft(draft):
    shutil.rmtree(draft["temp_root"], ignore_errors=True)


def save_profile(draft, new_id, *, repo_root):
    """Install a draft as profiles/<new_id>; refuses to overwrite."""
    validate_profile_id(new_id)
    repo_root = Path(repo_root).resolve()
    destination = repo_root / "profiles" / new_id
    if destination.exists():
        raise FileExistsError(f"profile already exists and was not overwritten: {new_id}")
    # Re-key a copy so the id inside profile.json matches the folder, then
    # validate it before anything touches the repository.
    check_root = Path(tempfile.mkdtemp(prefix="nt_profile_save_"))
    try:
        staged = check_root / "profiles" / new_id
        shutil.copytree(draft["path"], staged)
        config_path = staged / "profile.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["id"] = new_id
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        load_translation_profile(new_id, check_root)
        (repo_root / "profiles").mkdir(exist_ok=True)
        shutil.copytree(staged, destination)
    finally:
        shutil.rmtree(check_root, ignore_errors=True)
    return destination
