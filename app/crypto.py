"""Credential encryption at rest. Key priority:
1. ENCRYPTION_KEY environment variable (32-byte urlsafe base64 Fernet key)
2. data/.key file (chmod 600), generated on first use.
Never log plaintext credentials."""
import os
from cryptography.fernet import Fernet, InvalidToken
from .db import DATA_DIR

KEY_PATH = os.path.join(DATA_DIR, ".key")
_fernet = None


def _load_key():
    global _fernet
    if _fernet is None:
        env_key = os.environ.get("ENCRYPTION_KEY")
        if env_key:
            key = env_key.strip().encode()
            _fernet = Fernet(key)  # raises if malformed — fail fast, don't silently fall back
            return _fernet
        os.makedirs(DATA_DIR, exist_ok=True)
        if not os.path.exists(KEY_PATH):
            key = Fernet.generate_key()
            with open(KEY_PATH, "wb") as f:
                f.write(key)
            os.chmod(KEY_PATH, 0o600)
        else:
            with open(KEY_PATH, "rb") as f:
                key = f.read().strip()
        _fernet = Fernet(key)
    return _fernet


def reset_key_cache():
    """For tests: forget the cached Fernet so a new key source is picked up."""
    global _fernet
    _fernet = None


def encrypt(plaintext: str) -> str:
    if plaintext is None:
        return ""
    return _load_key().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _load_key().decrypt(token.encode()).decode()
    except InvalidToken:
        raise ValueError("Could not decrypt stored credential (key mismatch?)")
