"""Make command-line output UTF-8, so printing Chinese or Korean text can't stop a run.

A Windows console or a redirected log may default to a legacy code page (cp1252), and
printing a character it can't hold raises UnicodeEncodeError, which stopped a paid run
mid-novel. Characters still unprintable are escaped instead of raising.
"""
import sys


def configure_cli_text_streams():
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            # A redirected or test-owned stream may not permit reconfiguration.
            continue
