"""Copied verbatim from twinbase-backend/getbody/did.py (dependency-free —
only needs `cryptography`) so duck-sim's getbody client can sign requests
without depending on Django. Keep in sync by hand if the original changes.

DID (Ed25519) identity, spec §6 / decision #9: "Agents identified by
cryptographic DID, never self-description."

DIDs here are self-certifying, did:key style: did:key:z<base58btc(varint(
0xed01 multicodec for ed25519-pub) + raw 32-byte public key)>. That means an
agent_id is derivable from its own public key — the getbody Agent table is
the platform's registry of which DIDs it has *accepted* (root_type, stake,
attestations), not a name service resolving arbitrary DIDs to keys.

Base58btc is implemented locally (it's ~15 lines) rather than adding a
dependency for it. This has NOT been checked against the official did:key
W3C test vectors — only the algorithm shape (multicodec prefix + base58btc)
is asserted here; tests below check structural properties (determinism,
uniqueness, round-trip-free verification) rather than a pinned known-good
DID string.
"""
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_ED25519_PUB_MULTICODEC_PREFIX = bytes([0xED, 0x01])  # varint(0xed) = multicodec code for ed25519-pub


def _b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n > 0:
        n, rem = divmod(n, 58)
        out = _B58_ALPHABET[rem] + out
    leading_zero_bytes = len(data) - len(data.lstrip(b"\x00"))
    return "1" * leading_zero_bytes + out


def did_from_public_key_hex(public_key_hex: str) -> str:
    raw = bytes.fromhex(public_key_hex)
    if len(raw) != 32:
        raise ValueError("An Ed25519 public key must be 32 bytes (64 hex chars).")
    return "did:key:z" + _b58encode(_ED25519_PUB_MULTICODEC_PREFIX + raw)


def verify_signature(public_key_hex: str, message: bytes, signature_hex: str) -> bool:
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        public_key.verify(bytes.fromhex(signature_hex), message)
        return True
    except (InvalidSignature, ValueError):
        return False


def generate_keypair() -> tuple[str, str]:
    """Test/tooling helper only — real agents generate and keep their own
    private key; getbody never sees one. Returns (public_key_hex, private_key_hex)."""
    private_key = Ed25519PrivateKey.generate()
    from cryptography.hazmat.primitives import serialization

    priv_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return pub_bytes.hex(), priv_bytes.hex()


def sign(private_key_hex: str, message: bytes) -> str:
    """Test/tooling helper — mirrors what a real agent's own client does."""
    private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    return private_key.sign(message).hex()
