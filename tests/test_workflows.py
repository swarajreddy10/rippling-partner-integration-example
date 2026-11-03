"""Every GitHub Action is pinned to a full commit SHA; a movable tag fails the build."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOWS = sorted((Path(__file__).parent.parent / ".github" / "workflows").glob("*.y*ml"))
# Any spelling of a `uses:` key, quoted or not, inside a step list or not.
USES = re.compile(r"""^\s*-?\s*['"]?uses['"]?\s*:\s*['"]?([^\s'"#]+)""", re.MULTILINE)
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def references(text: str) -> list[str]:
    return [ref for ref in USES.findall(text) if not ref.startswith("./")]


def test_there_are_workflows_to_check() -> None:
    assert WORKFLOWS


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit_sha(workflow: Path) -> None:
    refs = references(workflow.read_text(encoding="utf-8"))
    assert refs, f"{workflow.name} uses no actions; the guard would pass vacuously"
    unpinned = [r for r in refs if not PINNED.match(r)]
    assert unpinned == []


@pytest.mark.parametrize(
    "line",
    [
        "      - uses: actions/checkout@v7",
        "      - 'uses': actions/checkout@main",
        '        "uses": "actions/setup-python@v7.0.0"',
        "      uses: actions/checkout@3d3c42e",
    ],
)
def test_the_guard_catches_every_spelling_of_an_unpinned_reference(line: str) -> None:
    refs = references(line)
    assert refs and not PINNED.match(refs[0])
