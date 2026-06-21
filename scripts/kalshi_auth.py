"""
Kalshi API authentication client.

Credentials are stored in `pass` (GPG password-store):
  - kalshi: API Key ID (UUID)
  - kalshi-private-key: RSA private key (PEM format)

Every authenticated request requires three headers:
  KALSHI-ACCESS-KEY:       the API Key ID
  KALSHI-ACCESS-TIMESTAMP: current time in milliseconds
  KALSHI-ACCESS-SIGNATURE: RSA-PSS signature of (timestamp + method + path)

Usage:
  from kalshi_auth import KalshiClient
  client = KalshiClient()          # defaults to production
  client = KalshiClient(demo=True) # demo environment

  balance = client.get("/portfolio/balance")
  markets = client.get("/markets", params={"limit": 5})
"""

import base64
import datetime
import subprocess
import json
from urllib.parse import urlparse, urlencode

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend


class KalshiAuthError(Exception):
    pass


class KalshiClient:
    """Authenticated Kalshi API client. Handles RSA-PSS signing automatically."""

    PRODUCTION_BASE = "https://external-api.kalshi.com/trade-api/v2"
    DEMO_BASE = "https://external-api.demo.kalshi.co/trade-api/v2"

    def __init__(self, demo=False):
        self.demo = demo
        self.base_url = self.DEMO_BASE if demo else self.PRODUCTION_BASE
        self.api_key_id = self._load_from_pass("kalshi")
        self.private_key_pem = self._load_from_pass("kalshi-private-key")
        self._private_key = serialization.load_pem_private_key(
            self.private_key_pem.encode("utf-8"),
            password=None,
            backend=default_backend(),
        )

    @staticmethod
    def _load_from_pass(entry: str) -> str:
        """Load a secret from the `pass` password store."""
        result = subprocess.run(
            ["pass", "show", entry],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise KalshiAuthError(
                f"Failed to load '{entry}' from pass: {result.stderr.strip()}"
            )
        return result.stdout.strip()

    def _sign(self, timestamp: str, method: str, path: str) -> str:
        """Create RSA-PSS signature for the request."""
        path_without_query = path.split("?")[0]
        message = f"{timestamp}{method}{path_without_query}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _make_headers(self, method: str, path: str) -> dict:
        """Build the three required auth headers."""
        timestamp = str(int(datetime.datetime.now().timestamp() * 1000))
        signature = self._sign(timestamp, method, path)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }

    def get(self, path: str, params: dict = None) -> dict:
        """Make an authenticated GET request.

        Args:
            path: API path relative to base URL (e.g. "/portfolio/balance")
            params: Optional query parameters

        Returns:
            Parsed JSON response
        """
        if params:
            query_string = urlencode(params)
            full_url = f"{self.base_url}{path}?{query_string}"
        else:
            full_url = f"{self.base_url}{path}"

        # For signing, use the full path from root (including /trade-api/v2)
        sign_path = urlparse(full_url).path

        headers = self._make_headers("GET", sign_path)

        response = requests.get(full_url, headers=headers, timeout=60)

        if response.status_code == 429:
            raise KalshiAuthError("Rate limited (429). Back off and retry.")
        if response.status_code == 401:
            raise KalshiAuthError(
                f"Authentication failed (401). Check API key ID and private key. "
                f"Response: {response.text}"
            )
        response.raise_for_status()
        return response.json()

    def post(self, path: str, body: dict = None) -> dict:
        """Make an authenticated POST request.

        Args:
            path: API path relative to base URL
            body: JSON body

        Returns:
            Parsed JSON response
        """
        full_url = f"{self.base_url}{path}"
        sign_path = urlparse(full_url).path

        headers = self._make_headers("POST", sign_path)
        headers["Content-Type"] = "application/json"

        response = requests.post(
            full_url, headers=headers, json=body, timeout=30
        )

        if response.status_code == 429:
            raise KalshiAuthError("Rate limited (429). Back off and retry.")
        if response.status_code == 401:
            raise KalshiAuthError(
                f"Authentication failed (401). Response: {response.text}"
            )
        response.raise_for_status()
        # Some POST endpoints return empty body on success
        if response.text:
            return response.json()
        return {}

    def delete(self, path: str) -> dict:
        """Make an authenticated DELETE request."""
        full_url = f"{self.base_url}{path}"
        sign_path = urlparse(full_url).path

        headers = self._make_headers("DELETE", sign_path)

        response = requests.delete(full_url, headers=headers, timeout=30)

        if response.status_code == 429:
            raise KalshiAuthError("Rate limited (429). Back off and retry.")
        response.raise_for_status()
        if response.text:
            return response.json()
        return {}


if __name__ == "__main__":
    print("Testing Kalshi API authentication...")

    client = KalshiClient(demo=True)
    print(f"Environment: DEMO")
    print(f"API Key ID: {client.api_key_id[:8]}...")
    print()

    # Test 1: Get account balance
    print("=== GET /portfolio/balance ===")
    try:
        balance = client.get("/portfolio/balance")
        print(json.dumps(balance, indent=2))
    except Exception as e:
        print(f"Error: {e}")

    print()

    # Test 2: Get account API limits (tier info)
    print("=== GET /account/limits ===")
    try:
        limits = client.get("/account/limits")
        print(json.dumps(limits, indent=2))
    except Exception as e:
        print(f"Error: {e}")
