"""Where the Claude API key comes from: ``ANTHROPIC_API_KEY`` first, then (optionally) the
Windows Credential Manager through the optional ``keyring`` package. Never a config file,
never a log, never the database. ``--set-claude-key`` prompts without echo."""

from __future__ import annotations

import getpass
import hashlib
import importlib.util
import logging
from typing import Any, Callable, Mapping, Optional

log = logging.getLogger("mt5_bot")

ENV_VAR = "ANTHROPIC_API_KEY"
KEYRING_SERVICE = "mt5bot"
KEYRING_USER = "anthropic_api_key"


def _keyring() -> Optional[Any]:
    if importlib.util.find_spec("keyring") is None:
        return None
    import keyring  # noqa: PLC0415 - optional dependency, imported lazily

    return keyring


def load_api_key(env: Mapping[str, str], use_keyring: bool = True,
                 keyring_module: Optional[Any] = None) -> Optional[str]:
    """The key, or None. Any keyring failure counts as "no key" (logged without the secret)."""
    value = (env.get(ENV_VAR) or "").strip()
    if value:
        return value
    if not use_keyring:
        return None
    module = keyring_module if keyring_module is not None else _keyring()
    if module is None:
        return None
    try:
        stored = module.get_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception as exc:  # noqa: BLE001 - backends raise many types
        log.debug("keyring lookup failed (%s)", type(exc).__name__)
        return None
    return stored.strip() if isinstance(stored, str) and stored.strip() else None


def fingerprint(key: Optional[str]) -> Optional[str]:
    """Stable, non-reversible id used only to notice that the key CHANGED. Never logged."""
    if not key:
        return None
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def sdk_available() -> bool:
    return importlib.util.find_spec("anthropic") is not None


def set_key_interactive(prompt: Callable[[str], str] = getpass.getpass, out: Callable[[str], None] = print,
                        keyring_module: Optional[Any] = None) -> int:
    """``--set-claude-key``: store the key in the OS credential store. Returns an exit code."""
    module = keyring_module if keyring_module is not None else _keyring()
    if module is None:
        out("The optional 'keyring' package is not installed, so the key cannot be stored in the Windows "
            "Credential Manager. Either run:  pip install keyring  and try again, or set the key as a user "
            "environment variable in a NEW cmd window:  setx ANTHROPIC_API_KEY \"<your key>\"")
        return 1
    key = prompt("Paste the Claude API key (input is hidden), then press Enter: ").strip()
    if not key:
        out("No key entered; nothing stored.")
        return 1
    if not key.startswith("sk-ant-"):
        out("That does not look like an Anthropic API key (they start with sk-ant-); nothing stored.")
        return 1
    try:
        module.set_password(KEYRING_SERVICE, KEYRING_USER, key)
    except Exception as exc:  # noqa: BLE001
        out(f"Could not store the key in the credential store ({type(exc).__name__}).")
        return 1
    out("Key stored in the operating system credential store (service 'mt5bot'). It is never written to "
        "config.json or to the logs. Restart the bot to use it.")
    return 0
