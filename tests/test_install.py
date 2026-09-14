"""Exercise local installation without touching user configuration or networking."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Bash installer uses POSIX paths")


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    shutil.copyfile(Path(__file__).parents[1] / "install.sh", root / "install.sh")
    for name in ("first", "second"):
        skill = root / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"new {name}\n")
    return root


def install(checkout: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # The fixture controls the script and every argument; no shell expansion.
    return subprocess.run(  # noqa: S603
        ["bash", str(checkout / "install.sh"), *args],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def test_install_all_and_upgrade_preserves_external_roles(checkout: Path, tmp_path: Path) -> None:
    target = tmp_path / "client" / "skills"
    personal = target.parent / "worker-agents" / "reviewer.md"
    personal.parent.mkdir(parents=True)
    personal.write_text("personal role")
    result = install(checkout, "--target", str(target))
    assert result.returncode == 0, result.stderr
    assert "Installed 2 skill(s)" in result.stdout
    (target / "first" / "obsolete").write_text("old")
    result = install(checkout, "--target", str(target), "--skill", "first")
    assert result.returncode == 0, result.stderr
    assert not (target / "first" / "obsolete").exists()
    assert (target / "second" / "SKILL.md").read_text() == "new second\n"
    assert personal.read_text() == "personal role"
    assert not list(target.glob(".sub-agents-install.*"))


@pytest.mark.parametrize("args", [("--target",), ("--skill",), ("--target", "--skill", "first")])
def test_missing_option_value(checkout: Path, args: tuple[str, ...]) -> None:
    result = install(checkout, *args)
    assert result.returncode != 0
    assert "Missing value" in result.stderr


@pytest.mark.parametrize("name", ["../first", "/tmp/first", ".", "..", "first/second"])
def test_reject_skill_traversal(checkout: Path, tmp_path: Path, name: str) -> None:
    result = install(checkout, "--target", str(tmp_path / "target"), "--skill", name)
    assert result.returncode != 0
    assert "Invalid skill name" in result.stderr


def test_reject_self_install_and_symlink(checkout: Path, tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(checkout / "skills", target_is_directory=True)
    for target in (checkout / "skills", alias, checkout / "skills" / "nested"):
        result = install(checkout, "--target", str(target))
        assert result.returncode != 0
        assert "outside source skills" in result.stderr
        assert (checkout / "skills" / "first" / "SKILL.md").read_text() == "new first\n"


def test_reject_destination_symlink(checkout: Path, tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "first").symlink_to(checkout / "skills" / "first", target_is_directory=True)
    result = install(checkout, "--target", str(target))
    assert result.returncode != 0
    assert (target / "first").is_symlink()


@pytest.mark.parametrize("command", ["cp", "mv"])
def test_failed_copy_or_publish_preserves_old_install(
    checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    target = tmp_path / "target"
    old = target / "first"
    old.mkdir(parents=True)
    (old / "SKILL.md").write_text("previous installation")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real_command = shutil.which(command)
    assert real_command is not None
    wrapper = bin_dir / command
    if command == "cp":
        wrapper.write_text("#!/bin/bash\nexit 23\n")
    else:
        # Fail only staged publication; permit backup and restoration moves.
        wrapper.write_text(
            f'#!/bin/bash\ncase "$1" in */new/*) exit 23 ;; esac\nexec "{real_command}" "$@"\n'
        )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    result = install(checkout, "--target", str(target), "--skill", "first")
    assert result.returncode != 0
    assert (old / "SKILL.md").read_text() == "previous installation"
    assert not list(target.glob(".sub-agents-install.*"))


def test_help_succeeds(checkout: Path) -> None:
    assert install(checkout, "--help").returncode == 0


def test_all_skills_are_staged_before_replacement(
    checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    old = target / "first"
    old.mkdir(parents=True)
    (old / "SKILL.md").write_text("previous installation")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real_cp = shutil.which("cp")
    assert real_cp is not None
    wrapper = bin_dir / "cp"
    wrapper.write_text(
        f'#!/bin/bash\ncase "$2" in */second) exit 23 ;; esac\nexec "{real_cp}" "$@"\n'
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    result = install(checkout, "--target", str(target))
    assert result.returncode != 0
    assert (old / "SKILL.md").read_text() == "previous installation"
    assert not (target / "second").exists()
    assert not list(target.glob(".sub-agents-install.*"))
