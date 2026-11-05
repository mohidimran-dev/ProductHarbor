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
    assert rows[0]['Published on online store'] == 'TRUE'
    assert rows[0]['Status'] == 'Active'
    assert rows[1]['URL handle'] == 'neverfull-mm'
    assert rows[1]['Title'] == ''
    assert rows[1]['Product image URL'] == 'https://cdn.example/side.jpg'
    assert rows[1]['Image position'] == '2'

    text = shopify_csv_text(products)
    reader = csv.DictReader(StringIO(text))
    assert reader.fieldnames == SHOPIFY_PRODUCT_HEADERS
