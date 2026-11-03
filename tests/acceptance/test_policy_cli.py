"""`llm-gateway policy check`: validate a policy file before deploying it."""

from pathlib import Path

import pytest

from llm_gateway.cli import main


def check(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    exit_code = main(["policy", "check", *args])
    out, err = capsys.readouterr()
    return exit_code, out.strip(), err.strip()


def test_the_built_in_policies_check_out(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code, out, _ = check(capsys)

    assert exit_code == 0
    assert out.startswith("ok: 5 policies: fs-move-needs-approval, ")


def test_a_broken_file_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    broken = tmp_path / "broken.cedar"
    broken.write_text("permit (principal, action, resource")

    exit_code, _, err = check(capsys, str(broken))

    assert exit_code == 1
    assert err.startswith("error: ")


def test_a_missing_file_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code, _, err = check(capsys, str(tmp_path / "nope.cedar"))

    assert exit_code == 1
    assert "nope.cedar" in err
