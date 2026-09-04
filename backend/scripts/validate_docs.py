"""Check that the documentation set still hangs together.

Two failures are worth a gate. A relative link or heading anchor that no longer resolves turns an
operational runbook into a dead end at exactly the moment somebody is following it under pressure.
And the supported-scope contract can quietly drift away from the code that produced it, which is
worse than having no contract: a 1.0 support promise nobody maintains is a promise that will be
broken.

Prose is deliberately not parsed. This checks only what has one right answer.

Standard library only: this runs on the operator's host, outside the backend virtualenv.
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from collections.abc import Sequence
from pathlib import Path

# Reach the sibling module under one name whether this file is run as a script on the operator's
# host or imported as `scripts.validate_docs` by the test suite. Two names for one file would give
# the type checker two modules and this gate two versions of the limitation list.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.release_evidence import KNOWN_LIMITATIONS

LINK = re.compile(r"\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$", re.MULTILINE)
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)

EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "file://", "tel:")

# Documents whose absence would leave the release contract unstated.
REQUIRED_DOCUMENTS = (
    "docs/supported-scope.md",
    "docs/release.md",
)


def slug(title: str) -> str:
    """Approximate the anchor GitHub derives from a heading.

    Inline code and link syntax are unwrapped first, because a heading that reads ``## `foo` bar``
    anchors as ``foo-bar``.
    """
    title = re.sub(r"`([^`]*)`", r"\1", title)
    title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", title)
    out: list[str] = []
    for character in title.strip().casefold():
        if character.isalnum() or character in "-_":
            out.append(character)
        elif character in " \t":
            out.append("-")
        elif unicodedata.category(character).startswith("M"):
            out.append(character)
    return "".join(out)


def documents(root: Path) -> list[Path]:
    found = [root / "README.md", *sorted((root / "docs").rglob("*.md"))]
    return [path for path in found if path.is_file()]


def check_links(root: Path) -> tuple[int, list[str]]:
    paths = documents(root)
    anchors = {path.resolve(): _anchors(path) for path in paths}
    failures: list[str] = []
    checked = 0

    for path in paths:
        body = FENCE.sub("", path.read_text(encoding="utf-8"))
        for href in LINK.findall(body):
            if href.startswith(EXTERNAL_PREFIXES) or href.startswith("#!"):
                continue
            checked += 1
            file_part, _, anchor = href.partition("#")
            target = (path.parent / file_part).resolve() if file_part else path.resolve()
            relative = path.relative_to(root).as_posix()
            try:
                target.relative_to(root)
            except ValueError:
                failures.append(f"{relative}: link leaves the repository -> {href}")
                continue
            if not target.exists():
                failures.append(f"{relative}: missing file -> {href}")
                continue
            known = anchors.get(target)
            if anchor and known is not None and anchor not in known:
                failures.append(f"{relative}: missing anchor -> {href}")
    return checked, failures


def _anchors(path: Path) -> set[str]:
    body = path.read_text(encoding="utf-8")
    return {slug(heading) for heading in HEADING.findall(FENCE.sub("", body))}


def check_supported_scope(root: Path) -> list[str]:
    """The support contract must mention every limitation the gates actually know about.

    `KNOWN_LIMITATIONS` is what the release evidence document publishes. If the human contract
    stops naming one of them, the two halves have drifted and the document is no longer the
    contract it claims to be.
    """
    failures: list[str] = []
    for relative in REQUIRED_DOCUMENTS:
        if not (root / relative).is_file():
            failures.append(f"{relative}: required release document is missing")

    scope = root / "docs/supported-scope.md"
    if not scope.is_file():
        return failures
    body = scope.read_text(encoding="utf-8")
    for limitation in KNOWN_LIMITATIONS:
        if limitation["id"] not in body:
            failures.append(
                f"docs/supported-scope.md: known limitation {limitation['id']} is not documented"
            )

    readme = (root / "README.md").read_text(encoding="utf-8")
    for relative in REQUIRED_DOCUMENTS:
        if relative.removeprefix("docs/") not in readme and relative not in readme:
            failures.append(f"README.md: does not link {relative}")
    return failures


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate documentation links and release scope.")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="repository root (default: the repository this script lives in)",
    )
    arguments = parser.parse_args(argv)
    root = arguments.root.resolve()

    checked, failures = check_links(root)
    failures += check_supported_scope(root)

    print(f"documentation: {checked} relative links in {len(documents(root))} documents")
    if failures:
        for failure in failures:
            print(f"documentation gate: FAIL: {failure}", file=sys.stderr)
        return 1
    print("documentation gate: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
