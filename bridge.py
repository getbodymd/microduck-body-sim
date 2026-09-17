"""The live body interface: connects to
wss://<host>/getbody/ws/bodies/<body_id>/interface as the owner agent and
speaks the protocol described on getbody.md's own homepage/llms.txt
("Starter prompt — lease your body to other agents", real-body section):
heartbeat, command -> command_result, read_feed -> feed_result, kill.

Run this on the same machine as the duck-sim daemon (see README.md) so it
can talk to robotd's own Unix socket directly — no SSH hop, no shelling out
to the robotctl binary. See duckipc.py for the wire protocol, ported by
reading pollen-robotics/microduck's actual source rather than guessed.

getbody "action" names (as declared in the Listing's permissions_offered)
map 1:1 onto robotd's real robot.* methods — see duckipc.py's module
docstring for the exact command/feed vocabulary (init/enable/relax/stop/
rebootMotors/do/look are request/response; move/head/pose are fire-and-
forget notifications, sent once per relayed getbody command). Use --dry-run
to exercise the getbody-side protocol (heartbeat/relay/reply) without
touching real hardware or even needing a robotd socket present.

Needs fleet_agent.json (from register_owner.py) in the same directory as
this script.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

import websockets

import duckipc
from getbody_client import FleetAgent

REPO_ROOT = Path(__file__).resolve().parent
FLEET_KEY_PATH = REPO_ROOT / "fleet_agent.json"
HEARTBEAT_INTERVAL_S = 2.0


def _ws_url(base_url: str, body_id: int) -> str:
    scheme = "wss" if base_url.startswith("https") else "ws"
    host = base_url.split("://", 1)[-1]
    return f"{scheme}://{host}/getbody/ws/bodies/{body_id}/interface"


# Discrete actions: request/response against robotd, call(method, params) built from
# getbody's relayed `params` dict, defaults filled in for anything the renter omits.
_DISCRETE_ACTIONS = {
    "init": lambda c, p: duckipc.robot_init(c),
    "enable": lambda c, p: duckipc.robot_enable(c, on=p.get("on", True), toggle=p.get("toggle", False)),
    "relax": lambda c, p: duckipc.robot_relax(c),
    "stop": lambda c, p: duckipc.robot_stop(c),
    "reboot_motors": lambda c, p: duckipc.robot_reboot_motors(c, ids=p.get("ids")),
    "do": lambda c, p: duckipc.robot_do(c, skill=p["skill"]),
    "look": lambda c, p: duckipc.robot_look(c, x=p["x"], y=p["y"], z=p["z"], neck_pitch=p.get("neck_pitch", 0.0)),
}

# Continuous actions: fire-and-forget notify, once per relayed getbody command. A renter
# driving continuously must keep sending these — getbody has no persistent streaming
# channel of its own, each relayed command is one invocation/reply round trip.
_CONTINUOUS_ACTIONS = {
    "move": lambda c, p: duckipc.robot_move(c, vx=p.get("vx", 0.0), vy=p.get("vy", 0.0), vyaw=p.get("vyaw", 0.0)),
    "head": lambda c, p: duckipc.robot_head(c, neck_pitch=p.get("neck_pitch", 0.0), head_pitch=p.get("head_pitch", 0.0),
                                             head_yaw=p.get("head_yaw", 0.0), head_roll=p.get("head_roll", 0.0)),
    "pose": lambda c, p: duckipc.robot_pose(c, z=p.get("z", 0.0), roll=p.get("roll", 0.0), pitch=p.get("pitch", 0.0),
                                             active=p.get("active", True)),
}

_FEEDS = {
    "health": lambda c: duckipc.robot_health(c),
    "battery": lambda c: duckipc.robot_health(c),  # same call; battery fields live inside HealthResult
}


class RobotdIpcBackend:
    """Speaks robotd's real IPC protocol directly (see duckipc.py) — one
    persistent connection, reused for every relayed command/feed-read."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.client = duckipc.RobotdClient(socket_path)
        self.client.hello()

    def _reconnect(self) -> None:
        self.client.close()
        self.client = duckipc.RobotdClient(self.socket_path)
        self.client.hello()

    def run_command(self, action: str, params: dict) -> dict:
        params = params or {}
        try:
            if action in _DISCRETE_ACTIONS:
                result = _DISCRETE_ACTIONS[action](self.client, params)
                return {"status": "ok", **(result or {})}
            if action in _CONTINUOUS_ACTIONS:
                _CONTINUOUS_ACTIONS[action](self.client, params)
                return {"status": "ok"}
            return {"status": "fault", "error": f"Unknown action {action!r}."}
        except duckipc.RobotdError as exc:
            return {"status": "fault", "error": str(exc)}
        except (OSError, ConnectionError, KeyError) as exc:
            self._reconnect()
            return {"status": "fault", "error": f"robotd connection error: {exc}"}

    def read_feed(self, feed: str) -> dict:
        if feed not in _FEEDS:
            return {"status": "fault", "error": f"Unknown feed {feed!r}."}
        try:
            return {"status": "ok", "value": _FEEDS[feed](self.client)}
        except duckipc.RobotdError as exc:
            return {"status": "fault", "error": str(exc)}
        except (OSError, ConnectionError) as exc:
            self._reconnect()
            return {"status": "fault", "error": f"robotd connection error: {exc}"}

    def kill(self) -> None:
        try:
            duckipc.robot_relax(self.client)
        except (duckipc.RobotdError, OSError, ConnectionError):
            pass


class DryRunBackend:
    """Exercises the getbody-side protocol without touching hardware —
    logs what it would have done and echoes canned results."""

    def run_command(self, action: str, params: dict) -> dict:
        print(f"[dry-run] would run command action={action!r} params={params!r}")
        return {"status": "ok", "output": f"dry-run: {action}"}

    def read_feed(self, feed: str) -> dict:
        print(f"[dry-run] would read feed={feed!r}")
        return {"status": "ok", "value": {"dry_run": True, "feed": feed}}

    def kill(self) -> None:
        print("[dry-run] would halt")


async def run_bridge(base_url: str, body_id: int, backend) -> None:
    agent = FleetAgent.load_or_create(FLEET_KEY_PATH)
    path = f"/getbody/ws/bodies/{body_id}/interface"
    url = _ws_url(base_url, body_id)

    while True:
        try:
            headers = agent.ws_headers(path)
            async with websockets.connect(url, additional_headers=headers) as ws:
                print(f"connected: body_id={body_id}")
                heartbeat_task = asyncio.create_task(_heartbeat_loop(ws))
                try:
                    async for raw in ws:
                        await _handle_frame(ws, raw, backend)
                finally:
                    heartbeat_task.cancel()
            print("connection closed; reconnecting in 3s...", file=sys.stderr)
        except (websockets.exceptions.ConnectionClosed, OSError) as exc:
            print(f"connection lost ({exc}); retrying in 3s...", file=sys.stderr)
        await asyncio.sleep(3)


async def _heartbeat_loop(ws) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)
        await ws.send(json.dumps({"type": "heartbeat"}))


async def _handle_frame(ws, raw: str, backend) -> None:
    try:
        frame = json.loads(raw)
    except json.JSONDecodeError:
        return
    frame_type = frame.get("type")

    if frame_type == "heartbeat_ack":
        return
    if frame_type == "status" or "status" in frame and "body_id" in frame:
        print(f"server: {frame}")
        return
    if frame_type == "kill":
        print("KILL received — halting.")
        await asyncio.get_running_loop().run_in_executor(None, backend.kill)
        return
    if frame_type == "command":
        # Off the event loop: a blocking robotd round trip here would also stall
        # _heartbeat_loop, risking a heartbeat-loss abort on a slow/hung call.
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, backend.run_command, frame.get("action"), frame.get("params") or {})
        await ws.send(json.dumps({
            "type": "command_result", "invocation_id": frame["invocation_id"], "result": result,
        }))
        return
    if frame_type == "read_feed":
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, backend.read_feed, frame.get("feed"))
        await ws.send(json.dumps({
            "type": "feed_result", "invocation_id": frame["invocation_id"], "result": result,
        }))
        return
    if "error" in frame:
        print(f"server error: {frame['error']}", file=sys.stderr)
        return
    print(f"unhandled frame: {frame}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("body_id", type=int)
    parser.add_argument("--base-url", default="https://getbody.md")
    parser.add_argument("--socket", dest="socket_path", default=duckipc.DEFAULT_SOCKET_PATH,
                         help="Path to robotd's Unix socket (default: /run/robotd.sock; duck-sim's "
                              "own wrapper script typically uses a custom path instead, e.g. "
                              "/root/.cache/duck-sim/duck-a.sock — check what it actually bound).")
    parser.add_argument("--dry-run", action="store_true",
                         help="Don't touch real hardware — just log and echo canned replies.")
    args = parser.parse_args()

    backend = DryRunBackend() if args.dry_run else RobotdIpcBackend(args.socket_path)
    asyncio.run(run_bridge(args.base_url, args.body_id, backend))


if __name__ == "__main__":
    main()
