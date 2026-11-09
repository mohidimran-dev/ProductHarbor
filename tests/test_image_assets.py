"""Product Harbor image collect / validate / dedupe."""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from shopextract.dashboard.image_assets import (
    collect_image_assets,
    detect_image,
    deterministic_filename,
    download_assets,
    rewrite_products_urls,
)


PNG_1X1 = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
    b'\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00'
    b'\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
)
JPEG_TINY = (
    b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00'
    b'\xff\xd9'
)


def test_detect_image_rejects_html():
    assert detect_image(b'<!DOCTYPE html><html>denied</html>') is None
    assert detect_image(PNG_1X1) == ('png', 'image/png')
    assert detect_image(JPEG_TINY)[0] == 'jpg'


def test_collect_gallery_variant_shared_and_blank_variant():
    products = [
        {
            'title': 'Bag A',
            'image_url': 'https://cdn.example/a1.jpg',
            'additional_images': [
                'https://cdn.example/a2.jpg',
                'https://cdn.example/shared.jpg',
            ],
            'variants': [
                {'sku': 'A-RED', 'title': 'Red', 'featured_image': 'https://cdn.example/shared.jpg'},
                {'sku': 'A-BLUE', 'title': 'Blue', 'featured_image': None},
            ],
        },
        {
            'title': 'Bag B',
            'image_url': 'https://cdn.example/b1.jpg',
            'additional_images': [],
            'variants': [
                {'sku': 'B1', 'title': 'Default', 'featured_image': 'https://cdn.example/b-var.jpg'},
            ],
        },
    ]
    assets, _stats = collect_image_assets(products)
    by_url = {a.source_url: a for a in assets}
    assert 'https://cdn.example/shared.jpg' in by_url
    shared = by_url['https://cdn.example/shared.jpg']
    roles = {(x.role, x.variant_sku) for x in shared.associations}
    assert ('gallery', None) in roles
    assert ('variant', 'A-RED') in roles
    # Blue has no featured image — no association invented
    assert not any(x.variant_sku == 'A-BLUE' for a in assets for x in a.associations)
    assert len(assets) == 5  # a1,a2,shared,b1,b-var


def test_deterministic_filename_collision_prevention():
    from shopextract.dashboard.image_assets import ImageAsset, ImageAssociation
    used = set()
    a1 = ImageAsset(
        source_url='https://cdn.example/x.jpg',
        content_hash='abc123abc123',
        associations=[ImageAssociation(0, 'bag-a', 'Bag A', 'gallery', position=1)],
    )
    a2 = ImageAsset(
        source_url='https://cdn.example/y.jpg',
        content_hash='abc123abc123',
        associations=[ImageAssociation(0, 'bag-a', 'Bag A', 'gallery', position=1)],
    )
    n1 = deterministic_filename(a1, ext='jpg', used=used)
    n2 = deterministic_filename(a2, ext='jpg', used=used)
    assert n1 != n2
    assert n1.endswith('.jpg')


def test_rewrite_preserves_unmapped_and_associations():
    products = [{
        'title': 'Bag',
        'image_url': 'https://src/a.jpg',
        'additional_images': ['https://src/b.jpg'],
        'variants': [{'sku': '1', 'featured_image': 'https://src/a.jpg'}],
    }]
    mapped = rewrite_products_urls(products, {
        'https://src/a.jpg': 'https://cdn.shopify.com/a.jpg',
    })
    assert mapped[0]['image_url'] == 'https://cdn.shopify.com/a.jpg'
    assert mapped[0]['additional_images'] == ['https://src/b.jpg']
    assert mapped[0]['variants'][0]['featured_image'] == 'https://cdn.shopify.com/a.jpg'


def test_rewrite_drop_unmapped_for_shopify_ready():
    products = [{
        'title': 'Bag',
        'image_url': 'https://src/a.jpg',
        'additional_images': ['https://src/b.jpg', 'https://src/c.jpg'],
        'variants': [
            {'sku': '1', 'featured_image': 'https://src/a.jpg'},
            {'sku': '2', 'featured_image': 'https://src/blocked.jpg'},
        ],
    }]
    mapped = rewrite_products_urls(
        products,
        {'https://src/a.jpg': 'https://cdn.shopify.com/a.jpg'},
        drop_unmapped=True,
    )
    assert mapped[0]['image_url'] == 'https://cdn.shopify.com/a.jpg'
    assert mapped[0]['additional_images'] == []
    assert mapped[0]['variants'][0]['featured_image'] == 'https://cdn.shopify.com/a.jpg'
    assert mapped[0]['variants'][1]['featured_image'] is None


@pytest.mark.asyncio
async def test_download_dedupes_bytes_and_rejects_html(tmp_path, monkeypatch):
    products = [{
        'title': 'Bag',
        'image_url': 'https://cdn.example/same.png',
        'additional_images': ['https://cdn.example/same-alias.png', 'https://cdn.example/bad.html'],
        'variants': [],
    }]
    # same bytes for two URLs -> one file on disk
    payloads = {
        'https://cdn.example/same.png': PNG_1X1,
        'https://cdn.example/same-alias.png': PNG_1X1,
        'https://cdn.example/bad.html': b'<!DOCTYPE html><html>nope</html>',
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
    assets, _stats = collect_image_assets(products)
    report = await download_assets(assets, tmp_path / 'images', concurrency=2)
    assert report['downloaded'] == 2
    assert report['failed'] == 1
    files = list((tmp_path / 'images').glob('*'))
    assert len(files) == 1  # content-hash dedupe
    ok = [a for a in assets if a.status == 'downloaded']
    assert ok[0].filename == ok[1].filename
    assert ok[0].local_path == ok[1].local_path
