"""Shopify Admin GraphQL staged upload + fileCreate for Product Harbor images."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from .image_assets import ImageAsset, save_asset_checkpoint
from .shopify_connection import ShopifyConnection

STAGED_UPLOADS_CREATE = """
mutation stagedUploadsCreate($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets {
      url
      resourceUrl
      parameters { name value }
    }
    userErrors { field message }
  }
}
"""

FILE_CREATE = """
mutation fileCreate($files: [FileCreateInput!]!) {
  fileCreate(files: $files) {
    files {
      id
      fileStatus
      alt
      ... on MediaImage {
        image { url }
      }
    }
    userErrors { field message }
  }
}
"""

NODE_MEDIA = """
query mediaImageNode($id: ID!) {
  node(id: $id) {
    ... on MediaImage {
      id
      fileStatus
      image { url }
    }
  }
}
"""

# Shopify Files API common image size guidance (bytes). Soft cap for personal tool.
MAX_IMAGE_BYTES = 20 * 1024 * 1024


class ShopifyUploadError(RuntimeError):
    pass


async def graphql(conn: ShopifyConnection, query: str, variables: dict | None = None) -> dict:
    headers = {
        'Content-Type': 'application/json',
        'X-Shopify-Access-Token': conn.access_token,
    }
    payload = {'query': query, 'variables': variables or {}}
    async with httpx.AsyncClient(timeout=60.0) as client:
        for attempt in range(5):
            response = await client.post(conn.graphql_url, headers=headers, json=payload)
            if response.status_code == 429:
                wait = float(response.headers.get('Retry-After') or (1.5 * (attempt + 1)))
                await asyncio.sleep(wait)
                continue
            if response.status_code >= 500:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            response.raise_for_status()
            data = response.json()
            if data.get('errors'):
                # Never include access token in error text
                messages = '; '.join(
                    str(err.get('message') or err) for err in data['errors']
                )
                raise ShopifyUploadError(f'GraphQL error: {messages}')
            return data.get('data') or {}
    raise ShopifyUploadError('Shopify GraphQL rate-limited or unavailable after retries')


async def staged_upload_file(conn: ShopifyConnection, asset: ImageAsset) -> str:
    """Upload local image bytes via stagedUploadsCreate; return resourceUrl."""
    if not asset.local_path or not asset.filename:
        raise ShopifyUploadError('missing local image path')
    path = Path(asset.local_path)
    if not path.is_file():
        raise ShopifyUploadError('local image file missing')
    size = path.stat().st_size
    if size <= 0 or size > MAX_IMAGE_BYTES:
        raise ShopifyUploadError(f'image size out of range ({size} bytes)')
    mime = asset.mime_type or 'image/jpeg'
    data = await graphql(conn, STAGED_UPLOADS_CREATE, {
        'input': [{
            'filename': asset.filename,
            'mimeType': mime,
            'httpMethod': 'POST',
            'resource': 'IMAGE',
            'fileSize': str(size),
        }],
    })
    payload = data.get('stagedUploadsCreate') or {}
    errors = payload.get('userErrors') or []
    if errors:
        raise ShopifyUploadError('; '.join(e.get('message') or str(e) for e in errors))
    targets = payload.get('stagedTargets') or []
    if not targets:
        raise ShopifyUploadError('no staged upload target returned')
    target = targets[0]
    upload_url = target.get('url')
    resource_url = target.get('resourceUrl')
    parameters = {p['name']: p['value'] for p in (target.get('parameters') or []) if 'name' in p}
    if not upload_url or not resource_url:
        raise ShopifyUploadError('staged upload missing url/resourceUrl')

    form = {**parameters}
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        with path.open('rb') as handle:
            files = {'file': (asset.filename, handle, mime)}
            for attempt in range(4):
                handle.seek(0)
                response = await client.post(upload_url, data=form, files=files)
                if response.status_code in (429, 500, 502, 503, 504):
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if response.status_code >= 400:
                    raise ShopifyUploadError(f'staged upload HTTP {response.status_code}')
                break
            else:
                raise ShopifyUploadError('staged upload failed after retries')
    return resource_url


async def create_file_and_wait_cdn(
    conn: ShopifyConnection,
    *,
    resource_url: str,
    alt: str = '',
    poll_timeout: float = 90.0,
) -> tuple[str, str]:
    """fileCreate then poll until READY; return (file_id, cdn_url)."""
    data = await graphql(conn, FILE_CREATE, {
        'files': [{
            'originalSource': resource_url,
            'contentType': 'IMAGE',
            'alt': alt or None,
            'duplicateResolutionMode': 'REPLACE',
        }],
    })
    payload = data.get('fileCreate') or {}
    errors = payload.get('userErrors') or []
    if errors:
        raise ShopifyUploadError('; '.join(e.get('message') or str(e) for e in errors))
    files = payload.get('files') or []
    if not files:
        raise ShopifyUploadError('fileCreate returned no files')
    file_node = files[0]
    file_id = file_node.get('id')
    if not file_id:
        raise ShopifyUploadError('fileCreate missing file id')

    import time as _time
    deadline = _time.monotonic() + poll_timeout
    status = file_node.get('fileStatus')
    cdn = ((file_node.get('image') or {}) or {}).get('url')
    while status in (None, 'UPLOADED', 'PROCESSING'):
        if _time.monotonic() > deadline:
            raise ShopifyUploadError(f'image processing timeout (status={status})')
        await asyncio.sleep(1.25)
        node_data = await graphql(conn, NODE_MEDIA, {'id': file_id})
        node = node_data.get('node') or {}
        status = node.get('fileStatus')
        cdn = ((node.get('image') or {}) or {}).get('url')
        if status == 'FAILED':
            raise ShopifyUploadError('Shopify media processing FAILED')
    if status != 'READY' or not cdn or not str(cdn).startswith('https://'):
        raise ShopifyUploadError(f'CDN URL unavailable (status={status})')
    return file_id, str(cdn)


async def upload_assets_to_shopify(
    conn: ShopifyConnection,
    assets: list[ImageAsset],
    *,
    concurrency: int = 2,
    cancel_event: asyncio.Event | None = None,
    on_progress=None,
    checkpoint_path: Path | None = None,
    mapping_path: Path | None = None,
) -> dict:
    """Upload downloaded images; skip assets that already have CDN for this store mapping."""
    prior_by_hash: dict[str, dict] = {}
    prior_by_pixel: dict[str, dict] = {}
    uploads_reused = 0
    if mapping_path and mapping_path.is_file():
        prior = json.loads(mapping_path.read_text(encoding='utf-8'))
        if prior.get('shop') == conn.shop:
            for item in prior.get('mappings') or []:
                if item.get('shopify_cdn_url') and item.get('content_hash'):
                    prior_by_hash[item['content_hash']] = item
                if item.get('shopify_cdn_url') and item.get('pixel_hash'):
                    prior_by_pixel[item['pixel_hash']] = item

    for asset in assets:
        if asset.status != 'downloaded':
            continue
        prior = None
        if asset.content_hash and asset.content_hash in prior_by_hash:
            prior = prior_by_hash[asset.content_hash]
        elif asset.pixel_hash and asset.pixel_hash in prior_by_pixel:
            prior = prior_by_pixel[asset.pixel_hash]
        if prior:
            asset.shopify_cdn_url = prior.get('shopify_cdn_url')
            asset.shopify_file_id = prior.get('shopify_file_id')
            uploads_reused += 1

    # Within this run, reuse CDN when assets share the same local file / pixel identity.
    by_path: dict[str, ImageAsset] = {}
    for asset in assets:
        if asset.status != 'downloaded' or not asset.local_path or not asset.shopify_cdn_url:
            continue
        by_path.setdefault(asset.local_path, asset)
    for asset in assets:
        if asset.status != 'downloaded' or asset.shopify_cdn_url or not asset.local_path:
            continue
        donor = by_path.get(asset.local_path)
        if donor and donor.shopify_cdn_url:
            asset.shopify_cdn_url = donor.shopify_cdn_url
            asset.shopify_file_id = donor.shopify_file_id
            uploads_reused += 1

    semaphore = asyncio.Semaphore(max(1, concurrency))
    uploaded = sum(1 for a in assets if a.shopify_cdn_url)
    failed = 0

    async def one(asset: ImageAsset) -> None:
        nonlocal uploaded, failed, uploads_reused
        if cancel_event is not None and cancel_event.is_set():
            return
        if asset.status != 'downloaded':
            return
        if asset.shopify_cdn_url:
            return
        # Another worker may have finished the shared file while we waited.
        if asset.local_path and asset.local_path in by_path and by_path[asset.local_path].shopify_cdn_url:
            donor = by_path[asset.local_path]
            asset.shopify_cdn_url = donor.shopify_cdn_url
            asset.shopify_file_id = donor.shopify_file_id
            uploads_reused += 1
            uploaded += 1
            return
        async with semaphore:
            if cancel_event is not None and cancel_event.is_set():
                return
            if asset.shopify_cdn_url:
                return
            try:
                alt = asset.associations[0].alt if asset.associations else ''
                resource_url = await staged_upload_file(conn, asset)
                file_id, cdn = await create_file_and_wait_cdn(conn, resource_url=resource_url, alt=alt)
                asset.shopify_file_id = file_id
                asset.shopify_cdn_url = cdn
                if asset.local_path:
                    by_path[asset.local_path] = asset
                uploaded += 1
            except Exception as exc:
                asset.error = f'upload: {type(exc).__name__}: {exc}'[:300]
                failed += 1
            if on_progress is not None:
                on_progress({
                    'total': sum(1 for a in assets if a.status == 'downloaded'),
                    'uploaded': uploaded,
                    'failed': failed,
                    'uploads_reused': uploads_reused,
                    'shop': conn.shop,
                })
            if checkpoint_path is not None:
                save_asset_checkpoint(assets, checkpoint_path)
            if mapping_path is not None:
                save_store_mapping(conn.shop, assets, mapping_path)

    await asyncio.gather(*(one(a) for a in assets if a.status == 'downloaded' and not a.shopify_cdn_url))
    if checkpoint_path is not None:
        save_asset_checkpoint(assets, checkpoint_path)
    if mapping_path is not None:
        save_store_mapping(conn.shop, assets, mapping_path)
    return {
        'shop': conn.shop,
        'total': sum(1 for a in assets if a.status == 'downloaded'),
        'uploaded': sum(1 for a in assets if a.shopify_cdn_url),
        'failed': sum(
            1 for a in assets
            if a.status == 'downloaded' and not a.shopify_cdn_url and a.error and a.error.startswith('upload:')
        ),
        'uploads_reused': uploads_reused,
    }


def save_store_mapping(shop: str, assets: list[ImageAsset], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'shop': shop,
        'mappings': [
            {
                'content_hash': a.content_hash,
                'pixel_hash': a.pixel_hash,
                'source_url': a.source_url,
                'shopify_cdn_url': a.shopify_cdn_url,
                'shopify_file_id': a.shopify_file_id,
                'filename': a.filename,
            }
            for a in assets
            if a.content_hash and a.shopify_cdn_url
        ],
    }
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    temporary.replace(path)
