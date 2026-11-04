from llm_gateway.approvals_cli import _printable


def test_client_controlled_text_cant_forge_lines_on_the_admins_terminal() -> None:
    assert _printable("fs__move_file\n0000  acme  fs__read\x1b[2K") == (
        "fs__move_file\\n0000  acme  fs__read\\x1b[2K"
    )
    assert _printable("café ✓") == "café ✓"
