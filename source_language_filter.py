"""Import-safe source-language matching for project discovery filters."""


def source_language_matches(actual, requested):
    """Match an exact tag or, for a bare language, its tagged variants.

    Examples: ``zh`` matches ``zh``, ``zh-Hans`` and ``zh-Hant`` while an
    explicit ``zh-Hant`` request matches only that exact normalized tag.
    ``None``/empty requested language means no filtering.
    """
    if requested is None or not str(requested).strip():
        return True
    actual = str(actual or "").strip().replace("_", "-").casefold()
    requested = str(requested).strip().replace("_", "-").casefold()
    if "-" in requested:
        return actual == requested
    return actual == requested or actual.startswith(requested + "-")
