"""Create one scraped product in a connected Shopify store.

Each product is uploaded on its own. A product already stored for that shop
is skipped. Images are downloaded here and staged into Shopify, because many
brand CDNs refuse Shopify's own image fetch.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from .image_assets import ImageAsset, detect_image
from .shopify_connection import ShopifyConnection, load_connection
from .shopify_files import MAX_IMAGE_BYTES, ShopifyUploadError, graphql, staged_upload_file

_LEDGER_LOCK = threading.Lock()

PRODUCT_SET = """
mutation ProductHarborProductSet($input: ProductSetInput!, $synchronous: Boolean!) {
  productSet(input: $input, synchronous: $synchronous) {
    product { id handle }
    userErrors { field message }
  }
}
"""

FIND_BY_HANDLE = """
query ProductHarborProductByHandle($identifier: ProductIdentifierInput!) {
  productByIdentifier(identifier: $identifier) {
    id
    handle
  }
}
"""


_FAMILY_ID = re.compile(r'(nvprod[a-z0-9]+)', re.I)

FAMILY_INDEX = """
query ProductHarborFamilies($cursor: String) {
  products(first: 100, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      handle
      title
      metafield(namespace: "custom", key: "source_family_id") { value }
    }
  }
}
"""


def family_id_of(record: dict) -> str | None:
    """Louis Vuitton family id. A color URL is not a separate product."""
    for key in ('family_id', 'external_id'):
        value = record.get(key)
        if isinstance(value, str) and value.lower().startswith('nvprod'):
            return value.lower()
    url = record.get('product_url') if isinstance(record.get('product_url'), str) else ''
    match = _FAMILY_ID.search(urlsplit(url).path) if url else None
    return match.group(1).lower() if match else None


def product_identity(record: dict) -> str:
    """Stable identity for 'already uploaded' checks. Family id wins over the color URL."""
    family = family_id_of(record)
    if family:
        return f'family:{family}'
    url = record.get('product_url') if isinstance(record.get('product_url'), str) else ''
    url = url.strip()
    if url:
        parts = urlsplit(url)
        if parts.scheme in ('http', 'https') and parts.netloc:
            return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, '', '')).rstrip('/').lower()
    external = str(record.get('external_id') or '').strip()
    title = str(record.get('title') or '').strip()
    token = external or title
    return f'id:{token}'.lower() if token else ''


def product_handle(title: str, identity: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', (title or 'product').lower()).strip('-')[:60]
    if identity.startswith('family:'):
        family = identity.split(':', 1)[1]
        handle = f'{slug}-{family}' if slug else family
        return handle[:80]
    digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:8]
    handle = f'{slug}-{digest}' if slug else digest
    return handle[:80]


def upload_record_from_product(product) -> dict:
    """Dict the uploader accepts, from a normalized Product."""
    family = None
    external = product.external_id if isinstance(product.external_id, str) else None
    if external and external.lower().startswith('nvprod'):
        family = external.lower()
    return {
        'title': product.title or '',
        'description': product.description or '',
        'description_html': getattr(product, 'description_html', '') or '',
        'price': str(product.price) if product.price is not None else None,
        'currency': product.currency or None,
        'sku': product.sku,
        'image_url': product.image_url or None,
        'additional_images': list(product.additional_images or []),
        'product_url': product.product_url or None,
        'external_id': external,
        'family_id': family,
        'vendor': product.vendor,
        'product_type': product.product_type,
        'option_names': list(getattr(product, 'option_names', None) or []),
        'variants': [
            {
                'title': variant.title or None,
                'sku': variant.sku,
                'price': str(variant.price) if variant.price is not None else None,
                'featured_image': variant.featured_image,
                'options': dict(variant.attributes or {}),
            }
            for variant in (product.variants or [])
        ],
    }


def _description_html(record: dict) -> str:
    source = record.get('description_html')
    if isinstance(source, str) and source.strip():
        return source
    text = record.get('description') if isinstance(record.get('description'), str) else ''
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return '\n'.join(f'<p>{html.escape(line)}</p>' for line in lines)


def _image_urls(record: dict) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def add(url) -> None:
        if not isinstance(url, str):
            return
        cleaned = url.strip()
        if not cleaned.startswith(('http://', 'https://')) or cleaned in seen:
            return
        seen.add(cleaned)
        found.append(cleaned)

    add(record.get('image_url'))
    for url in record.get('images') or []:
        add(url)
    for url in record.get('additional_images') or []:
        add(url)
    for variant in record.get('variants') or []:
        if isinstance(variant, dict):
            add(variant.get('featured_image'))
    return found[:12]


def _variant_options(variant: dict, option_names: list[str]) -> dict[str, str]:
    raw = variant.get('options') if isinstance(variant.get('options'), dict) else None
    if not raw and isinstance(variant.get('attributes'), dict):
        raw = variant.get('attributes')
    options = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if value is None or str(value).strip() == '':
                continue
            options[str(key)] = str(value)
    if options:
        return options
    title = variant.get('title') if isinstance(variant.get('title'), str) else ''
    if title.strip():
        name = option_names[0] if option_names else 'Title'
        return {name: title.strip()}
    return {}


def _money(value) -> str:
    if value is None or value == '':
        return '0.00'
    text = str(value).strip()
    return text or '0.00'


def build_product_set_input(record: dict, *, handle: str, resource_by_url: dict[str, str]) -> dict:
    """Shopify productSet input from one scraped record. Does not invent missing fields."""
    title = str(record.get('title') or '').strip()
    option_names = [name for name in (record.get('option_names') or []) if isinstance(name, str) and name.strip()]
    variants_in = [item for item in (record.get('variants') or []) if isinstance(item, dict)]
    prepared = []
    for variant in variants_in:
        options = _variant_options(variant, option_names)
        if options:
            prepared.append((variant, options))
    if not prepared:
        prepared.append((
            {
                'price': record.get('price'),
                'sku': record.get('sku') if isinstance(record.get('sku'), str) else None,
            },
            {'Title': 'Default Title'},
        ))
        option_names = ['Title']
    else:
        ordered: list[str] = []
        for name in option_names:
            if name not in ordered:
                ordered.append(name)
        for _variant, options in prepared:
            for name in options:
                if name not in ordered:
                    ordered.append(name)
        option_names = ordered[:3]

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
            'price': _money(variant.get('price') if variant.get('price') not in (None, '') else record.get('price')),
        }
        sku = variant.get('sku') if isinstance(variant.get('sku'), str) and variant.get('sku') else None
        if sku:
            row['sku'] = sku
        image = variant.get('featured_image') if isinstance(variant.get('featured_image'), str) else None
        resource = resource_by_url.get(image or '')
        if resource:
            row['file'] = {'originalSource': resource, 'alt': title, 'contentType': 'IMAGE'}
        variant_inputs.append(row)

    files = []
    seen_resources: set[str] = set()
    for url in _image_urls(record):
        resource = resource_by_url.get(url)
        if not resource or resource in seen_resources:
            continue
        seen_resources.add(resource)
        files.append({'originalSource': resource, 'alt': title, 'contentType': 'IMAGE'})

    payload = {
        'title': title,
        'handle': handle,
        'status': 'ACTIVE',
        'productOptions': [
            {'name': name, 'values': [{'name': value} for value in values]}
            for name, values in values_by_name.items()
            if values
        ],
        'variants': variant_inputs,
    }
    description = _description_html(record)
    if description:
        payload['descriptionHtml'] = description
    vendor = record.get('vendor') if isinstance(record.get('vendor'), str) and record.get('vendor').strip() else None
    if vendor:
        payload['vendor'] = vendor.strip()
    product_type = record.get('product_type') if isinstance(record.get('product_type'), str) else None
    if product_type and product_type.strip():
        payload['productType'] = product_type.strip()
    if files:
        payload['files'] = files
    family = family_id_of(record)
    if family:
        payload['metafields'] = [{
            'namespace': 'custom',
            'key': 'source_family_id',
            'type': 'single_line_text_field',
            'value': family,
        }]
    return payload


def _ledger_path(artifacts_root: Path, shop: str) -> Path:
    safe = re.sub(r'[^a-z0-9.-]+', '_', shop.lower())
    return Path(artifacts_root) / 'secrets' / f'shopify-products-{safe}.json'


def ledger_get(artifacts_root: Path, shop: str, identity: str) -> dict | None:
    path = _ledger_path(artifacts_root, shop)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get('shop') != shop:
        return None
    item = (data.get('products') or {}).get(identity)
    return item if isinstance(item, dict) else None


def ledger_put(artifacts_root: Path, shop: str, identity: str, *, shopify_id: str, handle: str) -> None:
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
        data.setdefault('products', {})[identity] = {
            'shopify_id': shopify_id,
            'handle': handle,
        }
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
        temporary.replace(path)


async def _download_image(url: str, referer: str | None) -> tuple[bytes, str, str]:
    headers = {'User-Agent': 'ProductHarbor/1.0'}
    if referer:
        headers['Referer'] = referer
    async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        content = response.content
    if len(content) > MAX_IMAGE_BYTES:
        raise ShopifyUploadError('image is larger than 20MB')
    detected = detect_image(content)
    if detected is None:
        raise ShopifyUploadError('downloaded file is not an image')
    return content, detected[0], detected[1]


async def _stage_images(conn: ShopifyConnection, record: dict) -> tuple[dict[str, str], list[str]]:
    mapping: dict[str, str] = {}
    errors: list[str] = []
    referer = record.get('product_url') if isinstance(record.get('product_url'), str) else None
    with tempfile.TemporaryDirectory(prefix='productharbor-image-') as tmp:
        root = Path(tmp)
        for index, url in enumerate(_image_urls(record)):
            try:
                content, ext, mime = await _download_image(url, referer)
                filename = f'image-{index}.{ext}'
                path = root / filename
                path.write_bytes(content)
                asset = ImageAsset(
                    source_url=url,
                    filename=filename,
                    local_path=str(path),
                    mime_type=mime,
                    status='downloaded',
                )
                mapping[url] = await staged_upload_file(conn, asset)
            except Exception as exc:
                errors.append(f'{url}: {type(exc).__name__}: {exc}')
    return mapping, errors


async def find_product_id(conn: ShopifyConnection, handle: str) -> str | None:
    try:
        data = await graphql(conn, FIND_BY_HANDLE, {'identifier': {'handle': handle}})
    except ShopifyUploadError as exc:
        text = str(exc).lower()
        if 'productbyidentifier' in text or "doesn't exist" in text or 'undefinedfield' in text:
            return None
        raise
    product = data.get('productByIdentifier') or {}
    product_id = product.get('id')
    return str(product_id) if product_id else None


class ShopifyCatalogIndex:
    """Families and handles read once from Shopify at the start of a run."""

    def __init__(self):
        self.families: dict[str, list[dict]] = {}
        self.handles: set[str] = set()
        self.titles: dict[str, int] = {}
        self.error: str | None = None

    def already_uploaded(self, record: dict) -> bool:
        family = family_id_of(record)
        if family and family in self.families:
            return True
        title = str(record.get('title') or '').strip()
        identity = product_identity(record)
        if identity and product_handle(title, identity) in self.handles:
            return True
        return False

    def duplicate_report(self) -> str:
        lines = []
        for family, products in sorted(self.families.items()):
            if len(products) < 2:
                continue
            handles = ', '.join(str(item.get('handle') or item.get('id')) for item in products)
            lines.append(f'{family} is on {len(products)} Shopify products ({handles}). Nothing was deleted.')
        for title, count in sorted(self.titles.items()):
            if count < 2:
                continue
            lines.append(
                f'Title "{title}" is on {count} Shopify products and has no family id. '
                'These may be old per-color uploads. Nothing was deleted.'
            )
        return ' '.join(lines)


def _remember_product(index: ShopifyCatalogIndex, node: dict) -> None:
    handle = str(node.get('handle') or '').strip().lower()
    title = str(node.get('title') or '').strip()
    product_id = str(node.get('id') or '')
    if handle:
        index.handles.add(handle)
    metafield = node.get('metafield') or {}
    value = metafield.get('value') if isinstance(metafield, dict) else None
    family = value.lower() if isinstance(value, str) and value.lower().startswith('nvprod') else None
    if family is None and handle:
        match = _FAMILY_ID.search(handle)
        family = match.group(1).lower() if match else None
    if family:
        index.families.setdefault(family, []).append({'id': product_id, 'handle': handle, 'title': title})
        return
    if title:
        index.titles[title] = index.titles.get(title, 0) + 1


async def list_family_index(conn: ShopifyConnection) -> ShopifyCatalogIndex:
    """Paginate the shop once. Do not call this per product."""
    index = ShopifyCatalogIndex()
    cursor = None
    for _page in range(50):
        try:
            data = await graphql(conn, FAMILY_INDEX, {'cursor': cursor})
        except ShopifyUploadError as exc:
            index.error = str(exc)
            return index
        products = data.get('products') or {}
        for node in products.get('nodes') or []:
            if isinstance(node, dict):
                _remember_product(index, node)
        page = products.get('pageInfo') or {}
        if not page.get('hasNextPage'):
            break
        cursor = page.get('endCursor')
        if not cursor:
            break
    return index


async def upload_scraped_product(
    conn: ShopifyConnection,
    record: dict,
    *,
    artifacts_root: Path,
    catalog_index: ShopifyCatalogIndex | None = None,
) -> dict:
    """Upload one complete product, or skip it when Shopify already has that family."""
    identity = product_identity(record)
    title = str(record.get('title') or '').strip()
    if not identity or not title:
        return {'status': 'failed', 'error': 'Product is missing a title or a stable identity.', 'shopify_id': None}
    if catalog_index is not None and catalog_index.already_uploaded(record):
        return {'status': 'skipped', 'error': None, 'shopify_id': None}
    handle = product_handle(title, identity)
    resources, image_errors = await _stage_images(conn, record)
    payload = build_product_set_input(record, handle=handle, resource_by_url=resources)
    data = await graphql(conn, PRODUCT_SET, {'input': payload, 'synchronous': True})
    body = data.get('productSet') or {}
    user_errors = body.get('userErrors') or []
    if user_errors:
        message = '; '.join(str(item.get('message') or item) for item in user_errors)
        return {'status': 'failed', 'error': message, 'shopify_id': None, 'image_errors': image_errors}
    product = body.get('product') or {}
    product_id = product.get('id')
    if not product_id:
        return {'status': 'failed', 'error': 'Shopify did not return a product id.', 'shopify_id': None}
    if catalog_index is not None:
        family = family_id_of(record)
        saved_handle = str(product.get('handle') or handle).lower()
        catalog_index.handles.add(saved_handle)
        if family:
            catalog_index.families.setdefault(family, []).append({
                'id': str(product_id),
                'handle': saved_handle,
                'title': title,
            })
    return {
        'status': 'uploaded',
        'error': None,
        'shopify_id': str(product_id),
        'image_errors': image_errors,
        'images_uploaded': len(resources),
    }


class LiveProductUploader:
    """Upload scraped products one at a time without blocking the scrape."""

    def __init__(self, artifacts_root: Path):
        self.artifacts_root = Path(artifacts_root)
        self.catalog_index: ShopifyCatalogIndex | None = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None

    def enqueue(self, job, record: dict) -> None:
        if not getattr(job, 'upload_to_shopify', False):
            return
        identity = product_identity(record)
        if not identity:
            job.upload_failed += 1
            job.upload_message = 'Skipped a product with no URL or title.'
            return
        if identity in job._upload_keys:
            return
        if self.catalog_index is not None and self.catalog_index.already_uploaded(record):
            job.upload_skipped += 1
            job.upload_message = f'Skipped "{record.get("title") or "product"}" — Shopify already has it.'
            job._upload_keys.add(identity)
            return
        job._upload_keys.add(identity)
        job.upload_queued += 1
        job.upload_message = f'Queued {job.upload_queued} product(s) for {self._shop_name()}.'
        self._queue.put_nowait((job, record))
        self._ensure()

    def _shop_name(self) -> str:
        conn = load_connection(self.artifacts_root)
        return conn.shop if conn is not None else 'Shopify'

    def _ensure(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            try:
                job, record = await asyncio.wait_for(self._queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                if self._queue.empty():
                    return
                continue
            try:
                await self._upload(job, record)
            finally:
                self._queue.task_done()

    async def _upload(self, job, record: dict) -> None:
        conn = load_connection(self.artifacts_root)
        title = str(record.get('title') or 'product')
        if conn is None:
            job.upload_failed += 1
            job.upload_message = f'No Shopify connection. "{title}" was not uploaded.'
            return
        try:
            result = await upload_scraped_product(
                conn, record, artifacts_root=self.artifacts_root, catalog_index=self.catalog_index,
            )
        except Exception as exc:
            job.upload_failed += 1
            job.upload_message = f'Upload failed for "{title}": {type(exc).__name__}: {exc}'
            return
        status = result.get('status')
        if status == 'uploaded':
            job.upload_done += 1
            images = result.get('images_uploaded')
            image_note = f' {images} image(s).' if images else ''
            missing = result.get('image_errors') or []
            if missing:
                image_note += f' {len(missing)} image(s) could not be fetched.'
            job.upload_message = f'Uploaded "{title}" to {conn.shop}.{image_note}'
        elif status == 'skipped':
            job.upload_skipped += 1
            job.upload_message = f'"{title}" is already in {conn.shop}. Not uploaded again.'
        else:
            job.upload_failed += 1
            job.upload_message = f'Upload failed for "{title}": {result.get("error") or "unknown error"}'
