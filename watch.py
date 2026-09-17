"""Live status view for your deployed ducks — local robotd health (polled
directly over each duck's Unix socket, via duckipc.py) side by side with
getbody-side registration status (owner agent approval, listing
visibility). Read-only: no leasing, no test commands sent to anything —
every call here is either a plain health read against robotd or a signed
GET against getbody.

Usage:
    python watch.py [--state-dir ~/.cache/duck-sim] [--interval 3] [--once]
"""
import argparse
import json
import sys
import time
from pathlib import Path

import duckipc
from getbody_client import FleetAgent, GetbodyClient, GetbodyError

REPO_ROOT = Path(__file__).resolve().parent
FLEET_KEY_PATH = REPO_ROOT / "fleet_agent.json"
STATE_PATH = REPO_ROOT / ".deployer-state.json"


def _load_deployer_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"ducks": {}}


def _duck_sockets(state_dir: Path, duck_names) -> dict:
    """name -> socket path, in the order register_owner.py registered them —
    matches duck-sim's own duck_name() (duck-a, duck-b, ...)."""
    letters = "abcdefghijklmnopqrstuvwxyz"
    return {name: str(state_dir / f"duck-{letters[i]}.sock") for i, name in enumerate(duck_names)}


def _poll_local(socket_path: str) -> dict:
    try:
        client = duckipc.RobotdClient(socket_path, timeout=3.0)
        client.hello()
        health = duckipc.robot_health(client)
        client.close()
        return health
    except (OSError, duckipc.RobotdError) as exc:
        return {"error": str(exc)}


def _poll_getbody(base_url: str):
    if not FLEET_KEY_PATH.exists():
        return None, None
    agent = FleetAgent.load_or_create(FLEET_KEY_PATH)
    client = GetbodyClient(agent, base_url=base_url)
    try:
        who = client.whoami()
    except GetbodyError as exc:
        who = {"error": str(exc)}
    try:
        listings = client.list_listings()
    except GetbodyError as exc:
        listings = {"error": str(exc)}
    return who, listings


def _format_local(health: dict) -> str:
    if "error" in health:
        return f"UNREACHABLE ({health['error']})"
    battery = health.get("battery", {})
    loop = health.get("control_loop", {})
    motors = health.get("motors", {})
    return (f"{'OK' if health.get('healthy') else 'UNHEALTHY'}  "
            f"battery={battery.get('percent', '?')}%  "
            f"loop={loop.get('achieved_hz', '?')}Hz missed={loop.get('missed', '?')}  "
            f"hottest_motor={motors.get('hottest', '?')}@{motors.get('max_c', '?')}C")


def print_report(deployer_state: dict, sockets: dict, base_url: str) -> None:
    print(f"\n=== {time.strftime('%H:%M:%S')} ===")

    who, listings = _poll_getbody(base_url)
    if who is None:
        print("  owner agent: not bootstrapped yet (run register_owner.py bootstrap-agent)")
    elif "error" in who:
        print(f"  owner agent: unreachable ({who['error']})")
    else:
        print(f"  owner agent: {who.get('agent_id', '?')}  approval_state={who.get('approval_state')}")

    live_listing_ids = {l["listing_id"] for l in listings} if isinstance(listings, list) else set()

    ducks = deployer_state.get("ducks", {})
    if not ducks:
        print("  no ducks registered yet (run register_owner.py register-duck <name>)")
        return

    for name, duck in ducks.items():
        sock = sockets.get(name)
        local_str = _format_local(_poll_local(sock)) if sock else "no socket path known for this duck"

        listing_id = duck.get("listing_id")
        if listing_id is None:
            listing_str = "not registered yet"
        elif listing_id in live_listing_ids:
            listing_str = f"listing {listing_id} LIVE (approved + active)"
        else:
            listing_str = f"listing {listing_id} pending / not yet visible"

        print(f"  {name}: body_id={duck.get('body_id')}  {listing_str}")
        print(f"    local: {local_str}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="https://getbody.md")
    parser.add_argument("--state-dir", default="~/.cache/duck-sim",
                         help="duck-sim's own state dir, where its sockets live (default matches "
                              "duck-sim's own default — override to match --state-dir/DUCK_SIM_STATE "
                              "if you changed it in deploy_local.py).")
    parser.add_argument("--interval", type=float, default=3.0, help="Seconds between polls.")
    parser.add_argument("--once", action="store_true", help="Poll once and exit instead of looping.")
    args = parser.parse_args()

    state_dir = Path(args.state_dir).expanduser()

    if args.once:
        deployer_state = _load_deployer_state()
        sockets = _duck_sockets(state_dir, deployer_state.get("ducks", {}))
        print_report(deployer_state, sockets, args.base_url)
        return

    try:
        while True:
            # Reloaded every tick so newly `register-duck`'d ducks show up
            # without restarting this.
            deployer_state = _load_deployer_state()
            sockets = _duck_sockets(state_dir, deployer_state.get("ducks", {}))
            print_report(deployer_state, sockets, args.base_url)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
