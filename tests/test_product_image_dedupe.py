"""Product Harbor product + image deduplication behaviour."""
from __future__ import annotations

import asyncio
import io
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from PIL import Image

from shopextract._dedupe import (
    canonical_product_url,
    dedupe_product_dicts,
    dedupe_products,
    product_identity_key,
)
from shopextract._models import Platform, Product, Variant
from shopextract.dashboard.image_assets import (
    apply_content_dedupe_to_products,
    collect_image_assets,
    download_assets,
    normalize_image_url,
    pixel_fingerprint,
)
from shopextract.dashboard.shopify_csv import products_to_shopify_rows


def _png(color: tuple[int, int, int, int], size=(8, 8)) -> bytes:
    image = Image.new('RGBA', size, color)
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def _jpeg(color: tuple[int, int, int], size=(8, 8)) -> bytes:
    image = Image.new('RGB', size, color)
    buffer = io.BytesIO()
    image.save(buffer, format='JPEG', quality=90)
    return buffer.getvalue()


def test_canonical_product_url_strips_tracking_keeps_identity_params():
    url = (
        'https://Shop.Example.com/products/Bag?variant=123&utm_source=x&gclid=1#frag'
    )
    assert canonical_product_url(url) == (
        'https://shop.example.com/products/Bag?variant=123'
    )


def test_product_dedupe_merges_same_id_keeps_distinct_titles():
    store = 'https://shop.example.com'
    a1 = Product(
        title='Same Title',
        external_id='100',
        product_url='https://shop.example.com/products/a?utm_source=1',
        image_url='https://cdn.example/a1.png',
        platform=Platform.SHOPIFY,
        variants=[Variant(variant_id='1', title='Red', sku='R', featured_image='https://cdn.example/r.png')],
    )
    a2 = Product(
        title='Same Title Updated',
        external_id='100',
        product_url='https://shop.example.com/products/a?utm_campaign=2',
        additional_images=['https://cdn.example/a2.png'],
        platform=Platform.SHOPIFY,
        variants=[Variant(variant_id='2', title='Blue', sku='B')],
    )
    b = Product(
        title='Same Title',
        external_id='200',
        product_url='https://shop.example.com/products/b',
        platform=Platform.SHOPIFY,
    )
    products, stats = dedupe_products([a1, a2, b], store=store)
    assert stats.unique_products == 2
    assert stats.duplicate_product_observations_merged == 1
    assert products[0].title == 'Same Title Updated'
    assert len(products[0].variants) == 2
    assert products[0].image_url == 'https://cdn.example/a1.png'
    assert 'https://cdn.example/a2.png' in products[0].additional_images
    assert products[1].external_id == '200'


def test_product_dedupe_by_canonical_url_without_id():
    p1 = Product(
        title='Bag',
        product_url='https://shop.example.com/products/bag?utm_medium=email',
        image_url='https://cdn.example/1.png',
        platform=Platform.GENERIC,
    )
    p2 = Product(
        title='Bag',
        product_url='https://shop.example.com/products/bag#reviews',
        additional_images=['https://cdn.example/2.png'],
        platform=Platform.GENERIC,
    )
    products, stats = dedupe_products([p1, p2], store='https://shop.example.com')
    assert stats.unique_products == 1
    assert len(products[0].additional_images) == 1


def test_ten_distinct_images_remain_ten():
    products = [{
        'title': 'Gallery',
        'image_url': 'https://cdn.example/i0.png',
        'additional_images': [f'https://cdn.example/i{i}.png' for i in range(1, 10)],
        'variants': [],
    }]
    assets, stats = collect_image_assets(products)
    assert len(assets) == 10
    assert stats.repeated_image_references_removed == 0
    assert len(products[0]['additional_images']) == 9


def test_ten_refs_to_one_image_become_one_gallery_entry():
    url = 'https://cdn.example/same.png?utm_source=a'
    products = [{
        'title': 'Dup Gallery',
        'image_url': url,
        'additional_images': [
            'https://cdn.example/same.png',
            'https://cdn.example/same.png?utm_campaign=x',
            'https://CDN.example/same.png',
        ] * 3,
        'variants': [{'sku': 'V1', 'featured_image': 'https://cdn.example/same.png'}],
    }]
    assets, stats = collect_image_assets(products)
    assert len(products[0]['additional_images']) == 0
    assert stats.repeated_image_references_removed >= 9
    assert len(assets) == 1
    assert any(a.role == 'variant' for a in assets[0].associations)


@pytest.mark.asyncio
async def test_identical_bytes_different_urls_one_file(tmp_path, monkeypatch):
    blob = _png((10, 20, 30, 255))
    payloads = {
        'https://cdn.example/a.png': blob,
        'https://cdn.example/b.png': blob,
    }

    class FakeResponse:
        def __init__(self, content):
            self.content = content
        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def get(self, url):
            return FakeResponse(payloads[url])

    monkeypatch.setattr(httpx, 'AsyncClient', FakeClient)
    products = [{
        'title': 'P',
        'image_url': 'https://cdn.example/a.png',
        'additional_images': ['https://cdn.example/b.png'],
        'variants': [],
    }]
    assets, _ = collect_image_assets(products)
    report = await download_assets(assets, tmp_path / 'images', concurrency=2)
    assert report['downloaded'] == 2
    assert report['dedupe']['files_reused'] >= 1
    assert len(list((tmp_path / 'images').glob('*'))) == 1
    products2, stats = apply_content_dedupe_to_products(products, assets)
    assert products2[0]['additional_images'] == []
    assert stats.repeated_image_references_removed >= 1


@pytest.mark.asyncio
async def test_identical_pixels_different_encoding_reuse_file(tmp_path, monkeypatch):
    color = (40, 50, 60)
    png = _png((*color, 255))
    jpg = _jpeg(color)
    # Confirm pixel fingerprints match
    assert pixel_fingerprint(png) == pixel_fingerprint(jpg)
    payloads = {
        'https://cdn.example/a.png': png,
        'https://cdn.example/a.jpg': jpg,
    }

    class FakeResponse:
        def __init__(self, content):
            self.content = content
        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def get(self, url):
            return FakeResponse(payloads[url])

    monkeypatch.setattr(httpx, 'AsyncClient', FakeClient)
    products = [{
        'title': 'P',
        'image_url': 'https://cdn.example/a.png',
        'additional_images': ['https://cdn.example/a.jpg'],
        'variants': [],
    }]
    assets, _ = collect_image_assets(products)
    await download_assets(assets, tmp_path / 'images', concurrency=2)
    assert len(list((tmp_path / 'images').glob('*'))) == 1


@pytest.mark.asyncio
async def test_similar_but_distinct_photos_remain_separate(tmp_path, monkeypatch):
    payloads = {
        'https://cdn.example/red.png': _png((255, 0, 0, 255)),
        'https://cdn.example/blue.png': _png((0, 0, 255, 255)),
        'https://cdn.example/red-large.png': _png((255, 0, 0, 255), size=(16, 16)),
    }

    class FakeResponse:
        def __init__(self, content):
            self.content = content
        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def get(self, url):
            return FakeResponse(payloads[url])

    monkeypatch.setattr(httpx, 'AsyncClient', FakeClient)
    products = [{
        'title': 'P',
        'image_url': 'https://cdn.example/red.png',
        'additional_images': [
            'https://cdn.example/blue.png',
            'https://cdn.example/red-large.png',
        ],
        'variants': [],
    }]
    assets, _ = collect_image_assets(products)
    await download_assets(assets, tmp_path / 'images', concurrency=3)
    assert len(list((tmp_path / 'images').glob('*'))) == 3


@pytest.mark.asyncio
async def test_shared_image_keeps_associations_across_products(tmp_path, monkeypatch):
    blob = _png((1, 2, 3, 255))
    payloads = {'https://cdn.example/shared.png': blob}

    class FakeResponse:
        def __init__(self, content):
            self.content = content
        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def get(self, url):
            return FakeResponse(payloads[url])

    monkeypatch.setattr(httpx, 'AsyncClient', FakeClient)
    products = [
        {
            'title': 'A',
            'image_url': 'https://cdn.example/shared.png',
            'additional_images': [],
            'variants': [{'sku': 'A1', 'featured_image': 'https://cdn.example/shared.png'}],
        },
        {
            'title': 'B',
            'image_url': 'https://cdn.example/shared.png',
            'additional_images': [],
            'variants': [],
        },
    ]
    assets, _ = collect_image_assets(products)
    await download_assets(assets, tmp_path / 'images')
    assert len(assets) == 1
    handles = {a.product_handle for a in assets[0].associations}
    assert handles == {'a', 'b'}
    assert any(a.role == 'variant' for a in assets[0].associations)
    # Both products still retain the image after content finalize
    products2, _ = apply_content_dedupe_to_products(products, assets)
    assert products2[0]['image_url']
    assert products2[1]['image_url']


@pytest.mark.asyncio
async def test_concurrent_downloads_no_duplicate_files(tmp_path, monkeypatch):
    blob = _png((9, 9, 9, 255))
    urls = [f'https://cdn.example/n{i}.png' for i in range(8)]
    payloads = {url: blob for url in urls}

    class FakeResponse:
        def __init__(self, content):
            self.content = content
        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def get(self, url):
            await asyncio.sleep(0.01)
            return FakeResponse(payloads[url])

    monkeypatch.setattr(httpx, 'AsyncClient', FakeClient)
    products = [{
        'title': 'P',
        'image_url': urls[0],
        'additional_images': urls[1:],
        'variants': [],
    }]
    assets, _ = collect_image_assets(products)
    await download_assets(assets, tmp_path / 'images', concurrency=8)
    assert len(list((tmp_path / 'images').glob('*'))) == 1


def test_shopify_csv_keeps_variant_and_image_rows():
    products = [{
        'title': 'Tote',
        'description': 'Bag',
        'price': '10',
        'image_url': 'https://cdn.example/1.png',
        'additional_images': ['https://cdn.example/2.png', 'https://cdn.example/3.png'],
        'variants': [
            {'sku': 'T-R', 'title': 'Red', 'price': '10', 'options': {'Color': 'Red'},
             'featured_image': 'https://cdn.example/1.png'},
            {'sku': 'T-B', 'title': 'Blue', 'price': '10', 'options': {'Color': 'Blue'},
             'featured_image': None},
        ],
    }]
    rows = products_to_shopify_rows(products)
    handles = [r['URL handle'] for r in rows]
    assert handles.count('tote') == 4  # 2 variants + 2 extra image rows
    assert any(r['SKU'] == 'T-R' and r['Variant image URL'] for r in rows)
    assert any(r['SKU'] == 'T-B' and r['Variant image URL'] == '' for r in rows)
    positions = [r['Image position'] for r in rows if r['Product image URL']]
    assert '1' in positions and '2' in positions and '3' in positions


def test_normalize_image_url_preserves_signed_and_size_params():
    signed = 'https://cdn.example/x.jpg?X-Amz-Signature=abc&utm_source=y&width=800'
    norm = normalize_image_url(signed)
    assert 'X-Amz-Signature=abc' in norm or 'x-amz-signature=abc' in norm.lower()
    assert 'width=800' in norm
    assert 'utm_source' not in norm


def test_dedupe_product_dicts_pages_and_tracking_links():
    products = [
        {'id': 1, 'title': 'A', 'product_url': 'https://s.example/products/a?utm_source=1'},
        {'id': 1, 'title': 'A v2', 'product_url': 'https://s.example/products/a',
         'additional_images': ['https://cdn.example/x.png']},
        {'id': 2, 'title': 'A', 'product_url': 'https://s.example/products/b'},
    ]
    out, stats = dedupe_product_dicts(products, store='https://s.example', platform='shopify')
    assert stats.unique_products == 2
    assert out[0]['title'] == 'A v2'
    assert product_identity_key(out[0], store='https://s.example').startswith('id:shopify:')
