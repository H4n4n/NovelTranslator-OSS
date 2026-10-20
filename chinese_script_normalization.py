"""Local, deterministic Chinese script normalization for comparison only.

The translation engine must never use this module to decide whether target
text is acceptable.  It exists solely to compare source-side anchors while
retaining offsets into the original, unmodified source text.
"""

from __future__ import annotations

import sys


TRADITIONAL_TO_SIMPLIFIED = "traditional_to_simplified"

_OPENCC_CONVERTER = None
_OPENCC_CHECKED = False


def _opencc_converter():
    """Return a reusable OpenCC t2s converter when one is installed."""
    global _OPENCC_CHECKED, _OPENCC_CONVERTER
    if _OPENCC_CHECKED:
        return _OPENCC_CONVERTER
    _OPENCC_CHECKED = True
    try:
        from opencc import OpenCC
    except (ImportError, OSError):
        return None
    for configuration in ("t2s", "t2s.json"):
        try:
            _OPENCC_CONVERTER = OpenCC(configuration)
            return _OPENCC_CONVERTER
        except (OSError, RuntimeError, ValueError):
            continue
    return None


def _windows_traditional_to_simplified(text):
    """Use the documented local Windows locale mapping, or return ``None``."""
    if sys.platform != "win32":
        return None
    # Windows wchar_t and LCMapStringEx offsets are UTF-16 code units.  Reject
    # non-BMP input instead of risking an unprovable Python-code-point mapping.
    if any(ord(character) > 0xFFFF for character in text):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        function = ctypes.WinDLL("kernel32", use_last_error=True).LCMapStringEx
        function.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.LPCWSTR,
            ctypes.c_int,
            wintypes.LPWSTR,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.LPARAM,
        ]
        function.restype = ctypes.c_int
        simplified_chinese = 0x02000000
        required = function(
            "zh-CN", simplified_chinese, text, len(text), None, 0,
            None, None, 0,
        )
        if required <= 0:
            return None
        buffer = ctypes.create_unicode_buffer(required)
        written = function(
            "zh-CN", simplified_chinese, text, len(text), buffer, required,
            None, None, 0,
        )
        if written <= 0:
            return None
        return "".join(buffer[:written])
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def traditional_to_simplified(text):
    """Return a trusted t2s comparison form, or ``None`` if unavailable.

    OpenCC is preferred when the runtime already provides it.  The supported
    Windows production environment has a deterministic local OS fallback.
    Neither backend performs network access or mutates its input.
    """
    if not isinstance(text, str):
        raise TypeError("Chinese script normalization requires text")
    if not text:
        return ""
    converter = _opencc_converter()
    if converter is not None:
        try:
            converted = converter.convert(text)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        return converted if isinstance(converted, str) else None
    return _windows_traditional_to_simplified(text)


def traditional_to_simplified_backend():
    """Return the active local backend name for bounded diagnostics."""
    if _opencc_converter() is not None:
        return "opencc_t2s"
    if sys.platform == "win32":
        return "windows_lcmap_simplified_chinese"
    return "unavailable"
