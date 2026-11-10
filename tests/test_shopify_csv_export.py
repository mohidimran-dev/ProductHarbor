"""Shopify Admin CSV mapping from extracted products."""
from __future__ import annotations

import csv
from decimal import Decimal
from io import StringIO

from shopextract._models import Platform, Product, Variant
from shopextract.dashboard.shopify_csv import (
    SHOPIFY_PRODUCT_HEADERS,
    products_to_shopify_rows,
    shopify_csv_text,
    slugify_handle,
)


def test_collection_target_strips_product_category():
    from shopextract.extractors.woocommerce import collection_target
    base, slug = collection_target('https://demo.athemes.com/botiga/product-category/cosmetics/')
    assert base == 'https://demo.athemes.com/botiga'
    assert slug == 'cosmetics'
    root, empty = collection_target('https://demo.athemes.com/botiga')
    assert root == 'https://demo.athemes.com/botiga'
    assert empty is None


def test_unimportable_image_urls_are_omitted_from_shopify_csv():
    from shopextract.dashboard.shopify_csv import omit_unimportable_image_urls
    rows = [{
        'Product image URL': 'http://127.0.0.1:8766/img/a.jpg',
        'Variant image URL': 'https://cdn.example/v.jpg',
        'Image position': '1',
        'Image alt text': 'A',
    }]
    assert omit_unimportable_image_urls(rows) == 1
    assert rows[0]['Product image URL'] == ''
    assert rows[0]['Variant image URL'] == 'https://cdn.example/v.jpg'


def test_slugify_handle():
    assert slugify_handle('Neverfull MM') == 'neverfull-mm'
    assert slugify_handle('  Alma BB!  ') == 'alma-bb'


def test_shopify_rows_match_template_headers_and_map_fields():
    products = [
        Product(
            title='Neverfull MM',
            description='Canvas tote.',
            price=Decimal('3050'),
            currency='USD',
            sku='M3A288',
            image_url='https://cdn.example/front.jpg',
            additional_images=['https://cdn.example/side.jpg'],
            platform=Platform.GENERIC,
            variants=[
                Variant(
                    title='Default',
                    sku='M3A288-BR',
                    price=Decimal('3050'),
                    attributes={'Color': 'Brown'},
                    featured_image='https://cdn.example/variant.jpg',
                ),
            ],
        ),
    ]
    rows = products_to_shopify_rows(products)
    assert rows[0]['Title'] == 'Neverfull MM'
    assert rows[0]['URL handle'] == 'neverfull-mm'
    assert rows[0]['Description'] == 'Canvas tote.'
    assert rows[0]['SKU'] == 'M3A288-BR'
    assert rows[0]['Price'] == '3050'
    assert rows[0]['Product image URL'] == 'https://cdn.example/front.jpg'
    assert rows[0]['Image position'] == '1'
    assert rows[0]['Option1 name'] == 'Color'
    assert rows[0]['Option1 value'] == 'Brown'
    assert rows[0]['Variant image URL'] == 'https://cdn.example/variant.jpg'
    assert rows[0]['Published on online store'] == 'true'
    assert rows[0]['Status'] == 'active'
    assert rows[0]['Charge tax'] == 'true'
    assert rows[0]['Requires shipping'] == 'true'
    assert rows[0]['Gift card'] == 'false'
    assert rows[0]['Continue selling when out of stock'] == 'deny'
    assert rows[1]['URL handle'] == 'neverfull-mm'
    assert rows[1]['Title'] == ''
    assert rows[1]['Product image URL'] == 'https://cdn.example/side.jpg'
    assert rows[1]['Image position'] == '2'

    text = shopify_csv_text(products)
    reader = csv.DictReader(StringIO(text))
    assert reader.fieldnames == SHOPIFY_PRODUCT_HEADERS


def test_shopify_rows_default_title_option_and_lowercase_enums():
    products = [
        Product(
            title='Solo Shoe',
            description='One size.',
            price=Decimal('100'),
            currency='USD',
            sku='SOLO-1',
            image_url='https://cdn.example/solo.jpg',
            platform=Platform.GENERIC,
            variants=[],
        ),
    ]
    rows = products_to_shopify_rows(products)
    assert len(rows) == 1
    assert rows[0]['Option1 name'] == 'Title'
    assert rows[0]['Option1 value'] == 'Default Title'
    assert rows[0]['Status'] == 'active'
    assert rows[0]['Published on online store'] == 'true'
    assert rows[0]['Continue selling when out of stock'] == 'deny'
