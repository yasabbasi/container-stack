import re
from pathlib import Path

PLAN = (Path(__file__).resolve().parent.parent / "docs" / "plan.md").read_text()


def section(heading: str) -> str:
    """Return the text under a heading, up to the next heading of the same or higher level."""
    level = len(heading) - len(heading.lstrip("#"))
    start = PLAN.index(f"\n{heading}\n") + 1
    end = re.compile(rf"^#{{1,{level}}} ", flags=re.M).search(PLAN, start + len(heading))
    return PLAN[start : end.start() if end else len(PLAN)]


def test_identities_table_has_a_reader_row() -> None:
    rows = section("## 5. Identities and what each may do")
    assert re.search(r"^\| `reader` \| The `check` workflow", rows, flags=re.M)


def test_section_5_says_how_claude_code_works_on_the_project() -> None:
    part = section("### How Claude Code works on the project")
    for word in ("Look", "Operate", "Change", "`gh`", "fixed list"):
        assert word in part, word


def test_federation_has_two_pools() -> None:
    part = section("## 5. Identities and what each may do")
    assert "`github-production`" in part


def test_section_3_has_settings() -> None:
    part = section("### Settings")
    assert "`.env.example`" in part
    assert "Secret Manager" in part


def test_bootstrap_comes_before_the_runtime_trial() -> None:
    steps = section("## 8. What the owner does by hand")
    assert steps.index("bootstrap script") < steps.index("runtime trial")


def test_bootstrap_step_creates_no_secret_and_lines_fit() -> None:
    steps = section("## 8. What the owner does by hand")
    step_2 = steps[steps.index("2. Run the bootstrap") : steps.index("3. Run the runtime")]
    assert "the secret" not in step_2
    assert all(len(line) <= 100 for line in step_2.splitlines())


def test_watcher_and_make_init_already_done() -> None:
    steps = section("## 8. What the owner does by hand")
    step_5 = steps[steps.index("5. ") : steps.index("6. ")]
    step_6 = steps[steps.index("6. ") : steps.index("7. ")]
    assert "2026-11-25" in step_5
    assert "make init" not in step_6
    assert "`make verify`" in step_6


def test_phase_1_step_4_uses_the_task_loop() -> None:
    phase = section("### Phase 1: containers on the laptop")
    assert "written by hand in four" not in phase
    assert "submitted as tasks" in phase


def test_quota_row_is_settled() -> None:
    assert "| Claude quota. Settled on 2026-11-24" in section("## 10. What could not be confirmed")
