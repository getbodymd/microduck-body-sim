"""Standalone getbody client — no Django dependency, just `requests` +
`websockets` + `cryptography` (via did.py). Ports the signing scheme from
getbody's own authentication.py/ws_auth.py: every REST call carries
X-Getbody-Agent-Id/Nonce/Expiry/Signature headers over a canonical
"{method}\\n{path}\\n{nonce}\\n{expiry}\\n{sha256(body)}" string; every WS
connect signs the same shape with method replaced by the literal
"WS-CONNECT".

Assumes a launched getbody deployment (no preview-gate header) — every
request here is just the real DID-signed request, nothing else.

One shared "fleet" agent identity can own every body you deploy (see
README.md) — its keypair lives only on this machine (`fleet_agent.json`,
gitignored — NEVER commit it, it's the private key that controls your
bodies).
"""
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

import requests

import did

DEFAULT_BASE_URL = "https://getbody.md"
API_PREFIX = "/api"


def _utcnow():
    return datetime.now(dt_timezone.utc)


class GetbodyError(RuntimeError):
    pass


@dataclass
class FleetAgent:
    agent_id: str
    public_key_hex: str
    private_key_hex: str

    @classmethod
    def load_or_create(cls, path: Path) -> "FleetAgent":
        if path.exists():
            data = json.loads(path.read_text())
            return cls(agent_id=data["agent_id"], public_key_hex=data["public_key_hex"],
                       private_key_hex=data["private_key_hex"])
        public_key_hex, private_key_hex = did.generate_keypair()
        agent_id = did.did_from_public_key_hex(public_key_hex)
        agent = cls(agent_id=agent_id, public_key_hex=public_key_hex, private_key_hex=private_key_hex)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "agent_id": agent.agent_id,
            "public_key_hex": agent.public_key_hex,
            "private_key_hex": agent.private_key_hex,
        }, indent=2))
        return agent

    def _sign(self, canonical: bytes) -> str:
        return did.sign(self.private_key_hex, canonical)

    def rest_headers(self, method: str, path: str, body: bytes) -> dict:
        nonce = secrets.token_hex(8)
        expiry_iso = (_utcnow() + timedelta(minutes=5)).isoformat()
        body_hash = hashlib.sha256(body).hexdigest()
        canonical = f"{method}\n{path}\n{nonce}\n{expiry_iso}\n{body_hash}".encode()
        return {
            "X-Getbody-Agent-Id": self.agent_id,
            "X-Getbody-Nonce": nonce,
            "X-Getbody-Expiry": expiry_iso,
            "X-Getbody-Signature": self._sign(canonical),
            "Content-Type": "application/json",
        }

    def ws_headers(self, path: str) -> dict:
        nonce = secrets.token_hex(8)
        expiry_iso = (_utcnow() + timedelta(minutes=5)).isoformat()
        body_hash = hashlib.sha256(b"").hexdigest()
        canonical = f"WS-CONNECT\n{path}\n{nonce}\n{expiry_iso}\n{body_hash}".encode()
        return {
            "X-Getbody-Agent-Id": self.agent_id,
            "X-Getbody-Nonce": nonce,
            "X-Getbody-Expiry": expiry_iso,
            "X-Getbody-Signature": self._sign(canonical),
        }


class GetbodyClient:
    """REST-only surface: registration, admission polling, body/listing
    creation. The live interface WS is handled separately by
    getbody_bridge.py (it's a long-lived connection, not a one-shot call)."""

    def __init__(self, agent: FleetAgent, base_url: str = DEFAULT_BASE_URL):
        self.agent = agent
        self.base_url = base_url.rstrip("/")

    def _post(self, path: str, payload: dict, signed: bool = True) -> requests.Response:
        full_path = API_PREFIX + path
        raw = json.dumps(payload).encode()
        headers = ({"Content-Type": "application/json"}
                   if not signed else self.agent.rest_headers("POST", full_path, raw))
        resp = requests.post(self.base_url + full_path, data=raw, headers=headers, timeout=15)
        return resp

    def _get(self, path: str) -> requests.Response:
        full_path = API_PREFIX + path
        headers = self.agent.rest_headers("GET", full_path, b"")
        return requests.get(self.base_url + full_path, headers=headers, timeout=15)

    # -- one-time fleet identity bootstrap --------------------------------

    def register_agent(self, intent: str, root_type: str = "anonymous_staked", key_custody: str = "laptop",
                        notify_method: str = "none", notify_target: str = "") -> dict:
        resp = self._post("/agents/register", {
            "public_key": self.agent.public_key_hex, "root_type": root_type,
            "key_custody": key_custody, "intent": intent,
            "notify_method": notify_method, "notify_target": notify_target,
        }, signed=False)
        if resp.status_code >= 400:
            raise GetbodyError(f"register_agent failed: {resp.status_code} {resp.text}")
        return resp.json()

    def complete_challenge_response(self) -> dict:
        challenge_resp = requests.post(
            f"{self.base_url}{API_PREFIX}/agents/{self.agent.agent_id}/challenge",
            json={}, timeout=15,
        )
        if challenge_resp.status_code >= 400:
            raise GetbodyError(f"challenge failed: {challenge_resp.status_code} {challenge_resp.text}")
        nonce = challenge_resp.json()["nonce"]
        signature = did.sign(self.agent.private_key_hex, nonce.encode())
        verify_resp = requests.post(
            f"{self.base_url}{API_PREFIX}/agents/{self.agent.agent_id}/verify",
            json={"nonce": nonce, "signature": signature}, timeout=15,
        )
        if verify_resp.status_code >= 400:
            raise GetbodyError(f"verify failed: {verify_resp.status_code} {verify_resp.text}")
        return verify_resp.json()

    def whoami(self) -> dict:
        resp = self._get("/whoami")
        if resp.status_code >= 400:
            raise GetbodyError(f"whoami failed: {resp.status_code} {resp.text}")
        return resp.json()

    # -- per-duck body/listing registration --------------------------------

    def create_body(self, declared_value: str, manifest: dict, fail_safe: dict, location: dict = None,
                     interface_mode: str = "simulated") -> dict:
        resp = self._post("/bodies", {
            "interface_mode": interface_mode,
            "declared_value": declared_value,
            "manifest": manifest,
            "fail_safe": fail_safe,
            "location": location or {},
        })
        if resp.status_code >= 400:
            raise GetbodyError(f"create_body failed: {resp.status_code} {resp.text}")
        return resp.json()

    def create_listing(self, body_id: int, permissions_offered: list, offered_feeds: list,
                        command_schemas: dict = None, feed_schemas: dict = None,
                        offered_control_modes: list = None, required_root_tier: str = "any") -> dict:
        resp = self._post(f"/bodies/{body_id}/listings", {
            "permissions_offered": permissions_offered,
            "offered_feeds": offered_feeds,
            "command_schemas": command_schemas or {},
            "feed_schemas": feed_schemas or {},
            "offered_control_modes": offered_control_modes or ["supervisory"],
            "required_root_tier": required_root_tier,
        })
        if resp.status_code >= 400:
            raise GetbodyError(f"create_listing failed: {resp.status_code} {resp.text}")
        return resp.json()

    # -- read-only status, for watch.py --------------------------------------

    def list_listings(self) -> list:
        """Active, approved listings visible to anyone — used by watch.py to
        show whether your own listings are actually live and browsable."""
        resp = self._get("/listings")
        if resp.status_code >= 400:
            raise GetbodyError(f"list_listings failed: {resp.status_code} {resp.text}")
        return resp.json()
