"""Product Harbor product identity keys and observation merge (no title/price-only merges)."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, is_dataclass
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ._models import Product, Variant
from .identity import supplier_scope

# Tracking / analytics only — never strip product/variant identity or signed URL params.
_TRACKING_KEYS = frozenset({
    'gclid', 'fbclid', 'mc_cid', 'mc_eid', 'msclkid', 'twclid', 'li_fat_id',
    'igshid', 'mkt_tok', 'vero_id', 'yclid', '_ga', '_gl', 'dclid',
})
_TRACKING_PREFIXES = ('utm_', 'mtm_', 'pk_', 'hsa_', 'spm')


@dataclass
class ProductDedupeStats:
    unique_products: int = 0
    duplicate_product_observations_merged: int = 0
    observations_in: int = 0

    def as_dict(self) -> dict:
        return {
            'unique_products': self.unique_products,
            'duplicate_product_observations_merged': self.duplicate_product_observations_merged,
            'observations_in': self.observations_in,
        }


def canonical_product_url(url: str) -> str:
    """Normalize a product URL: strip fragment + tracking params; keep identity params."""
    value = (url or '').strip()
    if not value:
        return ''
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return ''
    query = []
    for key, val in parse_qsl(parts.query, keep_blank_values=True):
        lower = key.lower()
        if lower in _TRACKING_KEYS or any(lower.startswith(p) for p in _TRACKING_PREFIXES):
            continue
        query.append((key, val))
    path = parts.path.rstrip('/') or ''
    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        path,
        urlencode(query, doseq=True),
        '',  # drop fragment
    ))


def product_identity_key(
    product: Product | dict,
    *,
    store: str = '',
) -> str | None:
    """Prefer platform+store+product ID; else verified canonical product URL. Never title/price."""
    if isinstance(product, dict):
        platform = product.get('platform') or 'generic'
        platform = getattr(platform, 'value', platform)
        external_id = str(product.get('external_id') or product.get('id') or '').strip()
        product_url = str(product.get('product_url') or product.get('url') or '')
        supplier = str(product.get('supplier_id') or store or '')
    else:
        platform = getattr(product.platform, 'value', product.platform) or 'generic'
        external_id = str(product.external_id or '').strip()
        product_url = product.product_url or ''
        supplier = product.supplier_id or store or ''

    host = supplier_scope(supplier or product_url)
    if external_id and host:
        return f'id:{platform}:{host}:{external_id}'

    canonical = canonical_product_url(product_url)
    if canonical:
        return f'url:{canonical}'
    return None


def _prefer_text(current: str, incoming: str) -> str:
    cur = (current or '').strip()
    new = (incoming or '').strip()
    if not cur:
        return incoming if incoming is not None else current
    if not new:
        return current
    # Refresh with newer non-empty observation (incoming wins when both present).
    return incoming


def _prefer_optional(current, incoming):
    if incoming is None or incoming == '' or incoming == []:
        return current
    if current is None or current == '' or current == []:
        return incoming
    return incoming


def _prefer_money(current: Decimal | None, incoming: Decimal | None) -> Decimal | None:
    if incoming is None:
        return current
    if current is None:
        return incoming
    return incoming


def _variant_key(variant: Variant) -> str:
    if variant.variant_id:
        return f'id:{variant.variant_id}'
    if variant.sku:
        return f'sku:{variant.sku}'
    if variant.canonical_variant_id:
        return f'can:{variant.canonical_variant_id}'
    attrs = ','.join(f'{k}={v}' for k, v in sorted((variant.attributes or {}).items()))
    if attrs:
        return f'attrs:{attrs}'
    title = (variant.title or '').strip()
    if title:
        return f'title:{title}'
    return f'obj:{id(variant)}'


def _merge_variant(existing: Variant, incoming: Variant) -> Variant:
    existing.title = _prefer_text(existing.title, incoming.title)
    existing.price = _prefer_money(existing.price, incoming.price) or existing.price
    existing.sku = _prefer_optional(existing.sku, incoming.sku)
    existing.in_stock = incoming.in_stock if incoming.in_stock is not None else existing.in_stock
    existing.gtin = _prefer_optional(existing.gtin, incoming.gtin)
    existing.featured_image = _prefer_optional(existing.featured_image, incoming.featured_image)
    existing.featured_image_id = _prefer_optional(existing.featured_image_id, incoming.featured_image_id)
    if not existing.variant_id and incoming.variant_id:
        existing.variant_id = incoming.variant_id
    if not existing.canonical_variant_id and incoming.canonical_variant_id:
        existing.canonical_variant_id = incoming.canonical_variant_id
    merged_attrs = dict(existing.attributes or {})
    merged_attrs.update({k: v for k, v in (incoming.attributes or {}).items() if v})
    existing.attributes = merged_attrs
    return existing


def _merge_image_lists(primary: str, additional: list[str], other_primary: str, other_additional: list[str]) -> tuple[str, list[str]]:
    """Preserve first gallery occurrence order; append newly seen URLs."""
    ordered: list[str] = []
    seen: set[str] = set()

    def add(url: str) -> None:
        value = (url or '').strip()
        if not value or value in seen:
            return
        seen.add(value)
        ordered.append(value)

    add(primary)
    for url in additional or []:
        add(url)
    add(other_primary)
    for url in other_additional or []:
        add(url)
    if not ordered:
        return '', []
    return ordered[0], ordered[1:]


def merge_product_observation(existing: Product, incoming: Product) -> Product:
    """Merge a repeated observation into the retained product without losing data."""
    existing.title = _prefer_text(existing.title, incoming.title)
    existing.description = _prefer_text(existing.description, incoming.description)
    existing.description_html = _prefer_text(
        getattr(existing, 'description_html', ''),
        getattr(incoming, 'description_html', ''),
    )
    if not getattr(existing, 'option_names', None) and getattr(incoming, 'option_names', None):
        existing.option_names = list(incoming.option_names)
    elif getattr(incoming, 'option_names', None):
        # Prefer longer sourced option label lists without inventing names.
        if len(incoming.option_names) > len(existing.option_names or []):
            existing.option_names = list(incoming.option_names)
    existing.price = _prefer_money(existing.price, incoming.price) or existing.price
    existing.compare_at_price = _prefer_money(existing.compare_at_price, incoming.compare_at_price)
    existing.currency = _prefer_text(existing.currency, incoming.currency) or existing.currency
    existing.sku = _prefer_optional(existing.sku, incoming.sku)
    existing.gtin = _prefer_optional(existing.gtin, incoming.gtin)
    existing.mpn = _prefer_optional(existing.mpn, incoming.mpn)
    existing.vendor = _prefer_optional(existing.vendor, incoming.vendor)
    existing.product_type = _prefer_optional(existing.product_type, incoming.product_type)
    existing.condition = _prefer_optional(existing.condition, incoming.condition)
    if incoming.in_stock is not None:
        existing.in_stock = incoming.in_stock
    if not existing.external_id and incoming.external_id:
        existing.external_id = incoming.external_id
    if not existing.canonical_product_id and incoming.canonical_product_id:
        existing.canonical_product_id = incoming.canonical_product_id
    # Prefer refreshed canonical URL when both resolve to same identity.
    if incoming.product_url:
        existing.product_url = incoming.product_url
    existing.image_url, existing.additional_images = _merge_image_lists(
        existing.image_url,
        list(existing.additional_images or []),
        incoming.image_url,
        list(incoming.additional_images or []),
    )
    existing.tags = list(dict.fromkeys([*(existing.tags or []), *(incoming.tags or [])]))
    existing.category_path = existing.category_path or list(incoming.category_path or [])
    if len(incoming.category_path or []) > len(existing.category_path or []):
        existing.category_path = list(incoming.category_path)
    attrs = dict(existing.attributes or {})
    attrs.update({k: v for k, v in (incoming.attributes or {}).items() if v})
    existing.attributes = attrs
    existing.pack_quantity = _prefer_optional(existing.pack_quantity, incoming.pack_quantity)
    existing.bundle_components = list(dict.fromkeys([
        *(existing.bundle_components or []), *(incoming.bundle_components or []),
    ]))
    by_key: dict[str, Variant] = {}
    order: list[str] = []
    for variant in list(existing.variants or []) + list(incoming.variants or []):
        key = _variant_key(variant)
        if key not in by_key:
            by_key[key] = variant
            order.append(key)
        else:
            _merge_variant(by_key[key], variant)
    existing.variants = [by_key[k] for k in order]
    # Refresh raw_data with newest observation while retaining prior under _prior_raw if useful.
    if incoming.raw_data:
        prior = existing.raw_data if isinstance(existing.raw_data, dict) else {}
        merged_raw = dict(prior)
        merged_raw.update(incoming.raw_data)
        existing.raw_data = merged_raw
    existing.scraped_at = incoming.scraped_at or existing.scraped_at
    return existing


def dedupe_products(
    products: list[Product],
    *,
    store: str = '',
) -> tuple[list[Product], ProductDedupeStats]:
    """Save each source product once; merge repeats; keep title-collisions with distinct IDs/URLs."""
    stats = ProductDedupeStats(observations_in=len(products))
    retained: dict[str, Product] = {}
    order: list[str] = []
    anonymous: list[Product] = []

    for product in products:
        key = product_identity_key(product, store=store)
        if key is None:
            # No stable identity — do not merge by title/price; keep as its own record.
            anonymous.append(product)
            continue
        if key not in retained:
            retained[key] = product
            order.append(key)
        else:
            merge_product_observation(retained[key], product)
            stats.duplicate_product_observations_merged += 1

    result = [retained[k] for k in order] + anonymous
    stats.unique_products = len(result)
    return result, stats


def _dict_from_product_like(item: dict) -> dict:
    return dict(item)


def merge_product_dict(existing: dict, incoming: dict) -> dict:
    """Merge raw/normalized product dicts using the same identity rules as Product merge."""
    # Lightweight dict merge for extractor-stage lists before normalization.
    for key in (
        'title', 'description', 'vendor', 'product_type', 'sku', 'gtin', 'mpn',
        'body_html', 'handle', 'product_url', 'url',
    ):
        if incoming.get(key) not in (None, ''):
            existing[key] = incoming[key]
    for key in ('price', 'compare_at_price', 'id', 'external_id'):
        if incoming.get(key) not in (None, '') and (existing.get(key) in (None, '') or key in ('price', 'compare_at_price')):
            if key in ('price', 'compare_at_price') and incoming.get(key) not in (None, ''):
                existing[key] = incoming[key]
            elif existing.get(key) in (None, ''):
                existing[key] = incoming[key]

    primary = existing.get('image_url') or existing.get('featured_image') or ''
    if isinstance(existing.get('images'), list) and existing['images'] and not primary:
        first = existing['images'][0]
        primary = first.get('src') if isinstance(first, dict) else (first if isinstance(first, str) else '')
    other_primary = incoming.get('image_url') or incoming.get('featured_image') or ''
    extras = [u for u in (existing.get('additional_images') or []) if isinstance(u, str)]
    other_extras = [u for u in (incoming.get('additional_images') or []) if isinstance(u, str)]
    # Also pull Shopify images arrays
    for block, target in ((existing, extras), (incoming, other_extras)):
        for image in block.get('images') or []:
            src = image.get('src') if isinstance(image, dict) else (image if isinstance(image, str) else None)
            if isinstance(src, str) and src.strip():
                target.append(src.strip())
    image_url, additional = _merge_image_lists(str(primary or ''), extras, str(other_primary or ''), other_extras)
    if image_url:
        existing['image_url'] = image_url
    existing['additional_images'] = additional

    # Variants: merge by id/sku
    existing_variants = list(existing.get('variants') or [])
    incoming_variants = list(incoming.get('variants') or [])
    by_key: dict[str, dict] = {}
    order: list[str] = []

    def vkey(variant: dict) -> str:
        if variant.get('id') is not None:
            return f"id:{variant['id']}"
        if variant.get('sku'):
            return f"sku:{variant['sku']}"
        title = str(variant.get('title') or '').strip()
        return f'title:{title}' if title else f'obj:{id(variant)}'

    for variant in existing_variants + incoming_variants:
        if not isinstance(variant, dict):
            continue
        key = vkey(variant)
        if key not in by_key:
            by_key[key] = dict(variant)
            order.append(key)
        else:
            cur = by_key[key]
            for field_name, value in variant.items():
                if value in (None, '', [], {}):
                    continue
                cur[field_name] = value
    existing['variants'] = [by_key[k] for k in order]
    return existing


def dedupe_product_dicts(
    products: list[dict],
    *,
    store: str = '',
    platform: str = 'generic',
) -> tuple[list[dict], ProductDedupeStats]:
    stats = ProductDedupeStats(observations_in=len(products))
    retained: dict[str, dict] = {}
    order: list[str] = []
    anonymous: list[dict] = []
    for product in products:
        if not isinstance(product, dict):
            continue
        item = dict(product)
        if 'platform' not in item:
            item['platform'] = platform
        key = product_identity_key(item, store=store)
        if key is None:
            anonymous.append(item)
            continue
        if key not in retained:
            retained[key] = item
            order.append(key)
        else:
            merge_product_dict(retained[key], item)
            stats.duplicate_product_observations_merged += 1
    result = [retained[k] for k in order] + anonymous
    stats.unique_products = len(result)
    return result, stats
