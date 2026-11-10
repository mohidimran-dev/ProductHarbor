"""Map extracted products to Shopify Admin product CSV (template-compatible)."""
from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from pathlib import Path

# Exact headers from Shopify product_template CSV sample.
SHOPIFY_PRODUCT_HEADERS = [
    'Title',
    'URL handle',
    'Description',
    'Vendor',
    'Product category',
    'Type',
    'Tags',
    'Published on online store',
    'Status',
    'SKU',
    'Variant Barcodes',
    'Option1 name',
    'Option1 value',
    'Option1 Linked To',
    'Option2 name',
    'Option2 value',
    'Option2 Linked To',
    'Option3 name',
    'Option3 value',
    'Option3 Linked To',
    'Price',
    'Compare-at price',
    'Cost per item',
    'Charge tax',
    'Tax code',
    'Unit price total measure',
    'Unit price total measure unit',
    'Unit price base measure',
    'Unit price base measure unit',
    'Inventory tracker',
    'Inventory quantity',
    'Continue selling when out of stock',
    'Weight value (grams)',
    'Weight unit for display',
    'Requires shipping',
    'Fulfillment service',
    'Product image URL',
    'Image position',
    'Image alt text',
    'Variant image URL',
    'Gift card',
    'SEO title',
    'SEO description',
    'Color (product.metafields.shopify.color-pattern)',
    'Google Shopping / Google product category',
    'Google Shopping / Gender',
    'Google Shopping / Age group',
    'Google Shopping / Manufacturer part number (MPN)',
    'Google Shopping / Ad group name',
    'Google Shopping / Ads labels',
    'Google Shopping / Condition',
    'Google Shopping / Custom product',
    'Google Shopping / Custom label 0',
    'Google Shopping / Custom label 1',
    'Google Shopping / Custom label 2',
    'Google Shopping / Custom label 3',
    'Google Shopping / Custom label 4',
    'Packed product length',
    'Packed product width',
    'Packed product height',
    'Packed product dimension unit',
]


def slugify_handle(title: str, *, fallback: str = 'product') -> str:
    text = (title or '').strip().lower()
    text = re.sub(r'[^a-z0-9]+', '-', text)
    text = re.sub(r'-{2,}', '-', text).strip('-')
    return text[:180] or fallback


def _blank_row() -> dict[str, str]:
    return {key: '' for key in SHOPIFY_PRODUCT_HEADERS}


def _money(value) -> str:
    if value is None or value == '':
        return ''
    return str(value)


def _option_pairs(attributes: dict | None, *, preferred_names: list[str] | None = None) -> list[tuple[str, str]]:
    """Build up to 3 option pairs. Prefer source option_names order when values exist."""
    if not attributes:
        return []
    pairs: list[tuple[str, str]] = []
    used_keys: set[str] = set()
    # Prefer ordered source labels when the variant carries those keys.
    for name in preferred_names or []:
        if not name or name not in attributes:
            continue
        value = attributes.get(name)
        if value is None or value == '':
            continue
        pairs.append((str(name), str(value)))
        used_keys.add(name)
        if len(pairs) >= 3:
            return pairs
    for key, value in attributes.items():
        if key in used_keys or value is None or value == '':
            continue
        # Positional fallback keys are exported with blank name only when no source label exists.
        label = '' if str(key).startswith('option') and str(key)[6:].isdigit() else str(key)
        pairs.append((label, str(value)))
        if len(pairs) >= 3:
            break
    return pairs


def products_to_shopify_rows(products: Iterable) -> list[dict[str, str]]:
    """Build Shopify CSV rows from Product objects or export dicts. No invented facts."""
    rows: list[dict[str, str]] = []
    used_handles: set[str] = set()

    for index, product in enumerate(products, start=1):
        if isinstance(product, dict):
            title = str(product.get('title') or '').strip()
            description_html = product.get('description_html') if isinstance(product.get('description_html'), str) else ''
            description_text = str(product.get('description') or '')
            # Shopify Body (HTML) prefers retained source HTML; fall back to plain text.
            description = description_html or description_text
            price = _money(product.get('price'))
            compare = _money(product.get('compare_at_price'))
            sku = product.get('sku') if isinstance(product.get('sku'), str) else ''
            image_url = product.get('image_url') if isinstance(product.get('image_url'), str) else ''
            additional = [i for i in (product.get('additional_images') or []) if isinstance(i, str)]
            variants = product.get('variants') or []
            vendor = product.get('vendor') if isinstance(product.get('vendor'), str) else ''
            product_type = product.get('product_type') if isinstance(product.get('product_type'), str) else ''
            option_names = [n for n in (product.get('option_names') or []) if isinstance(n, str)]
        else:
            title = str(getattr(product, 'title', '') or '').strip()
            description_html = str(getattr(product, 'description_html', '') or '')
            description_text = str(getattr(product, 'description', '') or '')
            description = description_html or description_text
            price = _money(getattr(product, 'price', None))
            compare = _money(getattr(product, 'compare_at_price', None))
            sku = getattr(product, 'sku', None) if isinstance(getattr(product, 'sku', None), str) else ''
            image_url = str(getattr(product, 'image_url', '') or '')
            additional = list(getattr(product, 'additional_images', None) or [])
            variants = list(getattr(product, 'variants', None) or [])
            vendor = getattr(product, 'vendor', None) if isinstance(getattr(product, 'vendor', None), str) else ''
            product_type = getattr(product, 'product_type', None) if isinstance(getattr(product, 'product_type', None), str) else ''
            option_names = [n for n in (getattr(product, 'option_names', None) or []) if isinstance(n, str)]

        if not title:
            continue

        handle = slugify_handle(title, fallback=f'product-{index}')
        base = handle
        suffix = 2
        while handle in used_handles:
            handle = f'{base}-{suffix}'
            suffix += 1
        used_handles.add(handle)

        variant_rows = []
        if variants:
            for variant in variants:
                if isinstance(variant, dict):
                    v_title = variant.get('title') if isinstance(variant.get('title'), str) else ''
                    v_sku = variant.get('sku') if isinstance(variant.get('sku'), str) else ''
                    v_price = _money(variant.get('price')) or price
                    v_image = variant.get('featured_image') if isinstance(variant.get('featured_image'), str) else ''
                    options = _option_pairs(variant.get('options') or variant.get('attributes'), preferred_names=option_names)
                else:
                    v_title = str(getattr(variant, 'title', '') or '')
                    v_sku = getattr(variant, 'sku', None) if isinstance(getattr(variant, 'sku', None), str) else ''
                    v_price = _money(getattr(variant, 'price', None)) or price
                    v_image = getattr(variant, 'featured_image', None) if isinstance(getattr(variant, 'featured_image', None), str) else ''
                    options = _option_pairs(getattr(variant, 'attributes', None), preferred_names=option_names)
                if not options and v_title:
                    options = [('Title', v_title)]
                variant_rows.append({
                    'sku': v_sku or sku,
                    'price': v_price,
                    'compare': compare,
                    'image': v_image,
                    'options': options,
                })
        else:
            variant_rows.append({
                'sku': sku,
                'price': price,
                'compare': compare,
                'image': '',
                'options': [],
            })

        for variant_index, variant in enumerate(variant_rows):
            row = _blank_row()
            row['URL handle'] = handle
            if variant_index == 0:
                row['Title'] = title
                row['Description'] = description
                row['Vendor'] = vendor
                row['Type'] = product_type
                # Shopify Admin CSV docs require lowercase enums (true/false/deny/active).
                row['Published on online store'] = 'true'
                row['Status'] = 'active'
                row['Charge tax'] = 'true'
                row['Requires shipping'] = 'true'
                row['Fulfillment service'] = 'manual'
                row['Gift card'] = 'false'
                row['Continue selling when out of stock'] = 'deny'
                if image_url:
                    row['Product image URL'] = image_url
                    row['Image position'] = '1'
                    row['Image alt text'] = title
                row['SEO title'] = title
                if description_text:
                    row['SEO description'] = description_text[:320]
                elif description:
                    # Strip tags lightly for SEO when only HTML was retained.
                    import re
                    row['SEO description'] = re.sub(r'<[^>]+>', ' ', description)[:320].strip()
            row['SKU'] = variant['sku'] or ''
            row['Price'] = variant['price'] or ''
            row['Compare-at price'] = variant['compare'] or ''
            if variant['image']:
                row['Variant image URL'] = variant['image']
            options = list(variant['options'][:3])
            # Shopify single-variant rows still need a Title option when none were scraped.
            if not options:
                options = [('Title', 'Default Title')]
            for option_index, (name, value) in enumerate(options, start=1):
                row[f'Option{option_index} name'] = name or 'Title'
                row[f'Option{option_index} value'] = value
            rows.append(row)

        for image_index, extra in enumerate(additional, start=2):
            image_row = _blank_row()
            image_row['URL handle'] = handle
            image_row['Product image URL'] = extra
            image_row['Image position'] = str(image_index)
            image_row['Image alt text'] = title
            rows.append(image_row)

    return rows


def shopify_image_url_problem(value: str) -> str | None:
    """Return why a cell cannot be imported by Shopify's native CSV importer."""
    lower = value.strip().lower()
    if not lower:
        return None
    forbidden_prefixes = ('file:', 'data:', 'blob:', '/', '\\\\', 'c:\\', 'c:/')
    if any(lower.startswith(prefix) for prefix in forbidden_prefixes):
        return 'local_or_data_url'
    if 'localhost' in lower or '127.0.0.1' in lower:
        return 'localhost_url'
    if not lower.startswith('https://') and not lower.startswith('http://'):
        return 'not_an_http_url'
    return None


def assert_shopify_image_urls_safe(rows: Iterable[dict[str, str]]) -> None:
    """Reject local/file/base64 values that Shopify native CSV cannot import."""
    for row in rows:
        for key in ('Product image URL', 'Variant image URL'):
            value = (row.get(key) or '').strip()
            problem = shopify_image_url_problem(value)
            if problem == 'local_or_data_url':
                raise ValueError(f'Shopify image column must be an HTTPS URL, not local/data: {key}')
            if problem == 'localhost_url':
                raise ValueError(f'Shopify image column must not use localhost: {key}')
            if problem == 'not_an_http_url':
                raise ValueError(f'Shopify image column must be an HTTP(S) URL: {key}')


def omit_unimportable_image_urls(rows: list[dict[str, str]]) -> int:
    """Blank image cells Shopify cannot fetch. Product rows stay in the file."""
    omitted = 0
    for row in rows:
        for key in ('Product image URL', 'Variant image URL'):
            value = (row.get(key) or '').strip()
            if value and shopify_image_url_problem(value):
                row[key] = ''
                if key == 'Product image URL':
                    row['Image position'] = ''
                    row['Image alt text'] = ''
                omitted += 1
    return omitted


def write_shopify_csv(products: Iterable, path: Path, *, validate_image_urls: bool = True) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = products_to_shopify_rows(products)
    if validate_image_urls:
        omit_unimportable_image_urls(rows)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=SHOPIFY_PRODUCT_HEADERS, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    temporary.replace(path)
    return path


def shopify_csv_text(products: Iterable) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=SHOPIFY_PRODUCT_HEADERS, extrasaction='ignore')
    writer.writeheader()
    for row in products_to_shopify_rows(products):
        writer.writerow(row)
    return buffer.getvalue()
