# LED Cube Agent Monitor

A tiny macOS daemon that shows live Codex and Claude session states on a
5×5×5 WLED cube. Five vertical planes display five iTerm panes in visual
left-to-right order. WLED is driven directly over DDP; Home Assistant is not
required.


Implemented in a single file [`agent_cube.py`](agent_cube.py) for macOS, using only the Python standard library.

<p align="center">
<img width="48.6%" alt="5x5x5 LED cube from amazon" src="https://github.com/user-attachments/assets/5cabe26f-8503-43a5-ac2c-2abbf0bf5019" /><img width="50%" alt="5x5x5 LED cube from amazon" src="https://github.com/user-attachments/assets/9fa4e2d8-41ab-4dd3-88cb-a9b23a3763dd" /><br/>
<small>Made for this LED cube, but works with any: <a href="https://www.amazon.com/dp/B0CGCH393Z"><code>Treedix WS2812B 5050 RGB LED Light Board Led Cube Light DIY Kit Squared LED Digital Individually Addressable 5X5X5</code></a></small>
</p>

## One-line macOS install

```bash
pip install git+https://github.com/pirate/led-cube-agent-monitor
led-cube-agent-monitor --host <ip address of wled device> install
```

This installs the package, then performs the one-time macOS setup: it merges Claude hooks,
creates `~/Library/LaunchAgents/com.pirate.led-cube-agent-monitor.plist`, starts
the background service that updates the cube animation periodically. Codex detection needs no hooks.


## Animations

| State | Plane animation |
|---|---|
| thinking / working | blue traveling plasma |
| streaming tokens | cyan fill from bottom to top |
| waiting on tool | amber perimeter spinner |
| sending request | purple swoosh |
| idle for input | green cursor blink for 10s, then an asymptotic fade (10% at 10m, 1% at 1h) |
| error / attention | red flash |

Escape/cancel and normal completion latch a session idle. Only a newer prompt
in that exact session can reactivate it.

## Cube layout

The defaults match the original cube:

- 125 WS2812 pixels: five 5×5 PCBs
- WLED at `192.168.5.147`, DDP UDP port `4048`
- serpentine rows
- vertical agent planes on the Y axis
- physical slice order mirrored so the first iTerm pane is on the left

Slice order follows iTerm window order, tab order, then split panes from
top/left to bottom/right. Internal subagents without a visible pane do not use
extra slices. If more than five agent panes exist, the first five are shown.

Override defaults before the command name:

```bash
led-cube-agent-monitor \
  --host 192.168.5.147 \
  --port 4048 \
  --wiring serpentine \
  --plane-axis y \
  install
```

Environment alternatives are `WLED_HOST`, `WLED_DDP_PORT`, and
`AGENT_CUBE_STATE_DIR`.

## Test and inspect

```bash
led-cube-agent-monitor --dry-run
led-cube-agent-monitor demo
tail -f ~/Library/Logs/led-cube-agent-monitor.log
tail -f ~/Library/Logs/led-cube-agent-monitor.error.log
```

## Remove

```bash
led-cube-agent-monitor uninstall
pip uninstall led-cube-agent-monitor
```

The legacy `agent-cube` and `agent-cube-hook` executable names remain available
as compatibility aliases.
