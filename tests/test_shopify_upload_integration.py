"""Shopify Files upload integration — live path is confirmation-gated.

Live uploads NEVER start because PRODUCTHARBOR_SHOPIFY_LIVE_UPLOAD=1 alone.
Use the user-started script instead:

  python scripts/verify_shopify_media_upload.py

That script prints destination shop + test scope and requires typing the shop
hostname before any stagedUploadsCreate / fileCreate call.

This module:
  - documents API version + scopes (always)
  - unit-tests sanitization / confirmation helpers (no network)
  - does not create/edit/delete/publish products
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from shopextract.dashboard.shopify_connection import SHOPIFY_API_VERSION, REQUIRED_SCOPES


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'verify_shopify_media_upload.py'


def _load_verify_module():
    spec = importlib.util.spec_from_file_location('verify_shopify_media_upload', SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_configured_api_version_and_scopes_documented():
    assert SHOPIFY_API_VERSION == '2026-10'
    assert 'write_files' in REQUIRED_SCOPES
    assert 'read_files' in REQUIRED_SCOPES


def test_live_upload_script_exists_and_requires_confirmation():
    text = SCRIPT.read_text(encoding='utf-8')
    assert 'require_user_confirmation' in text
    assert 'Type the destination shop hostname' in text
    assert 'PRODUCTHARBOR_SHOPIFY_LIVE_UPLOAD' not in text or 'alone' in text.lower()
    assert 'native_product_import' in text
    assert 'unverified' in text.lower()


def test_sanitize_cdn_url_strips_query():
    mod = _load_verify_module()
    raw = 'https://cdn.shopify.com/s/files/1/x/y.png?v=1&Signature=secret'
    assert mod.sanitize_cdn_url(raw) == 'https://cdn.shopify.com/s/files/1/x/y.png'
    assert mod.sanitize_cdn_url(None) is None
    assert mod.sanitize_cdn_url('http://example.com/a.png') == 'invalid-url'


def test_load_one_product_caps_images_at_two(tmp_path):
    mod = _load_verify_module()
    payload = {
        'products': [{
            'title': 'Probe',
            'image_url': 'https://cdn.example/a.png',
            'additional_images': [
                'https://cdn.example/b.png',
                'https://cdn.example/c.png',
                'https://cdn.example/d.png',
            ],
            'variants': [
                {'title': 'S', 'sku': 'S1', 'featured_image': 'https://cdn.example/c.png'},
            ],
            'option_names': ['Size'],
        }],
    }
    path = tmp_path / 'products.json'
    path.write_text(__import__('json').dumps(payload), encoding='utf-8')
    product = mod.load_one_product(path)
    assert product['image_url'] == 'https://cdn.example/a.png'
    assert product['additional_images'] == ['https://cdn.example/b.png']
    assert product['variants'][0]['featured_image'] == 'https://cdn.example/b.png'


def test_require_user_confirmation_aborts_on_mismatch(monkeypatch):
    mod = _load_verify_module()
    monkeypatch.setattr('builtins.input', lambda _: 'wrong-store.myshopify.com')
    with pytest.raises(SystemExit, match='Aborted'):
        mod.require_user_confirmation('right-store.myshopify.com')


@pytest.mark.asyncio
async def test_live_upload_not_invoked_by_env_flag_alone(monkeypatch, tmp_path):
    """Regression: enabling PRODUCTHARBOR_SHOPIFY_LIVE_UPLOAD must not upload."""
    import os

    monkeypatch.setenv('PRODUCTHARBOR_SHOPIFY_LIVE_UPLOAD', '1')
    # No shop/token → script must exit with setup steps, uploads_started False.
    monkeypatch.delenv('PRODUCTHARBOR_SHOPIFY_SHOP', raising=False)
    monkeypatch.delenv('PRODUCTHARBOR_SHOPIFY_ADMIN_TOKEN', raising=False)
    mod = _load_verify_module()
    ns = mod.build_parser().parse_args([
        '--artifacts-root', str(tmp_path),
        '--products', str(tmp_path / 'missing.json'),
    ])
    code = await mod.main_async(ns)
    assert code == 2
    report_path = mod.OUT / 'report.json'
    # OUT is package-relative; ensure script did not claim uploads started via env.
    assert os.environ.get('PRODUCTHARBOR_SHOPIFY_LIVE_UPLOAD') == '1'
    # When disconnected, report is written under OUT — if present, uploads_started false.
    if report_path.is_file():
        report = __import__('json').loads(report_path.read_text(encoding='utf-8'))
        assert report.get('uploads_started') is False
        assert report.get('native_product_import') == 'unverified'
