"""Opt-in interactive headed browser collection mode."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from shopextract._access import ACCESS_BLOCKED
from shopextract._execution import ExecutionPolicy, execution_policy, interactive_browser_enabled
from shopextract._extract import extract
from shopextract._models import Platform
from shopextract.extractors import _interactive_browser as ib


COLLECTION = 'https://us.louisvuitton.com/eng-us/women/handbags/new-in-handbags/_/N-t1dzbzff'
PRODUCT_A = 'https://us.louisvuitton.com/eng-us/products/neverfull-mm/_/N-abc123'
PRODUCT_B = 'https://us.louisvuitton.com/eng-us/products/onthego-pm/_/N-def456'


def test_default_profile_dir_is_host_scoped():
    path = ib.default_profile_dir(COLLECTION, root=Path('artifacts') / 'browser-profiles')
    assert path.as_posix().endswith('us.louisvuitton.com')


def test_discover_product_urls_dedupes_and_keeps_scope():
    payload = json.dumps({
        '@type': 'ItemList',
        'itemListElement': [{'url': PRODUCT_A}, {'item': {'url': PRODUCT_B}}],
    })
    html = (
        '<html><body>'
        f'<a href="{PRODUCT_A}">A</a>'
        '<a href="/eng-us/products/neverfull-mm/_/N-abc123">dup</a>'
        f'<a href="{PRODUCT_B}">B</a>'
        f'<a href="{COLLECTION}">collection facet</a>'
        '<a href="https://evil.example/products/x">out</a>'
        f'<script type="application/ld+json">{payload}</script>'
        '</body></html>'
    )
    urls = ib.discover_product_urls(html, COLLECTION, max_urls=10)
    assert urls == [PRODUCT_A, PRODUCT_B]
    assert COLLECTION not in urls


def test_extract_product_record_from_json_ld_only_published_fields():
    payload = json.dumps({
        '@type': 'Product',
        'name': 'Neverfull MM',
        'description': 'Canvas tote with leather trim.',
        'sku': 'M40156',
        'image': ['https://cdn.example/a.jpg', 'https://cdn.example/b.jpg'],
        'offers': {'@type': 'Offer', 'price': '1940.00', 'priceCurrency': 'USD'},
        'hasVariant': [{
            '@type': 'Product',
            'name': 'Brown',
            'sku': 'M40156-BR',
            'image': 'https://cdn.example/v.jpg',
            'offers': {'price': '1940.00', 'priceCurrency': 'USD'},
        }],
    })
    html = (
        '<html><head>'
        '<meta property="og:title" content="OG Title" />'
        f'<script type="application/ld+json">{payload}</script>'
        '</head><body></body></html>'
    )
    record = ib.extract_product_record(html, PRODUCT_A)
    assert record is not None
    assert record['title'] == 'Neverfull MM'
    assert record['description'] == 'Canvas tote with leather trim.'
    assert record['price'] == '1940.00'
    assert record['currency'] == 'USD'
    assert record['sku'] == 'M40156'
    assert record['images'] == ['https://cdn.example/a.jpg', 'https://cdn.example/b.jpg']
    assert record['variants'][0]['sku'] == 'M40156-BR'
    assert record['variants'][0]['featured_image'] == 'https://cdn.example/v.jpg'


def test_extract_product_record_returns_none_without_title():
    assert ib.extract_product_record('<html><body>empty</body></html>', PRODUCT_A) is None


def test_finite_collection_without_next_page_is_confirmed():
    confirmed, note = ib.decide_collection_end(
        discovered=24,
        requested=1000,
        notes=['no_load_more_control_found'],
        next_href=None,
    )
    assert confirmed is True
    assert note == 'collection_end_confirmed_no_further_page'


def test_remaining_next_page_stays_unconfirmed():
    confirmed, note = ib.decide_collection_end(
        discovered=24,
        requested=1000,
        notes=['no_load_more_control_found'],
        next_href='https://shop.example/collections/shoes?page=2',
    )
    assert confirmed is None
    assert note == 'pagination_end_unconfirmed'


def test_product_budget_is_not_treated_as_catalog_end():
    confirmed, note = ib.decide_collection_end(
        discovered=1000,
        requested=1000,
        notes=['no_load_more_control_found'],
        next_href=None,
    )
    assert confirmed is False
    assert note == 'hit_product_budget_end_unconfirmed'


def test_failed_next_navigation_stays_unconfirmed():
    confirmed, note = ib.decide_collection_end(
        discovered=24,
        requested=1000,
        notes=['next_page_navigation_failed'],
        next_href=None,
    )
    assert confirmed is None


def test_extract_product_record_reads_multiple_published_offers_as_variants():
    payload = json.dumps({
        '@type': 'Product',
        'name': 'Runner',
        'offers': {
            '@type': 'AggregateOffer',
            'lowPrice': '100.00',
            'priceCurrency': 'USD',
            'offers': [
                {'@type': 'Offer', 'name': 'Size 8', 'sku': 'R-8', 'price': '100.00', 'priceCurrency': 'USD'},
                {'@type': 'Offer', 'name': 'Size 9', 'sku': 'R-9', 'price': '110.00', 'priceCurrency': 'USD'},
            ],
        },
    })
    html = f'<html><head><script type="application/ld+json">{payload}</script></head></html>'
    record = ib.extract_product_record(html, PRODUCT_A)
    assert record is not None
    assert record['price'] == '100.00'
    assert [item['sku'] for item in record['variants']] == ['R-8', 'R-9']


def test_interactive_browser_enabled_requires_both_flags():
    with execution_policy(ExecutionPolicy(browser=True, interactive_browser=False)):
        assert interactive_browser_enabled() is False
    with execution_policy(ExecutionPolicy(browser=True, interactive_browser=True)):
        assert interactive_browser_enabled() is True
    with execution_policy(ExecutionPolicy(browser=False, interactive_browser=True)):
        assert interactive_browser_enabled() is False


@pytest.mark.asyncio
async def test_interactive_extractor_returns_access_blocked(monkeypatch):
    blocked = ib.InteractiveSessionResult(
        blocked=True,
        block_message=f'{ACCESS_BLOCKED} url={COLLECTION} status=403 reason=waiting_interstitial',
        collection_url=COLLECTION,
    )

    async def fake_run(*_a, **_k):
        return blocked

    monkeypatch.setattr(ib, 'run_interactive_collection', fake_run)
    result = await ib.interactive_extractor_result(COLLECTION, max_products=5)
    assert result.completeness_reason == ACCESS_BLOCKED
    assert result.error.startswith(ACCESS_BLOCKED)
    assert result.products == []


@pytest.mark.asyncio
async def test_extract_uses_interactive_path_and_skips_http_probe(monkeypatch):
    calls = {'probe': 0, 'api': 0}

    async def fake_probe(_url):
        calls['probe'] += 1
        return None

    async def fake_api(*_a, **_k):
        calls['api'] += 1
        return None

    async def fake_interactive(url, *, max_products, profile_dir, human_timeout, session_out=None):
        assert url == COLLECTION
        assert max_products == 5
        assert profile_dir is not None
        if session_out is not None:
            session_out.update({'pagination_end_confirmed': True, 'pagination_notes': ['ok']})
        from shopextract._models import ExtractorResult
        return ExtractorResult(
            products=[{
                'title': 'Neverfull MM',
                'description': 'Full description text.',
                'price': '1940.00',
                'currency': 'USD',
                'sku': 'M40156',
                'product_url': PRODUCT_A,
                'image_url': 'https://cdn.example/a.jpg',
                'additional_images': ['https://cdn.example/b.jpg'],
                'variants': [{
                    'title': 'Brown',
                    'sku': 'M40156-BR',
                    'price': '1940.00',
                    'featured_image': 'https://cdn.example/v.jpg',
                    'featured_image_id': None,
                    'options': {},
                }],
                '_capture': {'source': 'interactive_browser', 'collection_url': COLLECTION},
            }],
            complete=True,
            completeness_reason=None,
            pages_completed=1,
        )

    monkeypatch.setattr('shopextract._extract._probe_access_block', fake_probe)
    monkeypatch.setattr('shopextract._extract._try_api_extraction', fake_api)
    monkeypatch.setattr(
        'shopextract.extractors._interactive_browser.interactive_extractor_result',
        fake_interactive,
    )
    with execution_policy(ExecutionPolicy(browser=True, interactive_browser=True), 60):
        result = await extract(COLLECTION, platform=Platform.GENERIC, max_urls=5)
    assert calls['probe'] == 0
    assert calls['api'] == 0
    assert result.product_count == 1
    assert result.products[0].title == 'Neverfull MM'
    assert result.products[0].description == 'Full description text.'
    assert str(result.products[0].price) == '1940.00'
    assert result.products[0].currency == 'USD'
    assert result.products[0].sku == 'M40156'
    assert result.products[0].variants[0].sku == 'M40156-BR'
    assert 'ACCESS_BLOCKED' not in result.incompleteness_reasons
    assert result.catalog_complete is True
    assert result.errors == []
    assert result.outcome == 'complete'


@pytest.mark.asyncio
async def test_run_interactive_blocked_after_human_gate(monkeypatch):
    waiting = "<html><body class='lv-waiting'>Access Denied</body></html>"

    class FakeResponse:
        status = 403
        headers = {'content-type': 'text/html'}
        url = COLLECTION

        async def text(self):
            return waiting

    class FakePage:
        url = COLLECTION

        def on(self, *_a, **_k):
            return None

        async def goto(self, *_a, **_k):
            return FakeResponse()

        async def reload(self, *_a, **_k):
            return FakeResponse()

        async def content(self):
            return waiting

        async def wait_for_load_state(self, *_a, **_k):
            return None

        def locator(self, *_a, **_k):
            loc = MagicMock()
            loc.first = loc
            loc.count = AsyncMock(return_value=0)
            return loc

    class FakeContext:
        pages = []

        async def new_page(self):
            return FakePage()

        async def close(self):
            return None

    class FakePlaywright:
        def __init__(self):
            self.chromium = MagicMock()
            self.chromium.launch_persistent_context = AsyncMock(return_value=FakeContext())

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    import playwright.async_api as pw
    monkeypatch.setattr(pw, 'async_playwright', lambda: FakePlaywright())

    async def immediate_wait(_profile):
        return True

    with execution_policy(ExecutionPolicy(browser=True, interactive_browser=True), 30):
        session = await ib.run_interactive_collection(
            COLLECTION,
            max_products=5,
            profile_dir=Path('artifacts') / 'browser-profiles' / 'test-lv',
            human_timeout=5,
            wait_callback=immediate_wait,
        )
    assert session.blocked is True
    assert session.block_message is not None
    assert session.block_message.startswith(ACCESS_BLOCKED)
    assert 'waiting_interstitial' in session.block_message
    assert session.evidence.get('human_action', {}).get('kind') == 'waiting_interstitial'