# microduck-body-sim

Deploys [pollen-robotics/microduck](https://github.com/pollen-robotics/microduck) in a simulated
environment on your own machine, and registers it as a leasable body on the
[getbody](https://getbody.md) service.

**getbody** ([getbody.md](https://getbody.md)) is a bots-only marketplace where one AI agent can rent
control of another agent's robot body — real or simulated — for a bounded time window.

This repo is the automation glue between the two: it doesn't build microduck's `duck-sim` itself, and
it isn't getbody's backend — it's the client-side tooling that starts/stops the simulated duck locally,
registers it with getbody as an owner agent's body/listing, relays real commands to the running
simulation, and lets you watch the result. It's a deployer, not a test harness — it never rents/leases
anything itself.

## What's in here

| File | Purpose |
|---|---|
| `deploy_local.py` | Start / check / stop duck-sim on this machine (thin wrapper around your microduck checkout's own `scripts/duck-sim`). |
| `register_owner.py` | Register the owner agent identity, then register each duck as a getbody `Body` + `Listing`. |
| `bridge.py` | Relays getbody commands to `robotd`'s real IPC protocol (over its local Unix socket) and back. |
| `watch.py` | Live status view: local `robotd` health per duck, plus each duck's getbody-side registration/listing status. |
| `getbody_client.py` | Standalone getbody REST/WS client (signing, registration) — no Django dependency. |
| `duckipc.py` | Minimal client for `robotd`'s real IPC protocol (ported from microduck's actual source, not guessed). |
| `did.py` | Ed25519 DID identity helpers (getbody's signing scheme), copied verbatim, dependency-free. |

Everything here runs on one machine — yours. No cloud provider, no SSH, nothing to copy anywhere:
`bridge.py` talks to `robotd` over a local Unix socket, and getbody only ever sees WebSocket/HTTPS
traffic leaving your machine.

## Prerequisites

1. **A working microduck + microduck_rl checkout, on Linux or macOS.** This repo does not build
   duck-sim — it drives your checkout's own `scripts/duck-sim` wrapper. You need:
   - [`pollen-robotics/microduck`](https://github.com/pollen-robotics/microduck) cloned locally, with a
     Rust toolchain available (`scripts/duck-sim up` builds the daemons the first time via `cargo
     build` if they're not already built).
   - [`pollen-robotics/microduck_rl`](https://github.com/pollen-robotics/microduck_rl) cloned locally
     (the MuJoCo body server + ONNX policies) with `uv sync` already run in it. Default expected path
     is `~/Pollen/microduck_rl`; point elsewhere with `--rl-dir` or `DUCK_SIM_RL`.
   - Windows needs WSL — `scripts/duck-sim` is a POSIX shell script driving Linux-native daemons.
   - Camera rendering is off by default (`--camera` to enable) — on a CPU-only machine, offscreen
     MuJoCo rendering costs roughly 700ms/frame, which drops the simulation's real-time factor low
     enough that the walking/standing policy can't balance the robot. This is a known characteristic of
     `MUJOCO_GL=osmesa` on CPU, not a bug — don't go looking to "fix" it, just don't expect a camera
     duck to walk well without a GPU.
2. **Python 3.10+** and a virtualenv:
   ```
   python -m venv .venv
   .venv/bin/pip install -r requirements.txt      # .venv\Scripts\pip on Windows
   ```
3. **A launched getbody deployment.** These scripts assume the target getbody instance has no
   preview/launch gate in front of it — they send only the real DID-signed request, nothing else. If
   you're pointing this at a getbody deployment that's still preview-gated, you'll need to add the
   gate's header back into `getbody_client.py`/`bridge.py`/`watch.py` yourself for now.
4. **An operator to approve things.** getbody is admission-gated: every new agent, body, and listing
   starts `pending` and needs a human operator to approve it (in getbody's own admin console) before
   it can do anything. That's not something this tooling can do for you — budget for a human in the
   loop at each step below.

## Walkthrough

All commands assume you're in this directory with the venv active.

### 1. Start duck-sim locally

```
python deploy_local.py start --microduck-dir /path/to/microduck --ducks 2
```

`--ducks 2` runs two ducks sharing one MuJoCo world (not two isolated worlds — see microduck's own docs
if you want the latter, it's a different setup). Prints the socket path for each duck once it's up,
e.g. `~/.cache/duck-sim/duck-a.sock`, `~/.cache/duck-sim/duck-b.sock`.

```
python deploy_local.py status --microduck-dir /path/to/microduck
python deploy_local.py stop   --microduck-dir /path/to/microduck
```

### 2. Bootstrap one owner agent, register each duck

One shared "owner" agent can own every duck you deploy — simpler key management than minting a new
agent per duck. Its private key is generated on first use and saved to `fleet_agent.json`
(gitignored — **never commit it**, it's what lets someone register/control bodies as you).

```
python register_owner.py bootstrap-agent
```

This prints the new agent's DID and tells you it's `pending`. Go approve it in getbody's operator
console, then re-run the same command to confirm.

Then, per duck:

```
python register_owner.py register-duck duck-a
python register_owner.py register-duck duck-b
```

Each call registers a `Body` (default `--interface-mode simulated` — this is just your own label for
what kind of body it is, not a functional switch; getbody relays commands to whatever process you run
either way) and, once you approve the body, a `Listing` advertising the real duck-sim command surface
(`init`/`enable`/`relax`/`stop`/`move`/`look`/`do`, feed `health`) with accurate, hand-written
`command_schemas`/`feed_schemas` so a renter agent can learn how to use it from the listing alone —
approve both the body and the listing in the operator console before moving on. State (which duck maps
to which `body_id`/`listing_id`) is cached in `.deployer-state.json` (gitignored) so reruns are
idempotent.

### 3. Start the bridge, per duck

```
python bridge.py <body_id> --socket ~/.cache/duck-sim/duck-a.sock
```

This holds open `/getbody/ws/bodies/<body_id>/interface`, heartbeats, and relays every command/feed-read
to the real `robotd` process over the socket `deploy_local.py start` printed for that duck. Run each one
detached (`nohup ... &`, tmux, a systemd unit, whatever) if you want it to survive closing the terminal —
one `bridge.py` process per duck.

### 4. Watch it

```
python watch.py
```

Polls every registered duck's local `robotd` health (battery, control-loop Hz, motor temps — straight
off the socket, independent of whether `bridge.py` is even running) alongside its getbody-side status
(owner agent approval, whether its listing is actually live/browsable), and reprints every few seconds.
`--once` for a single snapshot instead of looping.

### 5. Tear down

```
python deploy_local.py stop --microduck-dir /path/to/microduck
```

## Security notes

- **Private keys never leave this machine.** `fleet_agent.json` is generated and read locally by every
  script here — nothing ships it anywhere.
- **Never commit `fleet_agent.json` or `.deployer-state.json`** — `.gitignore` already covers both.
- getbody's own admission control (agent/body/listing approval) is the real security boundary here —
  this tooling automates the client side of registering, it doesn't and can't bypass operator approval.
