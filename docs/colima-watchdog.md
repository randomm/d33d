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
  running (or the container probe failed — conservative; the safety rule),
  `64` usage/environment error (unknown flag, non-numeric env override).
  Any other non-zero code means the script aborted mid-run before reaching
  a decision.

The plist captures no stdout/stderr: the log file above is the single audit
surface. Launchd's own captures would just duplicate (or silently lose) what
the script already logs.

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

## Troubleshooting

- **Every tick logs "another watchdog instance is active; skipping" (exit 1).**
  A prior run was killed hard (SIGKILL, power loss) and left its lock
  directory behind. The watchdog reclaims a lock whose recorded holder pid is
  no longer alive automatically; if it cannot (e.g. pid reuse, or a lock
  directory that predates the pid-file scheme), remove it manually:
  `rmdir "${TMPDIR:-/tmp}/d33d-colima-watchdog.lock"`.
- **A `colima stop` or `colima start` failure during a heal** is logged with
  its exit status (`colima stop failed (rc=N)`) and the tick ends as
  "not healed" (exit 1) — the log line naming the failing step is the
  diagnostic; the next tick retries.

## Manual use

The script can be run by hand; `--dry-run` logs the intended heal path
(re-forward, restart, or start) without executing it — the post-heal
verification loop is skipped in dry-run, so it exits 0 after recording the
intended action, matching what a real run would attempt. Useful for verifying
the script against a real (or stubbed) environment:

```sh
$HOME/Library/Scripts/colima-watchdog.sh --dry-run
```

The script is idempotent: overlapping ticks are fenced by a lock directory
(with stale-lock recovery), a healthy socket exits 0 without touching
anything, and the heal paths are safe to re-run.

## Version baseline and the lima#5420 finding

Recorded on the operator's Mac (2026-09-25), each line below is the actual
command output (re-verified 2026-09-26):

- `colima version`:

  ```
  colima version 0.10.1
  git commit: ed905203afdbc6fd4eae6cc301918099ff31e86e

  runtime: docker
  arch: aarch64
  client: v29.4.1
  server: v29.2.1
  ```

- `limactl --version`: **limactl version 2.1.1**

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
