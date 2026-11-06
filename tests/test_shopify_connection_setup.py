"""Unit tests for interactive Shopify connection verify helpers (no live network)."""
from __future__ import annotations

from pathlib import Path

import pytest

from shopextract.dashboard.shopify_connection import (
    REQUIRED_SCOPES,
    ShopifyConnection,
    _sanitize_graphql_error_text,
    normalize_shop,
    save_connection,
    verify_connection_readonly,
)


def test_sanitize_redacts_token_like_substrings():
    raw = 'bad shpat_ABCDEFG1234567890 and shpss_XYZ'
    cleaned = _sanitize_graphql_error_text(raw)
    assert 'shpat_ABCDEFG' not in cleaned
    assert 'shpat_[REDACTED]' in cleaned
    assert 'shpss_[REDACTED]' in cleaned


def test_save_connection_roundtrip_does_not_echo(tmp_path, capsys):
    conn = save_connection(
        tmp_path,
        shop='demo-store',
        access_token='shpat_unit_test_token_value',
    )
    assert conn.shop == 'demo-store.myshopify.com'
    out = capsys.readouterr().out
    assert 'shpat_' not in out
    loaded_path = tmp_path / 'secrets' / 'shopify-connection.json'
    assert loaded_path.is_file()
    # File contains token (expected) but tests must not print it.
    text = loaded_path.read_text(encoding='utf-8')
    assert 'shpat_unit_test_token_value' in text
    assert 'write_files' in text


@pytest.mark.asyncio
async def test_verify_connection_readonly_reports_missing_scopes(monkeypatch):
    conn = ShopifyConnection(
        shop='demo-store.myshopify.com',
        access_token='shpat_unit_test_only',
    )

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                'data': {
                    'shop': {'name': 'Demo', 'myshopifyDomain': 'demo-store.myshopify.com'},
                    'currentAppInstallation': {
                        'accessScopes': [{'handle': 'read_products'}],
                    },
                    'files': None,
                }
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            assert 'graphql.json' in url
            assert headers['X-Shopify-Access-Token'] == 'shpat_unit_test_only'
            # Ensure we never put the token into the JSON body.
            assert 'shpat_' not in str(json)
            return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: FakeClient())
    result = await verify_connection_readonly(conn)
    assert result['ok'] is False
    assert set(result['missing_scopes']) == set(REQUIRED_SCOPES)
    assert result['shop_name'] == 'Demo'
    assert 'shpat_' not in str(result)


@pytest.mark.asyncio
async def test_verify_connection_readonly_ok_when_scopes_present(monkeypatch):
    conn = ShopifyConnection(
        shop=normalize_shop('demo-store.myshopify.com'),
        access_token='shpat_unit_test_only',
    )

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                'data': {
                    'shop': {'name': 'Demo', 'myshopifyDomain': 'demo-store.myshopify.com'},
                    'currentAppInstallation': {
                        'accessScopes': [
                            {'handle': 'read_files'},
                            {'handle': 'write_files'},
                        ],
                    },
                    'files': {'edges': []},
                }
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: FakeClient())
    result = await verify_connection_readonly(conn)
    assert result['ok'] is True
    assert result['missing_scopes'] == []
    assert result['files_readable'] is True


def test_setup_script_uses_getpass_and_not_argv_token():
    text = (Path(__file__).resolve().parents[1] / 'scripts' / 'setup_shopify_connection.py').read_text(
        encoding='utf-8'
    )
    assert 'getpass.getpass' in text
    assert 'save_connection' in text
    assert 'verify_connection_readonly' in text
    assert '--dry-run' in text
    assert 'PRODUCTHARBOR_SHOPIFY_ADMIN_TOKEN' not in text
    # Dry-run subprocess argv is path-only (no token literal / env token).
    assert "[sys.executable, str(VERIFY_SCRIPT), '--dry-run', '--artifacts-root', str(ARTIFACTS_ROOT)]" in text
