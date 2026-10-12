"""Compare the number of tests pytest collects at two commits (#27).

    python .github/scripts/compare_test_count.py [--tests-removed-label] BASE HEAD

BASE and HEAD are git revisions. Each one is checked out into a temporary worktree, where
`python -m pytest --collect-only -q` lists its tests with this Python environment. The script
exits with status 1 when HEAD has fewer tests than BASE, unless --tests-removed-label says the
pull request carries the tests-removed label, or when pytest cannot collect the tests at HEAD.

It also reports, without failing:
- the tests collected at BASE that are not collected at HEAD;
- the changed files under tests/, when the change touches other files too;
- added and removed lines under tests/ that mention xfail (such as a helper like xfail_16), or use
  pytest.mark.skip, pytest.mark.skipif or pytest.skip.

The report goes to stdout and, when GITHUB_STEP_SUMMARY names a file, to that job summary.
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile

LABEL = "tests-removed"
TESTS_DIR = "tests/"
# Any mention of xfail, since tests here mark known bugs through helpers such as xfail_16(); for
# skips only pytest's own forms, because "skip" is a common word in test names.
MARKER = re.compile(r"xfail|\bpytest\.(?:mark\.)?skip(?:if)?\b")
# "141 tests collected", "1/2 tests collected (1 deselected)" or "no tests collected".
COLLECTED = re.compile(r"^(?:(\d+)(?:/\d+)? tests? collected|no tests collected)\b")


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def commit_sha(rev):
    return git("rev-parse", "--verify", f"{rev}^{{commit}}").strip()


def collect(sha):
    """Return (the node IDs pytest collects at sha, None), or (None, why) if collection fails."""
    with tempfile.TemporaryDirectory() as tmp:
        tree = os.path.join(tmp, "tree")
        git("worktree", "add", "--detach", tree, sha)
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
                cwd=tree,
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            git("worktree", "remove", "--force", tree)
    output = (result.stdout + result.stderr).splitlines()
    tail = "\n".join(output[-30:])
    # Exit status 5: no tests collected.
    if result.returncode not in (0, 5):
        return None, f"pytest exited with status {result.returncode}:\n{tail}"
    # -q lists one node ID per line, then a blank line, any warnings, and the count.
    ids = []
    for line in output:
        if not line:
            break
        ids.append(line)
    counts = [match for match in map(COLLECTED.match, output) if match]
    stated = int(counts[-1].group(1) or 0) if counts else None
    if stated != len(ids) or not all("::" in node_id for node_id in ids):
        return None, f"could not read the list of tests ({len(ids)} lines, stated count {stated}):\n{tail}"
    return ids, None


def changed_files(base, head):
    """(status, path) of each file the change adds, modifies or deletes; a rename is a delete and an add."""
    # -z: NUL-separated status and path, with no quoting of unusual file names.
    fields = git("diff", "--name-status", "--no-renames", "-z", base, head).split("\0")
    # The output ends with a NUL, so the last field is an empty string: strict=False drops it.
    return list(zip(fields[0::2], fields[1::2], strict=False))


def marker_changes(base, head):
    """'path: +line' or 'path: -line' for each added or removed line under tests/ that MARKER matches."""
    changes, path = [], None
    for line in git("diff", "--unified=0", "--no-renames", base, head, "--", TESTS_DIR).splitlines():
        if line.startswith("--- "):
            path = line[len("--- a/") :] if line.startswith("--- a/") else None
        elif line.startswith("+++ "):
            path = line[len("+++ b/") :] if line.startswith("+++ b/") else path
        elif line[:1] in ("+", "-") and MARKER.search(line):
            changes.append(f"{path}: {line}")
    return changes


def code_block(lines):
    """A fenced code block, with a fence longer than any run of backticks in the lines."""
    longest = max((len(run) for line in lines for run in re.findall(r"`+", line)), default=0)
    fence = "`" * max(3, longest + 1)
    return [fence, *lines, fence, ""]


def tests(count):
    return f"{count} test" if count == 1 else f"{count} tests"


def compare(base_ref, head_ref, labelled):
    """Return (whether the check failed, the report's lines)."""
    base, head = commit_sha(base_ref), commit_sha(head_ref)
    base_ids, base_error = collect(base)
    head_ids, head_error = collect(head)
    report = [
        "## Test count",
        "",
        "| | Commit | Tests collected |",
        "| - | - | - |",
        f"| Base | `{base[:12]}` | {'could not collect' if base_ids is None else len(base_ids)} |",
        f"| Head | `{head[:12]}` | {'could not collect' if head_ids is None else len(head_ids)} |",
        "",
    ]
    failed = False
    if head_error:
        failed = True
        report += ["**Failed:** pytest could not collect the tests at head.", "", *code_block(head_error.splitlines())]
    elif base_error:
        report += ["**Not compared:** pytest could not collect the tests at base.", ""]
        report += code_block(base_error.splitlines())
    else:
        fewer = len(base_ids) - len(head_ids)
        if fewer > 0 and not labelled:
            failed = True
            report += [
                (
                    f"**Failed:** head has {tests(fewer)} fewer than base. If this pull request removes tests "
                    f"on purpose, add the `{LABEL}` label, which runs this check again."
                ),
                "",
            ]
        elif fewer > 0:
            report += [f"**Passed:** head has {tests(fewer)} fewer than base, and the `{LABEL}` label is set.", ""]
        else:
            report += ["**Passed:** head has no fewer tests than base.", ""]
        head_set = set(head_ids)
        missing = [node_id for node_id in base_ids if node_id not in head_set]
        if missing:
            report += ["### Tests at base that head does not collect", "", *code_block(missing)]

    changed = changed_files(base, head)
    test_files = [f"{status}\t{path}" for status, path in changed if path.startswith(TESTS_DIR)]
    if test_files and len(test_files) < len(changed):
        report += ["### Changed test files", "", "This change touches tests/ and other files.", ""]
        report += code_block(test_files)
    markers = marker_changes(base, head)
    if markers:
        report += ["### xfail and skip marker changes (not blocking)", "", *code_block(markers)]
    return failed, report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fail when HEAD collects fewer pytest tests than BASE.")
    parser.add_argument("base", help="git revision of the base, such as HEAD^1 of a pull request's merge commit")
    parser.add_argument("head", help="git revision to check")
    parser.add_argument("--tests-removed-label", action="store_true", help=f"the pull request has the {LABEL} label")
    args = parser.parse_args(argv)
    try:
        failed, report = compare(args.base, args.head, args.tests_removed_label)
    except subprocess.CalledProcessError as error:
        print(f"{' '.join(error.cmd)} failed:\n{error.stderr}", file=sys.stderr)
        return 2
    text = "\n".join(report)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as file:
            file.write(text + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
