import os
from pathlib import Path

import pytest

from verifelis.sandbox import Sandbox, SecretBlocked, OutsideRoot


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "paper.txt").write_text("hello")
    (tmp_path / ".env").write_text("API_KEY=x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "data.csv").write_text("a,b")
    return tmp_path


def test_read_inside_root(root):
    sb = Sandbox(root)
    assert sb.resolve("paper.txt") == root / "paper.txt"
    assert sb.resolve(str(root / "sub" / "data.csv")).name == "data.csv"


def test_env_blocked(root):
    sb = Sandbox(root)
    with pytest.raises(SecretBlocked):
        sb.resolve(".env")


@pytest.mark.parametrize(
    "name",
    [".env.local", "prod.env", "id_rsa", "id_rsa.pub", "server.pem",
     "private.key", "credentials", "credentials.json", ".netrc",
     "my_apikey.txt", "secrets.yaml", "login.keychain-db"],
)
def test_secret_basenames_blocked(root, name):
    sb = Sandbox(root)
    (root / name).write_text("x")
    with pytest.raises(SecretBlocked):
        sb.resolve(name)


def test_ssh_dir_blocked_anywhere(root):
    sb = Sandbox(root)
    d = root / ".ssh"
    d.mkdir()
    (d / "known_hosts").write_text("x")
    with pytest.raises(SecretBlocked):
        sb.resolve(".ssh/known_hosts")


def test_outside_root_denied_by_default(root):
    sb = Sandbox(root)
    with pytest.raises(OutsideRoot):
        sb.resolve("/etc/hosts")


def test_traversal_denied(root):
    sb = Sandbox(root)
    with pytest.raises(OutsideRoot):
        sb.resolve("../" * 10 + "etc/hosts")


def test_symlink_escape_denied(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "target.txt").write_text("secret-ish")
    os.symlink(outside / "target.txt", root / "link.txt")
    sb = Sandbox(root)
    with pytest.raises(OutsideRoot):
        sb.resolve("link.txt")


def test_symlink_to_secret_blocked_even_with_approval(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside2")
    (outside / "id_rsa").write_text("PRIVATE")
    os.symlink(outside / "id_rsa", root / "innocent.txt")
    sb = Sandbox(root, approval_gate=lambda p: True)
    with pytest.raises(SecretBlocked):
        sb.resolve("innocent.txt")


def test_approval_gate_expands(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside3")
    f = outside / "extra.txt"
    f.write_text("ok")
    asked: list[Path] = []

    def gate(p: Path) -> bool:
        asked.append(p)
        return True

    sb = Sandbox(root, approval_gate=gate)
    assert sb.resolve(str(f)) == f
    assert asked == [f]
    # Approved directory persists; sibling access does not re-ask.
    (outside / "extra2.txt").write_text("ok")
    assert sb.resolve(str(outside / "extra2.txt")) == outside / "extra2.txt"
    assert len(asked) == 1


def test_approval_denied(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside4")
    (outside / "x.txt").write_text("x")
    sb = Sandbox(root, approval_gate=lambda p: False)
    with pytest.raises(OutsideRoot):
        sb.resolve(str(outside / "x.txt"))


def test_filter_visible_drops_secrets(root):
    sb = Sandbox(root)
    visible = sb.filter_visible(sorted(root.iterdir()))
    names = {p.name for p in visible}
    assert ".env" not in names
    assert "paper.txt" in names
