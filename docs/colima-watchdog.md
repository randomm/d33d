# Colima docker.sock watchdog

Colima on this Mac forwards `~/.colima/default/docker.sock` into the VM via an
`ssh -O forward` over Lima's ControlMaster. The forward can die silently while
`colima status` still reports the VM as running (the ControlMaster dies
post-boot and nothing re-establishes the forward — lima-vm/lima#5420). Every
d33d render then fails against a dead socket. The watchdog detects this and
restores the forward automatically, once a minute, with a full audit trail.

- Script: `scripts/colima-watchdog.sh` (POSIX sh, run by launchd)
- Launch agent: `scripts/com.d33d.colima-watchdog.plist` (60 s `StartInterval`)
- Log: `~/Library/Logs/d33d-colima-watchdog.log` (one timestamped line per
  event: check failed, action taken, result; a healthy tick writes nothing)
- Exit codes: `0` healthy or healed, `1` not healed (or skipped — another
  watchdog instance is already healing), `2` skipped because containers were
  running (the safety rule), `64` bad usage.

## Install

The plist ships with a `__HOME__` template (the home path is never
hard-coded), and the plist points the agent at
`__HOME__/Library/Scripts/colima-watchdog.sh` — so the install step copies the
script next to the agent and substitutes `__HOME__` in one place:

```sh
REPO=/path/to/d33d   # this repository

# 1. Copy the script to the location the plist expects
mkdir -p "$HOME/Library/Scripts"
cp "$REPO/scripts/colima-watchdog.sh" "$HOME/Library/Scripts/colima-watchdog.sh"
chmod +x "$HOME/Library/Scripts/colima-watchdog.sh"

# 2. Render the plist with the real home path and install it
mkdir -p "$HOME/Library/LaunchAgents"
sed "s|__HOME__|$HOME|g" "$REPO/scripts/com.d33d.colima-watchdog.plist" \
  > "$HOME/Library/LaunchAgents/com.d33d.colima-watchdog.plist"

# 3. Load the launch agent
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.d33d.colima-watchdog.plist"
```

`launchctl bootstrap gui/$UID …` is the modern API (the old
`launchctl load -w` still works but is deprecated). To verify it is loaded:

```sh
launchctl print "gui/$(id -u)/com.d33d.colima-watchdog"
```

The agent runs the script every 60 s (`StartInterval`) plus once at load
(`RunAtLoad`), so the watchdog is active immediately.

## Uninstall

```sh
launchctl bootout "gui/$(id -u)/com.d33d.colima-watchdog"
rm "$HOME/Library/LaunchAgents/com.d33d.colima-watchdog.plist"
rm "$HOME/Library/Scripts/colima-watchdog.sh"   # optional: keep for manual use
```

`launchctl bootout` is the modern equivalent of `launchctl unload`. The log
file is left in place.

## Manual use

The script can be run by hand; `--dry-run` logs the intended action
(re-forward, restart, or start) without executing it — useful for verifying
the script against a real (or stubbed) environment:

```sh
$HOME/Library/Scripts/colima-watchdog.sh --dry-run
```

The script is idempotent: overlapping ticks are fenced by a lock directory,
a healthy socket exits 0 without touching anything, and the heal paths are
safe to re-run.

## Version baseline and the lima#5420 finding

Recorded on the operator's Mac (2026-09-25):

- `colima version`: **colima 0.10.1** (commit `ed905203afdbc6fd4eae6cc301918099ff31e86e`,
  aarch64, docker runtime), docker client v29.4.1, docker server v29.2.1
- `limactl --version`: **limactl 2.1.1**

Upstream finding (checked once, 2026-09-25): **lima-vm/lima#5420** — "guest
socket forwards die with the SSH ControlMaster and are never re-created
(silent on vz)" — state **OPEN (unfixed)** as of that date. It exactly matches
the twice-in-two-days symptom this watchdog papered over: the VM stays
healthy, `colima status` says running, but `~/.colima/default/docker.sock`
has no listener because the ControlMaster died post-boot and Lima only
registers forwards at boot.

**Recommended upgrade:** none performed. Because lima#5420 is still open,
upgrading Lima/Colima is **not** expected to fix the forward-death at this
baseline; the watchdog (re-forward via the ControlMaster, falling back to
`colima stop && colima start`) is the working mitigation. Re-check the issue
before upgrading, and if a fixed Lima release ships, upgrade and then this
watchdog becomes a safety net rather than the primary fix. No Colima config
is changed by the watchdog or this install step.
