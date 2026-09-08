import pytest

from agent_board import cli as board, identity


@pytest.mark.parametrize("field", ["from", "to"])
def test_historical_message_identity_preserves_bytes(tmp_path, field):
    root = board.initialize(tmp_path / "board")
    message = board.post_message(root, sender="gpt-master", recipient="gpt-master",
                                 kind="STATUS", priority="NORMAL", workstream="BOARD",
                                 summary="Historical", body="Original body")
    message[field] = "sol-master"
    path = root / "messages" / (message["id"] + ".md")
    path.write_text(board._message_markdown(message, "Original body"), encoding="utf-8")
    original = path.read_bytes()
    restored, _, _ = board.read_message(root, message["id"])
    assert restored[field] == "sol-master"
    assert board._ack_path(root, message["id"], restored["to"]).name.endswith(restored["to"] + ".json")
    assert path.read_bytes() == original
    validator = identity.require_message_sender if field == "from" else identity.require_message_recipient
    with pytest.raises(board.BoardError, match="unauthorized identity"):
        validator("sol-master", root)


def test_historical_identity_does_not_expand_unrelated_project(tmp_path):
    board.initialize(tmp_path)
    identity.seed_project_config(tmp_path)
    with pytest.raises(board.BoardError, match="unauthorized identity"):
        identity.require_message_sender("sol-master", tmp_path, historical=True)
