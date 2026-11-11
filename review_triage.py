"""Sort review findings into ones a local check proves mechanical and ones a person must judge. No API calls.

Two kinds are mechanical:
- a dialogue split: the fix is the old text with paragraph breaks added before an opening quote and
  nothing else changed;
- a locked-term restoration: the only change swaps a short phrase for a locked glossary term's English,
  the chapter's source uses that term, and the old text did not already have its English.
Everything else (meaning, omissions, unlocked terms, style) needs judging.
"""
import re

from batch_translation import MIN_KEY_CHARS, word_occurrences

OPENING_QUOTES = "“‘\"'"
MAX_SWAPPED_CHARS = 60


def _fold(text):
    return text.replace("\u2019", "'").replace("\u2018", "'").casefold()


def _changed(old, new):
    start = 0
    while start < min(len(old), len(new)) and old[start] == new[start]:
        start += 1
    end = 0
    while end < min(len(old), len(new)) - start and old[len(old) - 1 - end] == new[len(new) - 1 - end]:
        end += 1
    return old[start:len(old) - end], new[start:len(new) - end], start


QUOTE = "[" + OPENING_QUOTES + "]"


def _joined(text):
    """The text with every break before a quote undone, and the narration's last mark before a quote
    ('asked.' / 'asked,' / 'asked:') made the same."""
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"[.,:;]? (?=" + QUOTE + ")", ", ", text).strip()


def is_dialogue_split(old, new):
    """The fix only moves a quotation into its own paragraph."""
    if old == new or "\n" in old or not re.search(r"\n\s*" + QUOTE, new):
        return False
    return _joined(old) == _joined(new)


def locked_restoration(old, new, source, entries):
    """The locked term whose English the fix restores, or None."""
    removed, added, _ = _changed(old, new)
    if not added or len(removed) > MAX_SWAPPED_CHARS or len(added) > MAX_SWAPPED_CHARS + 20 or re.search(r"[.?!\n]", removed + added):
        return None
    for key, entry in entries.items():
        english = entry.get("english") or ""
        if (entry.get("status") != "locked" or not english or len(key) < MIN_KEY_CHARS or key not in source
                or not word_occurrences(key, source)[0]):
            continue
        if _fold(english) in _fold(new) and _fold(english) not in _fold(old) and _fold(english) in _fold(
                new[max(0, new.find(added) - len(english)):new.find(added) + len(added) + len(english)]):
            return key
    return None


def triage(item, source, entries):
    """'dialogue_split', 'locked_term:<key>', or None when the finding needs judging."""
    old, new = item["english"], item["fix"]
    if is_dialogue_split(old, new):
        return "dialogue_split"
    if item.get("kind") in ("term", "rule"):
        key = locked_restoration(old, new, source, entries)
        if key:
            return f"locked_term:{key}"
    return None
