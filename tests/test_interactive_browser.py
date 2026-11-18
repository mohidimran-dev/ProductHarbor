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


def test_discover_product_urls_skips_faq_pages_that_contain_products():
    faq = 'https://us.louisvuitton.com/eng-us/faq/products/how-to-care-for-louis-vuitton-shoes'
    html = (
        '<html><body>'
        f'<a href="{PRODUCT_A}">A</a>'
        f'<a href="{faq}">Care</a>'
        '</body></html>'
    )
    urls = ib.discover_product_urls(html, COLLECTION, max_urls=10)
    assert urls == [PRODUCT_A]
    assert ib.is_catalog_product_url(faq) is False


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


def test_jsonld_image_objects_and_size_color_become_variants():
    html = """
    <html><head>
      <script type="application/ld+json">
      {"@type":"Product","name":"Territory Ankle Boot","sku":"1AKZ71","color":"Brown","size":"38.0",
       "image":[{"@type":"ImageObject","url":"https://cdn.example/front.jpg"},
                {"@type":"ImageObject","url":"https://cdn.example/side.jpg"}],
       "offers":{"@type":"Offer","price":"2250","priceCurrency":"USD"}}
      </script>
    </head><body></body></html>
    """
    record = ib.extract_product_record(html, PRODUCT_A)
    assert record is not None
    assert record['images'] == ['https://cdn.example/front.jpg', 'https://cdn.example/side.jpg']
    assert record['variants'][0]['options'] == {'Size': '38.0', 'Color': 'Brown'}
    assert record['variants'][0]['sku'] == '1AKZ71'
    ib.apply_published_sizes(record, ['35.0', '38.0', '42.0', 'Size guide', '0'])
    assert [row['options']['Size'] for row in record['variants']] == ['35.0', '38.0', '42.0']
    assert record['variants'][1]['sku'] == '1AKZ71'
    assert record['variants'][0]['sku'] is None
    assert record['variants'][0]['options']['Color'] == 'Brown'
    assert record['option_names'] == ['Size', 'Color']


def test_normalize_size_label_accepts_common_forms():
    assert ib.normalize_size_label('38') == '38'
    assert ib.normalize_size_label('Size 38') == '38'
    assert ib.normalize_size_label('38 EU') == '38'
    assert ib.normalize_size_label('39.5') == '39.5'
    assert ib.normalize_size_label('xl') == 'XL'
    assert ib.normalize_size_label('Size guide') is None


def test_sizes_from_html_skips_unavailable_markup():
    html = """
    <select>
      <option value="38">38</option>
      <option value="39" disabled>39</option>
      <option value="40" class="sold-out">40</option>
      <option value="41">Size 41 EU</option>
    </select>
    <button aria-label="42">42</button>
    """
    assert ib.sizes_from_html(html) == ['38', '41', '42']


def test_color_links_keep_same_family_only():
    page = 'https://us.louisvuitton.com/eng-us/products/territory-ankle-boot-nvprod7720056v/1AKZ71'
    html = f"""
    <html><body>
      <a href="{page}" aria-label="Brown">Brown</a>
      <a href="/eng-us/products/territory-ankle-boot-nvprod7720056v/1AKZ6L" title="Camel">Camel</a>
      <a href="/eng-us/products/other-boot-nvprod9999999v/ZZZZZ" aria-label="Black">Black</a>
      <a href="/eng-us/faq/products/care">Care</a>
    </body></html>
    """
    links = ib.color_links_from_html(html, page)
    brown = ib.canonical_page_url(page)
    camel = ib.canonical_page_url(
        'https://us.louisvuitton.com/eng-us/products/territory-ankle-boot-nvprod7720056v/1AKZ6L',
    )
    assert [url for url, _label in links] == [brown, camel]
    labels = {url: label for url, label in links}
    assert labels[brown] == 'Brown'
    assert labels[camel] == 'Camel'


def test_colorways_of_one_family_become_one_product():
    brown = {
        'title': 'Territory Ankle Boot',
        'description': 'Shearling ankle boot.',
        'price': '2250',
        'currency': 'USD',
        'sku': '1AKZ71',
        'image_url': 'https://cdn.example/brown.jpg',
        'additional_images': [],
        'product_url': 'https://us.louisvuitton.com/eng-us/products/territory-ankle-boot-nvprod7720056v/1AKZ71',
        'variants': [{
            'title': '38',
            'sku': '1AKZ71',
            'price': '2250',
            'options': {'Size': '38', 'Color': 'Brown'},
            'featured_image': 'https://cdn.example/brown.jpg',
        }],
    }
    camel = {
        'title': 'Territory Ankle Boot',
        'description': 'Shearling ankle boot. Camel brown.',
        'price': '2250',
        'currency': 'USD',
        'sku': '1AKZ6L',
        'image_url': 'https://cdn.example/camel.jpg',
        'additional_images': ['https://cdn.example/camel-side.jpg'],
        'product_url': 'https://us.louisvuitton.com/eng-us/products/territory-ankle-boot-nvprod7720056v/1AKZ6L',
        'variants': [
            {'title': '38', 'sku': '1AKZ6L-38', 'price': '2250', 'options': {'Size': '38', 'Color': 'Camel'}, 'featured_image': 'https://cdn.example/camel.jpg'},
            {'title': '39', 'sku': '1AKZ6L-39', 'price': '2250', 'options': {'Size': '39', 'Color': 'Camel'}, 'featured_image': 'https://cdn.example/camel.jpg'},
        ],
    }
    other = {
        'title': 'LV Cloud Ankle Boot',
        'description': 'Nylon boot.',
        'price': '1240',
        'sku': '1AKYSY',
        'image_url': 'https://cdn.example/cloud.jpg',
        'product_url': 'https://us.louisvuitton.com/eng-us/products/lv-cloud-ankle-boot-nvprod7720051v/1AKYSY',
        'variants': [],
    }
    merged = ib.merge_colorway_records([brown, other, camel])
    assert [item['title'] for item in merged] == ['Territory Ankle Boot', 'LV Cloud Ankle Boot']
    territory = merged[0]
    assert territory['description'] == 'Shearling ankle boot. Camel brown.'
    assert territory['image_url'] == 'https://cdn.example/brown.jpg'
    assert 'https://cdn.example/camel.jpg' in territory['additional_images']
    pairs = {(row['options'].get('Color'), row['options'].get('Size')) for row in territory['variants']}
    assert pairs == {('Brown', '38'), ('Camel', '38'), ('Camel', '39')}
    assert territory['option_names'] == ['Color', 'Size']
    assert merged[1]['sku'] == '1AKYSY'
    assert merged[1]['variants'] == []


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


def test_operation_timeout_is_not_a_confirmed_catalog_end():
    confirmed, note = ib.decide_collection_end(
        discovered=72,
        requested=1000,
        notes=['followed_next_page_round_1', 'operation_timeout'],
        next_href='https://shop.example/collections/shoes?page=3',
    )
    assert confirmed is False
    assert note == 'operation_timeout'


def test_human_wait_does_not_consume_execution_budget():
    import time
    from shopextract._execution import (
        ExecutionPolicy,
        execution_policy,
        pause_execution_deadline,
        remaining_execution_seconds,
    )

    with execution_policy(ExecutionPolicy(), 10) as state:
        before = state.deadline
        with pause_execution_deadline():
            time.sleep(0.05)
            assert remaining_execution_seconds() > 1000
        assert state.deadline >= before + 0.04


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
async def test_run_interactive_hard_denial_stops_without_retry(monkeypatch):
    denied = (
        '<html><body>Access denied. We invite you to return at a later time. '
        'REF #0.75dac817.1794819823.200497c4</body></html>'
    )

    class FakeResponse:
        status = 403
        headers = {'content-type': 'text/html'}
        url = COLLECTION

        async def text(self):
            return denied

    class FakePage:
        url = COLLECTION

        def on(self, *_a, **_k):
            return None

        async def goto(self, *_a, **_k):
            return FakeResponse()

        async def content(self):
            return denied

        async def screenshot(self, **_k):
            return b'jpeg'

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

    gate = AsyncMock(return_value=True)
    monkeypatch.setattr(ib, '_human_gate', gate)

    with execution_policy(ExecutionPolicy(browser=True, interactive_browser=True), 30):
        session = await ib.run_interactive_collection(
            COLLECTION,
            max_products=5,
            profile_dir=Path('artifacts') / 'browser-profiles' / 'test-lv',
            human_timeout=5,
            delay_range=(0, 0),
        )
    assert session.blocked is True
    assert session.block_message.startswith(ACCESS_BLOCKED)
    assert 'access_denied' in session.block_message
    assert session.evidence['denial']['reference'] == '#0.75dac817.1794819823.200497c4'
    assert session.evidence['denial']['completed_products'] == 0
    gate.assert_not_called()


@pytest.mark.asyncio
async def test_run_interactive_waiting_room_uses_human_gate(monkeypatch):
    waiting = "<html><body class='lv-waiting'>Please wait while we verify</body></html>"

    class FakeResponse:
        status = 200
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

        async def screenshot(self, **_k):
            return b'jpeg'

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
            delay_range=(0, 0),
        )
    assert session.blocked is True
    assert session.block_message is not None
    assert session.block_message.startswith(ACCESS_BLOCKED)
    assert 'waiting_interstitial' in session.block_message
    assert session.evidence.get('human_action', {}).get('kind') == 'waiting_interstitial'