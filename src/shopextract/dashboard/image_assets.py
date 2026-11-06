"""Collect, download, validate, and deduplicate product/variant images for Product Harbor."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .shopify_csv import slugify_handle

IMAGE_MAGIC = (
    (b'\xff\xd8\xff', 'jpg', 'image/jpeg'),
    (b'\x89PNG\r\n\x1a\n', 'png', 'image/png'),
    (b'GIF87a', 'gif', 'image/gif'),
    (b'GIF89a', 'gif', 'image/gif'),
    (b'RIFF', 'webp', 'image/webp'),
)
FORBIDDEN_SCHEMES = ('file', 'data', 'blob')
HTML_MARKERS = (b'<!doctype html', b'<html', b'<head', b'<body', b'access denied')

# Strip only analytics noise from image URLs. Keep signed params and size transforms.
_IMAGE_TRACKING_KEYS = frozenset({
    'gclid', 'fbclid', 'mc_cid', 'mc_eid', 'msclkid', '_ga', '_gl', 'dclid',
})
_IMAGE_TRACKING_PREFIXES = ('utm_', 'mtm_', 'pk_')
# Never strip these — signed / CDN auth / meaningful transforms.
_IMAGE_PRESERVE_PREFIXES = (
    'x-amz-', 'x-goog-', 'x-amz-security-', 'signature', 'expires', 'token',
    'policy', 'key-pair-id', 'sig', 'hw', 'w', 'h', 'width', 'height',
)


@dataclass
class ImageAssociation:
    product_index: int
    product_handle: str
    product_title: str
    role: str  # gallery | variant
    position: int | None = None
    variant_sku: str | None = None
    variant_title: str | None = None
    alt: str = ''


@dataclass
class ImageAsset:
    source_url: str
    associations: list[ImageAssociation] = field(default_factory=list)
    alternate_urls: list[str] = field(default_factory=list)
    normalized_url: str | None = None
    content_hash: str | None = None
    pixel_hash: str | None = None
    filename: str | None = None
    local_path: str | None = None
    mime_type: str | None = None
    byte_size: int | None = None
    status: str = 'pending'  # pending | downloaded | failed | skipped
    error: str | None = None
    shopify_cdn_url: str | None = None
    shopify_file_id: str | None = None
    reused_file: bool = False


@dataclass
class ImageDedupeStats:
    unique_downloaded_images: int = 0
    repeated_image_references_removed: int = 0
    files_reused: int = 0
    uploads_reused: int = 0
    failures: int = 0
    source_urls_in: int = 0
    unique_source_urls: int = 0

    def as_dict(self) -> dict:
        return {
            'unique_downloaded_images': self.unique_downloaded_images,
            'repeated_image_references_removed': self.repeated_image_references_removed,
            'files_reused': self.files_reused,
            'uploads_reused': self.uploads_reused,
            'failures': self.failures,
            'source_urls_in': self.source_urls_in,
            'unique_source_urls': self.unique_source_urls,
        }


def _is_http_url(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme.lower() in FORBIDDEN_SCHEMES:
        return False
    if parts.scheme.lower() not in ('http', 'https'):
        return False
    host = (parts.hostname or '').lower()
    if host in ('localhost', '127.0.0.1', '::1') or host.endswith('.local'):
        return False
    return True


def normalize_image_url(url: str) -> str:
    """Conservative image URL key: drop fragment + tracking only; keep signed/transform params."""
    value = (url or '').strip()
    if not value or not _is_http_url(value):
        return ''
    parts = urlsplit(value)
    query = []
    for key, val in parse_qsl(parts.query, keep_blank_values=True):
        lower = key.lower()
        if any(lower.startswith(p) for p in _IMAGE_PRESERVE_PREFIXES) or lower in {
            'signature', 'expires', 'token', 'policy', 'width', 'height', 'w', 'h',
        }:
            query.append((key, val))
            continue
        if lower in _IMAGE_TRACKING_KEYS or any(lower.startswith(p) for p in _IMAGE_TRACKING_PREFIXES):
            continue
        query.append((key, val))
    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path,
        urlencode(query, doseq=True),
        '',
    ))


def detect_image(content: bytes) -> tuple[str, str] | None:
    if not content or len(content) < 12:
        return None
    head = content[:64].lower()
    if any(marker in head for marker in HTML_MARKERS):
        return None
    if content.startswith(b'RIFF') and content[8:12] == b'WEBP':
        return 'webp', 'image/webp'
    for magic, ext, mime in IMAGE_MAGIC:
        if magic != b'RIFF' and content.startswith(magic):
            return ext, mime
    return None


def pixel_fingerprint(content: bytes) -> str | None:
    """Exact decoded-pixel identity: dimensions + SHA-256 of RGBA bytes. Not perceptual."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            rgba = image.convert('RGBA')
            width, height = rgba.size
            digest = hashlib.sha256(rgba.tobytes()).hexdigest()
            return f'{width}x{height}:{digest}'
    except Exception:
        return None


def _safe_stem(value: str) -> str:
    text = re.sub(r'[^A-Za-z0-9._-]+', '-', (value or 'image').strip())
    text = re.sub(r'-{2,}', '-', text).strip('-._')
    return (text or 'image')[:80]


def deterministic_filename(asset: ImageAsset, *, ext: str, used: set[str]) -> str:
    digest = (asset.content_hash or hashlib.sha256(asset.source_url.encode()).hexdigest())[:12]
    assoc = asset.associations[0] if asset.associations else None
    handle = _safe_stem(assoc.product_handle if assoc else 'image')
    role = assoc.role if assoc else 'image'
    position = assoc.position if assoc and assoc.position is not None else 0
    base = f'{handle}-{role}-{position}-{digest}.{ext}'
    name = base
    counter = 2
    while name in used:
        name = f'{handle}-{role}-{position}-{digest}-{counter}.{ext}'
        counter += 1
    used.add(name)
    return name


def dedupe_gallery_urls(urls: list[str]) -> tuple[list[str], int]:
    """Keep first gallery occurrence; collapse normalized-URL repeats within one product."""
    ordered: list[str] = []
    seen_norm: set[str] = set()
    removed = 0
    for url in urls:
        value = (url or '').strip()
        if not value or not _is_http_url(value):
            continue
        norm = normalize_image_url(value) or value
        if norm in seen_norm:
            removed += 1
            continue
        seen_norm.add(norm)
        ordered.append(value)
    return ordered, removed


def collect_image_assets(products: list[dict]) -> tuple[list[ImageAsset], ImageDedupeStats]:
    """Build assets keyed by normalized URL; preserve all associations + alternate refs."""
    stats = ImageDedupeStats()
    by_norm: dict[str, ImageAsset] = {}
    source_count = 0

    def add(url: str, association: ImageAssociation) -> None:
        nonlocal source_count
        url = (url or '').strip()
        if not url or not _is_http_url(url):
            return
        source_count += 1
        norm = normalize_image_url(url) or url
        asset = by_norm.get(norm)
        if asset is None:
            asset = ImageAsset(source_url=url, normalized_url=norm)
            by_norm[norm] = asset
        else:
            if url != asset.source_url and url not in asset.alternate_urls:
                asset.alternate_urls.append(url)
        asset.associations.append(association)

    for index, product in enumerate(products):
        title = str(product.get('title') or '').strip() or f'product-{index + 1}'
        handle = slugify_handle(title, fallback=f'product-{index + 1}')
        gallery_raw: list[str] = []
        primary = product.get('image_url')
        if isinstance(primary, str) and primary.strip():
            gallery_raw.append(primary.strip())
        for extra in product.get('additional_images') or []:
            if isinstance(extra, str) and extra.strip():
                gallery_raw.append(extra.strip())
        gallery, removed = dedupe_gallery_urls(gallery_raw)
        stats.repeated_image_references_removed += removed
        # Write back per-product gallery so exports stay consistent before download.
        if gallery:
            product['image_url'] = gallery[0]
            product['additional_images'] = gallery[1:]
        else:
            product['image_url'] = product.get('image_url') if isinstance(product.get('image_url'), str) else ''
            product['additional_images'] = []
        for position, url in enumerate(gallery, start=1):
            add(url, ImageAssociation(
                product_index=index,
                product_handle=handle,
                product_title=title,
                role='gallery',
                position=position,
                alt=title,
            ))
        for variant in product.get('variants') or []:
            if not isinstance(variant, dict):
                continue
            v_image = variant.get('featured_image')
            if not isinstance(v_image, str) or not v_image.strip():
                continue
            add(v_image.strip(), ImageAssociation(
                product_index=index,
                product_handle=handle,
                product_title=title,
                role='variant',
                variant_sku=variant.get('sku') if isinstance(variant.get('sku'), str) else None,
                variant_title=variant.get('title') if isinstance(variant.get('title'), str) else None,
                alt=str(variant.get('title') or title),
            ))

    stats.source_urls_in = source_count
    stats.unique_source_urls = len(by_norm)
    return list(by_norm.values()), stats


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_bytes(content)
    temporary.replace(path)


async def download_assets(
    assets: list[ImageAsset],
    images_dir: Path,
    *,
    concurrency: int = 4,
    timeout: float = 45.0,
    cancel_event: asyncio.Event | None = None,
    on_progress=None,
    checkpoint_path: Path | None = None,
    prior_stats: ImageDedupeStats | None = None,
) -> dict:
    images_dir.mkdir(parents=True, exist_ok=True)
    used_names: set[str] = set()
    # content_hash / pixel_hash -> file record
    hash_to_file: dict[str, tuple[str, str, int, str]] = {}
    pixel_to_hash: dict[str, str] = {}
    stats = prior_stats or ImageDedupeStats()
    write_lock = asyncio.Lock()

    if checkpoint_path and checkpoint_path.is_file():
        prior = json.loads(checkpoint_path.read_text(encoding='utf-8'))
        for item in prior.get('assets') or []:
            if item.get('status') == 'downloaded' and item.get('content_hash') and item.get('local_path'):
                path = Path(item['local_path'])
                if path.is_file():
                    hash_to_file[item['content_hash']] = (
                        item['filename'], str(path), int(item.get('byte_size') or path.stat().st_size),
                        item.get('mime_type') or 'application/octet-stream',
                    )
                    used_names.add(item['filename'])
                    if item.get('pixel_hash'):
                        pixel_to_hash[item['pixel_hash']] = item['content_hash']
                    for asset in assets:
                        if asset.source_url == item.get('source_url') or (
                            item.get('normalized_url') and asset.normalized_url == item.get('normalized_url')
                        ):
                            asset.content_hash = item['content_hash']
                            asset.pixel_hash = item.get('pixel_hash')
                            asset.filename = item['filename']
                            asset.local_path = item['local_path']
                            asset.mime_type = item.get('mime_type')
                            asset.byte_size = item.get('byte_size')
                            asset.status = 'downloaded'
                            asset.shopify_cdn_url = item.get('shopify_cdn_url')
                            asset.shopify_file_id = item.get('shopify_file_id')
                            asset.alternate_urls = list(item.get('alternate_urls') or asset.alternate_urls)
                            asset.reused_file = True
                            stats.files_reused += 1

    semaphore = asyncio.Semaphore(max(1, concurrency))
    downloaded = sum(1 for a in assets if a.status == 'downloaded')
    failed = sum(1 for a in assets if a.status == 'failed')

    async def one(asset: ImageAsset) -> None:
        nonlocal downloaded, failed
        if cancel_event is not None and cancel_event.is_set():
            return
        if asset.status == 'downloaded' and asset.local_path and Path(asset.local_path).is_file():
            return
        async with semaphore:
            if cancel_event is not None and cancel_event.is_set():
                return
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
                    response = await client.get(asset.source_url)
                    response.raise_for_status()
                    content = response.content
                detected = detect_image(content)
                if detected is None:
                    asset.status = 'failed'
                    asset.error = 'not_a_supported_image_or_html_error'
                    failed += 1
                    stats.failures += 1
                    return
                ext, mime = detected
                digest = hashlib.sha256(content).hexdigest()
                pix = pixel_fingerprint(content)
                asset.content_hash = digest
                asset.pixel_hash = pix

                async with write_lock:
                    # Byte-identical reuse
                    if digest in hash_to_file:
                        filename, local_path, size, known_mime = hash_to_file[digest]
                        asset.filename = filename
                        asset.local_path = local_path
                        asset.byte_size = size
                        asset.mime_type = known_mime
                        asset.status = 'downloaded'
                        asset.reused_file = True
                        stats.files_reused += 1
                        downloaded += 1
                    # Exact same decoded pixels (different encoding/metadata)
                    elif pix and pix in pixel_to_hash and pixel_to_hash[pix] in hash_to_file:
                        prior_digest = pixel_to_hash[pix]
                        filename, local_path, size, known_mime = hash_to_file[prior_digest]
                        asset.filename = filename
                        asset.local_path = local_path
                        asset.byte_size = size
                        asset.mime_type = known_mime
                        asset.status = 'downloaded'
                        asset.reused_file = True
                        # Keep content_hash as this download's bytes for provenance,
                        # but file points at retained path; map pixel -> retained digest.
                        stats.files_reused += 1
                        downloaded += 1
                    else:
                        filename = deterministic_filename(asset, ext=ext, used=used_names)
                        path = images_dir / filename
                        _atomic_write_bytes(path, content)
                        asset.filename = filename
                        asset.local_path = str(path)
                        asset.byte_size = len(content)
                        asset.mime_type = mime
                        asset.status = 'downloaded'
                        hash_to_file[digest] = (filename, str(path), len(content), mime)
                        if pix:
                            pixel_to_hash[pix] = digest
                        downloaded += 1
            except Exception as exc:
                asset.status = 'failed'
                asset.error = f'{type(exc).__name__}: {exc}'[:300]
                failed += 1
                stats.failures += 1
            if on_progress is not None:
                on_progress({
                    'total': len(assets),
                    'downloaded': downloaded,
                    'failed': failed,
                    'pending': len(assets) - downloaded - failed,
                    'files_reused': stats.files_reused,
                })
            if checkpoint_path is not None:
                save_asset_checkpoint(assets, checkpoint_path)

    await asyncio.gather(*(one(asset) for asset in assets))
    if checkpoint_path is not None:
        save_asset_checkpoint(assets, checkpoint_path)

    unique_files = {a.local_path for a in assets if a.status == 'downloaded' and a.local_path}
    stats.unique_downloaded_images = len(unique_files)
    stats.unique_source_urls = len(assets)
    return {
        'total': len(assets),
        'downloaded': sum(1 for a in assets if a.status == 'downloaded'),
        'failed': sum(1 for a in assets if a.status == 'failed'),
        'pending': sum(1 for a in assets if a.status == 'pending'),
        'dedupe': stats.as_dict(),
    }


def content_identity(asset: ImageAsset) -> str | None:
    """Stable identity for file/upload reuse: prefer pixel hash, else content hash."""
    if asset.pixel_hash:
        return f'pixel:{asset.pixel_hash}'
    if asset.content_hash:
        return f'bytes:{asset.content_hash}'
    return None


def retained_url_for_content(assets: list[ImageAsset]) -> dict[str, str]:
    """Map any source/alternate URL -> first retained source_url for that content identity."""
    identity_to_url: dict[str, str] = {}
    url_to_identity: dict[str, str] = {}
    for asset in assets:
        if asset.status != 'downloaded':
            continue
        identity = content_identity(asset)
        if not identity:
            continue
        if identity not in identity_to_url:
            identity_to_url[identity] = asset.source_url
        url_to_identity[asset.source_url] = identity
        for alt in asset.alternate_urls:
            url_to_identity[alt] = identity
        # Also map by local path group: assets sharing path share identity
    # Remap URLs that share a local_path to the earliest source_url for that path
    path_to_url: dict[str, str] = {}
    for asset in assets:
        if asset.status != 'downloaded' or not asset.local_path:
            continue
        if asset.local_path not in path_to_url:
            path_to_url[asset.local_path] = asset.source_url
        url_to_identity[asset.source_url] = f'path:{asset.local_path}'
        for alt in asset.alternate_urls:
            url_to_identity[alt] = f'path:{asset.local_path}'
        identity_to_url.setdefault(f'path:{asset.local_path}', path_to_url[asset.local_path])

    mapping: dict[str, str] = {}
    for url, identity in url_to_identity.items():
        mapping[url] = identity_to_url.get(identity, url)
    return mapping


def apply_content_dedupe_to_products(
    products: list[dict],
    assets: list[ImageAsset],
) -> tuple[list[dict], ImageDedupeStats]:
    """Collapse gallery slots that resolve to the same downloaded content; keep variant links."""
    stats = ImageDedupeStats()
    url_map = retained_url_for_content(assets)
    rewritten = []
    for product in products:
        item = dict(product)
        gallery_raw: list[str] = []
        if isinstance(item.get('image_url'), str) and item['image_url'].strip():
            gallery_raw.append(item['image_url'].strip())
        for extra in item.get('additional_images') or []:
            if isinstance(extra, str) and extra.strip():
                gallery_raw.append(extra.strip())
        remapped = [url_map.get(u, u) for u in gallery_raw]
        gallery, removed = dedupe_gallery_urls(remapped)
        # Also collapse when different norms map to same retained URL
        final: list[str] = []
        seen: set[str] = set()
        for url in gallery:
            key = url_map.get(url, url)
            if key in seen:
                removed += 1
                continue
            seen.add(key)
            final.append(key)
        stats.repeated_image_references_removed += removed
        if final:
            item['image_url'] = final[0]
            item['additional_images'] = final[1:]
        variants = []
        for variant in item.get('variants') or []:
            if not isinstance(variant, dict):
                continue
            copy = dict(variant)
            featured = copy.get('featured_image')
            if isinstance(featured, str) and featured.strip():
                copy['featured_image'] = url_map.get(featured.strip(), featured.strip())
            variants.append(copy)
        item['variants'] = variants
        rewritten.append(item)
    unique_files = {a.local_path for a in assets if a.status == 'downloaded' and a.local_path}
    stats.unique_downloaded_images = len(unique_files)
    stats.files_reused = sum(1 for a in assets if a.reused_file)
    stats.failures = sum(1 for a in assets if a.status == 'failed')
    stats.unique_source_urls = len(assets)
    return rewritten, stats


def save_asset_checkpoint(assets: list[ImageAsset], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'assets': [
            {
                'source_url': a.source_url,
                'alternate_urls': list(a.alternate_urls),
                'normalized_url': a.normalized_url,
                'content_hash': a.content_hash,
                'pixel_hash': a.pixel_hash,
                'filename': a.filename,
                'local_path': a.local_path,
                'mime_type': a.mime_type,
                'byte_size': a.byte_size,
                'status': a.status,
                'error': a.error,
                'shopify_cdn_url': a.shopify_cdn_url,
                'shopify_file_id': a.shopify_file_id,
                'reused_file': a.reused_file,
                'associations': [asdict(x) for x in a.associations],
            }
            for a in assets
        ],
    }
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    temporary.replace(path)


def load_products_json(path: Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    products = data.get('products') if isinstance(data, dict) else data
    if not isinstance(products, list):
        raise ValueError('products.json does not contain a product list')
    return [p for p in products if isinstance(p, dict)]


def url_map_from_assets(assets: list[ImageAsset], *, prefer_cdn: bool = False) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for asset in assets:
        targets = [asset.source_url, *asset.alternate_urls]
        if prefer_cdn and asset.shopify_cdn_url:
            for url in targets:
                if url:
                    mapping[url] = asset.shopify_cdn_url
        elif asset.status == 'downloaded' and asset.source_url:
            for url in targets:
                if url:
                    mapping[url] = asset.source_url
    return mapping


def rewrite_products_urls(products: list[dict], url_map: dict[str, str]) -> list[dict]:
    rewritten = []
    for product in products:
        item = dict(product)
        if isinstance(item.get('image_url'), str) and item['image_url'] in url_map:
            item['image_url'] = url_map[item['image_url']]
        extras = []
        for url in item.get('additional_images') or []:
            if isinstance(url, str):
                extras.append(url_map.get(url, url))
        item['additional_images'] = extras
        variants = []
        for variant in product.get('variants') or []:
            if not isinstance(variant, dict):
                continue
            copy = dict(variant)
            featured = copy.get('featured_image')
            if isinstance(featured, str) and featured in url_map:
                copy['featured_image'] = url_map[featured]
            variants.append(copy)
        item['variants'] = variants
        rewritten.append(item)
    return rewritten
