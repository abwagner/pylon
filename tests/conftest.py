import json
from pathlib import Path

import pytest


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def pr_opened() -> dict:
    return json.loads((FIXTURES / "pr_opened.json").read_text())


@pytest.fixture
def pr_merged() -> dict:
    return json.loads((FIXTURES / "pr_merged.json").read_text())


@pytest.fixture
def pr_closed_unmerged() -> dict:
    return json.loads((FIXTURES / "pr_closed_unmerged.json").read_text())
