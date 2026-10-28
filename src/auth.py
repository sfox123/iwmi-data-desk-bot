"""App-only Graph tokens via MSAL client credentials (certificate preferred)."""
from __future__ import annotations

import threading
from pathlib import Path

import msal

from .config import ConfigError, Settings

SCOPE = ["https://graph.microsoft.com/.default"]


class AuthError(Exception):
    pass


class TokenProvider:
    """Returns a valid access token. MSAL caches it and fetches a new one before expiry."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._lock = threading.Lock()
        self._app = self._build()

    def _credential(self):
        s = self._settings
        if s.cert_path:
            path = Path(s.cert_path)
            if not path.exists():
                raise ConfigError(f"CERT_PATH not found: {path}")
            if path.suffix.lower() in (".pfx", ".p12"):
                cred = {"private_key_pfx_path": str(path)}
            else:
                if not s.cert_thumbprint:
                    raise ConfigError("CERT_THUMBPRINT is required with a PEM certificate")
                cred = {"private_key": path.read_text(), "thumbprint": s.cert_thumbprint}
            if s.cert_password:
                cred["passphrase"] = s.cert_password
            return cred
        return s.client_secret

    def _build(self) -> msal.ConfidentialClientApplication:
        return msal.ConfidentialClientApplication(
            client_id=self._settings.client_id,
            authority=f"https://login.microsoftonline.com/{self._settings.tenant_id}",
            client_credential=self._credential(),
        )

    def get_token(self) -> str:
        with self._lock:
            result = self._app.acquire_token_for_client(scopes=SCOPE)
        if "access_token" not in result:
            # error_description never contains the secret, but keep it short.
            raise AuthError(f"{result.get('error')}: {str(result.get('error_description', ''))[:200]}")
        return result["access_token"]

    def invalidate(self) -> None:
        """Drop cached tokens (e.g. after a 401) so the next call fetches a fresh one."""
        with self._lock:
            self._app = self._build()
