"""Offline repository checks. Phase gates fail closed when tests are absent."""

import argparse
import ast
import re
import subprocess
import sys
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

AUDIO = {".amr", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".flac", ".aac"}
PRIVATE_DIRS = {"data", "recordings", "transcripts", "summaries", "backups", "secrets", "spool", ".stversions"}


def forbidden(path):
    """Inspect names only; never print or read secret/runtime contents."""
    name = path.name.lower()
    return (
        bool({p.lower() for p in path.parts} & PRIVATE_DIRS)
        or path.suffix.lower() in AUDIO | {".pem", ".key", ".db", ".dump", ".log"}
        or ".sqlite" in name
        or name.endswith(".sql.gz")
        or (name.startswith(".env") and name != ".env.example")
    )


def markdown_errors(path, root):
    text = path.read_text(encoding="utf-8-sig")
    errors = []
    fence = None
    prose = []
    for line in text.splitlines():
        match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if match:
            marker = match[1]
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
        elif fence is None:
            prose.append(line)
    if fence:
        errors.append("unclosed code fence")
    for target in re.findall(r"\]\(([^)]+)\)", "\n".join(prose)):
        target = target.strip().strip("<>")
        parts = urlsplit(target)
        if parts.scheme or not parts.path:
            continue
        resolved = (path.parent / unquote(parts.path)).resolve()
        if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
            errors.append("missing or out-of-repository relative file link")
    return errors


def run_suite(suite, stream=None):
    if suite.countTestCases() == 0:
        print("FAIL: no tests discovered")
        return False
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return result.wasSuccessful() and not result.skipped and not result.expectedFailures


def run_group(root, group):
    directory = root / "tests" / group
    if not directory.is_dir() or not (directory / "__init__.py").is_file():
        print(f"NOT IMPLEMENTED: tests/{group} (phase cannot pass)")
        return False
    suite = unittest.TestLoader().discover(str(directory), top_level_dir=str(root))
    return run_suite(suite)


def repository_checks(root):
    result = subprocess.run(
        # The explicitly selected workspace may be owned by a sandbox account.
        # Scope trust to this invocation, never change global Git configuration.
        ["git", "-c", f"safe.directory={root.resolve().as_posix()}", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root, capture_output=True, check=True,
    )
    names = sorted(set(result.stdout.decode("utf-8").split("\0")) - {""})
    ok = True
    for name in names:
        relative = Path(name)
        if forbidden(relative):
            print("FAIL: runtime/secret file in Git inventory (name withheld)")
            ok = False
            continue
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            print("FAIL: file link in Git inventory")
            ok = False
            continue
        if not path.exists():  # Working-tree deletions are handled by Git.
            continue
        try:
            if path.suffix == ".py":
                ast.parse(path.read_text(encoding="utf-8-sig"), filename=name)
            if path.suffix == ".md":
                for error in markdown_errors(path, root):
                    print(f"FAIL: {name}: {error}")
                    ok = False
        except (OSError, UnicodeError, SyntaxError, ValueError) as error:
            print(f"FAIL: {name}: {type(error).__name__} (contents withheld)")
            ok = False
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["P1", "P2", "P3"])
    args = parser.parse_args()
    if sys.version_info < (3, 12):
        parser.error("Python 3.12+ is required")
    try:
        ok = repository_checks(ROOT)
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        print("FAIL: cannot inspect Git inventory")
        return 1
    # Do not import tests if the inventory contains forbidden files or bad syntax.
    if not ok:
        return 1
    ok = run_group(ROOT, "harness")
    if args.phase:
        for number in range(1, int(args.phase[1:]) + 1):
            ok = run_group(ROOT, f"p{number}") and ok
    label = args.phase or "HARNESS ONLY (product phases not verified)"
    print(f"{'PASS' if ok else 'FAIL'}: {label}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
