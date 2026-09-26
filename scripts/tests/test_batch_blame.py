"""scripts/batch_blame.py -- which members of a red batch its failing tests implicate."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import batch_blame as b  # noqa: E402

MAP = {"scripts/tests/test_a.py": {"a.txt", "lib/"}, "scripts/tests/test_b.py": {"b.txt"}}


def test_pytest_summary_and_unproven_lines_name_their_files_and_nothing_else_does():
    log = ["  2026-09-23T00:14:47.1Z FAILED scripts/tests/test_a.py::t - AssertionError",
           "  ERROR scripts/tests/test_b.py",
           "      PASSED        scripts/tests/test_c.py::t  [NOT PROVEN -- passed on the base]",
           # a test NAMED with the words, and the differential's accepted test: neither is a failure line
           "  3.05s call     scripts/tests/test_d.py::test_a_FAILED_emission",
           "  scripts/tests/test_e.py::test_the_assertion_can_fail__trap_removed",
           "      FAILED        scripts/tests/test_f.py::t  [proven: FAILED on base]"]
    assert b.failing_test_files(log) == {"scripts/tests/test_a.py", "scripts/tests/test_b.py", "scripts/tests/test_c.py"}


def test_only_the_member_whose_diff_the_failing_test_reads_is_ejected():
    hit, _ = b.decide({"1": {"lib/x.py"}, "2": {"b.txt"}}, {"scripts/tests/test_a.py"}, MAP)
    assert hit == ["1"]


def test_a_member_that_changes_the_failing_test_file_itself_is_ejected():
    hit, _ = b.decide({"1": {"scripts/tests/test_a.py"}, "2": {"b.txt"}}, {"scripts/tests/test_a.py"}, MAP)
    assert hit == ["1"]


def test_an_unplaceable_path_or_machinery_or_an_unreadable_diff_cannot_be_cleared():
    failing = {"scripts/tests/test_a.py"}
    assert b.implicated({"nowhere.md"}, failing, MAP)
    assert b.implicated({"scripts/tests/conftest.py"}, failing, MAP, machinery={"scripts/tests/conftest.py"})
    assert b.implicated(None, failing, MAP)


def test_a_new_test_file_that_is_not_failing_does_not_implicate_its_member():
    assert not b.implicated({"scripts/tests/test_new.py", "b.txt"}, {"scripts/tests/test_a.py"}, MAP)


def test_none_or_all_implicated_means_halve():
    failing = {"scripts/tests/test_a.py"}
    assert b.decide({"1": {"b.txt"}, "2": {"b.txt"}}, failing, MAP)[0] == []
    assert b.decide({"1": {"a.txt"}, "2": {"lib/y"}}, failing, MAP)[0] == []
    assert b.decide({"1": {"a.txt"}}, set(), MAP)[0] == []
