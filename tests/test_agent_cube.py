import json

import agent_cube
import pytest


def test_pixel_mapping_covers_cube():
    for wiring in ("serpentine", "progressive"):
        indexes = {
            agent_cube.pixel_index(x, y, z, wiring)
            for x in range(5)
            for y in range(5)
            for z in range(5)
        }
        assert indexes == set(range(125))


def test_each_state_is_a_plane():
    for state in (
        "working",
        "streaming",
        "tool",
        "sending",
        "idle",
        "idle_dim",
        "error",
    ):
        assert len(agent_cube.plane_frame(state, 1.25)) == 25


def test_idle_fade_curve_hits_requested_anchors():
    assert agent_cube.idle_fade(600) == pytest.approx(0.1)
    assert agent_cube.idle_fade(3600) == pytest.approx(0.01)
    assert agent_cube.idle_fade(7200) < agent_cube.idle_fade(3600)


def test_codex_completion_is_only_unlocked_by_new_user_message(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rows = [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"type": "message", "role": "user"},
        },
        {"timestamp": "2026-01-01T00:00:10Z", "payload": {"type": "task_complete"}},
        {"timestamp": "2026-01-01T00:00:11Z", "payload": {"type": "agent_message"}},
    ]
    rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))
    user, complete, _, _ = agent_cube._rollout_state(rollout)
    assert complete > user
    rows.append(
        {
            "timestamp": "2026-01-01T00:01:00Z",
            "payload": {"type": "message", "role": "user"},
        }
    )
    rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))
    user, complete, _, _ = agent_cube._rollout_state(rollout)
    assert user > complete


def test_interrupt_timestamp_latches_idle_until_new_user():
    last_user = 100.0
    last_complete = 0.0
    interrupted = 110.0
    assert not last_user > max(last_complete, interrupted)
    last_user = 120.0
    assert last_user > max(last_complete, interrupted)


def test_cube_frame_is_rgb8():
    sessions = [agent_cube.Session(str(i), "test", "idle", 0) for i in range(5)]
    for axis in ("x", "y", "z"):
        assert len(agent_cube.cube_frame(sessions, 0, "serpentine", axis)) == 125 * 3


def test_ddp_packet_shape(monkeypatch):
    sent = []
    ddp = agent_cube.DDP("example.invalid")

    class Socket:
        def sendto(self, packet, address):
            sent.append((packet, address))

    ddp.socket = Socket()
    ddp.send(bytes(125 * 3))
    assert len(sent[0][0]) == 10 + 125 * 3
    assert sent[0][0][0] == 0x41


def test_claude_hook(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_cube, "STATE_DIR", tmp_path)
    path = agent_cube.record_hook(
        {"session_id": "abc", "hook_event_name": "PreToolUse"}
    )
    assert json.loads(path.read_text())["state"] == "tool"


def test_claude_idle_is_latched_until_user_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_cube, "STATE_DIR", tmp_path)
    stop = {"session_id": "abc", "hook_event_name": "Stop"}
    agent_cube.record_hook(stop)
    idle_since = json.loads((tmp_path / "claude-abc.json").read_text())["changed"]
    agent_cube.record_hook({"session_id": "abc", "hook_event_name": "PreToolUse"})
    still_idle = json.loads((tmp_path / "claude-abc.json").read_text())
    assert still_idle["state"] == "idle"
    assert still_idle["changed"] == idle_since
    agent_cube.record_hook({"session_id": "abc", "hook_event_name": "UserPromptSubmit"})
    assert json.loads((tmp_path / "claude-abc.json").read_text())["state"] == "sending"


def test_claude_hook_installer_preserves_existing_hooks(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"command": "existing"}]}]}})
    )
    monkeypatch.setattr(agent_cube, "CLAUDE_HOME", tmp_path)
    agent_cube.install_claude_hooks()
    installed = json.loads(settings.read_text())
    commands = [
        hook["command"]
        for group in installed["hooks"]["PreToolUse"]
        for hook in group["hooks"]
    ]
    assert "existing" in commands
    assert any(command.endswith("led-cube-agent-monitor-hook") for command in commands)
