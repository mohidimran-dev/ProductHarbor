"""Server-side Shopify Admin API connection for Product Harbor media uploads."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Admin GraphQL API version used for stagedUploadsCreate / fileCreate.
# Confirm against https://shopify.dev/docs/api/usage/versioning when upgrading.
# 2025-10 became inaccessible after 2026-10-16 15:00 UTC (Shopify falls forward).
# Pin a currently accessible stable Admin GraphQL version.
SHOPIFY_API_VERSION = '2026-10'
REQUIRED_SCOPES = ('write_files', 'read_files')
SHOP_HOST_RE = re.compile(r'^[a-z0-9][a-z0-9\-]*\.myshopify\.com$', re.I)

# Read-only GraphQL: auth + granted scopes + Files library readability.
# Never logs or returns the access token.
VERIFY_CONNECTION_QUERY = """
query ProductHarborConnectionVerify {
  shop {
    name
    myshopifyDomain
  }
  currentAppInstallation {
    accessScopes {
      handle
    }
  }
  files(first: 1) {
    edges {
      node {
        id
      }
    }
  }
}
"""


@dataclass(frozen=True)
class ShopifyConnection:
    shop: str
    access_token: str
    api_version: str = SHOPIFY_API_VERSION

    @property
    def graphql_url(self) -> str:
        return f'https://{self.shop}/admin/api/{self.api_version}/graphql.json'

    @property
    def public_status(self) -> dict:
        return {
            'connected': True,
            'shop': self.shop,
            'api_version': self.api_version,
            'required_scopes': list(REQUIRED_SCOPES),
            'note': 'Product Harbor uploads media only (write_files / read_files).',
        }


def normalize_shop(shop: str) -> str:
    value = (shop or '').strip().lower()
    value = value.removeprefix('https://').removeprefix('http://').split('/')[0]
    if value and not value.endswith('.myshopify.com'):
        value = f'{value}.myshopify.com'
    if not SHOP_HOST_RE.match(value):
        raise ValueError('Shop must look like your-store.myshopify.com')
    return value


def connection_path(artifacts_root: Path) -> Path:
    return Path(artifacts_root) / 'secrets' / 'shopify-connection.json'


def load_connection(artifacts_root: Path) -> ShopifyConnection | None:
    """Load connection from env overrides or server-side secrets file."""
    shop = (os.environ.get('PRODUCTHARBOR_SHOPIFY_SHOP') or '').strip()
    token = (os.environ.get('PRODUCTHARBOR_SHOPIFY_ADMIN_TOKEN') or '').strip()
    api_version = (os.environ.get('PRODUCTHARBOR_SHOPIFY_API_VERSION') or SHOPIFY_API_VERSION).strip()
    if shop and token:
        return ShopifyConnection(shop=normalize_shop(shop), access_token=token, api_version=api_version)

    path = connection_path(artifacts_root)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding='utf-8'))
    shop = str(data.get('shop') or '').strip()
    token = str(data.get('access_token') or '').strip()
    if not shop or not token:
        return None
    return ShopifyConnection(
        shop=normalize_shop(shop),
        access_token=token,
        api_version=str(data.get('api_version') or SHOPIFY_API_VERSION).strip(),
    )


def save_connection(artifacts_root: Path, *, shop: str, access_token: str, api_version: str | None = None) -> ShopifyConnection:
    path = connection_path(artifacts_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = ShopifyConnection(
        shop=normalize_shop(shop),
        access_token=access_token.strip(),
        api_version=(api_version or SHOPIFY_API_VERSION).strip(),
    )
    payload = {
        'shop': conn.shop,
        'access_token': conn.access_token,
        'api_version': conn.api_version,
        'required_scopes': list(REQUIRED_SCOPES),
        'purpose': 'Product Harbor media-only uploads (Files API)',
    }
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    temporary.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return conn


def clear_connection(artifacts_root: Path) -> None:
    path = connection_path(artifacts_root)
    if path.is_file():
        path.unlink()


def public_connection_status(artifacts_root: Path) -> dict:
    conn = load_connection(artifacts_root)
    if conn is None:
        return {
            'connected': False,
            'shop': None,
            'api_version': SHOPIFY_API_VERSION,
            'required_scopes': list(REQUIRED_SCOPES),
            'setup_hint': (
                'Create a custom app in Shopify Admin → Settings → Apps → Develop apps. '
                f'Enable Admin API scopes {", ".join(REQUIRED_SCOPES)}. '
                'Install the app, then run: python scripts/setup_shopify_connection.py '
                '(interactive; token is entered via getpass, never printed).'
            ),
        }
    return conn.public_status


def _sanitize_graphql_error_text(text: str) -> str:
    """Strip token-like substrings from API error text before returning/logging."""
    cleaned = re.sub(r'shpat_[A-Za-z0-9]+', 'shpat_[REDACTED]', text or '')
    cleaned = re.sub(r'shpca_[A-Za-z0-9]+', 'shpca_[REDACTED]', cleaned)
    cleaned = re.sub(r'shpss_[A-Za-z0-9]+', 'shpss_[REDACTED]', cleaned)
    return cleaned[:500]


async def verify_connection_readonly(conn: ShopifyConnection) -> dict[str, Any]:
    """Confirm auth + required Files scopes with read-only Admin GraphQL only.

    Does not upload, create files, or mutate products. Never includes the token
    in the returned payload.
    """
    import httpx

    headers = {
        'Content-Type': 'application/json',
        'X-Shopify-Access-Token': conn.access_token,
    }
    payload = {'query': VERIFY_CONNECTION_QUERY}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(conn.graphql_url, headers=headers, json=payload)
    except Exception as exc:
        return {
            'ok': False,
            'shop': conn.shop,
            'api_version': conn.api_version,
            'error': _sanitize_graphql_error_text(f'{type(exc).__name__}: {exc}'),
            'granted_scopes': [],
            'missing_scopes': list(REQUIRED_SCOPES),
            'shop_name': None,
            'files_readable': False,
        }

    if response.status_code == 401:
        return {
            'ok': False,
            'shop': conn.shop,
            'api_version': conn.api_version,
            'error': 'Unauthorized (HTTP 401). Check the Admin API access token.',
            'granted_scopes': [],
            'missing_scopes': list(REQUIRED_SCOPES),
            'shop_name': None,
            'files_readable': False,
        }
    if response.status_code == 404:
        return {
            'ok': False,
            'shop': conn.shop,
            'api_version': conn.api_version,
            'error': (
                f'Admin API endpoint not found (HTTP 404) for version {conn.api_version}. '
                'Confirm the shop hostname and API version.'
            ),
            'granted_scopes': [],
            'missing_scopes': list(REQUIRED_SCOPES),
            'shop_name': None,
            'files_readable': False,
        }
    if response.status_code >= 400:
        return {
            'ok': False,
            'shop': conn.shop,
            'api_version': conn.api_version,
            'error': _sanitize_graphql_error_text(
                f'HTTP {response.status_code}: {response.text}'
            ),
            'granted_scopes': [],
            'missing_scopes': list(REQUIRED_SCOPES),
            'shop_name': None,
            'files_readable': False,
        }

    body = response.json()
    if body.get('errors'):
        messages = '; '.join(
            str(err.get('message') or err) for err in body['errors']
        )
        return {
            'ok': False,
            'shop': conn.shop,
            'api_version': conn.api_version,
            'error': _sanitize_graphql_error_text(f'GraphQL error: {messages}'),
            'granted_scopes': [],
            'missing_scopes': list(REQUIRED_SCOPES),
            'shop_name': None,
            'files_readable': False,
        }

    data = body.get('data') or {}
    shop_info = data.get('shop') or {}
    installation = data.get('currentAppInstallation') or {}
    granted = sorted({
        str(item.get('handle') or '').strip()
        for item in (installation.get('accessScopes') or [])
        if item.get('handle')
    })
    missing = [scope for scope in REQUIRED_SCOPES if scope not in granted]
    # files(first:1) succeeds only when read_files (or broader) is granted.
    files_readable = isinstance(data.get('files'), dict) and 'edges' in (data.get('files') or {})
    if 'read_files' in missing and files_readable:
        # Defensive: rare installs may expose files without listing the handle.
        missing = [s for s in missing if s != 'read_files']

    return {
        'ok': not missing and bool(shop_info.get('myshopifyDomain') or shop_info.get('name')),
        'shop': conn.shop,
        'api_version': conn.api_version,
        'shop_name': shop_info.get('name'),
        'myshopify_domain': shop_info.get('myshopifyDomain'),
        'granted_scopes': granted,
        'missing_scopes': missing,
        'required_scopes': list(REQUIRED_SCOPES),
        'files_readable': files_readable,
        'error': (
            None if not missing
            else f'Missing required Admin API scopes: {", ".join(missing)}'
        ),
    }
