"""Authenticated encryption for OAuth credential material at rest.

Uses Fernet (AES-128-CBC + HMAC-SHA256, from the `cryptography` package): a standard,
well-reviewed authenticated-encryption construction, not a custom one. A single
symmetric key (`INTEGRATION_ENCRYPTION_KEY`) encrypts every stored refresh token.
Rotating that key is a documented future concern (see docs/ARCHITECTURE.md), not
implemented here: rotation would mean decrypting every stored token with the old key
and re-encrypting with the new one in one migration step.
"""

from cryptography.fernet import Fernet, InvalidToken

from nerva.core.config import Settings, get_settings


class EncryptionNotConfigured(RuntimeError):
    """`INTEGRATION_ENCRYPTION_KEY` is missing, malformed, or does not match the key
    a stored value was encrypted with."""


def _fernet(settings: Settings) -> Fernet:
    key = settings.integration_encryption_key
    if not key:
        raise EncryptionNotConfigured(
            "INTEGRATION_ENCRYPTION_KEY is not set; cannot store or read OAuth credentials."
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise EncryptionNotConfigured(
            "INTEGRATION_ENCRYPTION_KEY is not a valid Fernet key "
            "(expected 44 characters of urlsafe-base64)."
        ) from exc


def validate_encryption_key(settings: Settings | None = None) -> None:
    """Raises `EncryptionNotConfigured` if the key is missing or malformed.

    Called at startup when `ENVIRONMENT=production` (see `create_app`), so a
    misconfigured deployment fails immediately and loudly rather than only when the
    first integration is connected.
    """
    _fernet(settings or get_settings())


def encrypt_secret(plaintext: str, *, settings: Settings | None = None) -> str:
    return _fernet(settings or get_settings()).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str, *, settings: Settings | None = None) -> str:
    try:
        decrypted = _fernet(settings or get_settings()).decrypt(ciphertext.encode("ascii"))
        return decrypted.decode("utf-8")
    except InvalidToken as exc:
        raise EncryptionNotConfigured(
            "Stored credential could not be decrypted with the configured key."
        ) from exc
