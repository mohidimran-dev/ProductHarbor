"""Accessible Shopify collection extraction (mocked products.json)."""
from __future__ import annotations

import httpx
import pytest

from shopextract._access import ACCESS_BLOCKED
from shopextract._extract import extract
from shopextract._models import Platform

COLLECTION = 'https://accessible.myshopify.com/collections/frontpage'


@pytest.mark.asyncio
async def test_accessible_shopify_collection_extracts_products(monkeypatch):
    products = [{
        'id': 1,
        'title': 'Canvas Tote',
        'handle': 'canvas-tote',
        'body_html': '<p>Cotton canvas tote</p>',
        'images': [{'src': 'https://cdn.shopify.com/tote.png'}],
        'variants': [
            {'id': 11, 'title': 'Natural / Soft', 'price': '48.00', 'available': True,
             'option1': 'Natural', 'option2': 'Soft'},
            {'id': 12, 'title': 'Black / Soft', 'price': '48.00', 'available': True,
             'option1': 'Black', 'option2': 'Soft'},
        ],
    }]

    def respond(request):
        path = request.url.path
        if path.endswith('/products.json'):
            return httpx.Response(200, json={'products': products})
        if path.endswith('/robots.txt'):
            return httpx.Response(404)
        return httpx.Response(404)

    original = httpx.AsyncClient

    def factory(*a, **k):
        k['transport'] = httpx.MockTransport(respond)
        return original(*a, **k)

    monkeypatch.setattr(httpx, 'AsyncClient', factory)
    result = await extract(COLLECTION, platform=Platform.SHOPIFY, max_urls=5)
    assert result.product_count >= 1
    assert ACCESS_BLOCKED not in result.incompleteness_reasons
    assert not any(str(e).startswith(ACCESS_BLOCKED) for e in result.errors)
    assert result.products[0].title == 'Canvas Tote'
    assert result.products[0].price is not None
