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
    assert refs
    assert not PINNED.match(refs[0])


ROOT = Path(__file__).parent.parent
IMAGE = re.compile(r"^\s*image:\s*(postgres:\S+)", re.MULTILINE)


def postgres_images(text: str) -> list[str]:
    return IMAGE.findall(text)


def test_compose_and_ci_test_against_the_same_postgres_image() -> None:
    compose = postgres_images((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    ci = postgres_images((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    assert len(compose) == 1
    assert compose == ci


def test_the_image_check_catches_drift() -> None:
    assert postgres_images("    image: postgres:18.4@sha256:abc\n") != postgres_images(
        "        image: postgres:17.10@sha256:def\n"
    )
