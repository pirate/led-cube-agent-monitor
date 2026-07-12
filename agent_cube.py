from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import plistlib
import re
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

SIZE = 5
PIXELS = SIZE**3
UUID_RE = re.compile(r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})")
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
STATE_DIR = Path(
    os.environ.get("AGENT_CUBE_STATE_DIR", Path.home() / ".cache/agent-cube")
)
LAUNCHD_LABEL = "com.pirate.led-cube-agent-monitor"
LEGACY_LAUNCHD_LABEL = "com.squash.agent-cube"


@dataclass
class Session:
    id: str
    kind: str
    state: str
    changed: float
    label: str = ""
    order: int = 999
    tty: str = ""


def pixel_index(x: int, y: int, z: int, wiring: str = "serpentine") -> int:
    """Five horizontal 5x5 PCBs, bottom first, each wired row-by-row."""
    if wiring == "progressive":
        within = y * SIZE + x
    else:
        within = y * SIZE + (x if y % 2 == 0 else SIZE - 1 - x)
    return z * SIZE * SIZE + within


def _run(args: list[str]) -> str:
    try:
        return subprocess.run(args, text=True, capture_output=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def iterm_tty_order() -> list[str]:
    """TTY order by window, tab, then iTerm's visual split-pane traversal."""
    script = """tell application \"iTerm2\"
set output to \"\"
repeat with w from 1 to count of windows
repeat with t from 1 to count of tabs of window w
repeat with s from 1 to count of sessions of tab t of window w
set output to output & (tty of session s of tab t of window w) & linefeed
end repeat
end repeat
end repeat
return output
end tell"""
    return [line.strip() for line in _run(["osascript", "-e", script]).splitlines()]


def open_codex_sessions() -> dict[str, tuple[Path, int, str]]:
    # macOS pgrep occasionally omits CLI processes launched inside Codex's PTYs.
    # `ps` sees both the CLI and app-server variants consistently.
    tty_order = iterm_tty_order()
    tty_rank = {tty.removeprefix("/dev/"): i for i, tty in enumerate(tty_order)}
    processes: list[tuple[int, str, str]] = []
    for line in _run(["ps", "ax", "-o", "pid=,tty=,comm="]).splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and Path(fields[2]).name == "codex":
            processes.append((tty_rank.get(fields[1], 999), fields[0], fields[1]))
    processes.sort()
    pids = [pid for _, pid, _ in processes]
    if not pids:
        return {}
    found: dict[str, tuple[Path, int, str]] = {}
    for order, pid, tty in processes:
        paths = []
        for line in _run(["lsof", "-n", "-a", "-p", pid, "-Fn"]).splitlines():
            if line.startswith("n") and "/.codex/" in line and line.endswith(".jsonl"):
                paths.append(Path(line[1:]))
        # Each iTerm split pane gets its own process/slice. Collaboration subagent
        # rollouts inherited within that process do not create extra visible panes.
        # The oldest rollout is the primary session represented by this pane.
        if paths:
            path = min(paths, key=lambda candidate: candidate.name)
            match = UUID_RE.search(path.name)
            if match:
                found[match.group(1)] = (path, order, "/dev/" + tty)
    return found


def _rollout_state(path: Path) -> tuple[float, float, float, bool]:
    """Return exact-session user, completion, activity, and outstanding-tool state."""
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            start = max(0, stream.tell() - 2_000_000)
            stream.seek(start)
            lines = stream.read().splitlines()
            if start:
                lines = lines[1:]
    except OSError:
        return 0, 0, 0, False
    outstanding = 0
    last_user = 0.0
    last_complete = 0.0
    last_activity = 0.0
    for raw in lines:
        try:
            row = json.loads(raw)
            payload = row.get("payload", {})
            typ = payload.get("type", "")
            stamp = row.get("timestamp")
            if not stamp:
                continue
            event_time = datetime.fromisoformat(
                stamp.replace("Z", "+00:00")
            ).timestamp()
            if typ in {"function_call", "custom_tool_call", "local_shell_call"}:
                outstanding += 1
                last_activity = event_time
            elif typ in {
                "function_call_output",
                "custom_tool_call_output",
                "local_shell_call_output",
            }:
                outstanding = max(0, outstanding - 1)
                last_activity = event_time
            elif typ == "task_complete":
                last_complete = event_time
                last_activity = event_time
            elif typ == "message" and payload.get("role") == "user":
                last_user = event_time
                last_activity = event_time
            elif typ in {"agent_message", "reasoning"} or (
                typ == "message" and payload.get("role") == "assistant"
            ):
                last_activity = event_time
        except (json.JSONDecodeError, AttributeError):
            continue
    return last_user, last_complete, last_activity, outstanding > 0


def codex_sessions(now: float) -> list[Session]:
    paths = open_codex_sessions()
    if not paths:
        return []
    db = CODEX_HOME / "logs_2.sqlite"
    events: dict[str, list[tuple[float, str, str, bool]]] = {sid: [] for sid in paths}
    interrupts: dict[str, float] = {sid: 0.0 for sid in paths}
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.15)
            marks = ",".join("?" for _ in paths)
            rows = con.execute(
                f"SELECT ts + ts_nanos/1e9, thread_id, level, target, "
                f"feedback_log_body, process_uuid FROM logs WHERE thread_id IN ({marks}) "
                "AND ts > ? ORDER BY ts, ts_nanos",
                [*paths, int(now - 120)],
            ).fetchall()
            proc_for: dict[str, str] = {}
            for ts, sid, level, target, body, proc in rows:
                proc_for[sid] = proc
                events[sid].append(
                    (ts, level, (target or "") + " " + (body or ""), True)
                )
            procs = list(set(proc_for.values()))
            if procs:
                marks = ",".join("?" for _ in procs)
                for ts, level, body, proc in con.execute(
                    f"SELECT ts + ts_nanos/1e9, level, feedback_log_body, process_uuid FROM logs "
                    f"WHERE process_uuid IN ({marks}) AND thread_id IS NULL AND ts > ? ORDER BY ts, ts_nanos",
                    [*procs, int(now - 120)],
                ):
                    for sid, sid_proc in proc_for.items():
                        if sid_proc == proc:
                            events[sid].append((ts, level, body or "", False))
            for sid, interrupted_at in con.execute(
                f"SELECT thread_id, MAX(ts + ts_nanos/1e9) FROM logs "
                f"WHERE thread_id IN ({','.join('?' for _ in paths)}) "
                "AND feedback_log_body LIKE '%codex.op=\"interrupt\"%' GROUP BY thread_id",
                list(paths),
            ):
                interrupts[sid] = interrupted_at
            con.close()
        except sqlite3.Error:
            pass

    result = []
    for sid, (path, order, tty) in paths.items():
        last_user, last_complete, rollout_activity, outstanding = _rollout_state(path)
        state = "idle"
        delta = 0.0
        error = 0.0
        for ts, level, text, scoped in sorted(events[sid]):
            if "agentMessage/delta" in text or "response.output_text.delta" in text:
                delta = max(delta, ts)
            if scoped and level == "ERROR":
                error = max(error, ts)
        # Completion and Escape/cancel are latches. Process-level telemetry is
        # advisory and cannot unlock them; only a newer exact-session user message can.
        terminal_at = max(last_complete, interrupts[sid])
        active = last_user > terminal_at
        if not active:
            state = "idle"
            changed = terminal_at or rollout_activity
        elif now - last_user < 1.2:
            state = "sending"
            changed = last_user
        elif now - error < 12:
            state = "error"
            changed = error
        elif outstanding:
            state = "tool"
            changed = max(rollout_activity, last_user)
        elif now - delta < 1.0:
            state = "streaming"
            changed = max(delta, rollout_activity)
        else:
            state = "working"
            changed = max(rollout_activity, last_user)
        result.append(Session(sid, "codex", state, changed, path.stem[:24], order, tty))
    return result


HOOK_STATES = {
    "SessionStart": "idle",
    "SessionEnd": "gone",
    "UserPromptSubmit": "sending",
    "PreToolUse": "tool",
    "PostToolUse": "working",
    "PostToolUseFailure": "error",
    "PermissionRequest": "error",
    "Stop": "idle",
    "SubagentStart": "working",
    "SubagentStop": "working",
}


def _parent_tty() -> str:
    pid = os.getppid()
    for _ in range(8):
        fields = _run(["ps", "-p", str(pid), "-o", "ppid=,tty="]).split()
        if len(fields) < 2:
            break
        pid, tty = int(fields[0]), fields[1]
        if tty not in {"??", "?"}:
            return "/dev/" + tty.removeprefix("/dev/")
    return ""


def record_hook(data: dict) -> Path:
    sid = str(data.get("session_id") or data.get("sessionId") or os.getppid())
    event = str(data.get("hook_event_name") or data.get("hookEventName") or "")
    state = HOOK_STATES.get(event, "working")
    if event == "Notification" and data.get("notification_type") == "permission_prompt":
        state = "error"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"claude-{sid}.json"
    changed = time.time()
    tty = _parent_tty()
    try:
        previous = json.loads(path.read_text())
        tty = tty or str(previous.get("tty", ""))
        if previous.get("state") == "idle" and event not in {
            "SessionStart",
            "SessionEnd",
            "UserPromptSubmit",
        }:
            state = "idle"
            changed = float(previous["changed"])
    except (OSError, ValueError, KeyError):
        pass
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {"id": sid, "state": state, "changed": changed, "event": event, "tty": tty}
        )
    )
    tmp.replace(path)
    return path


def claude_sessions(now: float) -> list[Session]:
    result = []
    if not STATE_DIR.exists():
        return result
    tty_rank = {tty: i for i, tty in enumerate(iterm_tty_order())}
    for path in STATE_DIR.glob("claude-*.json"):
        try:
            data = json.loads(path.read_text())
            changed = float(data["changed"])
            if now - changed > 24 * 3600:
                continue
            state = str(data["state"])
            if state == "gone":
                continue
            if state == "sending" and now - changed > 1.2:
                state = "working"
            transcripts = list(CLAUDE_HOME.glob(f"projects/*/{data['id']}.jsonl"))
            if state == "working" and transcripts:
                transcript_changed = max(p.stat().st_mtime for p in transcripts)
                if now - transcript_changed < 1.0:
                    state = "streaming"
            result.append(
                Session(
                    str(data["id"]),
                    "claude",
                    state,
                    changed,
                    data.get("event", ""),
                    tty_rank.get(data.get("tty", ""), 999),
                    data.get("tty", ""),
                )
            )
        except (OSError, ValueError, KeyError):
            continue
    return result


def sessions(now: float) -> list[Session]:
    combined = codex_sessions(now) + claude_sessions(now)
    return sorted(combined, key=lambda s: (s.order, -s.changed))[:SIZE]


def _scale(color: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, round(c * amount))) for c in color)  # type: ignore[return-value]


def idle_fade(idle_seconds: float) -> float:
    """Asymptotic curve: 10% at 10 minutes and 1% at one hour."""
    exponent = math.log(10) / math.log(5)
    return (1 + max(0, idle_seconds) / 150) ** -exponent


def plane_frame(
    state: str, t: float, phase: float = 0, intensity: float = 1.0
) -> list[tuple[int, int, int]]:
    """A 5x5 yz plane. Rows are z (bottom-up), columns are y."""
    black = (0, 0, 0)
    p = [[black for _ in range(SIZE)] for _ in range(SIZE)]
    if state == "idle":
        color = (0, 170, 35) if int((t + phase) * 2) % 2 == 0 else (0, 12, 2)
        p = [[color] * SIZE for _ in range(SIZE)]
    elif state == "idle_dim":
        color = _scale((0, 170, 35), intensity)
        p = [[color] * SIZE for _ in range(SIZE)]
    elif state == "error":
        color = (255, 0, 0) if int((t + phase) * 4) % 2 == 0 else (15, 0, 0)
        p = [[color] * SIZE for _ in range(SIZE)]
    elif state == "streaming":
        fill = ((t * 7 + phase * 5) % (SIZE * SIZE + 4)) - 2
        for z in range(SIZE):
            for y in range(SIZE):
                n = z * SIZE + y
                if n <= fill:
                    p[z][y] = (15, 80 + z * 30, 255)
                elif n == math.ceil(fill):
                    p[z][y] = (100, 200, 255)
    elif state == "tool":
        ring = [
            (0, 0),
            (0, 1),
            (0, 2),
            (0, 3),
            (0, 4),
            (1, 4),
            (2, 4),
            (3, 4),
            (4, 4),
            (4, 3),
            (4, 2),
            (4, 1),
            (4, 0),
            (3, 0),
            (2, 0),
            (1, 0),
        ]
        head = int((t * 10 + phase) % len(ring))
        for tail in range(6):
            z, y = ring[(head - tail) % len(ring)]
            p[z][y] = _scale((255, 125, 0), 1 - tail / 7)
        p[2][2] = (50, 15, 0)
    elif state == "sending":
        head = int((t * 12 + phase) % (SIZE + 4)) - 2
        for z in range(SIZE):
            for y in range(SIZE):
                distance = abs(y - head) + abs(z - 2) * 0.7
                if distance < 2.2:
                    p[z][y] = _scale((190, 30, 255), 1 - distance / 2.5)
    else:  # working / thinking
        wave = t * 4 + phase
        for z in range(SIZE):
            for y in range(SIZE):
                glow = 0.12 + 0.55 * (0.5 + 0.5 * math.sin(wave + y * 1.3 + z * 0.8))
                p[z][y] = _scale((0, 120, 255), glow)
    return [p[z][y] for z in range(SIZE) for y in range(SIZE)]


def cube_frame(
    items: list[Session], t: float, wiring: str, plane_axis: str = "z"
) -> bytes:
    colors = [(0, 0, 0)] * PIXELS
    for slot, item in enumerate(items[:SIZE]):
        physical_slot = SIZE - 1 - slot
        idle_seconds = t - item.changed
        display_state = (
            "idle_dim" if item.state == "idle" and idle_seconds > 10 else item.state
        )
        intensity = idle_fade(idle_seconds) if display_state == "idle_dim" else 1.0
        plane = plane_frame(display_state, t, slot * 0.7, intensity)
        for row in range(SIZE):
            for column in range(SIZE):
                if plane_axis == "x":
                    x, y, z = physical_slot, column, row
                elif plane_axis == "y":
                    x, y, z = column, physical_slot, row
                else:
                    x, y, z = column, row, physical_slot
                colors[pixel_index(x, y, z, wiring)] = plane[row * SIZE + column]
    return bytes(channel for rgb in colors for channel in rgb)


class DDP:
    def __init__(self, host: str, port: int = 4048):
        self.address = (host, port)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sequence = 0

    def send(self, pixels: bytes) -> None:
        # DDP v1: version/push, sequence, data type RGB8, destination, offset, length.
        self.sequence = (self.sequence + 1) % 16 or 1
        header = struct.pack(">BBBBIH", 0x41, self.sequence, 0x01, 0x01, 0, len(pixels))
        self.socket.sendto(header + pixels, self.address)


def print_status(items: Iterable[Session]) -> None:
    print(
        "  ".join(
            f"{i + 1}:{s.kind}:{s.id[:8]}={s.state}" for i, s in enumerate(items)
        ),
        flush=True,
    )


def publish_iterm_states(items: Iterable[Session]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / "iterm-states.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {
                "updated": time.time(),
                "states": {
                    session.tty: session.state for session in items if session.tty
                },
            },
            sort_keys=True,
        )
    )
    tmp.replace(path)


def daemon(args: argparse.Namespace) -> None:
    sender = DDP(args.host, args.port)
    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    last_key = None
    frame_time = 1 / args.fps
    items: list[Session] = []
    next_poll = 0.0
    scan: concurrent.futures.Future[list[Session]] | None = None
    send_failed = False
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        while not stopping:
            started = time.monotonic()
            now = time.time()
            if scan is not None and scan.done():
                items = scan.result()
                scan = None
                publish_iterm_states(items)
                key = tuple((s.id, s.state) for s in items)
                if key != last_key:
                    print_status(items)
                    last_key = key
            if scan is None and started >= next_poll:
                scan = executor.submit(sessions, now)
                next_poll = started + args.poll_interval
            if not args.dry_run:
                try:
                    sender.send(cube_frame(items, now, args.wiring, args.plane_axis))
                    if send_failed:
                        print("WLED connection recovered", file=sys.stderr, flush=True)
                    send_failed = False
                except OSError as error:
                    if not send_failed:
                        print(f"WLED send failed: {error}", file=sys.stderr, flush=True)
                    send_failed = True
            time.sleep(max(0, frame_time - (time.monotonic() - started)))


def demo(args: argparse.Namespace) -> None:
    sender = DDP(args.host, args.port)
    states = ["working", "streaming", "tool", "sending", "idle"]
    end = time.monotonic() + args.seconds
    while time.monotonic() < end:
        now = time.time()
        items = [Session(str(i), "demo", state, now) for i, state in enumerate(states)]
        sender.send(cube_frame(items, now, args.wiring, args.plane_axis))
        time.sleep(1 / args.fps)


def executable(name: str) -> str:
    return shutil.which(name) or str(Path(sys.executable).parent / name)


def install_claude_hooks() -> None:
    settings = CLAUDE_HOME / "settings.json"
    data = json.loads(settings.read_text()) if settings.exists() else {}
    hooks = data.setdefault("hooks", {})
    command = executable("led-cube-agent-monitor-hook")
    entry = {"type": "command", "command": command}
    events = (
        "SessionStart",
        "SessionEnd",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "Notification",
        "Stop",
    )
    changed = False
    for event in events:
        groups = hooks.setdefault(event, [])
        installed = False
        for group in groups:
            for hook in group.get("hooks", []):
                if Path(hook.get("command", "")).name in {
                    "agent-cube-hook",
                    "led-cube-agent-monitor-hook",
                }:
                    installed = True
                    if hook != entry:
                        hook.clear()
                        hook.update(entry)
                        changed = True
        if not installed:
            groups.append({"hooks": [entry]})
            changed = True
    if not changed:
        print(f"Claude hooks already installed in {settings}")
        return
    CLAUDE_HOME.mkdir(parents=True, exist_ok=True)
    if settings.exists():
        backup = settings.with_name(
            f"settings.json.agent-cube-backup-{int(time.time())}"
        )
        backup.write_text(settings.read_text())
    settings.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Installed Claude hooks in {settings}")


def launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args], text=True, capture_output=True, check=False
    )


def install_macos(args: argparse.Namespace) -> None:
    if sys.platform != "darwin":
        raise SystemExit(
            "The background-service installer currently supports macOS only"
        )
    install_claude_hooks()
    uid = str(os.getuid())
    launch_agents = Path.home() / "Library/LaunchAgents"
    logs = Path.home() / "Library/Logs"
    launch_agents.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents / f"{LAUNCHD_LABEL}.plist"
    command = executable("led-cube-agent-monitor")
    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [
            command,
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--fps",
            str(args.fps),
            "--poll-interval",
            str(args.poll_interval),
            "--wiring",
            args.wiring,
            "--plane-axis",
            args.plane_axis,
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(logs / "led-cube-agent-monitor.log"),
        "StandardErrorPath": str(logs / "led-cube-agent-monitor.error.log"),
    }
    plist_path.write_bytes(plistlib.dumps(plist))
    launchctl("bootout", f"gui/{uid}/{LAUNCHD_LABEL}")
    launchctl("bootout", f"gui/{uid}/{LEGACY_LAUNCHD_LABEL}")
    legacy_plist = launch_agents / f"{LEGACY_LAUNCHD_LABEL}.plist"
    legacy_plist.unlink(missing_ok=True)
    result = launchctl("bootstrap", f"gui/{uid}", str(plist_path))
    if result.returncode:
        raise SystemExit(result.stderr.strip() or "launchctl bootstrap failed")
    launchctl("kickstart", "-k", f"gui/{uid}/{LAUNCHD_LABEL}")
    print(f"Installed and started {LAUNCHD_LABEL}")
    print(f"Logs: {logs / 'led-cube-agent-monitor.log'}")


def uninstall_macos() -> None:
    uid = str(os.getuid())
    launchctl("bootout", f"gui/{uid}/{LAUNCHD_LABEL}")
    (Path.home() / f"Library/LaunchAgents/{LAUNCHD_LABEL}.plist").unlink(
        missing_ok=True
    )
    print(f"Removed {LAUNCHD_LABEL}")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default=os.environ.get("WLED_HOST", "192.168.5.147"))
    p.add_argument(
        "--port", type=int, default=int(os.environ.get("WLED_DDP_PORT", "4048"))
    )
    p.add_argument("--fps", type=float, default=12)
    p.add_argument("--poll-interval", type=float, default=0.5)
    p.add_argument(
        "--wiring", choices=("serpentine", "progressive"), default="serpentine"
    )
    p.add_argument(
        "--plane-axis",
        choices=("x", "y", "z"),
        default="y",
        help="cube axis perpendicular to each agent plane",
    )
    p.add_argument("--dry-run", action="store_true")
    sub = p.add_subparsers(dest="command")
    demo_p = sub.add_parser("demo", help="show all five animations")
    demo_p.add_argument("--seconds", type=float, default=15)
    sub.add_parser(
        "install-claude-hooks", help="merge lifecycle hooks into Claude settings"
    )
    sub.add_parser(
        "install", help="install hooks and start the macOS background service"
    )
    sub.add_parser("uninstall", help="remove the macOS background service")
    return p


def main() -> None:
    args = parser().parse_args()
    if args.command == "demo":
        demo(args)
    elif args.command == "install-claude-hooks":
        install_claude_hooks()
    elif args.command == "install":
        install_macos(args)
    elif args.command == "uninstall":
        uninstall_macos()
    else:
        daemon(args)


def hook_main() -> None:
    try:
        data = json.load(sys.stdin)
        record_hook(data)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"agent-cube-hook: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
