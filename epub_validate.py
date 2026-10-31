"""Local EPUB validation. No downloads, no network, no provider calls.

Runs EPUBCheck when a jar and Java are available. Otherwise it falls back to a
cheap built-in structural check so a broken archive is still caught.
"""
import json
import os
import posixpath
import shutil
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree

EPUBCHECK_ENV = "NOVELTRANSLATOR_EPUBCHECK_JAR"
EPUBCHECK_TIMEOUT_SECONDS = 180
MAX_MESSAGES = 20

_CONTAINER_NS = "{urn:oasis:names:tc:opendocument:xmlns:container}"
_OPF_NS = "{http://www.idpf.org/2007/opf}"


def structural_check(path):
    """Return a list of error strings; empty means the basic structure is sound."""
    errors = []
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"not a readable ZIP: {exc}"]
    with archive:
        bad = archive.testzip()
        if bad:
            errors.append(f"corrupt ZIP member: {bad}")
        infos = archive.infolist()
        if not infos or infos[0].filename != "mimetype":
            errors.append("mimetype is not the first ZIP entry")
        else:
            if infos[0].compress_type != zipfile.ZIP_STORED:
                errors.append("mimetype entry is compressed")
            if archive.read("mimetype").strip() != b"application/epub+zip":
                errors.append("mimetype content is wrong")
        names = set(archive.namelist())
        if "META-INF/container.xml" not in names:
            errors.append("META-INF/container.xml missing")
            return errors
        try:
            container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
            rootfile = container.find(f".//{_CONTAINER_NS}rootfile")
            opf_path = rootfile.get("full-path") if rootfile is not None else None
        except ElementTree.ParseError as exc:
            errors.append(f"container.xml is not valid XML: {exc}")
            return errors
        if not opf_path or opf_path not in names:
            errors.append(f"OPF package document missing: {opf_path}")
            return errors
        try:
            opf = ElementTree.fromstring(archive.read(opf_path))
        except ElementTree.ParseError as exc:
            errors.append(f"OPF is not valid XML: {exc}")
            return errors
        base = posixpath.dirname(opf_path)
        manifest = {
            item.get("id"): item.get("href")
            for item in opf.iter(f"{_OPF_NS}item")
        }
        for ref in opf.iter(f"{_OPF_NS}itemref"):
            href = manifest.get(ref.get("idref"))
            if href is None:
                errors.append(f"spine idref not in manifest: {ref.get('idref')}")
                continue
            member = posixpath.normpath(posixpath.join(base, href))
            if member not in names:
                errors.append(f"spine item missing from archive: {member}")
                continue
            if member.endswith((".xhtml", ".html")):
                try:
                    ElementTree.fromstring(archive.read(member))
                except ElementTree.ParseError as exc:
                    errors.append(f"{member} is not well-formed XML: {exc}")
    return errors


def _result(status, errors=0, warnings=0, messages=(), **extra):
    result = {
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "messages": list(messages)[:MAX_MESSAGES],
    }
    result.update(extra)
    return result


def _fallback(path, reason):
    problems = structural_check(path)
    if problems:
        return _result(
            "FAIL", len(problems), 0, problems, engine="builtin", reason=reason
        )
    return _result(
        "NOT_AVAILABLE", messages=[reason], engine="builtin", structural_ok=True
    )


def validate_epub(path, epubcheck_jar=None):
    """Validate an EPUB. Never raises; returns a status dict."""
    try:
        return _validate(path, epubcheck_jar)
    except Exception as exc:  # validation must never break a build
        return _result(
            "NOT_AVAILABLE", messages=[f"validator error: {type(exc).__name__}: {exc}"]
        )


def _validate(path, epubcheck_jar):
    path = Path(path)
    jar = epubcheck_jar or os.environ.get(EPUBCHECK_ENV)
    java = shutil.which("java")
    if not jar or not Path(jar).is_file():
        return _fallback(path, "EPUBCheck jar not configured or not found")
    if not java:
        return _fallback(path, "Java not found on PATH")
    try:
        completed = subprocess.run(
            [java, "-jar", str(jar), "--json", "-", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=EPUBCHECK_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _fallback(path, f"EPUBCheck could not run: {type(exc).__name__}")
    try:
        data = json.loads(completed.stdout)
    except ValueError:
        tail = (completed.stderr or completed.stdout or "").strip()[:300]
        return _result(
            "FAIL", 1, 0, [f"EPUBCheck gave no JSON (exit {completed.returncode}): {tail}"],
            engine="epubcheck",
        )
    errors, warnings, shown = 0, 0, []
    for message in data.get("messages", []):
        severity = str(message.get("severity", "")).upper()
        if severity in ("ERROR", "FATAL"):
            errors += 1
        elif severity == "WARNING":
            warnings += 1
        else:
            continue
        locations = message.get("locations") or []
        where = ""
        if locations:
            where = f" ({locations[0].get('path', '')}:{locations[0].get('line', '')})"
        shown.append(
            f"{severity} {message.get('ID', '')}: {message.get('message', '')}{where}"
        )
    return _result(
        "FAIL" if errors else "PASS", errors, warnings, shown, engine="epubcheck"
    )
