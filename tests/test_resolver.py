from src.resolver import ProjectStates, Resolver


class FakePlane:
    def __init__(self, projects: list[dict], states: dict[str, list[dict]]):
        self.projects = projects
        self.states = states
        self.list_projects_calls = 0
        self.list_states_calls: dict[str, int] = {}

    async def list_projects(self) -> list[dict]:
        self.list_projects_calls += 1
        return self.projects

    async def list_states(self, project_id: str) -> list[dict]:
        self.list_states_calls[project_id] = self.list_states_calls.get(project_id, 0) + 1
        return self.states.get(project_id, [])


PROJECTS = [
    {"id": "uuid-qf", "identifier": "QF"},
    {"id": "uuid-pt", "identifier": "PT"},
]
STATES_QF = [
    {"id": "qf-todo", "name": "Todo", "group": "unstarted"},
    {"id": "qf-progress", "name": "In Progress", "group": "started"},
    {"id": "qf-review", "name": "In Review", "group": "started"},
    {"id": "qf-testing", "name": "Testing", "group": "started"},
    {"id": "qf-done", "name": "Done", "group": "completed"},
]


async def test_project_uuid_caches_after_first_call() -> None:
    plane = FakePlane(PROJECTS, {})
    resolver = Resolver(plane)
    assert await resolver.project_uuid("QF") == "uuid-qf"
    assert await resolver.project_uuid("PT") == "uuid-pt"
    assert plane.list_projects_calls == 1


async def test_project_uuid_unknown_returns_none() -> None:
    plane = FakePlane(PROJECTS, {})
    resolver = Resolver(plane)
    assert await resolver.project_uuid("WEB") is None


async def test_states_caches_per_project() -> None:
    plane = FakePlane(PROJECTS, {"uuid-qf": STATES_QF})
    resolver = Resolver(plane)
    states = await resolver.states("uuid-qf")
    states_again = await resolver.states("uuid-qf")
    assert states is states_again
    assert plane.list_states_calls == {"uuid-qf": 1}


async def test_states_case_insensitive_lookup() -> None:
    plane = FakePlane(PROJECTS, {"uuid-qf": STATES_QF})
    resolver = Resolver(plane)
    states = await resolver.states("uuid-qf")
    assert states.uuid_for("In Review") == "qf-review"
    assert states.uuid_for("in review") == "qf-review"
    assert states.uuid_for("IN REVIEW") == "qf-review"


def test_states_reverse_lookup() -> None:
    states = ProjectStates(STATES_QF)
    assert states.name_for("qf-review") == "In Review"
    assert states.name_for("nonexistent") is None
    assert states.name_for(None) is None


def test_states_unknown_name_returns_none() -> None:
    states = ProjectStates(STATES_QF)
    assert states.uuid_for("Phantom State") is None


def test_states_skips_malformed_entries() -> None:
    states = ProjectStates([
        {"id": "x", "name": "X"},
        {"id": "", "name": "EmptyId"},
        {"id": "y", "name": ""},
        {"name": "MissingId"},
        {"id": "z"},
    ])
    assert states.uuid_for("X") == "x"
    assert "" not in states.by_uuid_to_name
    assert states.uuid_for("EmptyId") is None
    assert states.uuid_for("MissingId") is None
