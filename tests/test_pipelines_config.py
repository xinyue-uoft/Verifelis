"""v1.0.3: user-configured pipeline whitelist."""

from pathlib import Path

import pytest

from verifelis.sandbox import Sandbox
from verifelis.tools import ToolBox, load_pipelines, validate_pipeline


def test_validate_accepts_fixed_argv():
    p = validate_pipeline("pdftolatex", {"argv": ["pdftolatex", "<file>"], "description": "d"})
    assert p.build(Path("/tmp/x.pdf")) == ["pdftolatex", "/tmp/x.pdf"]


@pytest.mark.parametrize(
    "name,spec",
    [
        ("bad name!", {"argv": ["cat", "<file>"]}),
        ("nofile", {"argv": ["cat"]}),                       # missing <file>
        ("twofile", {"argv": ["cp", "<file>", "<file>"]}),   # placeholder twice
        ("empty", {"argv": []}),
        ("notlist", {"argv": "cat <file>"}),
        ("badtype", {"argv": ["cat", 42, "<file>"]}),
        ("baddesc", {"argv": ["cat", "<file>"], "description": 7}),
    ],
)
def test_validate_rejects_malformed(name, spec):
    with pytest.raises(ValueError):
        validate_pipeline(name, spec)


def test_load_pipelines_merges_and_notes():
    config = {
        "pipelines": {
            "mycat": {"argv": ["cat", "<file>"], "description": "test cat"},
            "ghost": {"argv": ["no-such-binary-xyz", "<file>"]},
            "broken": {"argv": "not a list"},
        }
    }
    active, notes = load_pipelines(config)
    assert "mycat" in active
    assert "ghost" not in active
    assert "broken" not in active
    assert any("ghost" in n and "not found" in n for n in notes)
    assert any("broken" in n for n in notes)
    assert "pdftotext" in active  # defaults preserved


def test_config_pipeline_runs_through_toolbox(tmp_path: Path):
    (tmp_path / "in.txt").write_text("via config pipeline")
    active, notes = load_pipelines(
        {"pipelines": {"mycat": {"argv": ["cat", "<file>"], "description": "d"}}}
    )
    box = ToolBox(sandbox=Sandbox(tmp_path), pipelines=active)
    assert notes == []
    tc = box.call("run_pipeline", {"pipeline": "mycat", "file": "in.txt"})
    assert not tc.error
    assert tc.result == "via config pipeline"
    # still refuses anything not whitelisted
    tc2 = box.call("run_pipeline", {"pipeline": "rm", "file": "in.txt"})
    assert tc2.error


async def test_pipelines_command_lists_whitelist(tmp_path):
    from verifelis.tui import VerifelisApp, PromptInput

    class FakeBackend:
        name = "fake"

        async def chat(self, messages, tools):
            raise AssertionError("not used")

    (tmp_path / "doc.txt").write_text("x")
    app = VerifelisApp(
        FakeBackend(), tmp_path, reviewer="black",
        config={"backend": "fake",
                "pipelines": {"mycat": {"argv": ["cat", "<file>"], "description": "test cat"},
                              "ghost": {"argv": ["no-such-binary-xyz", "<file>"]}}},
    )
    async with app.run_test() as pilot:
        inp = app.query_one(PromptInput)
        inp.value = "/pipelines "
        await pilot.press("enter")
        text = "\n".join(str(s) for s in app.query_one("#chat").lines)
        assert "mycat" in text and "test cat" in text
        assert "ghost" in text and "not found" in text
