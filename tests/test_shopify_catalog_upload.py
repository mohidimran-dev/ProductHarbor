"""Shopify product create from a scraped record. No live store calls."""
from __future__ import annotations

import pytest

from shopextract.dashboard.shopify_catalog import (
    ShopifyCatalogIndex,
    build_product_set_input,
    family_id_of,
    product_handle,
    product_identity,
    upload_scraped_product,
)
from shopextract.dashboard.shopify_connection import ShopifyConnection


def _record(**overrides):
    base = {
        'title': 'Neverfull MM',
        'description': 'Coated canvas tote.',
        'price': '2100',
        'currency': 'USD',
        'product_url': 'https://us.louisvuitton.com/eng-us/products/neverfull-mm-nvprod7720056v/1AKZ71',
        'family_id': 'nvprod7720056v',
        'external_id': 'nvprod7720056v',
        'image_url': 'https://cdn.example/a.jpg',
        'additional_images': ['https://cdn.example/b.jpg'],
        'option_names': ['Color', 'Size'],
        'variants': [
            {'title': '8', 'sku': 'NV-8', 'price': '2100', 'options': {'Color': 'Brown', 'Size': '8'},
             'featured_image': 'https://cdn.example/a.jpg'},
            {'title': '9', 'sku': 'NV-9', 'price': '2100', 'options': {'Color': 'Brown', 'Size': '9'}},
        ],
    }
    base.update(overrides)
    return base


def test_build_product_set_keeps_title_description_variants_images_and_family_metafield():
    record = _record()
    payload = build_product_set_input(
        record,
        handle=product_handle(record['title'], product_identity(record)),
        resource_by_url={
            'https://cdn.example/a.jpg': 'https://shopify-staged.example/a',
            'https://cdn.example/b.jpg': 'https://shopify-staged.example/b',
        },
    )
    assert payload['title'] == 'Neverfull MM'
    assert '<p>Coated canvas tote.</p>' in payload['descriptionHtml']
    assert payload['handle'] == 'neverfull-mm-nvprod7720056v'
    assert payload['metafields'][0]['key'] == 'source_family_id'
    assert payload['metafields'][0]['value'] == 'nvprod7720056v'
    assert [row['sku'] for row in payload['variants']] == ['NV-8', 'NV-9']
    assert payload['variants'][0]['file']['originalSource'] == 'https://shopify-staged.example/a'
    assert len(payload['files']) == 2
    assert payload['status'] == 'ACTIVE'


def test_family_identity_wins_over_color_url():
    record = _record()
    assert family_id_of(record) == 'nvprod7720056v'
    assert product_identity(record) == 'family:nvprod7720056v'
    other_color = _record(
        product_url='https://us.louisvuitton.com/eng-us/products/neverfull-mm-nvprod7720056v/1AKZ6L',
    )
    assert product_identity(other_color) == product_identity(record)


def test_same_url_is_the_same_identity_without_family():
    left = product_identity({'product_url': 'https://Example.com/products/Bag/'})
    right = product_identity({'product_url': 'https://example.com/products/Bag'})
    assert left == right


@pytest.mark.asyncio
async def test_upload_skips_when_shopify_already_has_the_family(tmp_path, monkeypatch):
    record = _record()
    conn = ShopifyConnection(shop='demo-store.myshopify.com', access_token='shpat_unit')
    index = ShopifyCatalogIndex()
    index.families['nvprod7720056v'] = [{'id': 'gid://shopify/Product/9', 'handle': 'neverfull-mm-nvprod7720056v'}]

    async def explode(*_args, **_kwargs):
        raise AssertionError('Shopify should not be called for a known family')

    monkeypatch.setattr('shopextract.dashboard.shopify_catalog.graphql', explode)
    result = await upload_scraped_product(
        conn, record, artifacts_root=tmp_path, catalog_index=index,
    )
    assert result['status'] == 'skipped'
