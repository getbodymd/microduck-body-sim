"""One-time owner-agent bootstrap + per-duck body/listing registration
against getbody.md — see README.md for the full walkthrough. State (the
owner's keypair, agent_id, and each duck's body_id/listing_id) persists in
fleet_agent.json / .deployer-state.json next to this script — both
gitignored, never commit either one.

Usage:
    python register_owner.py bootstrap-agent
    python register_owner.py register-duck duck-1
    python register_owner.py status
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from getbody_client import FleetAgent, GetbodyClient, GetbodyError

REPO_ROOT = Path(__file__).resolve().parent
FLEET_KEY_PATH = REPO_ROOT / "fleet_agent.json"
STATE_PATH = REPO_ROOT / ".deployer-state.json"

# Documentation-only (getbody/command_schemas.py) — shown to renters, matches
# duck-ipc-proto's real param shapes exactly (see duckipc.py's module docstring).
DEFAULT_COMMAND_SCHEMAS = {
    "init": {"description": "Power the joints and ramp to the home pose (no policy needed)."},
    "enable": {
        "description": "Hand the robot to its walking policy, or take it back.",
        "params": {
            "on": {"type": "boolean", "description": "true = enable, false = disable."},
            "toggle": {"type": "boolean", "description": "Flip current state instead of using 'on'."},
        },
    },
    "relax": {"description": "Cut power to the joints. The robot collapses if nothing holds it."},
    "stop": {"description": "Zero the velocity and hold standing (not the same as relax)."},
    "move": {
        "description": "Continuous velocity twist — send repeatedly to keep driving.",
        "params": {
            "vx": {"type": "number", "description": "Forward, m/s."},
            "vy": {"type": "number", "description": "Left, m/s."},
            "vyaw": {"type": "number", "description": "Yaw rate, rad/s, positive turns left."},
        },
    },
    "look": {
        "description": "Point the camera at a trunk-frame point (metres): x forward, y left, z up.",
        "params": {
            "x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number"},
            "neck_pitch": {"type": "number", "description": "Neck posture to aim around, radians."},
        },
        "returns": {
            "head": {"type": "object", "description": "Resulting head joint targets."},
            "clamped": {"type": "boolean", "description": "true if the point was beyond the head's reach."},
        },
    },
    "do": {
        "description": "Run a one-shot skill by name (e.g. 'roulade', 'kick_left', 'sit_toggle') — config-driven per robot.",
        "params": {"skill": {"type": "string", "required": True}},
    },
}
DEFAULT_FEED_SCHEMAS = {
    "health": {
        "description": "Robot health snapshot — battery, motor bus, whether it's currently healthy.",
        "returns": {
            "healthy": {"type": "boolean"},
            "degraded": {"type": "boolean"},
            "reason": {"type": "string", "description": "Set when unhealthy or degraded."},
        },
    },
}


def _load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"ducks": {}}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


def cmd_bootstrap_agent(args):
    agent = FleetAgent.load_or_create(FLEET_KEY_PATH)
    client = GetbodyClient(agent, base_url=args.base_url)

    state = _load_state()
    if state.get("agent_registered"):
        print(f"Owner agent {agent.agent_id} already registered (per local state). "
              f"Checking admission status...")
    else:
        result = client.register_agent(intent=args.intent, notify_method=args.notify_method,
                                         notify_target=args.notify_target or "")
        print(f"Registered owner agent {agent.agent_id}: {result}")
        verify_result = client.complete_challenge_response()
        print(f"Challenge/verify complete: {verify_result}")
        state["agent_registered"] = True
        _save_state(state)

    who = client.whoami()
    print(f"whoami: {who}")
    if who.get("approval_state") != "approved":
        print("\nThis agent is PENDING operator approval. It cannot register bodies or\n"
              "listings until an operator approves it — open the GetBody Admin console\n"
              f"and approve agent {agent.agent_id}, then re-run this command (or\n"
              "'status') to confirm before registering ducks.")
        sys.exit(1)
    print("\nOwner agent is approved. You can now run 'register-duck <name>'.")


def cmd_register_duck(args):
    agent = FleetAgent.load_or_create(FLEET_KEY_PATH)
    client = GetbodyClient(agent, base_url=args.base_url)
    state = _load_state()
    duck = state["ducks"].setdefault(args.name, {})

    if "body_id" not in duck:
        fail_safe = {
            "mode": "halt",
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
        manifest = {"kind": "microduck", "sim": True, "control_modes": ["supervisory"]}
        body_result = client.create_body(
            declared_value=args.declared_value, manifest=manifest, fail_safe=fail_safe,
            interface_mode=args.interface_mode,
            location={"host": args.name},
        )
        duck["body_id"] = body_result["body_id"]
        print(f"Created body {duck['body_id']} for {args.name}: {body_result}")
        _save_state(state)
    else:
        print(f"{args.name} already has body_id={duck['body_id']} (per local state).")

    if "listing_id" not in duck:
        # Matches duckipc.py's verified robot.* command set exactly (ported from
        # pollen-robotics/microduck's actual source, not guessed) — see bridge.py.
        permissions_offered = args.permissions or ["init", "enable", "relax", "stop", "move", "look", "do"]
        offered_feeds = args.feeds or ["health"]
        listing_result = client.create_listing(
            body_id=duck["body_id"],
            permissions_offered=permissions_offered,
            offered_feeds=offered_feeds,
            command_schemas={k: v for k, v in DEFAULT_COMMAND_SCHEMAS.items() if k in permissions_offered},
            feed_schemas={k: v for k, v in DEFAULT_FEED_SCHEMAS.items() if k in offered_feeds},
        )
        duck["listing_id"] = listing_result["listing_id"]
        print(f"Created listing {duck['listing_id']} for {args.name}: {listing_result}")
        _save_state(state)
    else:
        print(f"{args.name} already has listing_id={duck['listing_id']} (per local state).")

    print(f"\nBody {duck['body_id']} and listing {duck['listing_id']} are PENDING operator\n"
          "approval — approve both in the GetBody Admin console before this duck is\n"
          "leasable. Once approved, start the bridge:\n"
          f"    python bridge.py {duck['body_id']}")


def cmd_status(args):
    state = _load_state()
    if not FLEET_KEY_PATH.exists():
        print("No owner agent bootstrapped yet — run 'bootstrap-agent' first.")
        return
    agent = FleetAgent.load_or_create(FLEET_KEY_PATH)
    print(f"Owner agent: {agent.agent_id}")
    print(json.dumps(state, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="https://getbody.md")
    sub = parser.add_subparsers(dest="command", required=True)

    p_bootstrap = sub.add_parser("bootstrap-agent")
    p_bootstrap.add_argument("--intent", default="Operates a fleet of duck-sim microduck simulators.")
    p_bootstrap.add_argument("--notify-method", dest="notify_method", default="none", choices=["none", "webhook", "email"])
    p_bootstrap.add_argument("--notify-target", dest="notify_target", default="")
    p_bootstrap.set_defaults(func=cmd_bootstrap_agent)

    p_register = sub.add_parser("register-duck")
    p_register.add_argument("name")
    p_register.add_argument("--declared-value", default="500.00", dest="declared_value")
    p_register.add_argument("--interface-mode", default="simulated", choices=["simulated", "real"], dest="interface_mode")
    p_register.add_argument("--permissions", nargs="*")
    p_register.add_argument("--feeds", nargs="*")
    p_register.set_defaults(func=cmd_register_duck)

    p_status = sub.add_parser("status")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    try:
        args.func(args)
    except GetbodyError as exc:
        print(f"getbody error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
