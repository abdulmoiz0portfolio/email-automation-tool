"""Credential encryption at rest. Fernet key lives in data/.key (chmod 600).
Never log plaintext credentials."""
import os
from cryptography.fernet import Fernet, InvalidToken
from .db import DATA_DIR

KEY_PATH = os.path.join(DATA_DIR, ".key")
_fernet = None


def _load_key():
    global _fernet
    if _fernet is None:
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
