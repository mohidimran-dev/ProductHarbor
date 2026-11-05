"""Blocked responses versus genuinely empty collections."""
from __future__ import annotations

import json
import httpx
import pytest
from click.testing import CliRunner

from shopextract._access import (
    ACCESS_BLOCKED,
    AccessBlockedError,
    detect_access_block,
    format_access_blocked,
)
from shopextract._extract import extract
from shopextract._models import Platform
from shopextract.cli import main
from shopextract.extractors.shopify import ShopifyExtractor
from shopextract.extractors.unified import UnifiedCrawlExtractor

URL = 'https://blocked.example/collection'
SHOP = 'https://shop.example'
EMPTY = 'https://empty.example'
LV_WAITING = (
    "<html><body class='lv-waiting'><div class='lv-waiting__logo'>Access Denied</div>"
    "</body></html>"
)


def test_detect_http_403_and_waiting_interstitial():
    block = detect_access_block(url=URL, status=403, body=LV_WAITING)
    assert block is not None
    assert block.status == 403
    assert block.reason == 'waiting_interstitial'
    message = format_access_blocked(block)
    assert message.startswith(ACCESS_BLOCKED)
    assert f'url={URL}' in message
    assert 'status=403' in message
    assert 'reason=waiting_interstitial' in message


def test_detect_plain_403_without_body_markers():
    block = detect_access_block(url=URL, status=403, body='<html></html>')
    assert block is not None and block.reason == 'http_403'


def test_empty_200_collection_is_not_access_blocked():
    assert detect_access_block(url=EMPTY, status=200, body='<html><body>No products</body></html>') is None


@pytest.mark.asyncio
async def test_httpx_403_stops_without_browser_retry(monkeypatch):
    calls = {'browser': 0}

    async def blocked_fetch(self, url):
        raise AccessBlockedError(detect_access_block(url=url, status=403, body=LV_WAITING))

    async def browser(self, url, include_diagnostics=False):
        calls['browser'] += 1
        return []

    monkeypatch.setattr(UnifiedCrawlExtractor, '_fetch_html_httpx', blocked_fetch)
    monkeypatch.setattr(UnifiedCrawlExtractor, '_extract_with_browser', browser)
    result = await UnifiedCrawlExtractor().extract(URL)
    assert result.completeness_reason == ACCESS_BLOCKED
    assert result.products == []
    assert result.error.startswith(ACCESS_BLOCKED)
    assert calls['browser'] == 0


@pytest.mark.asyncio
async def test_shopify_403_is_access_blocked_not_empty_complete():
    def respond(request):
        if request.url.path.endswith('/products.json'):
            return httpx.Response(403, text='Access Denied')
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await ShopifyExtractor(http_client=client).extract(SHOP, max_products=5)
    assert result.products == []
    assert result.complete is False
    assert result.completeness_reason == ACCESS_BLOCKED
    assert result.error.startswith(ACCESS_BLOCKED)
    assert 'status=403' in result.error


@pytest.mark.asyncio
async def test_shopify_empty_200_is_complete_empty_not_blocked():
    def respond(request):
        if request.url.path.endswith('/products.json'):
            return httpx.Response(200, json={'products': []})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await ShopifyExtractor(http_client=client).extract(SHOP, max_products=5)
    assert result.products == []
    assert result.complete is True
    assert result.completeness_reason is None
    assert not (result.error and result.error.startswith(ACCESS_BLOCKED))


@pytest.mark.asyncio
async def test_extract_reports_access_blocked_and_skips_discovery(monkeypatch):
    from shopextract import _extract as module
    import shopextract._discover as discovery

    async def no_api(*a, **k):
        return None

    async def blocked_probe(url):
        return detect_access_block(url=url, status=403, body=LV_WAITING)

    async def discover(*a, **k):
        raise AssertionError('discovery must not run after confirmed access block')

    monkeypatch.setattr(module, '_try_api_extraction', no_api)
    monkeypatch.setattr(module, '_probe_access_block', blocked_probe)
    monkeypatch.setattr(discovery, 'discover', discover)

    result = await extract(URL, platform=Platform.GENERIC, max_urls=5)
    assert result.products == []
    assert ACCESS_BLOCKED in result.incompleteness_reasons
    assert result.catalog_complete is False
    assert any(str(e).startswith(ACCESS_BLOCKED) for e in result.errors)


def test_cli_access_blocked_preserves_existing_export(tmp_path, monkeypatch):
    target = tmp_path / 'out.json'
    target.write_text(json.dumps([{'title': 'Kept'}]), encoding='utf-8')

    async def blocked(*a, **k):
        from shopextract._models import ExtractionResult, ExtractionTier
        return ExtractionResult(
            products=[], platform=Platform.GENERIC, tier=ExtractionTier.UNIFIED_CRAWL,
            catalog_complete=False, incompleteness_reasons=[ACCESS_BLOCKED],
            errors=[f'{ACCESS_BLOCKED} url={URL} status=403 reason=waiting_interstitial'],
        )

    monkeypatch.setattr('shopextract.extract', blocked)
    monkeypatch.setattr('shopextract._extract.extract', blocked)
    runner = CliRunner()
    result = runner.invoke(main, ['--output-format', 'json', 'extract', URL, '-n', '5', '-f', 'json', '-o', str(target)])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload['ok'] is False
    assert payload['error']['code'] == ACCESS_BLOCKED
    assert 'status=403' in payload['error']['message']
    assert json.loads(target.read_text(encoding='utf-8')) == [{'title': 'Kept'}]


def test_legacy_cli_does_not_overwrite_export_with_empty(tmp_path, monkeypatch):
    target = tmp_path / 'legacy.json'
    target.write_text(json.dumps([{'title': 'Prior'}]), encoding='utf-8')

    async def empty(*a, **k):
        from shopextract._models import ExtractionResult, ExtractionTier
        return ExtractionResult(products=[], platform=Platform.GENERIC, tier=ExtractionTier.CSS)

    monkeypatch.setattr('shopextract.extract', empty)
    monkeypatch.setattr('shopextract._extract.extract', empty)
    runner = CliRunner()
    result = runner.invoke(main, ['extract', EMPTY, '-n', '5', '-f', 'json', '-o', str(target)])
    assert result.exit_code == 1
    assert 'Preserved existing export' in result.output or 'Preserved existing export' in (result.stderr or '')
    assert json.loads(target.read_text(encoding='utf-8')) == [{'title': 'Prior'}]
