"""v1.0.4: derived-output pipelines — index results, auto-whitelisted
output dir, caching, and streamed line-range reads."""

import os
import stat
import time
from pathlib import Path

import pytest

from verifelis import tools as tools_mod
from verifelis.sandbox import Sandbox
from verifelis.tools import Pipeline, ToolBox, validate_pipeline


@pytest.fixture
def derived_base(tmp_path_factory, monkeypatch):
    base = tmp_path_factory.mktemp("derived")
    monkeypatch.setattr(tools_mod, "DERIVED_BASE", base)
    return base


@pytest.fixture
def fake_ocr(tmp_path_factory) -> Path:
    """Script mimicking an OCR pipeline: writes markdown + counts runs."""
    d = tmp_path_factory.mktemp("bin")
    script = d / "fakeocr"
    script.write_text(
        "#!/bin/sh\n"
        "mkdir -p \"$2/auto\"\n"
        "printf '# Title\\n\\nbody text about FeSe\\n\\n## Results\\nTc is 65 K\\n' > \"$2/auto/out.md\"\n"
        "echo run >> \"$2/../runs.log\"\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.fixture
def box(tmp_path, fake_ocr, derived_base) -> ToolBox:
    (tmp_path / "paper.pdf").write_text("pdf bytes")
    pl = Pipeline("fakeocr", [str(fake_ocr), "<file>", "<outdir>"], "test ocr")
    return ToolBox(sandbox=Sandbox(tmp_path), pipelines={"fakeocr": pl})


def test_outdir_pipeline_returns_index_not_content(box, derived_base):
    tc = box.call("run_pipeline", {"pipeline": "fakeocr", "file": "paper.pdf"})
    assert not tc.error
    assert "output dir (now readable):" in tc.result
    assert "auto/out.md" in tc.result
    assert "main document:" in tc.result
    assert "outline (heading lines):" in tc.result and "## Results" in tc.result
    assert "read_file(path, start_line, end_line)" in tc.result
    # index, not payload: body text is not inlined
    assert "body text about FeSe" not in tc.result


def test_outdir_added_to_sandbox_without_approval(box, derived_base):
    box.call("run_pipeline", {"pipeline": "fakeocr", "file": "paper.pdf"})
    md = derived_base / "fakeocr" / "paper" / "auto" / "out.md"
    # default gate denies everything, yet the derived file is readable
    tc = box.call("read_file", {"path": str(md)})
    assert not tc.error
    assert "Tc is 65 K" in tc.result


def test_pipeline_output_cached(box, derived_base):
    box.call("run_pipeline", {"pipeline": "fakeocr", "file": "paper.pdf"})
    tc2 = box.call("run_pipeline", {"pipeline": "fakeocr", "file": "paper.pdf"})
    assert "reused cached output" in tc2.result
    runs = (derived_base / "fakeocr" / "runs.log").read_text().count("run")
    assert runs == 1


def test_stale_cache_reruns(box, derived_base, tmp_path):
    box.call("run_pipeline", {"pipeline": "fakeocr", "file": "paper.pdf"})
    future = time.time() + 60
    os.utime(tmp_path / "paper.pdf", (future, future))
    tc = box.call("run_pipeline", {"pipeline": "fakeocr", "file": "paper.pdf"})
    assert "completed" in tc.result
    runs = (derived_base / "fakeocr" / "runs.log").read_text().count("run")
    assert runs == 2


def test_validate_outdir_at_most_once():
    validate_pipeline("ok", {"argv": ["x", "<file>", "<outdir>"]})
    with pytest.raises(ValueError):
        validate_pipeline("bad", {"argv": ["x", "<file>", "<outdir>", "<outdir>"]})


def test_read_file_range_beyond_256kb(tmp_path):
    big = tmp_path / "big.md"
    with big.open("w") as f:
        for i in range(1, 20001):
            f.write(f"line {i} " + "x" * 20 + "\n")  # ~560KB total
    box = ToolBox(sandbox=Sandbox(tmp_path), pipelines={})
    tc = box.call("read_file", {"path": "big.md", "start_line": 19990, "end_line": 19995})
    assert not tc.error
    assert "19990: line 19990" in tc.result
    assert "truncated" not in tc.result


def test_read_file_full_read_still_capped(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("y" * 300 + "\n" * 1 + ("z" * 100 + "\n") * 5000)
    box = ToolBox(sandbox=Sandbox(tmp_path), pipelines={})
    tc = box.call("read_file", {"path": "big.txt"})
    assert "truncated at 256KB" in tc.result
