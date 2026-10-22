"""Fail closed on accidental lab files, credentials or local paths in releases."""
from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import zipfile

ALLOWED_ROOTS = {"src", "tests", "docs", "examples", "scripts", "native", ".github", "LICENSES"}
ALLOWED_FILES = {"README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "CHANGELOG.md", "AUTHORS.md",
                 "pyproject.toml", "uv.lock", ".gitignore", ".gitattributes", "MANIFEST.in"}
FORBIDDEN_PARTS = {".venv", "logs", "recordings", "attachments", "__pycache__", ".DS_Store"}
FORBIDDEN_SUFFIXES = {".pdf", ".pcap", ".pcapng", ".wav", ".mp4", ".mov", ".log", ".pyc"}
TEXT_SUFFIXES = {".py", ".toml", ".md", ".txt", ".json", ".yaml", ".yml", ".asn", ".cfg", ".ini", ".spec"}
RULES = {
    "personal absolute path": re.compile(rb"/(?:Users|home)/[A-Za-z0-9_.-]+/"),
    "Windows user path": re.compile(rb"[A-Za-z]:[\\/]Users[\\/][A-Za-z0-9_.-]+[\\/]"),
    "private network address": re.compile(rb"(?<![\d.])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?![\d.])"),
    "GitHub credential": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    "PyPI credential": re.compile(rb"pypi-[A-Za-z0-9_-]{40,}"),
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "lab USB serial device": re.compile(rb"/dev/(?:cu|tty)\.usbmodem\d+"),
}


def inspect(name: str, data: bytes, *, native: bool = False) -> list[str]:
    path = PurePosixPath(name)
    errors = []
    if path.is_absolute() or ".." in path.parts:
        errors.append(f"{name}: unsafe archive path")
    if set(path.parts) & FORBIDDEN_PARTS:
        errors.append(f"{name}: forbidden lab/cache directory")
    if not native and path.suffix.lower() in FORBIDDEN_SUFFIXES:
        errors.append(f"{name}: forbidden recording/capture/document/cache")
    if native and path.name.lower() in {"ffmpeg", "ffmpeg.exe", "ffprobe", "ffprobe.exe", "uv", "uv.exe"}:
        errors.append(f"{name}: external tool must not be bundled")
    # Native binaries contain compiler paths and upstream SDK metadata. Inspect
    # source/package text, never promise that a string scan proves binary safety.
    if path.suffix.lower() in TEXT_SUFFIXES or path.name in ALLOWED_FILES:
        for description, pattern in RULES.items():
            if pattern.search(data):
                errors.append(f"{name}: {description}")
    return errors


def audit_source(root: Path) -> list[str]:
    result = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                            cwd=root, check=True, capture_output=True)
    errors = []
    for entry in result.stdout.decode().split("\0"):
        if not entry:
            continue
        path = PurePosixPath(entry)
        if path.parts[0] not in ALLOWED_ROOTS and entry not in ALLOWED_FILES:
            errors.append(f"{entry}: not on public source allowlist")
            continue
        file = root / entry
        if file.is_symlink():
            errors.append(f"{entry}: source symlinks are not allowed")
        elif file.is_file():
            errors.extend(inspect(entry, file.read_bytes()))
    return errors


def audit_archive(path: Path, native: bool) -> list[str]:
    errors = []
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as stream:
            for item in stream.infolist():
                if not item.is_dir():
                    errors.extend(inspect(item.filename, stream.read(item), native=native))
    elif tarfile.is_tarfile(path):
        with tarfile.open(path) as stream:
            for item in stream:
                if item.isfile():
                    errors.extend(inspect(item.name, stream.extractfile(item).read(), native=native))
                elif item.issym() or item.islnk():
                    # Framework symlinks are expected in native macOS bundles.
                    if not native or PurePosixPath(item.linkname).is_absolute():
                        errors.append(f"{item.name}: unsupported archive link")
    else:
        errors.append(f"{path.name}: unsupported archive type")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--native", action="store_true")
    parser.add_argument("archives", nargs="*", type=Path)
    args = parser.parse_args()
    if not args.source and not args.archives:
        parser.error("supply --source or archive paths")
    errors = audit_source(args.source.resolve()) if args.source else []
    for archive in args.archives:
        errors.extend(audit_archive(archive, args.native))
    if errors:
        print("\n".join(errors))
        return 1
    print("Release allowlist/content audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
