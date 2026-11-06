"""Shopify option labels and description HTML preservation."""
from __future__ import annotations

from shopextract._models import Platform
from shopextract._normalize import normalize
from shopextract.dashboard.shopify_csv import products_to_shopify_rows


SHOP = 'https://shop.example'


def _shopify_raw(**overrides):
    raw = {
        'id': 1,
        'title': 'Runner',
        'handle': 'runner',
        'body_html': '<p>Soft wool.</p><ul><li>Machine wash</li></ul>',
        'vendor': 'Allbirds',
        'product_type': 'Shoes',
        'tags': 'wool',
        'options': [
            {'name': 'Size', 'position': 1},
            {'name': 'Color', 'position': 2},
        ],
        'images': [{'src': 'https://cdn.example/a.png'}],
        'variants': [
            {
                'id': 11, 'title': '8 / Blue', 'price': '100.00', 'sku': 'R-8-B',
                'option1': '8', 'option2': 'Blue', 'available': True,
            },
        ],
        '_shop_currency': 'USD',
    }
    raw.update(overrides)
    return raw


def test_option_names_map_to_source_labels():
    product = normalize(_shopify_raw(), Platform.SHOPIFY, SHOP)
    assert product is not None
    assert product.option_names == ['Size', 'Color']
    assert product.variants[0].attributes['Size'] == '8'
    assert product.variants[0].attributes['Color'] == 'Blue'
    assert 'option1' not in product.variants[0].attributes

    rows = products_to_shopify_rows([product])
    assert rows[0]['Option1 name'] == 'Size'
    assert rows[0]['Option1 value'] == '8'
    assert rows[0]['Option2 name'] == 'Color'
    assert rows[0]['Option2 value'] == 'Blue'


def test_missing_option_labels_do_not_invent_size_color():
    raw = _shopify_raw(options=[])
    # Still has option1/option2 values on the variant.
    product = normalize(raw, Platform.SHOPIFY, SHOP)
    assert product is not None
    assert product.option_names == []
    assert product.variants[0].attributes.get('option1') == '8'
    rows = products_to_shopify_rows([product])
    # Blank name — not guessed as Size.
    assert rows[0]['Option1 name'] == ''
    assert rows[0]['Option1 value'] == '8'


def test_duplicate_option_labels_do_not_overwrite_values():
    raw = _shopify_raw(options=[
        {'name': 'Color', 'position': 1},
        {'name': 'Color', 'position': 2},
    ])
    product = normalize(raw, Platform.SHOPIFY, SHOP)
    assert product is not None
    attrs = product.variants[0].attributes
    assert attrs['Color'] == '8'
    assert attrs.get('option2') == 'Blue'


def test_description_html_and_plain_text_retained():
    product = normalize(_shopify_raw(), Platform.SHOPIFY, SHOP)
    assert product is not None
    assert '<p>Soft wool.</p>' in product.description_html
    assert '<ul>' in product.description_html
    assert 'Soft wool.' in product.description
    assert '<p>' not in product.description
    rows = products_to_shopify_rows([product])
    assert '<p>Soft wool.</p>' in rows[0]['Description']
    assert '<li>Machine wash</li>' in rows[0]['Description']
    # SEO uses plain text
    assert '<p>' not in rows[0]['SEO description']
