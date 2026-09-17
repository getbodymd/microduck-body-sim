"""Minimal Python client for robotd's real IPC protocol — ported by reading
pollen-robotics/microduck's duck-ipc-proto and robotd source directly
(Documents/github/microduck on this machine), NOT guessed. Confirmed from
source, not documentation:

- Transport: newline-delimited JSON over a Unix domain socket at
  /run/robotd.sock (duck_ipc_proto::socket::ROBOT), one JSON value per line
  (robotd/src/main.rs: `BufReader::new(read_half).lines()` on the read side;
  `serde_json::to_vec` + push(b'\n') + write_all + flush on the write side).
- Envelope: plain JSON-RPC 2.0 (`{"jsonrpc":"2.0","id":..,"method":..,
  "params":..}`). A request with `id` gets exactly one matching Response
  line back; a notification (`id` omitted) gets none — that's how
  continuous, high-rate intents (move/head/pose/mouth) avoid a reply per
  message (duck_ipc_proto's own doc comment on `Request::notify`).
- Every params struct is `#[serde(deny_unknown_fields)]` — extra keys are a
  hard error, not ignored, so ONLY send the fields listed here.

Method/param reference actually used by getbody_bridge.py (there are many
more methods in duck-ipc-proto for updates/net/system/pad — out of scope
here, this fleet only needs the robot.* control surface):

  DISCRETE (send as a request, i.e. WITH an id, and wait for the matching
  Response — robotctl's own CLI subcommands all do this, e.g. `robotctl
  robot init`):
    robot.init            {}                              -> {}
    robot.enable          {"on": bool, "toggle": bool}     -> {}
    robot.relax           {}                              -> {}
    robot.stop            {}                               -> {}
    robot.rebootMotors    {"ids": [u8, ...]}  (empty = all) -> {}
    robot.do              {"skill": "<name>"}              -> {}
      (skill names are config, e.g. "roulade", "kick_left", "kick_right",
      "ground_pick", "sit_toggle" — `robotctl monitor` lists what a given
      robot actually has; an unknown name is refused, named.)
    robot.look            {"x":f64,"y":f64,"z":f64,"neck_pitch":f64}
                             -> {"head": {...HeadParams}, "clamped": bool}
    robot.health          {}   -> {"healthy":bool,"degraded":bool,
                                    "reason":str|null, ...battery/bus fields}

  CONTINUOUS (send as a NOTIFICATION, i.e. no id, no reply expected —
  last-writer-wins, meant to be sent repeatedly at 20-50Hz by a real
  joystick client; getbody's own relay is one-command-per-invocation, so
  the bridge sends exactly one of these per relayed getbody command, and a
  renter that wants to keep moving must keep sending "move" commands):
    robot.move   {"vx": f64, "vy": f64, "vyaw": f64}   (m/s, m/s, rad/s)
    robot.head   {"neck_pitch":f64,"head_pitch":f64,"head_yaw":f64,"head_roll":f64}
    robot.pose   {"z":f64,"roll":f64,"pitch":f64,"active":bool}

Not implemented here (exists in duck-ipc-proto but out of scope for the
getbody bridge today): robot.mouth, robot.sound, robot.theremin,
robot.chorale, and the subscription-based streams (robot state, ToF, head
IMU) — those are continuous *server-to-client* pushes, which doesn't fit
getbody's one-shot read_feed model without a redesign.
"""
import json
import socket as _socket
import threading

DEFAULT_SOCKET_PATH = "/run/robotd.sock"
API_VERSION = 31  # duck_ipc_proto::API_VERSION as of this writing — bumps on incompatible changes; re-check if robotd refuses hello.
JSONRPC_VERSION = "2.0"


class RobotdError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(f"robotd error {code}: {message}")
        self.code = code
        self.message = message


class RobotdClient:
    """One connection to robotd. Not safe for concurrent use from multiple
    threads without external locking — the read loop and any caller share
    one socket and one _next_id counter."""

    def __init__(self, socket_path: str = DEFAULT_SOCKET_PATH, timeout: float = 5.0):
        self.sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(socket_path)
        self._buf = b""
        self._lock = threading.Lock()
        self._next_id = 1

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _read_line(self) -> dict:
        while b"\n" not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("robotd closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))

    def _write_line(self, obj: dict) -> None:
        self.sock.sendall(json.dumps(obj).encode("utf-8") + b"\n")

    def hello(self) -> dict:
        return self.call("hello", {"api_version": API_VERSION})

    def call(self, method: str, params: dict) -> dict:
        """Discrete request/response. Raises RobotdError on a JSON-RPC
        error; returns the `result` object (possibly {}) on success."""
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self._write_line({
                "jsonrpc": JSONRPC_VERSION, "id": request_id, "method": method, "params": params,
            })
            response = self._read_line()
            if response.get("id") != request_id:
                raise RobotdError("protocol", f"reply id {response.get('id')!r} != request id {request_id}")
            if "error" in response and response["error"] is not None:
                err = response["error"]
                raise RobotdError(err.get("code"), err.get("message"))
            return response.get("result") or {}

    def notify(self, method: str, params: dict) -> None:
        """Continuous, fire-and-forget — no id, no reply."""
        with self._lock:
            self._write_line({"jsonrpc": JSONRPC_VERSION, "method": method, "params": params})


# -- robot.* convenience wrappers, param shapes exactly as duck-ipc-proto declares them ----

def robot_init(client: RobotdClient) -> dict:
    return client.call("robot.init", {})


def robot_enable(client: RobotdClient, on: bool = True, toggle: bool = False) -> dict:
    return client.call("robot.enable", {"on": on, "toggle": toggle})


def robot_relax(client: RobotdClient) -> dict:
    return client.call("robot.relax", {})


def robot_stop(client: RobotdClient) -> dict:
    return client.call("robot.stop", {})


def robot_reboot_motors(client: RobotdClient, ids=None) -> dict:
    return client.call("robot.rebootMotors", {"ids": ids or []})


def robot_do(client: RobotdClient, skill: str) -> dict:
    return client.call("robot.do", {"skill": skill})


def robot_look(client: RobotdClient, x: float, y: float, z: float, neck_pitch: float = 0.0) -> dict:
    return client.call("robot.look", {"x": x, "y": y, "z": z, "neck_pitch": neck_pitch})


def robot_health(client: RobotdClient) -> dict:
    return client.call("robot.health", {})


def robot_move(client: RobotdClient, vx: float = 0.0, vy: float = 0.0, vyaw: float = 0.0) -> None:
    client.notify("robot.move", {"vx": vx, "vy": vy, "vyaw": vyaw})


def robot_head(client: RobotdClient, neck_pitch: float, head_pitch: float, head_yaw: float, head_roll: float) -> None:
    client.notify("robot.head", {
        "neck_pitch": neck_pitch, "head_pitch": head_pitch, "head_yaw": head_yaw, "head_roll": head_roll,
    })


def robot_pose(client: RobotdClient, z: float = 0.0, roll: float = 0.0, pitch: float = 0.0, active: bool = True) -> None:
    client.notify("robot.pose", {"z": z, "roll": roll, "pitch": pitch, "active": active})
