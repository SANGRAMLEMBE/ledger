"""
Reject stray control characters in tracked text files.

WHY THIS EXISTS
---------------
Twice during this build, a shell escape-sequence collapse turned
``.venv\\Scripts\\activate`` into a bell character mid-word. The rendered result
reads as ``.venv\\Scriptsctivate`` — a documented command that is silently wrong,
in the two files a new contributor reads first.

It is invisible in a diff, invisible in a terminal, and survives review. A linter
catches it in a second; a human does not catch it at all.

Only control characters that carry no meaning in a text file are rejected. Tabs,
newlines and carriage returns are left alone.

    python scripts/check_text_hygiene.py
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

CHECKED_SUFFIXES = {".md", ".py", ".toml", ".yml", ".yaml", ".json", ".tf", ".html", ".txt"}
ALLOWED = {"\n", "\r", "\t"}


def tracked_files() -> list[pathlib.Path]:
    result = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    )
    return [pathlib.Path(line) for line in result.stdout.split() if line]


def main() -> int:
    problems: list[str] = []

    for path in tracked_files():
        if path.suffix not in CHECKED_SUFFIXES or not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            problems.append(f"{path}: not valid UTF-8")
            continue

        for number, line in enumerate(text.splitlines(), start=1):
            offenders = {c for c in line if ord(c) < 32 and c not in ALLOWED}
            if offenders:
                names = ", ".join(f"U+{ord(c):04X}" for c in sorted(offenders))
                problems.append(f"{path}:{number}: control character(s) {names}")

    if problems:
        print("Text hygiene check FAILED:\n")
        for problem in problems:
            print(f"  {problem}")
        print(
            "\nThese are usually a shell escape collapse — a backslash sequence "
            "such as \\a or \\t interpreted rather than written literally. Fix the "
            "source line, do not just delete the character."
        )
        return 1

    print(f"Text hygiene: clean ({len(tracked_files())} tracked files checked).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
