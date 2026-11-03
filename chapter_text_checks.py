"""Free local checks on a translated chapter: curly quotes, romanized sound effects, dropped dialogue.

curl_quotes rewrites straight quotes before commit. The other two only find things worth a
look; the run flags them for review and keeps going.
"""
import re

_OPENS_AFTER = frozenset(" \t\r\n([{—–“‘\"")
_CLOSES_BEFORE = frozenset(" \t\r\n.,!?;:—–…)]}”’")


def curl_quotes(text):
    """Straight quotes to curly ones. Curly text comes back unchanged."""
    out = []
    for index, char in enumerate(text):
        before = text[index - 1] if index else "\n"
        after = text[index + 1] if index + 1 < len(text) else "\n"
        if char == '"':
            out.append("“" if before in _OPENS_AFTER and not after.isspace() else "”")
        elif char == "'":
            if before.isalnum() and (after.isalnum() or after in _CLOSES_BEFORE):
                out.append("’")  # apostrophe, or a quote closing after a word
            elif before in _OPENS_AFTER and not after.isspace():
                out.append("‘")
            else:
                out.append("’")
        else:
            out.append(char)
    return "".join(out)


ENGLISH_SOUNDS = frozenset("""
bam bang bash beep blam boom bong bonk buzz chink clack clang clank clap clatter click clink clop
clunk crack crackle crash creak crunch ding dong drip fizz flap flick flutter gasp grr growl gulp
hiss honk hum huff meow mew kaboom knock pat patter ping plink plip plop pop pow puff rap rat rattle ring rip
roar rumble rustle scrape screech shatter shh shing shink shuffle slam slap slash slice slurp smack
snap snick snip snort splash splat sploosh squeak squelch squish swish swoosh tap tat thud thump
thunk thwack tick tock thwip twang wham whack whap whine whir whirr whistle whiz whizz whomp whoosh
whump woosh zap zing zip zoom
""".split())
_COMMON_WORDS = frozenset("""
a an and but or so then the it he she i you we they no yes oh ah wait stop what why huh hm hmm
""".split())
_SOUND_LINE = re.compile(r"^[*_]*([A-Za-z][A-Za-z ,-]{0,40}?)[.,!]?\s*[—–-]+\.?[*_]*$")
_ROMANIZED = re.compile(r"eo|eu|ae|^kw|^gw|^kk|^tt|^pp|^jj|^ch[ae]|ng$")


def _is_english_sound(word):
    squeezed = re.sub(r"(.)\1+", r"\1", word)  # "booom" -> "bom"
    return word in ENGLISH_SOUNDS or any(re.sub(r"(.)\1+", r"\1", s) == squeezed for s in ENGLISH_SOUNDS)


def _looks_romanized(parts):
    return any(_ROMANIZED.search(p) and not p.endswith("ing") and not _is_english_sound(p) for p in parts)


def romanized_sound_lines(text):
    """Short unquoted sound-effect paragraphs ("Kwa-kwa-kwang—.") left in romanized Korean."""
    found = []
    for line in text.splitlines():
        match = _SOUND_LINE.match(line.strip())
        if not match or len(match.group(1).split()) > 3:  # longer lines are cut-off narration
            continue
        parts = [re.sub(r"[^a-z]", "", part.lower())
                 for token in match.group(1).replace(",", " ").split() for part in token.split("-")]
        parts = [part for part in parts if part and part not in _COMMON_WORDS]
        if parts and len(parts) <= 6 and _looks_romanized(parts):
            found.append(line.strip())
    return found


_DIALOGUE_OPENERS = "\"“'‘「『"


def dialogue_line_count(text):
    lines = (line.strip().lstrip("*>_ ").strip() for line in text.splitlines())
    return sum(1 for line in lines if line and line[0] in _DIALOGUE_OPENERS)


def missing_dialogue_lines(source, english):
    """How many fewer lines open with a quotation mark in the English than in the source.

    Reliable for Korean webnovels, where every line of speech stands alone. Chinese
    chapters often put the speaker before the quote, so callers use it for Korean only.
    """
    return max(0, dialogue_line_count(source) - dialogue_line_count(english))


def review_flags(source, english, source_language):
    """(kind, detail) pairs for the review list. Never blocks a commit."""
    flags = []
    sounds = romanized_sound_lines(english)
    if sounds:
        flags.append(("romanized_sound_effect", "Sound effects left in romanized Korean: " + "; ".join(sounds[:5])))
    if (source_language or "").lower().startswith("ko"):
        missing = missing_dialogue_lines(source, english)
        if missing:
            flags.append(("possible_missing_dialogue",
                          f"{missing} fewer line(s) of dialogue than the source; a line may have been dropped."))
    return flags
