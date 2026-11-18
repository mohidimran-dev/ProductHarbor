"""Import a Shopify product CSV into the connected Admin store.

Groups rows by handle, uploads image URLs into Shopify Files, then creates a
draft product (or attaches images to an existing product that has none).
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import logging
import re
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .image_assets import ImageAsset, detect_image
from .shopify_connection import ShopifyConnection
from .shopify_files import (
    MAX_IMAGE_BYTES,
    ShopifyStoreAccessError,
    ShopifyUploadError,
    create_file_and_wait_cdn,
    file_create_access_denied_message,
    graphql,
    is_file_create_access_denied,
    staged_upload_file,
)

logger = logging.getLogger(__name__)
_LEDGER_LOCK = threading.Lock()
# Shopify Body (HTML) / image URL cells can exceed Python's default 128 KiB field cap.
csv.field_size_limit(max(10 * 1024 * 1024, csv.field_size_limit()))


class CsvNetworkError(RuntimeError):
    """Image host unreachable / dropped — distinct from bad image bytes or Shopify auth."""

PRODUCT_SET = """
mutation ProductHarborCsvProductSet($input: ProductSetInput!, $synchronous: Boolean!) {
  productSet(input: $input, synchronous: $synchronous) {
    product { id handle status }
    userErrors { field message }
  }
}
"""

FIND_PRODUCT = """
query ProductHarborCsvByHandle($identifier: ProductIdentifierInput!) {
  productByIdentifier(identifier: $identifier) {
    id
    handle
    status
    media(first: 20) {
      nodes { id }
    }
  }
}
"""

CREATE_MEDIA = """
mutation ProductHarborCsvCreateMedia($productId: ID!, $media: [CreateMediaInput!]!) {
  productCreateMedia(productId: $productId, media: $media) {
    media {
      ... on MediaImage {
        id
        fileStatus
        image { url }
      }
    }
    mediaUserErrors { field message code }
  }
}
"""

NODE_MEDIA = """
query ProductHarborCsvMediaNode($id: ID!) {
  node(id: $id) {
    ... on MediaImage {
      id
      fileStatus
      image { url }
    }
  }
}
"""

# Current Product Harbor / Shopify Admin names plus legacy Admin CSV names.
_COLUMN_ALIASES = {
    'handle': ('url handle', 'handle', 'handle (product)', 'product handle'),
    'title': ('title', 'product title'),
    'description': ('description', 'body (html)', 'body html', 'body'),
    'vendor': ('vendor',),
    'product_type': ('type', 'product type', 'product category'),
    'sku': ('sku', 'variant sku'),
    'price': ('price', 'variant price'),
    'compare_at': ('compare-at price', 'compare at price', 'variant compare at price'),
    'option1_name': ('option1 name', 'option1name'),
    'option1_value': ('option1 value', 'option1value'),
    'option2_name': ('option2 name', 'option2name'),
    'option2_value': ('option2 value', 'option2value'),
    'option3_name': ('option3 name', 'option3name'),
    'option3_value': ('option3 value', 'option3value'),
    'image': (
        'product image url',
        'image src',
        'image url',
        'image',
        'product image',
    ),
    'image_position': ('image position', 'image position (product)'),
    'image_alt': ('image alt text', 'image alt text (product)', 'image alt'),
    'variant_image': (
        'variant image url',
        'variant image',
        'variant src',
        'variant image src',
    ),
}


@dataclass
class CsvVariant:
    sku: str | None = None
    price: str | None = None
    compare_at: str | None = None
    options: dict[str, str] = field(default_factory=dict)
    image_url: str | None = None
    source_row: int | None = None


@dataclass
class CsvProduct:
    handle: str
    title: str = ''
    description: str = ''
    vendor: str = ''
    product_type: str = ''
    gallery: list[str] = field(default_factory=list)
    variants: list[CsvVariant] = field(default_factory=list)
    source_rows: list[int] = field(default_factory=list)
    source_handles: list[str] = field(default_factory=list)

    @property
    def all_image_urls(self) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        for url in [*self.gallery, *(variant.image_url for variant in self.variants)]:
            if not isinstance(url, str):
                continue
            cleaned = url.strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            found.append(cleaned)
        return found

    @property
    def ledger_handles(self) -> list[str]:
        handles = []
        seen: set[str] = set()
        for value in [self.handle, *self.source_handles]:
            cleaned = (value or '').strip().lower()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            handles.append(cleaned)
        return handles


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_header(value: str) -> str:
    return re.sub(r'\s+', ' ', (value or '').strip().lower())


def _header_map(fieldnames: list[str] | None) -> dict[str, str]:
    available = {_norm_header(name): name for name in (fieldnames or []) if name}
    mapping: dict[str, str] = {}
    for key, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in available:
                mapping[key] = available[alias]
                break
    return mapping


def _cell(row: dict[str, str], mapping: dict[str, str], key: str) -> str:
    column = mapping.get(key)
    if not column:
        return ''
    return str(row.get(column) or '').strip()


def _is_http_url(value: str) -> bool:
    parts = urlsplit(value.strip())
    return parts.scheme in ('http', 'https') and bool(parts.netloc)


def parse_shopify_product_csv(text: str) -> list[CsvProduct]:
    """Read a Shopify product CSV into one CsvProduct per handle."""
    reader = csv.DictReader(io.StringIO(text))
    mapping = _header_map(list(reader.fieldnames or []))
    if 'handle' not in mapping:
        raise ValueError('CSV is missing a Handle / URL handle column.')
    products: dict[str, CsvProduct] = {}
    order: list[str] = []
    last_option_names: dict[str, list[str]] = {}
    for index, raw in enumerate(reader, start=2):
        row = {str(key): ('' if value is None else str(value)) for key, value in raw.items()}
        handle = _cell(row, mapping, 'handle').lower().strip().strip('/')
        if not handle:
            continue
        product = products.get(handle)
        if product is None:
            product = CsvProduct(handle=handle)
            products[handle] = product
            order.append(handle)
            last_option_names[handle] = []
        product.source_rows.append(index)

        title = _cell(row, mapping, 'title')
        if title and not product.title:
            product.title = title
        description = _cell(row, mapping, 'description')
        if description and not product.description:
            product.description = description
        vendor = _cell(row, mapping, 'vendor')
        if vendor and not product.vendor:
            product.vendor = vendor
        product_type = _cell(row, mapping, 'product_type')
        if product_type and not product.product_type:
            product.product_type = product_type

        image = _cell(row, mapping, 'image')
        if image and _is_http_url(image) and image not in product.gallery:
            product.gallery.append(image)

        named_options: list[tuple[str | None, str]] = []
        for option_index in (1, 2, 3):
            name = _cell(row, mapping, f'option{option_index}_name') or None
            value = _cell(row, mapping, f'option{option_index}_value')
            if value:
                named_options.append((name, value))
        options: dict[str, str] = {}
        carried = last_option_names.get(handle) or []
        for option_index, (name, value) in enumerate(named_options):
            label = name
            if not label and option_index < len(carried):
                label = carried[option_index]
            if not label:
                label = f'Option{option_index + 1}'
            options[label] = value
        if any(name for name, _value in named_options):
            last_option_names[handle] = [
                name or (carried[index] if index < len(carried) else f'Option{index + 1}')
                for index, (name, _value) in enumerate(named_options)
            ]

        sku = _cell(row, mapping, 'sku') or None
        price = _cell(row, mapping, 'price') or None
        compare_at = _cell(row, mapping, 'compare_at') or None
        variant_image = _cell(row, mapping, 'variant_image')
        if variant_image and not _is_http_url(variant_image):
            variant_image = ''

        has_variant_signal = bool(options or sku or price or compare_at or variant_image)
        # Image-only continuation rows carry gallery media for the same handle.
        if not has_variant_signal and not title:
            continue
        product.variants.append(CsvVariant(
            sku=sku,
            price=price,
            compare_at=compare_at,
            options=options,
            image_url=variant_image or None,
            source_row=index,
        ))

    result: list[CsvProduct] = []
    for handle in order:
        product = products[handle]
        if not product.title and product.variants:
            product.title = handle
        if not product.source_handles:
            product.source_handles = [handle]
        if product.title or product.variants or product.gallery:
            result.append(product)
    return merge_csv_products_by_title(result)


def _title_key(title: str) -> str:
    return re.sub(r'\s+', ' ', (title or '').strip().lower())


def _slug_handle(title: str, fallback: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', (title or '').strip().lower()).strip('-')
    return (slug[:80] or fallback or 'product').strip('-')


def merge_csv_products_by_title(products: list[CsvProduct]) -> list[CsvProduct]:
    """Merge CSV products that share the same title into one Color/Size listing.

    Louis Vuitton exports often use one handle per colorway (…-nvprod…). Shopify
    should get one product per title with Color + Size variants instead.
    """
    groups: dict[str, list[CsvProduct]] = {}
    order: list[str] = []
    for product in products:
        key = _title_key(product.title) or product.handle
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(product)

    merged: list[CsvProduct] = []
    for key in order:
        items = groups[key]
        if len(items) == 1:
            merged.append(items[0])
            continue
        primary = items[0]
        combined = CsvProduct(
            handle=_slug_handle(primary.title, primary.handle),
            title=primary.title,
            description=next((item.description for item in items if item.description), ''),
            vendor=next((item.vendor for item in items if item.vendor), ''),
            product_type=next((item.product_type for item in items if item.product_type), ''),
        )
        seen_gallery: set[str] = set()
        seen_option_keys: set[tuple] = set()
        seen_handles: set[str] = set()
        color_owner: dict[str, str] = {}
        for item in items:
            for value in item.ledger_handles:
                if value in seen_handles or value == combined.handle:
                    continue
                seen_handles.add(value)
                combined.source_handles.append(value)
            combined.source_rows.extend(item.source_rows)
            for url in item.gallery:
                if url not in seen_gallery:
                    seen_gallery.add(url)
                    combined.gallery.append(url)
            for variant in item.variants:
                options = _disambiguate_color_options(
                    dict(variant.options or {}),
                    source_handle=item.handle,
                    color_owner=color_owner,
                )
                option_key = tuple(sorted(options.items()))
                if option_key in seen_option_keys:
                    continue
                seen_option_keys.add(option_key)
                combined.variants.append(CsvVariant(
                    sku=variant.sku,
                    price=variant.price,
                    compare_at=variant.compare_at,
                    options=options,
                    image_url=variant.image_url,
                    source_row=variant.source_row,
                ))
        combined.source_rows = sorted(set(combined.source_rows))
        # Shopify classic variant cap; expanded catalogs can go higher but stay safe.
        if len(combined.variants) > 100:
            logger.info(
                'Title merge kept separate for %r (%s variants > 100)',
                primary.title, len(combined.variants),
            )
            merged.extend(items)
            continue
        merged.append(combined)
    return merged


def _disambiguate_color_options(
    options: dict[str, str],
    *,
    source_handle: str,
    color_owner: dict[str, str],
) -> dict[str, str]:
    """Keep Color+Size unique across merged colorways (e.g. two LV 'Beige' handles)."""
    color_key = next(
        (name for name in options if name.lower() in ('color', 'colour')),
        None,
    )
    if not color_key:
        return options
    color_val = (options.get(color_key) or '').strip()
    if not color_val:
        return options
    owner = color_owner.get(color_val.lower())
    if owner is None:
        color_owner[color_val.lower()] = source_handle
        return options
    if owner == source_handle:
        return options
    tag = source_handle.rsplit('-', 1)[-1][:14]
    options[color_key] = f'{color_val} ({tag})'
    # Claim the disambiguated label too so later collisions stay unique.
    color_owner[options[color_key].lower()] = source_handle
    return options


def _ledger_path(artifacts_root: Path, shop: str) -> Path:
    safe = re.sub(r'[^a-z0-9.-]+', '_', shop.lower())
    return Path(artifacts_root) / 'secrets' / f'shopify-csv-import-{safe}.json'


def ledger_get(artifacts_root: Path, shop: str, handle: str) -> dict | None:
    path = _ledger_path(artifacts_root, shop)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get('shop') != shop:
        return None
    item = (data.get('products') or {}).get(handle.lower())
    return item if isinstance(item, dict) else None


def ledger_put(artifacts_root: Path, shop: str, handle: str, payload: dict) -> None:
    path = _ledger_path(artifacts_root, shop)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LEDGER_LOCK:
        data = {'shop': shop, 'products': {}}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding='utf-8'))
                if loaded.get('shop') == shop and isinstance(loaded.get('products'), dict):
                    data = loaded
            except (OSError, json.JSONDecodeError):
                data = {'shop': shop, 'products': {}}
        entry = dict(payload)
        entry['updated_at'] = _utc_now()
        data.setdefault('products', {})[handle.lower()] = entry
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
        temporary.replace(path)


def admin_product_url(shop: str, product_gid: str) -> str:
    digits = re.sub(r'\D', '', product_gid.split('/')[-1])
    return f'https://{shop}/admin/products/{digits}' if digits else f'https://{shop}/admin/products'


def _description_html(text: str) -> str:
    raw = (text or '').strip()
    if not raw:
        return ''
    if '<' in raw and '>' in raw:
        return raw
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    return '\n'.join(f'<p>{html.escape(line)}</p>' for line in lines)


def _money(value: str | None) -> str:
    text = (value or '').strip()
    return text or '0.00'


def is_network_image_error(exc: BaseException | str) -> bool:
    if isinstance(exc, CsvNetworkError):
        return True
    text = str(exc).lower()
    markers = (
        'connecterror',
        'readerror',
        'timeouterror',
        'connecttimeout',
        'readtimeout',
        'remoteprotocolerror',
        'getaddrinfo failed',
        'name or service not known',
        'temporary failure in name resolution',
        'all connection attempts failed',
        'network is unreachable',
        'connection reset',
        'server disconnected',
    )
    return any(marker in text for marker in markers)


# LV (and similar CDNs) often 403 Chrome-like Accept/Referer bundles, but allow a
# plain User-Agent. Try minimal profiles first, then fall back.
_IMAGE_HEADER_PROFILES: tuple[dict[str, str], ...] = (
    {'User-Agent': 'ProductHarbor/1.0'},
    {'User-Agent': 'Mozilla/5.0'},
    {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        ),
    },
)


async def _download_image(url: str, *, attempts: int = 4) -> tuple[bytes, str, str]:
    import asyncio

    last_exc: BaseException | None = None
    content: bytes | None = None
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        for attempt in range(1, max(1, attempts) + 1):
            headers = _IMAGE_HEADER_PROFILES[(attempt - 1) % len(_IMAGE_HEADER_PROFILES)]
            try:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                content = response.content
                break
            except (
                httpx.ConnectError,
                httpx.ReadError,
                httpx.TimeoutException,
                httpx.RemoteProtocolError,
                httpx.NetworkError,
            ) as exc:
                last_exc = exc
                if attempt >= attempts:
                    raise CsvNetworkError(
                        f'Network: could not download image after {attempts} tries '
                        f'({type(exc).__name__}: {exc})'
                    ) from exc
                await asyncio.sleep(1.25 * attempt)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                last_exc = exc
                # 403 is often header/WAF related — rotate profile and retry.
                if status in (403, 408, 425, 429, 500, 502, 503, 504):
                    if attempt >= attempts:
                        if status == 403:
                            raise ShopifyUploadError(
                                'image HTTP 403 (source CDN blocked the download; '
                                'retry later or use locally hosted image URLs)'
                            ) from exc
                        raise CsvNetworkError(
                            f'Network: image host returned HTTP {status} after {attempts} tries'
                        ) from exc
                    await asyncio.sleep(1.25 * attempt)
                    continue
                raise ShopifyUploadError(f'image HTTP {status}') from exc
        else:
            raise CsvNetworkError(f'Network: could not download image ({last_exc})')
    if content is None:
        raise CsvNetworkError(f'Network: could not download image ({last_exc})')
    if len(content) > MAX_IMAGE_BYTES:
        raise ShopifyUploadError('image is larger than 20MB')
    detected = detect_image(content)
    if detected is None:
        raise ShopifyUploadError('downloaded file is not an image')
    return content, detected[0], detected[1]


async def _stage_urls(
    conn: ShopifyConnection,
    urls: list[str],
    *,
    alt: str,
    handle: str = 'product',
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    safe_handle = re.sub(r'[^a-z0-9]+', '-', (handle or 'product').lower()).strip('-')[:48] or 'product'
    with tempfile.TemporaryDirectory(prefix='productharbor-csv-') as tmp:
        root = Path(tmp)
        for index, url in enumerate(urls):
            content, ext, mime = await _download_image(url)
            digest = hashlib.sha256(content).hexdigest()[:12]
            filename = f'{safe_handle}-{index}-{digest}.{ext}'
            path = root / filename
            path.write_bytes(content)
            asset = ImageAsset(
                source_url=url,
                filename=filename,
                local_path=str(path),
                mime_type=mime,
                status='downloaded',
            )
            resource = await staged_upload_file(conn, asset)
            _file_id, cdn = await create_file_and_wait_cdn(
                conn,
                resource_url=resource,
                alt=alt,
                filename=filename,
            )
            mapping[url] = cdn
    return mapping


async def _wait_media_ready(conn: ShopifyConnection, media_ids: list[str], *, timeout: float = 90.0) -> None:
    import time as _time
    deadline = _time.monotonic() + timeout
    pending = set(media_ids)
    while pending:
        if _time.monotonic() > deadline:
            raise ShopifyUploadError('Shopify media did not become READY in time')
        ready: list[str] = []
        for media_id in list(pending):
            data = await graphql(conn, NODE_MEDIA, {'id': media_id})
            node = data.get('node') or {}
            status = node.get('fileStatus')
            cdn = ((node.get('image') or {}) or {}).get('url')
            if status == 'FAILED':
                raise ShopifyUploadError('Shopify media processing FAILED')
            if status == 'READY' and cdn:
                ready.append(media_id)
        for media_id in ready:
            pending.discard(media_id)
        if pending:
            import asyncio
            await asyncio.sleep(1.25)


async def find_existing_product(conn: ShopifyConnection, handle: str) -> dict | None:
    try:
        data = await graphql(conn, FIND_PRODUCT, {'identifier': {'handle': handle}})
    except ShopifyUploadError as exc:
        text = str(exc).lower()
        if 'productbyidentifier' in text or "doesn't exist" in text or 'undefinedfield' in text:
            return None
        raise
    product = data.get('productByIdentifier')
    return product if isinstance(product, dict) and product.get('id') else None


def _build_product_set_input(
    product: CsvProduct,
    *,
    resource_by_url: dict[str, str],
    product_id: str | None = None,
) -> dict:
    option_names: list[str] = []
    prepared: list[tuple[CsvVariant, dict[str, str]]] = []
    for variant in product.variants:
        options = dict(variant.options)
        if options:
            prepared.append((variant, options))
            for name in options:
                if name not in option_names:
                    option_names.append(name)
    if not prepared:
        prepared.append((CsvVariant(price=None, sku=None), {'Title': 'Default Title'}))
        option_names = ['Title']
    option_names = option_names[:3]

    values_by_name: dict[str, list[str]] = {name: [] for name in option_names}
    variant_inputs = []
    for variant, options in prepared:
        option_values = []
        for name in option_names:
            value = options.get(name) or 'Default'
            if value not in values_by_name[name]:
                values_by_name[name].append(value)
            option_values.append({'optionName': name, 'name': value})
        row = {
            'optionValues': option_values,
            'price': _money(variant.price),
        }
        if variant.compare_at:
            row['compareAtPrice'] = _money(variant.compare_at)
        if variant.sku:
            row['sku'] = variant.sku
        resource = resource_by_url.get(variant.image_url or '')
        if resource:
            row['file'] = {
                'originalSource': resource,
                'alt': product.title,
                'contentType': 'IMAGE',
            }
        variant_inputs.append(row)

    files = []
    seen: set[str] = set()
    for url in product.all_image_urls:
        resource = resource_by_url.get(url)
        if not resource or resource in seen:
            continue
        seen.add(resource)
        files.append({'originalSource': resource, 'alt': product.title, 'contentType': 'IMAGE'})

    payload = {
        'title': product.title,
        'handle': product.handle,
        'status': 'DRAFT',
        'productOptions': [
            {'name': name, 'values': [{'name': value} for value in values]}
            for name, values in values_by_name.items()
            if values
        ],
        'variants': variant_inputs,
    }
    if product_id:
        payload['id'] = product_id
    description = _description_html(product.description)
    if description:
        payload['descriptionHtml'] = description
    if product.vendor:
        payload['vendor'] = product.vendor
    if product.product_type:
        payload['productType'] = product.product_type
    if files:
        payload['files'] = files
    return payload


async def _attach_images_to_existing(
    conn: ShopifyConnection,
    *,
    product_id: str,
    resource_by_url: dict[str, str],
    alt: str,
) -> list[str]:
    media_input = [
        {
            'originalSource': resource,
            'alt': alt,
            'mediaContentType': 'IMAGE',
        }
        for resource in dict.fromkeys(resource_by_url.values())
    ]
    if not media_input:
        return []
    data = await graphql(conn, CREATE_MEDIA, {'productId': product_id, 'media': media_input})
    body = data.get('productCreateMedia') or {}
    errors = body.get('mediaUserErrors') or []
    if errors:
        raise ShopifyUploadError('; '.join(str(item.get('message') or item) for item in errors))
    media_ids = []
    for node in body.get('media') or []:
        if isinstance(node, dict) and node.get('id'):
            media_ids.append(str(node['id']))
    await _wait_media_ready(conn, media_ids)
    return media_ids


def _ledger_put_all(
    artifacts_root: Path,
    shop: str,
    product: CsvProduct,
    payload: dict,
) -> None:
    for handle in product.ledger_handles:
        entry = dict(payload)
        entry['source_handles'] = list(product.ledger_handles)
        ledger_put(artifacts_root, shop, handle, entry)


async def import_csv_product(
    conn: ShopifyConnection,
    product: CsvProduct,
    *,
    artifacts_root: Path,
) -> dict:
    """Import one CSV product. Never creates a duplicate for an already-saved handle."""
    handle = product.handle
    prior = None
    for candidate in product.ledger_handles:
        saved = ledger_get(artifacts_root, conn.shop, candidate)
        if saved and saved.get('status') == 'imported' and saved.get('shopify_id'):
            prior = saved
            break
    if prior and prior.get('status') == 'imported' and prior.get('shopify_id'):
        _ledger_put_all(artifacts_root, conn.shop, product, {
            'status': 'imported',
            'shopify_id': prior.get('shopify_id'),
            'admin_url': prior.get('admin_url'),
            'mode': 'already_present',
            'source_rows': list(product.source_rows),
        })
        return {
            'status': 'skipped',
            'reason': 'Already imported earlier for this store.',
            'handle': handle,
            'shopify_id': prior.get('shopify_id'),
            'admin_url': prior.get('admin_url'),
            'source_rows': list(product.source_rows),
        }

    if not product.all_image_urls:
        reason = 'No image URL in the CSV for this product.'
        _ledger_put_all(artifacts_root, conn.shop, product, {
            'status': 'skipped',
            'reason': reason,
            'source_rows': list(product.source_rows),
        })
        return {
            'status': 'skipped',
            'reason': reason,
            'handle': handle,
            'shopify_id': None,
            'admin_url': None,
            'source_rows': list(product.source_rows),
        }

    try:
        resources = await _stage_urls(
            conn,
            product.all_image_urls,
            alt=product.title or handle,
            handle=handle,
        )
    except ShopifyStoreAccessError:
        raise
    except CsvNetworkError as exc:
        reason = str(exc)
        _ledger_put_all(artifacts_root, conn.shop, product, {
            'status': 'skipped',
            'reason': reason,
            'skip_kind': 'network',
            'source_rows': list(product.source_rows),
        })
        return {
            'status': 'skipped',
            'reason': reason,
            'skip_kind': 'network',
            'handle': handle,
            'shopify_id': None,
            'admin_url': None,
            'source_rows': list(product.source_rows),
        }
    except Exception as exc:
        if is_file_create_access_denied(exc):
            raise ShopifyStoreAccessError(file_create_access_denied_message()) from exc
        if is_network_image_error(exc):
            reason = f'Network: image download failed ({type(exc).__name__}: {exc})'
            _ledger_put_all(artifacts_root, conn.shop, product, {
                'status': 'skipped',
                'reason': reason,
                'skip_kind': 'network',
                'source_rows': list(product.source_rows),
            })
            return {
                'status': 'skipped',
                'reason': reason,
                'skip_kind': 'network',
                'handle': handle,
                'shopify_id': None,
                'admin_url': None,
                'source_rows': list(product.source_rows),
            }
        reason = f'Image upload failed: {type(exc).__name__}: {exc}'
        _ledger_put_all(artifacts_root, conn.shop, product, {
            'status': 'skipped',
            'reason': reason,
            'skip_kind': 'image',
            'source_rows': list(product.source_rows),
        })
        return {
            'status': 'skipped',
            'reason': reason,
            'skip_kind': 'image',
            'handle': handle,
            'shopify_id': None,
            'admin_url': None,
            'source_rows': list(product.source_rows),
        }

    if len(resources) != len(product.all_image_urls):
        missing = [url for url in product.all_image_urls if url not in resources]
        reason = f'Image upload incomplete for: {missing[0]}'
        _ledger_put_all(artifacts_root, conn.shop, product, {
            'status': 'skipped',
            'reason': reason,
            'source_rows': list(product.source_rows),
        })
        return {
            'status': 'skipped',
            'reason': reason,
            'handle': handle,
            'shopify_id': None,
            'admin_url': None,
            'source_rows': list(product.source_rows),
        }

    existing = None
    for candidate in product.ledger_handles:
        existing = await find_existing_product(conn, candidate)
        if existing and existing.get('id'):
            break

    try:
        if existing and existing.get('id'):
            media_nodes = ((existing.get('media') or {}).get('nodes') or [])
            product_id = str(existing['id'])
            if not media_nodes:
                await _attach_images_to_existing(
                    conn,
                    product_id=product_id,
                    resource_by_url=resources,
                    alt=product.title or handle,
                )
                admin_url = admin_product_url(conn.shop, product_id)
                _ledger_put_all(artifacts_root, conn.shop, product, {
                    'status': 'imported',
                    'shopify_id': product_id,
                    'admin_url': admin_url,
                    'mode': 'attached_images',
                    'source_rows': list(product.source_rows),
                })
                return {
                    'status': 'imported',
                    'reason': 'Attached images to the existing product that had none.',
                    'handle': handle,
                    'shopify_id': product_id,
                    'admin_url': admin_url,
                    'source_rows': list(product.source_rows),
                }
            admin_url = admin_product_url(conn.shop, product_id)
            _ledger_put_all(artifacts_root, conn.shop, product, {
                'status': 'imported',
                'shopify_id': product_id,
                'admin_url': admin_url,
                'mode': 'already_present',
                'source_rows': list(product.source_rows),
            })
            return {
                'status': 'skipped',
                'reason': 'Product already exists in Shopify with images.',
                'handle': handle,
                'shopify_id': product_id,
                'admin_url': admin_url,
                'source_rows': list(product.source_rows),
            }

        payload = _build_product_set_input(product, resource_by_url=resources)
        data = await graphql(conn, PRODUCT_SET, {'input': payload, 'synchronous': True})
        body = data.get('productSet') or {}
        user_errors = body.get('userErrors') or []
        if user_errors:
            raise ShopifyUploadError('; '.join(str(item.get('message') or item) for item in user_errors))
        created = body.get('product') or {}
        product_id = created.get('id')
        if not product_id:
            raise ShopifyUploadError('Shopify did not return a product id.')
        admin_url = admin_product_url(conn.shop, str(product_id))
        _ledger_put_all(artifacts_root, conn.shop, product, {
            'status': 'imported',
            'shopify_id': str(product_id),
            'admin_url': admin_url,
            'mode': 'created',
            'source_rows': list(product.source_rows),
        })
        return {
            'status': 'imported',
            'reason': (
                'Created draft product with gallery and variant images'
                + (
                    f' (merged {len(product.source_handles)} colorways by title).'
                    if len(product.source_handles) > 1 else '.'
                )
            ),
            'handle': handle,
            'shopify_id': str(product_id),
            'admin_url': admin_url,
            'source_rows': list(product.source_rows),
        }
    except Exception as exc:
        reason = f'Shopify write failed: {type(exc).__name__}: {exc}'
        _ledger_put_all(artifacts_root, conn.shop, product, {
            'status': 'skipped',
            'reason': reason,
            'source_rows': list(product.source_rows),
        })
        return {
            'status': 'skipped',
            'reason': reason,
            'handle': handle,
            'shopify_id': None,
            'admin_url': None,
            'source_rows': list(product.source_rows),
        }


def csv_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]
