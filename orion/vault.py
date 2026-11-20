"""Encryption for private giveaway rewards (gift-card codes, game keys, delivery instructions).

Everything secret is encrypted (Fernet: AES-128-CBC + HMAC) BEFORE it is written to the database, so a database
dump or backup never contains a readable code. The key lives outside the database:

  1. GIVEAWAY_ENCRYPTION_KEY in .env, if set, otherwise
  2. DATA_DIR/giveaway.key, created automatically (owner-read-only) the first time it is needed.

If the key is lost the codes cannot be recovered, so back the key up separately from the database backups.
A fingerprint is stored in the database so a *different* key (for example a new empty data folder) is detected
instead of silently producing unreadable rewards.
"""
import hashlib
import hmac
import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from . import config

log = logging.getLogger("orion.vault")

KEY_CHECK = "giveaway_key_check"
_CHECK_TEXT = b"orion-giveaway-key-check"
_fernet: Fernet | None = None
_index_key: bytes | None = None


class VaultError(Exception):
    """A plain-words reason the reward vault can't be used right now."""


def key_path() -> Path:
    return Path(config.DATA_DIR) / "giveaway.key"


def _read_or_create_key() -> bytes:
    if config.GIVEAWAY_ENCRYPTION_KEY:
        return config.GIVEAWAY_ENCRYPTION_KEY.encode()
    path = key_path()
    try:
        if path.exists():
            return path.read_bytes().strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:                       # another worker created it a moment ago
            return path.read_bytes().strip()
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        log.warning("Created a new giveaway encryption key at %s. BACK THIS FILE UP: without it the stored "
                    "reward codes cannot be read.", path)
        return key
    except OSError as e:
        raise VaultError(
            f"I can't read or create the encryption key file `{path}` ({e.strerror or e}). "
            "Make sure the `data` folder is writable, or set `GIVEAWAY_ENCRYPTION_KEY` in `.env`.") from e


def cipher() -> Fernet:
    global _fernet
    if _fernet is None:
        try:
            _fernet = Fernet(_read_or_create_key())
        except (ValueError, TypeError) as e:
            raise VaultError("The giveaway encryption key isn't valid. A key is 44 characters; generate one with "
                             "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`.") from e
    return _fernet


def reset() -> None:
    """Forget the loaded key (tests, or after changing the key settings)."""
    global _fernet, _index_key
    _fernet = None
    _index_key = None


def blind_index(text: str) -> str:
    """A keyed fingerprint of `text` (HMAC-SHA256). Lets the database spot "this code is already stored" without a
    readable copy, and, unlike a plain hash, can't be brute-forced from a database dump alone."""
    global _index_key
    if _index_key is None:
        cipher()                                    # raises VaultError with a clear message if the key is unusable
        _index_key = b"beta-keys-index:" + _read_or_create_key()
    return hmac.new(_index_key, text.encode("utf-8"), hashlib.sha256).hexdigest()


def encrypt(text: str) -> bytes:
    return cipher().encrypt(text.encode("utf-8"))


def decrypt(blob: bytes | memoryview | None) -> str:
    if blob is None:
        return ""
    try:
        return cipher().decrypt(bytes(blob)).decode("utf-8")
    except InvalidToken:
        raise VaultError("I can't decrypt this reward: the encryption key has changed since it was saved. "
                         "Restore the original `giveaway.key` (or GIVEAWAY_ENCRYPTION_KEY).") from None


async def verify(db) -> None:
    """Raise VaultError if the current key differs from the one that protected the rewards already stored."""
    c = cipher()
    stored = db.get(KEY_CHECK)
    if stored is None:
        await db.set(KEY_CHECK, c.encrypt(_CHECK_TEXT).decode())
        return
    try:
        ok = c.decrypt(stored.encode()) == _CHECK_TEXT
    except InvalidToken:
        ok = False
    if not ok:
        raise VaultError(
            "The giveaway encryption key doesn't match the one used for the rewards already stored, so I won't "
            "touch them. Restore the original `data/giveaway.key` (or GIVEAWAY_ENCRYPTION_KEY) and restart.")
