"""Starts/stops/checks duck-sim on THIS machine, by driving your own
pollen-robotics/microduck checkout's `scripts/duck-sim` wrapper directly —
this repo doesn't build or vendor duck-sim itself, see README.md. Confirmed
against that script's actual source (its `up`/`down`/`status`/`ctl`
subcommands, `DUCK_SIM_DUCKS`/`DUCK_SIM_CAMERAS`/`DUCK_SIM_RL`/
`DUCK_SIM_STATE` env vars), not guessed.

Needs, in your microduck checkout:
  - the Rust daemons buildable (`cargo build` — done automatically by `up`
    the first time, but needs a Rust toolchain present),
  - a `microduck_rl` checkout with `uv sync` already run (the MuJoCo body
    server + ONNX policies) — default path `~/Pollen/microduck_rl`, override
    with --rl-dir or DUCK_SIM_RL,
  - Linux or macOS. Windows needs WSL — this is a POSIX shell script driving
    Linux-native daemons.

Sockets land under ~/.cache/duck-sim/ by default (override with
--state-dir/DUCK_SIM_STATE): duck-a.sock, duck-a-tof.sock, duck-b.sock, ...
one pair per duck. Point bridge.py --socket at one of them.

Usage:
    python deploy_local.py start --microduck-dir <path> [--ducks N] [--camera]
    python deploy_local.py status --microduck-dir <path>
    python deploy_local.py stop --microduck-dir <path>
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path


def _resolve_microduck_dir(args) -> Path:
    raw = args.microduck_dir or os.environ.get("DUCK_SIM_MICRODUCK_DIR")
    if not raw:
        print("Pass --microduck-dir or set DUCK_SIM_MICRODUCK_DIR to your pollen-robotics/microduck checkout.",
              file=sys.stderr)
        sys.exit(1)
    path = Path(raw).expanduser().resolve()
    script = path / "scripts" / "duck-sim"
    if not script.exists():
        print(f"{script} not found — is --microduck-dir pointing at the right checkout?", file=sys.stderr)
        sys.exit(1)
    return path


def _run(microduck_dir: Path, *duck_sim_args, env_extra=None) -> int:
    env = os.environ.copy()
    if env_extra:
        env.update({k: v for k, v in env_extra.items() if v is not None})
    result = subprocess.run(["scripts/duck-sim", *duck_sim_args], cwd=microduck_dir, env=env)
    return result.returncode


def cmd_start(args):
    microduck_dir = _resolve_microduck_dir(args)
    env_extra = {
        "DUCK_SIM_DUCKS": str(args.ducks) if args.ducks and args.ducks != 1 else None,
        "DUCK_SIM_CAMERAS": "1" if args.camera else None,
        "DUCK_SIM_RL": args.rl_dir,
        "DUCK_SIM_STATE": args.state_dir,
    }
    rc = _run(microduck_dir, "up", env_extra=env_extra)
    if rc != 0:
        sys.exit(rc)
    letters = "abcdefghijklmnopqrstuvwxyz"
    state_dir = args.state_dir or "~/.cache/duck-sim"
    print(f"\nStarted {args.ducks} duck(s). Sockets under {state_dir}/:")
    for i in range(args.ducks):
        print(f"  duck-{letters[i]}.sock  (+ duck-{letters[i]}-tof.sock)")
    print("\nPoint bridge.py --socket at one of those, e.g.:")
    print(f"    python bridge.py <body_id> --socket {state_dir}/duck-a.sock")


def cmd_status(args):
    microduck_dir = _resolve_microduck_dir(args)
    sys.exit(_run(microduck_dir, "status", env_extra={"DUCK_SIM_STATE": args.state_dir}))


def cmd_stop(args):
    microduck_dir = _resolve_microduck_dir(args)
    sys.exit(_run(microduck_dir, "down", env_extra={"DUCK_SIM_STATE": args.state_dir}))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--microduck-dir", default=None, help="Or set DUCK_SIM_MICRODUCK_DIR.")
    parser.add_argument("--state-dir", default=None, help="Default: ~/.cache/duck-sim (duck-sim's own default).")
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start")
    p_start.add_argument("--ducks", type=int, default=1, help="How many ducks, sharing one MuJoCo world.")
    p_start.add_argument("--camera", action="store_true",
                          help="Enable camera rendering. Off by default — on CPU-only machines "
                               "offscreen rendering costs ~700ms/frame and a walking/standing "
                               "policy cannot balance the robot at the resulting real-time factor. "
                               "This is a known, already-diagnosed characteristic of MUJOCO_GL=osmesa "
                               "on CPU, not a bug to 'fix'.")
    p_start.add_argument("--rl-dir", default=None, help="microduck_rl checkout. Default: ~/Pollen/microduck_rl or DUCK_SIM_RL.")
    p_start.set_defaults(func=cmd_start)

    p_status = sub.add_parser("status")
    p_status.set_defaults(func=cmd_status)

    p_stop = sub.add_parser("stop")
    p_stop.set_defaults(func=cmd_stop)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
