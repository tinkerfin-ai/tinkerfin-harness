"""密码哈希与校验"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from anyio import to_thread

_ALGORITHM = "pbkdf2-sha256"
_ITERATIONS = 600_000
_SALT_BYTES = 16


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        _ITERATIONS,
    )
    return f"${_ALGORITHM}${_ITERATIONS}${_encode(salt)}${_encode(digest)}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        marker, algorithm, iterations_text, salt_text, digest_text = encoded.split("$")
        if marker or algorithm != _ALGORITHM:
            return False
        iterations = int(iterations_text)
        if iterations < 100_000 or iterations > 2_000_000:
            return False
        salt = _decode(salt_text)
        expected = _decode(digest_text)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual, expected)


async def hash_password(password: str) -> str:
    """在线程边界生成带独立盐值的密码哈希"""

    return await to_thread.run_sync(_hash_password, password)


async def verify_password(password: str, encoded: str) -> bool:
    """在线程边界校验密码"""

    return await to_thread.run_sync(_verify_password, password, encoded)
