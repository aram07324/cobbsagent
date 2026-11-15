"""Publish builder tests use isolated temporary roots and synthetic credentials."""

from pathlib import Path

import pytest

from build_publish import PublishSafetyError, build_publish


def test_builder_copies_exact_allowlist_and_no_extra_files(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("Project overview\n", encoding="utf-8")
    (tmp_path / "agent.py").write_text("print('safe')\n", encoding="utf-8")
    (tmp_path / "report.json").write_text("private report\n", encoding="utf-8")
    allowlist = tmp_path / "publish_allowlist.txt"
    allowlist.write_text("README.md\nagent.py\n", encoding="utf-8")

    files = build_publish(root=tmp_path, allowlist=allowlist)
    output = tmp_path / "publish"
    assert files == ["README.md", "agent.py"]
    assert sorted(path.name for path in output.iterdir()) == ["README.md", "agent.py"]
    assert not (output / "report.json").exists()


def test_builder_rejects_unlisted_eval_paths_before_creating_output(
    tmp_path: Path,
) -> None:
    eval_dir = tmp_path / "eval"
    (eval_dir / "jwt_breaking").mkdir(parents=True)
    (eval_dir / "jwt_breaking" / "test_app.py").write_text(
        "def test_example(): pass\n", encoding="utf-8",
    )
    allowlist = tmp_path / "publish_allowlist.txt"
    allowlist.write_text(
        "eval/jwt_breaking/test_app.py\n", encoding="utf-8",
    )

    with pytest.raises(PublishSafetyError, match="protected path"):
        build_publish(root=tmp_path, allowlist=allowlist)
    assert not (tmp_path / "publish").exists()


def test_builder_copies_only_explicit_evaluation_files(tmp_path: Path) -> None:
    expected = tmp_path / "eval" / "expected.json"
    expected.parent.mkdir()
    expected.write_text('{"case": {"expected": "safe_upgrade"}}\n',
                        encoding="utf-8")
    hidden = (
        tmp_path / "eval_hidden" / "jwt_weak_tests" / "test_hidden.py"
    )
    hidden.parent.mkdir(parents=True)
    hidden.write_text("def test_hidden(): pass\n", encoding="utf-8")
    allowlist = tmp_path / "publish_allowlist.txt"
    allowlist.write_text(
        "eval/expected.json\neval_hidden/jwt_weak_tests/test_hidden.py\n",
        encoding="utf-8",
    )

    files = build_publish(root=tmp_path, allowlist=allowlist)

    assert files == [
        "eval/expected.json",
        "eval_hidden/jwt_weak_tests/test_hidden.py",
    ]
    assert (tmp_path / "publish" / "eval" / "expected.json").is_file()
    assert (
        tmp_path / "publish" / "eval_hidden" / "jwt_weak_tests"
        / "test_hidden.py"
    ).is_file()


def test_secret_scan_fails_closed_without_echoing_content(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "Key: " + "AIza" + "A" * 24, encoding="utf-8",
    )
    allowlist = tmp_path / "publish_allowlist.txt"
    allowlist.write_text("README.md\n", encoding="utf-8")

    with pytest.raises(PublishSafetyError) as error:
        build_publish(root=tmp_path, allowlist=allowlist)
    assert "Google API key" in str(error.value)
    assert "AIza" not in str(error.value)
    assert not (tmp_path / "publish").exists()


def test_builder_never_overwrites_existing_publish_folder(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("safe\n", encoding="utf-8")
    allowlist = tmp_path / "publish_allowlist.txt"
    allowlist.write_text("README.md\n", encoding="utf-8")
    destination = tmp_path / "publish"
    destination.mkdir()
    existing = destination / "keep.txt"
    existing.write_text("keep\n", encoding="utf-8")

    with pytest.raises(PublishSafetyError, match="new directory"):
        build_publish(root=tmp_path, allowlist=allowlist)
    assert existing.read_text(encoding="utf-8") == "keep\n"


def test_license_is_explicitly_allowlisted_but_gitignore_is_not(
    tmp_path: Path,
) -> None:
    (tmp_path / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    allowlist = tmp_path / "publish_allowlist.txt"
    allowlist.write_text("LICENSE\n", encoding="utf-8")
    build_publish(root=tmp_path, allowlist=allowlist)
    assert (tmp_path / "publish" / "LICENSE").read_text(
        encoding="utf-8",
    ) == "MIT License\n"

    other = tmp_path / "other"
    other.mkdir()
    (other / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    deny = other / "publish_allowlist.txt"
    deny.write_text(".gitignore\n", encoding="utf-8")
    with pytest.raises(PublishSafetyError, match="protected path"):
        build_publish(root=other, allowlist=deny)
    assert not (other / "publish").exists()
