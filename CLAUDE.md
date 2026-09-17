# Working in this repo

This is `microduck-body-sim`: automation for running [pollen-robotics/microduck](https://github.com/pollen-robotics/microduck)
`duck-sim` instances on the user's own machine and registering them as leasable bodies on
[getbody](https://getbody.md) — see README.md for the full picture. It's client-side tooling only: it
doesn't build duck-sim and it isn't getbody's backend. Everything runs locally — no cloud provider, no
SSH, nothing to copy anywhere.

## On starting a session here

If the user hasn't said what they want yet, a reasonable first offer is: "want me to walk you through
starting a duck and registering it with getbody?"

## What you need from the user before you can do anything live

1. **A working `pollen-robotics/microduck` checkout**, with a Rust toolchain available. This repo does
   not build duck-sim itself.
2. **A working `pollen-robotics/microduck_rl` checkout** with `uv sync` already run in it (default
   expected path `~/Pollen/microduck_rl`, override with `--rl-dir`/`DUCK_SIM_RL`). If the user doesn't
   have either checkout yet, that's a separate setup step — point them at microduck's own docs rather
   than attempting to improvise cloning/building it here.
3. **Linux or macOS.** On Windows, this needs WSL — `scripts/duck-sim` is a POSIX shell script driving
   Linux-native daemons; don't try to run it directly from PowerShell/cmd.
4. **A launched (non-preview-gated) getbody deployment to target.** These scripts send no preview-gate
   header at all — see getbody_client.py's module docstring. If the getbody instance the user wants to
   use is still preview-gated, that header needs adding back into getbody_client.py/bridge.py/watch.py
   before anything here will get past a 404; don't silently hardcode a specific key if you do that,
   still take it as a runtime argument or env var.
5. **Someone able to approve things in that getbody deployment's operator console.** Every agent, body,
   and listing this tooling registers starts `pending` — you cannot approve your own registrations from
   here, that needs a human with operator access on the getbody side.

## The workflow (see README.md for full command syntax)

1. `deploy_local.py start --microduck-dir <path> --ducks N` — starts N ducks sharing one MuJoCo world on
   this machine, via the checkout's own `scripts/duck-sim up` (this repo doesn't reimplement that
   script — it's a thin wrapper, see deploy_local.py's module docstring for which env vars it sets).
   Prints each duck's socket path (`~/.cache/duck-sim/duck-a.sock`, `duck-b.sock`, ...).
2. `register_owner.py bootstrap-agent` then `register-duck <name>` per duck — registers the owner
   agent, then each duck's `Body` + `Listing`. Tell the user what needs approving after each step; don't
   assume it happened.
3. `bridge.py <body_id> --socket <path from step 1>` — one process per duck, run detached (tmux, a
   systemd unit, `nohup ... &`) if it should outlive the current terminal session.
4. `watch.py` — live local + getbody-side status, so the user can see it's actually working without
   needing to lease anything themselves. This repo never leases/rents — that's a real renter agent's
   job, not this tooling's.
5. `deploy_local.py stop --microduck-dir <path>` when done.

## Rules specific to this repo

- **Never add a preview-key parameter back** to `getbody_client.py`/`bridge.py`/`watch.py` "just in
  case" — this repo is scoped to launched getbody deployments on purpose (see README's Prerequisites
  §4). If a launched deployment is ever preview-gated again, that's a deliberate, discussed change, not
  a silent one.
- **Never commit `fleet_agent.json` or `.deployer-state.json`** — both are gitignored already; don't
  work around that.
- **Don't add a renter/test-lease script back.** This repo is a deployer + a read-only watcher, not a
  test harness — if the user wants to exercise leasing, that's a separate tool (their own renter agent),
  not something to bolt onto this repo.
- **Don't reintroduce cloud/SSH deployment.** This repo deliberately targets the user's own machine
  only — no DigitalOcean, no droplet IDs, no SSH copying. If a future request wants remote deployment
  back, that's a deliberate, discussed change (and should probably be its own script alongside
  `deploy_local.py`, not a replacement for it).
- `bridge.py`'s command/feed mapping (`_DISCRETE_ACTIONS`/`_CONTINUOUS_ACTIONS` in that file) is ported
  from microduck's actual `duck-ipc-proto` source (see `duckipc.py`'s module docstring), not guessed —
  if you extend it, verify against the real source rather than inventing plausible-looking commands.
- `deploy_local.py`'s env vars/subcommands (`up`/`down`/`status`/`DUCK_SIM_DUCKS`/`DUCK_SIM_CAMERAS`/
  `DUCK_SIM_RL`/`DUCK_SIM_STATE`) were confirmed against `scripts/duck-sim`'s actual source, not
  guessed — if microduck's script changes shape, re-check it rather than assuming this wrapper still
  matches.
