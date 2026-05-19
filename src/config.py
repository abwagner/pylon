from pathlib import Path

import yaml
from pydantic import BaseModel


class ClosedUnmergedRule(BaseModel):
    """Conditional rule for the `closed_unmerged` action.

    If the work item's current state name (case-insensitive) matches `if_state`,
    pylon transitions it to `set_to`. Otherwise no state change. The PR-closed
    comment is always posted regardless.
    """

    if_state: str
    set_to: str


class StateMachine(BaseModel):
    """Maps each pylon action to a Plane state name.

    Names are matched case-insensitively against each project's state list.
    The named states must exist in every Plane project pylon handles; if a
    name is missing, pylon logs a warning and skips that action.
    """

    opened_draft: str
    opened_ready: str
    ready_for_review: str
    converted_to_draft: str
    merged: str
    closed_unmerged: ClosedUnmergedRule


class ModulesConfig(BaseModel):
    """Module-state reconciliation behaviour.

    When ``enabled``, pylon reconciles each work item's module(s)
    after a state transition lands. The rules are hardcoded (see
    :mod:`src.module_state`) and not configurable per project for
    v1; the flag exists so operators can opt out without touching
    code if the auto-transitions ever conflict with a different
    workflow.
    """

    enabled: bool = True


class Config(BaseModel):
    plane_base_url: str
    plane_workspace: str
    state_machine: StateMachine
    modules: ModulesConfig = ModulesConfig()


def load(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text())
    return Config.model_validate(raw)
