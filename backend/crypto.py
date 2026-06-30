import base64
import hashlib
import os

from cryptography.fernet import Fernet


def _make_fernet() -> Fernet:
    secret = os.environ["APP_SECRET_KEY"].encode()
    derived = hashlib.sha256(secret).digest()
    fernet_key = base64.urlsafe_b64encode(derived)
    return Fernet(fernet_key)


def encrypt(plaintext: str) -> bytes:
    return _make_fernet().encrypt(plaintext.encode())


def decrypt(ciphertext: bytes) -> str:
    return _make_fernet().decrypt(ciphertext).decode()
