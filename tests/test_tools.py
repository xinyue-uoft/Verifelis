from pathlib import Path

import pytest

from verifelis.sandbox import Sandbox
from verifelis.tools import ToolBox, Pipeline


@pytest.fixture
def box(tmp_path: Path) -> ToolBox:
    (tmp_path / "a.txt").write_text("alpha\nbeta\ngamma\n")
    (tmp_path / ".env").write_text("KEY=leak")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("beta again\n")
    return ToolBox(sandbox=Sandbox(tmp_path), pipelines={})


def test_list_dir_hides_secrets(box, tmp_path):
    tc = box.call("list_dir", {"path": str(tmp_path)})
    assert not tc.error
    assert "a.txt" in tc.result
    assert ".env" not in tc.result


def test_read_file(box, tmp_path):
    tc = box.call("read_file", {"path": "a.txt"})
    assert not tc.error
    assert "1: alpha" in tc.result


def test_read_file_range(box):
    tc = box.call("read_file", {"path": "a.txt", "start_line": 2, "end_line": 2})
    assert tc.result == "2: beta"


def test_read_secret_denied(box):
    tc = box.call("read_file", {"path": ".env"})
    assert tc.error
    assert "DENIED" in tc.result


def test_grep_skips_secrets(box, tmp_path):
    tc = box.call("grep", {"pattern": "leak|beta", "path": str(tmp_path)})
    assert not tc.error
    assert "b.txt" in tc.result
    assert ".env" not in tc.result


def test_grep_single_file(box):
    tc = box.call("grep", {"pattern": "gamma", "path": "a.txt"})
    assert "a.txt:3" in tc.result


def test_stat(box):
    tc = box.call("stat", {"path": "a.txt"})
    assert "file" in tc.result


def test_unknown_tool_denied(box):
    tc = box.call("write_file", {"path": "x", "content": "y"})
    assert tc.error


def test_pipeline_not_whitelisted(box):
    tc = box.call("run_pipeline", {"pipeline": "rm", "file": "a.txt"})
    assert tc.error
    assert "DENIED" in tc.result


def test_pipeline_runs_fixed_argv(tmp_path):
    (tmp_path / "in.txt").write_text("hello pipeline")
    box = ToolBox(
        sandbox=Sandbox(tmp_path),
        pipelines={"cat": Pipeline("cat", ["cat", "<file>"], "test")},
    )
    tc = box.call("run_pipeline", {"pipeline": "cat", "file": "in.txt"})
    assert not tc.error
    assert tc.result == "hello pipeline"


def test_pipeline_file_sandboxed(tmp_path):
    box = ToolBox(
        sandbox=Sandbox(tmp_path),
        pipelines={"cat": Pipeline("cat", ["cat", "<file>"], "test")},
    )
    tc = box.call("run_pipeline", {"pipeline": "cat", "file": "/etc/hosts"})
    assert tc.error
    assert "DENIED" in tc.result


def test_log_records_all_calls(box):
    box.call("read_file", {"path": "a.txt"})
    box.call("read_file", {"path": ".env"})
    assert len(box.log) == 2
    assert box.log[1].error
