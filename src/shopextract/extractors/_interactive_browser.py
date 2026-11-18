"""Opt-in headed Playwright collection session with a persistent profile.

Human may clear challenges in the visible browser. This module never solves
CAPTCHAs or bypasses login/access protections. If the page remains an
interstitial after the human gate, ACCESS_BLOCKED is returned.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from .._access import (
    ACCESS_BLOCKED,
    AccessBlock,
    denial_reference,
    detect_access_block,
    format_access_blocked,
    human_action_guidance,
    is_hard_denial,
)
from .._models import ExtractorResult

logger = logging.getLogger(__name__)

CONTINUE_SENTINEL = '.productharbor_continue'
DEFAULT_PROFILE_ROOT = Path('artifacts') / 'browser-profiles'

ProductHook = Callable[[dict], Awaitable[None] | None]
SnapshotHook = Callable[[bytes, str], Awaitable[None] | None]
HumanGateHook = Callable[[dict, AccessBlock | None], Awaitable[None] | None]

_product_hook: contextvars.ContextVar[ProductHook | None] = contextvars.ContextVar(
    'interactive_product_hook', default=None,
)
_snapshot_hook: contextvars.ContextVar[SnapshotHook | None] = contextvars.ContextVar(
    'interactive_snapshot_hook', default=None,
)
_human_gate_hook: contextvars.ContextVar[HumanGateHook | None] = contextvars.ContextVar(
    'interactive_human_gate_hook', default=None,
)


def set_live_hooks(
    *,
    on_product: ProductHook | None = None,
    on_snapshot: SnapshotHook | None = None,
    on_human_gate: HumanGateHook | None = None,
):
    """Install per-task live preview / human-gate hooks for the interactive session."""
    return (
        _product_hook.set(on_product),
        _snapshot_hook.set(on_snapshot),
        _human_gate_hook.set(on_human_gate),
    )


def reset_live_hooks(product_token, snapshot_token, human_token=None) -> None:
    _product_hook.reset(product_token)
    _snapshot_hook.reset(snapshot_token)
    if human_token is not None:
        _human_gate_hook.reset(human_token)


@dataclass
class InteractiveSessionResult:
    blocked: bool
    block_message: str | None = None
    collection_url: str = ''
    collection_html: str = ''
    product_urls: list[str] = field(default_factory=list)
    products: list[dict] = field(default_factory=list)
    pages_visited: int = 0
    failed_pages: int = 0
    pagination_end_confirmed: bool | None = None
    pagination_notes: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)


def default_profile_dir(url: str, root: Path | None = None) -> Path:
    host = urlsplit(url).hostname or 'unknown'
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', host)
    return (root or DEFAULT_PROFILE_ROOT) / safe


def continue_path(profile_dir: Path) -> Path:
    return Path(profile_dir) / CONTINUE_SENTINEL


def signal_continue(profile_dir: Path) -> None:
    path = continue_path(profile_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('continue\n', encoding='utf-8')


def clear_continue(profile_dir: Path) -> None:
    path = continue_path(profile_dir)
    if path.is_file():
        path.unlink()


async def _notify_human_gate(action: dict, block: AccessBlock | None) -> None:
    hook = _human_gate_hook.get()
    if hook is None:
        return
    try:
        maybe = hook(action, block)
        if asyncio.iscoroutine(maybe):
            await maybe
    except Exception as exc:
        logger.info('Interactive human-gate hook failed: %s', exc)


async def wait_for_human(
    *,
    profile_dir: Path,
    timeout: float,
    prompt: bool = True,
    block: AccessBlock | None = None,
) -> bool:
    """Wait until the operator signals readiness (sentinel file or stdin Enter)."""
    clear_continue(profile_dir)
    action = human_action_guidance(block.reason if block is not None else None)
    await _notify_human_gate(action, block)
    if prompt:
        steps = '\n'.join(f'  {index}. {step}' for index, step in enumerate(action['steps'], start=1))
        print(
            f"\nInteractive browser is open — {action['title']}\n"
            f'{steps}\n'
            'ProductHarbor will not solve CAPTCHAs or bypass protections.\n'
            f'When ready, press Enter here, click Continue in the dashboard, or create:\n'
            f'  {continue_path(profile_dir)}\n',
            flush=True,
        )
    from .._execution import pause_execution_deadline
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(1.0, timeout)
    stdin_task = None
    try:
        import sys
        if sys.stdin is not None and sys.stdin.isatty():
            stdin_task = asyncio.create_task(asyncio.to_thread(input, ''))
    except Exception:
        stdin_task = None
    with pause_execution_deadline():
        return await _wait_for_human_signal(loop, deadline, profile_dir, stdin_task)


async def _wait_for_human_signal(loop, deadline, profile_dir: Path, stdin_task) -> bool:
    try:
        while loop.time() < deadline:
            if continue_path(profile_dir).is_file():
                clear_continue(profile_dir)
                return True
            if stdin_task is not None and stdin_task.done():
                try:
                    stdin_task.result()
                except Exception:
                    # A closed console is not the operator pressing Enter.
                    stdin_task = None
                else:
                    return True
            await asyncio.sleep(0.4)
        return False
    finally:
        if stdin_task is not None and not stdin_task.done():
            stdin_task.cancel()
            with contextlib.suppress(Exception):
                await stdin_task


async def _human_gate(
    profile: Path,
    *,
    block: AccessBlock | None,
    human_timeout: float,
    wait_callback,
) -> bool:
    """Shared human pause used for collection and product-page gates."""
    if wait_callback is not None:
        action = human_action_guidance(block.reason if block is not None else None)
        await _notify_human_gate(action, block)
        try:
            return bool(await wait_callback(profile, block=block))
        except TypeError:
            # Older test callbacks accept only profile_dir.
            return bool(await wait_callback(profile))
    return await wait_for_human(profile_dir=profile, timeout=human_timeout, block=block)


def _same_collection_scope(collection_url: str, candidate: str) -> bool:
    base = urlsplit(collection_url)
    item = urlsplit(candidate)
    if item.scheme not in ('http', 'https'):
        return False
    if (item.hostname or '').lower() != (base.hostname or '').lower():
        return False
    return True


# Collection pages link to care guides and help articles that contain "/products/"
# in the path. Those are not catalog products and must not stop a scrape.
_NON_PRODUCT_PATHS = (
    '/faq/',
    '/help/',
    '/customer-service/',
    '/customer-care/',
    '/pages/',
    '/blogs/',
    '/blog/',
    '/account/',
    '/stores/',
    '/magazine/',
    '/articles/',
)


def _skip_blocked_product(result: InteractiveSessionResult, url: str, block: AccessBlock, *, timed_out: bool) -> None:
    """Record one blocked product URL and continue the collection."""
    result.failed_pages += 1
    note = format_access_blocked(block)
    if timed_out:
        note += ' (product human gate timed out)'
    result.pagination_notes.append(f'skipped_blocked_product:{note}')
    skipped = result.evidence.setdefault('skipped_product_urls', [])
    if isinstance(skipped, list):
        skipped.append(url)
    result.evidence['blocked_product_url'] = url


_FAMILY_ID = re.compile(r'(nvprod[a-z0-9]+)', re.I)


def product_family_key(url: str | None) -> str | None:
    """Louis Vuitton colorways share one nvprod id and differ by the final SKU."""
    if not isinstance(url, str) or not url:
        return None
    match = _FAMILY_ID.search(urlsplit(url).path)
    return match.group(1).lower() if match else None


def _color_label(record: dict) -> str:
    """Color only when the page published one. A SKU is not a color name."""
    explicit = record.get('color')
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    for variant in record.get('variants') or []:
        if not isinstance(variant, dict):
            continue
        color = (variant.get('options') or {}).get('Color')
        if isinstance(color, str) and color.strip():
            return color.strip()
    return ''


def _rows_for_colorway(record: dict) -> list[dict]:
    color = _color_label(record)
    image = record.get('image_url') if isinstance(record.get('image_url'), str) else None
    size_rows = []
    for variant in record.get('variants') or []:
        if not isinstance(variant, dict):
            continue
        options = dict(variant.get('options') or {})
        if not options.get('Size'):
            continue
        if color and not options.get('Color'):
            options['Color'] = color
        size_rows.append({
            'title': variant.get('title') or options.get('Size'),
            'sku': variant.get('sku') if isinstance(variant.get('sku'), str) else record.get('sku'),
            'price': variant.get('price') if variant.get('price') not in (None, '') else record.get('price'),
            'currency': variant.get('currency') or record.get('currency'),
            'featured_image': variant.get('featured_image') or image,
            'featured_image_id': variant.get('featured_image_id'),
            'options': options,
        })
    if size_rows:
        return size_rows
    if not color and not record.get('sku'):
        return []
    return [{
        'title': color or record.get('title'),
        'sku': record.get('sku') if isinstance(record.get('sku'), str) else None,
        'price': record.get('price'),
        'currency': record.get('currency'),
        'featured_image': image,
        'featured_image_id': None,
        'options': {'Color': color} if color else {},
    }]


def merge_colorway_records(records: list[dict]) -> list[dict]:
    """Keep every color and size of one product family on a single product."""
    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        key = product_family_key(record.get('product_url'))
        token = key or f'single:{index}'
        if token not in grouped:
            order.append(token)
            grouped[token] = []
        grouped[token].append(record)

    merged: list[dict] = []
    for token in order:
        members = grouped[token]
        if token.startswith('single:') or len(members) == 1 and token.startswith('single:'):
            merged.append(members[0])
            continue
        if not token.startswith('nvprod') or len(members) == 1:
            # A single colorway stays as scraped. Sizes already on it are kept.
            merged.append(members[0] if len(members) == 1 else _merge_family(members))
            continue
        merged.append(_merge_family(members))
    return merged


def _merge_family(members: list[dict]) -> dict:
    base = dict(members[0])
    descriptions = [item.get('description') for item in members if isinstance(item.get('description'), str) and item.get('description').strip()]
    if descriptions:
        base['description'] = max(descriptions, key=len)
    images: list[str] = []
    for item in members:
        for url in [item.get('image_url'), *(item.get('additional_images') or []), *(item.get('images') or [])]:
            if isinstance(url, str) and url and url not in images:
                images.append(url)
    if images:
        base['image_url'] = images[0]
        base['additional_images'] = images[1:]
        base['images'] = images
    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for item in members:
        for row in _rows_for_colorway(item):
            options = row.get('options') or {}
            identity = (
                str(options.get('Color') or '').lower(),
                str(options.get('Size') or '').lower(),
                str(row.get('sku') or ''),
            )
            if identity in seen:
                continue
            seen.add(identity)
            rows.append(row)
    base['variants'] = rows
    names: list[str] = []
    if any((row.get('options') or {}).get('Color') for row in rows):
        names.append('Color')
    if any((row.get('options') or {}).get('Size') for row in rows):
        names.append('Size')
    base['option_names'] = names
    family = product_family_key(base.get('product_url'))
    if family:
        base['family_id'] = family
        base['external_id'] = family
    return base


def is_catalog_product_url(url: str) -> bool:
    """True when the path looks like a product page, not a help or account page."""
    path = urlsplit(url).path.lower()
    if '/products/' not in path and '/product/' not in path:
        return False
    return not any(marker in path for marker in _NON_PRODUCT_PATHS)


def discover_product_urls(html: str, collection_url: str, *, max_urls: int) -> list[str]:
    """Collect product URLs from anchors and JSON-LD; preserve document order; dedupe."""
    from bs4 import BeautifulSoup
    ordered: list[str] = []
    seen: set[str] = set()

    collection_clean = urlunsplit((*urlsplit(collection_url)[:3], '', ''))

    def add(url: str) -> None:
        if not url:
            return
        absolute = urljoin(collection_url, url)
        parts = urlsplit(absolute)
        clean = urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
        if clean in seen or clean.rstrip('/') == collection_clean.rstrip('/'):
            return
        if not _same_collection_scope(collection_url, clean):
            return
        # Require a product path segment. Category facet URLs also end in /_/N-...
        # and must not be treated as products. Help pages that merely contain
        # "/products/" (for example a shoe-care FAQ) are not catalog products.
        if not is_catalog_product_url(clean):
            return
        seen.add(clean)
        ordered.append(clean)

    soup = BeautifulSoup(html or '', 'html.parser')
    for script in soup.select('script[type="application/ld+json"]'):
        text = script.string or script.get_text() or ''
        try:
            data = json.loads(text)
        except Exception:
            continue
        for node in _walk_json(data):
            if isinstance(node, dict):
                types = node.get('@type')
                type_names = types if isinstance(types, list) else [types]
                if any(isinstance(t, str) and t.rstrip('/').rsplit('/', 1)[-1] in ('Product', 'ProductGroup', 'ItemList') for t in type_names):
                    if isinstance(node.get('url'), str):
                        add(node['url'])
                    for entry in node.get('itemListElement') or []:
                        if isinstance(entry, dict):
                            item = entry.get('item') if isinstance(entry.get('item'), dict) else entry
                            if isinstance(item, dict) and isinstance(item.get('url'), str):
                                add(item['url'])
                            elif isinstance(entry.get('url'), str):
                                add(entry['url'])

    for anchor in soup.find_all('a', href=True):
        href = anchor.get('href')
        if isinstance(href, str):
            add(href)

    return ordered[:max_urls]


def _walk_json(value):
    stack = [value]
    while stack:
        item = stack.pop()
        yield item
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


_SHOE_OR_APPAREL_SIZE = re.compile(
    r'^(?:[3-9]|[1-5]\d)(?:\.[05])?$|^(?:XXS|XS|S|M|L|XL|XXL|XXXL|2XL|3XL)$',
    re.IGNORECASE,
)
_SIZE_TEXT = re.compile(
    r'^(?:size\s*)?([3-9]|[1-5]\d)(?:\.([05]))?\s*(?:eu|us|uk|it|fr)?$',
    re.IGNORECASE,
)
_ALPHA_SIZE = re.compile(r'^(?:XXS|XS|S|M|L|XL|XXL|XXXL|2XL|3XL)$', re.IGNORECASE)
_SKU_TOKEN = re.compile(r'^[0-9A-Z]{5,}$')


def _text_value(value) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if text and text.lower() != 'undefined':
            return text
        return None
    if isinstance(value, dict):
        for key in ('name', 'value'):
            text = _text_value(value.get(key))
            if text:
                return text
    return None


def _collect_images(image, images: list[str]) -> None:
    if isinstance(image, str):
        images.append(image)
    elif isinstance(image, list):
        for item in image:
            _collect_images(item, images)
    elif isinstance(image, dict) and isinstance(image.get('url'), str):
        images.append(image['url'])


def _schema_options(node: dict | None) -> dict[str, str]:
    """Published Size/Color labels only. Skip empty or placeholder values."""
    if not isinstance(node, dict):
        return {}
    options: dict[str, str] = {}
    size = _text_value(node.get('size'))
    color = _text_value(node.get('color'))
    if size:
        options['Size'] = size
    if color:
        options['Color'] = color
    props = node.get('additionalProperty')
    if isinstance(props, dict):
        props = [props]
    if isinstance(props, list):
        for prop in props:
            if not isinstance(prop, dict):
                continue
            name = _text_value(prop.get('name'))
            value = _text_value(prop.get('value'))
            if not name or not value:
                continue
            if name.lower() in ('size', 'color') and name.title() not in options:
                options[name.title()] = value
            elif name.lower() not in ('dimensions',):
                options.setdefault(name, value)
    return options


def canonical_page_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        return url.strip()
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip('/'), '', ''))


def normalize_size_label(value: str) -> str | None:
    """Turn 'Size 38', '38 EU', and '39.5' into one size token. Return None when it is not a size."""
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    if not text or len(text) > 24:
        return None
    if _ALPHA_SIZE.fullmatch(text):
        return text.upper()
    match = _SIZE_TEXT.fullmatch(text)
    if not match:
        return None
    whole, fraction = match.group(1), match.group(2)
    return f'{int(whole)}.{fraction}' if fraction else str(int(whole))


def plausible_color_name(value: str | None, sku: str | None = None) -> str | None:
    """Keep a swatch label the page printed. Reject SKUs and size tokens."""
    if not isinstance(value, str):
        return None
    text = re.sub(r'\s+', ' ', value).strip()
    if not text or len(text) > 40 or not re.search(r'[A-Za-z]', text):
        return None
    if sku and text.lower() == sku.strip().lower():
        return None
    if _SKU_TOKEN.fullmatch(text):
        return None
    if normalize_size_label(text):
        return None
    return text


def _size_key(value: str) -> str:
    normalized = normalize_size_label(value)
    if normalized:
        return normalized.lower()
    text = value.strip()
    if re.fullmatch(r'\d+\.0', text):
        return text[:-2]
    return text.lower()


def apply_published_sizes(record: dict, sizes: list[str]) -> None:
    """Replace a single selected size with every available size the page listed."""
    cleaned = []
    for size in sizes:
        text = normalize_size_label(str(size))
        if text and text not in cleaned:
            cleaned.append(text)
    existing = [
        (variant.get('options') or {}).get('Size')
        for variant in record.get('variants') or []
        if isinstance(variant, dict)
    ]
    published = [size for size in existing if size]
    if len(cleaned) < 2 and not (len(cleaned) == 1 and not published):
        return
    color = None
    known_size = None
    known_sku = record.get('sku') if isinstance(record.get('sku'), str) else None
    for variant in record.get('variants') or []:
        if not isinstance(variant, dict):
            continue
        options = variant.get('options') or {}
        if not color and isinstance(options.get('Color'), str):
            color = options['Color']
        if not known_size and isinstance(options.get('Size'), str):
            known_size = options['Size']
        if isinstance(variant.get('sku'), str) and variant.get('sku'):
            known_sku = variant['sku']
    rows = []
    for size in cleaned:
        matched = bool(known_size) and _size_key(size) == _size_key(known_size)
        options = {'Size': size}
        if color:
            options['Color'] = color
        rows.append({
            'title': size,
            'sku': known_sku if matched else None,
            'price': record.get('price'),
            'currency': record.get('currency'),
            'featured_image': record.get('image_url'),
            'featured_image_id': None,
            'options': options,
        })
    record['variants'] = rows
    record['option_names'] = ['Size', 'Color'] if color else ['Size']


def _node_unavailable(tag) -> bool:
    if tag.has_attr('disabled'):
        return True
    if str(tag.get('aria-disabled') or '').lower() == 'true':
        return True
    classes = ' '.join(tag.get('class') or []).lower()
    if any(token in classes for token in ('unavailable', 'sold-out', 'soldout', 'out-of-stock', 'disabled')):
        return True
    for key in ('data-available', 'data-instock', 'data-available-online'):
        raw = tag.get(key)
        if isinstance(raw, str) and raw.strip().lower() in ('false', '0', 'no'):
            return True
    return False


def sizes_from_html(html: str) -> list[str]:
    """Available sizes from selects, buttons, and options. Unavailable markup is skipped."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or '', 'html.parser')
    sizes: list[str] = []
    selectors = 'option, button, [role="option"], [role="menuitemradio"], li'
    for tag in soup.select(selectors):
        if _node_unavailable(tag):
            continue
        chunks = [
            tag.get('aria-label'),
            tag.get('title'),
            tag.get('value'),
            tag.get_text(' ', strip=True),
        ]
        for chunk in chunks:
            if not isinstance(chunk, str):
                continue
            label = normalize_size_label(chunk)
            if label and label not in sizes:
                sizes.append(label)
                break
    return sizes


def color_links_from_html(html: str, page_url: str) -> list[tuple[str, str | None]]:
    """Color URLs the page itself prints for this nvprod family. No guessed colors."""
    family = product_family_key(page_url)
    if not family:
        return []
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or '', 'html.parser')
    found: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for anchor in soup.find_all('a', href=True):
        href = anchor.get('href')
        if not isinstance(href, str):
            continue
        absolute = canonical_page_url(urljoin(page_url, href))
        if product_family_key(absolute) != family or absolute in seen:
            continue
        if not is_catalog_product_url(absolute):
            continue
        seen.add(absolute)
        label = plausible_color_name(
            anchor.get('aria-label') if isinstance(anchor.get('aria-label'), str) else None,
        ) or plausible_color_name(
            anchor.get('title') if isinstance(anchor.get('title'), str) else None,
        ) or plausible_color_name(anchor.get_text(' ', strip=True))
        found.append((absolute, label))
    return found


def _stamp_family(record: dict) -> dict:
    family = product_family_key(record.get('product_url'))
    if family:
        record['family_id'] = family
        record['external_id'] = family
    return record


def _apply_color_name(record: dict, label: str | None) -> None:
    name = plausible_color_name(label, record.get('sku') if isinstance(record.get('sku'), str) else None)
    if not name or _color_label(record):
        return
    record['color'] = name
    variants = record.get('variants') or []
    if not variants:
        record['variants'] = [{
            'title': name,
            'sku': record.get('sku') if isinstance(record.get('sku'), str) else None,
            'price': record.get('price'),
            'currency': record.get('currency'),
            'featured_image': record.get('image_url'),
            'featured_image_id': None,
            'options': {'Color': name},
        }]
        return
    for variant in variants:
        if isinstance(variant, dict):
            options = dict(variant.get('options') or {})
            options.setdefault('Color', name)
            variant['options'] = options


async def _read_published_sizes(page) -> list[str]:
    """Read size choices the storefront shows, including a closed size dropdown."""
    try:
        button = page.get_by_role('button', name=re.compile(r'select your size|size', re.I))
        if await button.count():
            await button.first.click(timeout=3000)
            await page.wait_for_timeout(400)
        html = await page.content()
    except Exception:
        return []
    return sizes_from_html(html)


def extract_product_record(html: str, page_url: str) -> dict | None:
    """Pull published fields from JSON-LD/OpenGraph only; leave missing as null."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or '', 'html.parser')
    product = None
    for script in soup.select('script[type="application/ld+json"]'):
        text = script.string or script.get_text() or ''
        try:
            data = json.loads(text)
        except Exception:
            continue
        for node in _walk_json(data):
            if not isinstance(node, dict):
                continue
            types = node.get('@type')
            names = types if isinstance(types, list) else [types]
            if any(isinstance(t, str) and t.rstrip('/').rsplit('/', 1)[-1] in ('Product', 'ProductGroup') for t in names):
                product = node
                break
        if product is not None:
            break

    title = None
    description = None
    price = None
    currency = None
    sku = None
    images: list[str] = []
    variants: list[dict] = []

    if isinstance(product, dict):
        title = product.get('name') if isinstance(product.get('name'), str) else None
        description = product.get('description') if isinstance(product.get('description'), str) else None
        sku = product.get('sku') if isinstance(product.get('sku'), str) else None
        _collect_images(product.get('image'), images)
        offers = product.get('offers')
        offer_list = offers if isinstance(offers, list) else [offers] if isinstance(offers, dict) else []
        # AggregateOffer wraps the real purchasable offers.
        flattened: list[dict] = []
        for offer in offer_list:
            if not isinstance(offer, dict):
                continue
            nested = offer.get('offers')
            if isinstance(nested, list) and nested and all(isinstance(item, dict) for item in nested):
                flattened.extend(nested)
            elif isinstance(nested, dict):
                flattened.append(nested)
            else:
                flattened.append(offer)
        offer_list = flattened or offer_list
        for offer in offer_list:
            if not isinstance(offer, dict):
                continue
            if price is None and offer.get('price') is not None:
                price = str(offer.get('price'))
            if currency is None and isinstance(offer.get('priceCurrency'), str):
                currency = offer.get('priceCurrency')
            if sku is None and isinstance(offer.get('sku'), str):
                sku = offer.get('sku')
        # A published list of offers is the variant set when hasVariant is absent.
        if len([offer for offer in offer_list if isinstance(offer, dict)]) > 1:
            for offer in offer_list:
                if not isinstance(offer, dict):
                    continue
                variants.append({
                    'title': offer.get('name') if isinstance(offer.get('name'), str) else (
                        offer.get('sku') if isinstance(offer.get('sku'), str) else None
                    ),
                    'sku': offer.get('sku') if isinstance(offer.get('sku'), str) else None,
                    'price': str(offer.get('price')) if offer.get('price') is not None else None,
                    'currency': offer.get('priceCurrency') if isinstance(offer.get('priceCurrency'), str) else None,
                    'featured_image': None,
                    'featured_image_id': None,
                    'options': _schema_options(offer) or _schema_options(product),
                })
        for key in ('hasVariant', 'model'):
            entries = product.get(key)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                variant = {
                    'title': entry.get('name') if isinstance(entry.get('name'), str) else None,
                    'sku': entry.get('sku') if isinstance(entry.get('sku'), str) else None,
                    'price': None,
                    'currency': None,
                    'featured_image': None,
                    'featured_image_id': None,
                    'options': {},
                }
                v_offers = entry.get('offers')
                v_offer = v_offers[0] if isinstance(v_offers, list) and v_offers else v_offers
                if isinstance(v_offer, dict):
                    if v_offer.get('price') is not None:
                        variant['price'] = str(v_offer.get('price'))
                    if isinstance(v_offer.get('priceCurrency'), str):
                        variant['currency'] = v_offer.get('priceCurrency')
                v_image = entry.get('image')
                if isinstance(v_image, str):
                    variant['featured_image'] = v_image
                elif isinstance(v_image, dict) and isinstance(v_image.get('url'), str):
                    variant['featured_image'] = v_image.get('url')
                variant['options'] = _schema_options(entry) or _schema_options(product)
                variants.append(variant)
        if not variants:
            options = _schema_options(product)
            if options:
                variants.append({
                    'title': None,
                    'sku': sku,
                    'price': price,
                    'currency': currency,
                    'featured_image': images[0] if images else None,
                    'featured_image_id': None,
                    'options': options,
                })

    # Open Graph fallbacks for missing published fields only.
    og = {}
    for meta in soup.select('meta[property], meta[name]'):
        key = meta.get('property') or meta.get('name')
        content = meta.get('content')
        if isinstance(key, str) and isinstance(content, str) and content.strip():
            og[key] = content.strip()
    title = title or og.get('og:title') or (soup.title.string.strip() if soup.title and soup.title.string else None)
    description = description or og.get('og:description')
    if price is None and og.get('product:price:amount'):
        price = og.get('product:price:amount')
    if currency is None and og.get('product:price:currency'):
        currency = og.get('product:price:currency')
    if not images and og.get('og:image'):
        images.append(og['og:image'])
    # Additional published image metas (do not scrape decorative UI assets).
    for meta in soup.select('meta[property="og:image"], meta[name="twitter:image"]'):
        content = meta.get('content')
        if isinstance(content, str) and content.strip():
            images.append(content.strip())
    for link in soup.select('link[rel="image_src"]'):
        href = link.get('href')
        if isinstance(href, str) and href.strip():
            images.append(href.strip())

    if not title:
        return None
    images = list(dict.fromkeys(images))
    return {
        'title': title,
        'description': description,
        'price': price,
        'currency': currency,
        'sku': sku,
        'product_url': page_url,
        'images': images,
        'image_url': images[0] if images else None,
        'additional_images': images[1:],
        'variants': variants,
        '_capture': {'url': page_url, 'source': 'interactive_browser'},
    }


def _finish_denial(result: InteractiveSessionResult, block: AccessBlock, body: str, *, completed: int) -> None:
    """Record a hard denial and stop. Does not retry and does not keep a partial family."""
    from datetime import datetime, timezone
    reference = denial_reference(body)
    result.blocked = True
    result.block_message = format_access_blocked(block)
    if reference:
        result.block_message += f' reference={reference}'
    detected_at = datetime.now(timezone.utc).isoformat()
    result.evidence['denial'] = {
        'url': block.url,
        'status': block.status,
        'reason': block.reason,
        'reference': reference,
        'completed_products': completed,
        'detected_at': detected_at,
    }
    result.evidence['human_action'] = human_action_guidance(block.reason)
    result.evidence['final_url'] = block.url
    logger.info(
        'Denial detected completed=%s url=%s reference=%s at=%s',
        completed, block.url, reference or '', detected_at,
    )


async def _pause_between_pages(delay_range: tuple[float, float]) -> None:
    low, high = delay_range
    if high <= 0:
        return
    import random
    await asyncio.sleep(random.uniform(min(low, high), max(low, high)))


async def run_interactive_collection(
    url: str,
    *,
    max_products: int = 5,
    profile_dir: Path | None = None,
    human_timeout: float = 300.0,
    navigation_timeout_ms: int = 60000,
    wait_callback=None,
    delay_range: tuple[float, float] | None = None,
) -> InteractiveSessionResult:
    """Open headed persistent Chromium, wait for human, then scrape if cleared."""
    from .._execution import browser_allowed
    if not browser_allowed():
        raise RuntimeError('Browser execution is disabled by the operation policy')

    from ..run_control import current_known_families, current_pace
    pace = current_pace()
    if delay_range is None:
        delay_range = (0.0, 0.0) if pace is None else (pace.delay_min_seconds, pace.delay_max_seconds)
    known_families = {item.lower() for item in current_known_families()}
    logger.info(
        'Interactive run starting url=%s max_products=%s delay=%s-%ss known_families=%s',
        url, max_products, delay_range[0], delay_range[1], len(known_families),
    )
    profile = Path(profile_dir or default_profile_dir(url))
    profile.mkdir(parents=True, exist_ok=True)
    result = InteractiveSessionResult(blocked=False, collection_url=url, evidence={'profile_dir': str(profile)})
    network_samples: list[dict] = []

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError('Playwright is required for interactive browser mode') from exc

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,
            viewport={'width': 1365, 'height': 900},
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()

            async def _on_response(response) -> None:
                try:
                    req_url = response.url
                    ctype = (response.headers or {}).get('content-type', '')
                    lower_ctype = ctype.lower()
                    lower_url = req_url.lower()
                    # Cache real image bytes while the headed session can still fetch them.
                    if response.status == 200 and (
                        lower_ctype.startswith('image/')
                        or 'is/image' in lower_url
                        or any(lower_url.endswith(ext) for ext in ('.jpg', '.jpeg', '.png', '.webp', '.gif'))
                    ):
                        try:
                            from ..dashboard.image_assets import write_image_cache
                            body = await response.body()
                            write_image_cache(profile, req_url, body)
                        except Exception:
                            pass
                    interesting = (
                        'json' in lower_ctype
                        or any(token in lower_url for token in (
                            'product', 'catalog', 'plp', 'search', 'graphql', 'ajax', 'api',
                        ))
                    )
                    if not interesting:
                        return
                    if len(network_samples) >= 40:
                        return
                    body_text = ''
                    try:
                        body_text = (await response.text())[:12000]
                    except Exception:
                        body_text = ''
                    # Skip tiny sensor/beacon acknowledgements.
                    if body_text.strip() in ('{"success": true}', '{"success":true}') or len(body_text) < 80:
                        if 'product' not in lower_url and 'catalog' not in lower_url:
                            return
                    network_samples.append({
                        'url': req_url,
                        'status': response.status,
                        'content_type': ctype,
                        'body_prefix': body_text,
                    })
                except Exception:
                    return

            page.on('response', _on_response)
            with contextlib.suppress(Exception):
                await page.bring_to_front()
            page_cache: dict[str, str] = {}

            async def open_url(target: str):
                """Open one URL in this browser. Cached URLs are not opened again."""
                key = canonical_page_url(target)
                if key in page_cache:
                    return page_cache[key], None
                await _pause_between_pages(delay_range)
                response = await page.goto(target, wait_until='domcontentloaded', timeout=navigation_timeout_ms)
                body = await page.content()
                status = response.status if response is not None else None
                block = detect_access_block(url=page.url, status=status, body=body)
                result.pages_visited += 1
                if is_hard_denial(block):
                    return body, block
                page_cache[key] = body
                page_cache[canonical_page_url(page.url)] = body
                return body, None

            await _pause_between_pages(delay_range)
            response = await page.goto(url, wait_until='domcontentloaded', timeout=navigation_timeout_ms)
            status = response.status if response is not None else None
            html = await page.content()
            result.pages_visited += 1
            await _emit_snapshot(page, label='collection_open')
            block = detect_access_block(url=page.url, status=status, body=html)
            if is_hard_denial(block):
                _finish_denial(result, block, html, completed=0)
                await _emit_snapshot(page, label='access_denied')
                return result
            if block is None:
                page_cache[canonical_page_url(url)] = html
                page_cache[canonical_page_url(page.url)] = html
            if block is not None:
                gate_label = f'human_gate_{block.reason}'
                await _emit_snapshot(page, label=gate_label)
                gate_task = asyncio.create_task(_human_gate(
                    profile, block=block, human_timeout=human_timeout, wait_callback=wait_callback,
                ))
                try:
                    while not gate_task.done():
                        done, _pending = await asyncio.wait({gate_task}, timeout=3.0)
                        if not done:
                            await _emit_snapshot(page, label=gate_label)
                    ready = gate_task.result()
                except BaseException:
                    if not gate_task.done():
                        gate_task.cancel()
                    raise
                if not ready:
                    result.blocked = True
                    result.block_message = format_access_blocked(block) + ' (human gate timed out)'
                    result.evidence.update({
                        'status': status,
                        'final_url': page.url,
                        'network_samples': network_samples[:8],
                        'human_action': human_action_guidance(block.reason),
                    })
                    await _emit_snapshot(page, label='blocked_timeout')
                    return result
                # Re-read after human interaction; do not auto-retry loops.
                try:
                    await page.wait_for_load_state('domcontentloaded', timeout=15000)
                except Exception:
                    pass
                with contextlib.suppress(Exception):
                    await page.reload(wait_until='domcontentloaded', timeout=navigation_timeout_ms)
                html = await page.content()
                status = None
                block = detect_access_block(url=page.url, status=status, body=html)
                await _emit_snapshot(page, label='after_human')
                if is_hard_denial(block):
                    _finish_denial(result, block, html, completed=0)
                    result.collection_html = html[:200000]
                    await _emit_snapshot(page, label='access_denied')
                    return result
                if block is not None:
                    result.blocked = True
                    result.block_message = format_access_blocked(block)
                    result.collection_html = html[:200000]
                    result.evidence.update({
                        'final_url': page.url,
                        'status_after_human': status,
                        'network_samples': network_samples[:8],
                        'human_action': human_action_guidance(block.reason),
                    })
                    return result

            # Collection appears loaded (no interstitial markers). Give SPAs time
            # to hydrate product tiles and fire catalog XHRs before discovery.
            await _wait_for_collection_content(page, timeout_ms=25000)
            html = await page.content()
            await _emit_snapshot(page, label='collection_ready')
            result.collection_url = page.url
            result.collection_html = html[:200000]
            product_urls = discover_product_urls(html, page.url, max_urls=max_products * 4)
            seen_urls = set(product_urls)
            collection_clean = urlunsplit((*urlsplit(page.url)[:3], '', ''))
            for sample in network_samples:
                for candidate in _urls_from_network_body(sample.get('body_prefix') or '', page.url):
                    parts = urlsplit(candidate)
                    clean = urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
                    if clean in seen_urls or clean.rstrip('/') == collection_clean.rstrip('/'):
                        continue
                    if not _same_collection_scope(page.url, clean):
                        continue
                    path = parts.path.lower()
                    if '/products/' not in path and '/product/' not in path:
                        continue
                    product_urls.append(clean)
                    seen_urls.add(clean)
                    if len(product_urls) >= max_products * 4:
                        break
            # Attempt in-page load-more, scroll, then a real next-page link.
            pagination_notes = []
            load_rounds = 0
            visited_collections = {collection_clean.rstrip('/')}
            while len(product_urls) < max_products and load_rounds < 40:
                if _crawl_budget_low():
                    pagination_notes.append('operation_timeout')
                    break
                before = set(product_urls)
                clicked = await _try_load_more(page)
                followed = False
                if clicked:
                    pagination_notes.append(f'clicked_load_more_or_next_round_{load_rounds + 1}')
                else:
                    scrolled = await _try_scroll_for_more(page)
                    if scrolled:
                        pagination_notes.append(f'scrolled_for_more_round_{load_rounds + 1}')
                    else:
                        next_href = await _next_collection_href(page, visited=visited_collections)
                        if next_href:
                            opened = False
                            nav_error = None
                            await _pause_between_pages(delay_range)
                            for _attempt in range(2):
                                try:
                                    await page.goto(
                                        next_href,
                                        wait_until='domcontentloaded',
                                        timeout=navigation_timeout_ms,
                                    )
                                    await _wait_for_collection_content(page, timeout_ms=12000)
                                    opened = True
                                    break
                                except Exception as exc:
                                    nav_error = exc
                                    if not _is_navigation_timeout(exc):
                                        break
                            if opened:
                                next_html = await page.content()
                                next_block = detect_access_block(url=page.url, status=None, body=next_html)
                                if is_hard_denial(next_block):
                                    _finish_denial(result, next_block, next_html, completed=len(result.products))
                                    pagination_notes.append('access_denied')
                                    break
                                visited_collections.add(next_href.rstrip('/'))
                                followed = True
                                pagination_notes.append(f'followed_next_page_round_{load_rounds + 1}')
                            else:
                                logger.info('Next collection page failed %s: %s', next_href, nav_error)
                                pagination_notes.append('next_page_navigation_failed')
                                if nav_error is not None and _is_navigation_timeout(nav_error):
                                    pagination_notes.append('operation_timeout')
                                break
                        else:
                            pagination_notes.append('no_load_more_control_found')
                            break
                load_rounds += 1
                await asyncio.sleep(1.5)
                html2 = await page.content()
                more = discover_product_urls(html2, page.url, max_urls=max_products * 4)
                added = 0
                for item in more:
                    if item not in before:
                        product_urls.append(item)
                        before.add(item)
                        added += 1
                if added == 0 and not followed:
                    pagination_notes.append('load_more_yielded_no_additional_unique_urls')
                    break
                if added == 0 and followed:
                    pagination_notes.append('next_page_yielded_no_additional_unique_urls')
                    break
            if load_rounds == 0 and 'no_load_more_control_found' not in pagination_notes:
                pagination_notes.append('single_page_no_pagination_attempt')

            result.product_urls = list(product_urls)
            if result.blocked:
                result.pagination_notes = pagination_notes
                return result
            next_href = await _next_collection_href(page, visited=visited_collections)
            confirmed, end_note = decide_collection_end(
                discovered=len(product_urls),
                requested=max_products,
                notes=pagination_notes,
                next_href=next_href,
            )
            result.pagination_end_confirmed = confirmed
            result.pagination_notes = pagination_notes
            if end_note:
                result.pagination_notes.append(end_note)

            result.evidence.update({
                'final_url': page.url,
                'discovered_url_count': len(product_urls),
                'network_samples': network_samples[:12],
                'pagination_notes': list(pagination_notes),
                'pagination_end_confirmed': result.pagination_end_confirmed,
                'html_prefix': (html or '')[:4000],
                'html_length': len(html or ''),
            })

            family_queue: list[tuple[str, str]] = []
            seen_families: set[str] = set()
            skipped_existing = 0
            for product_url in result.product_urls:
                family = product_family_key(product_url)
                token = family or canonical_page_url(product_url)
                if token in seen_families:
                    continue
                seen_families.add(token)
                if family and family in known_families:
                    skipped_existing += 1
                    logger.info('Skipping family %s because Shopify already has it', family)
                    continue
                family_queue.append((token, product_url))
            batch = family_queue[:max_products]
            remaining = max(0, len(family_queue) - len(batch))
            result.evidence['families_discovered'] = len(seen_families)
            result.evidence['families_skipped_existing'] = skipped_existing
            result.evidence['families_remaining'] = remaining
            logger.info(
                'Families discovered=%s skipped_existing=%s this_batch=%s remaining=%s',
                len(seen_families), skipped_existing, len(batch), remaining,
            )

            for token, product_url in batch:
                if _crawl_budget_low():
                    result.pagination_notes.append('operation_timeout')
                    result.pagination_end_confirmed = False
                    break
                try:
                    body, page_block = await open_url(product_url)
                    await _emit_snapshot(page, label=f'product_{result.pages_visited}')
                    if is_hard_denial(page_block):
                        _finish_denial(result, page_block, body, completed=len(result.products))
                        await _emit_snapshot(page, label='access_denied')
                        break
                    record = extract_product_record(body, page.url or product_url)
                    if record is None:
                        result.failed_pages += 1
                        continue
                    apply_published_sizes(record, sizes_from_html(body) or await _read_published_sizes(page))
                    color_pages = [(canonical_page_url(product_url), None)]
                    for link, label in color_links_from_html(body, page.url or product_url):
                        if all(link != existing for existing, _label in color_pages):
                            color_pages.append((link, label))
                    color_records = [record]
                    first_label = next((label for link, label in color_links_from_html(body, page.url or product_url) if link == canonical_page_url(page.url or product_url)), None)
                    _apply_color_name(record, first_label)
                    denied_family = False
                    for link, label in color_pages[1:]:
                        color_body, color_block = await open_url(link)
                        if is_hard_denial(color_block):
                            denied_family = True
                            _finish_denial(result, color_block, color_body, completed=len(result.products))
                            await _emit_snapshot(page, label='access_denied')
                            break
                        color_record = extract_product_record(color_body, link)
                        if color_record is None:
                            continue
                        apply_published_sizes(color_record, sizes_from_html(color_body))
                        _apply_color_name(color_record, label)
                        color_records.append(color_record)
                    if denied_family:
                        logger.info('Family %s was not uploaded because a color page was denied', token)
                        break
                    merged_set = merge_colorway_records(color_records)
                    merged = _stamp_family(merged_set[0] if merged_set else record)
                    merged['_capture'] = {
                        **(merged.get('_capture') or {}),
                        'collection_url': result.collection_url,
                        'network_hit_count': len(network_samples),
                        'color_pages': len(color_records),
                    }
                    result.products.append(merged)
                    await _emit_product(merged)
                    logger.info('Product completed family=%s title=%s colors=%s', token, merged.get('title'), len(color_records))
                except Exception as exc:
                    logger.info('Interactive product navigation failed for %s: %s', product_url, exc)
                    result.failed_pages += 1
                    continue
            logger.info(
                'Interactive run finished completed=%s failed=%s denied=%s remaining=%s',
                len(result.products), result.failed_pages, result.blocked, remaining,
            )
            return result
        finally:
            await context.close()


async def _emit_product(record: dict) -> None:
    hook = _product_hook.get()
    if hook is None:
        return
    try:
        maybe = hook(record)
        if asyncio.iscoroutine(maybe):
            await maybe
    except Exception as exc:
        logger.info('Interactive product hook failed: %s', exc)


async def _emit_snapshot(page, *, label: str) -> None:
    hook = _snapshot_hook.get()
    if hook is None:
        return
    try:
        png = await page.screenshot(type='jpeg', quality=55, full_page=False)
        maybe = hook(png, label)
        if asyncio.iscoroutine(maybe):
            await maybe
    except Exception as exc:
        logger.info('Interactive snapshot hook failed: %s', exc)


async def _wait_for_collection_content(page, *, timeout_ms: int = 25000) -> None:
    selectors = [
        'a[href*="/products/"]',
        'a[href*="/product/"]',
        '[data-testid*="product" i]',
        '.lv-product-card',
        '.product-item',
        'a[href*="/_/N-"]',
    ]
    deadline = asyncio.get_running_loop().time() + max(1.0, timeout_ms / 1000)
    for selector in selectors:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            await page.wait_for_selector(selector, timeout=min(8000, int(remaining * 1000)))
            return
        except Exception:
            continue
    with contextlib.suppress(Exception):
        await page.wait_for_load_state('networkidle', timeout=5000)


def _urls_from_network_body(body: str, base_url: str) -> list[str]:
    """Pull absolute product URLs from public JSON/XHR bodies only."""
    found: list[str] = []
    if not body:
        return found
    try:
        data = json.loads(body)
    except Exception:
        # Loose absolute URL scrape for non-strict JSON payloads.
        for match in re.findall(r'https?://[^"\s<>]+/(?:products?)/[^"\s<>]+', body, flags=re.I):
            found.append(match.rstrip('",}\''))
        return list(dict.fromkeys(found))

    for node in _walk_json(data):
        if isinstance(node, dict):
            for key in ('url', 'productUrl', 'product_url', 'canonicalUrl', 'href'):
                value = node.get(key)
                if isinstance(value, str) and ('/product' in value.lower() or '/_/N-' in value):
                    found.append(urljoin(base_url, value))
        elif isinstance(node, str) and node.startswith('http') and ('/product' in node.lower() or '/_/N-' in node):
            found.append(node)
    return list(dict.fromkeys(found))


def _crawl_budget_low(margin: float = 40.0) -> bool:
    from .._execution import remaining_execution_seconds
    remaining = remaining_execution_seconds()
    return remaining is not None and remaining < margin


def _is_navigation_timeout(exc: BaseException) -> bool:
    return 'timeout' in type(exc).__name__.lower() or 'timeout' in str(exc).lower()


def decide_collection_end(
    *,
    discovered: int,
    requested: int,
    notes: list[str],
    next_href: str | None,
) -> tuple[bool | None, str]:
    """Decide whether the visible collection ended, from controls already tried.

    A finite page with no further link is confirmed. A remaining next link, or
    a hit product cap, is not proof that the catalog ended.
    """
    if discovered <= 0:
        return False, 'no_product_urls_discovered'
    if discovered >= requested:
        return False, 'hit_product_budget_end_unconfirmed'
    if 'operation_timeout' in notes:
        return False, 'operation_timeout'
    if 'next_page_navigation_failed' in notes:
        return None, 'pagination_end_unconfirmed'
    if next_href:
        return None, 'pagination_end_unconfirmed'
    exhausted = (
        'no_load_more_control_found' in notes
        or 'load_more_yielded_no_additional_unique_urls' in notes
        or 'next_page_yielded_no_additional_unique_urls' in notes
        or 'single_page_no_pagination_attempt' in notes
    )
    if exhausted:
        return True, 'collection_end_confirmed_no_further_page'
    return None, 'pagination_end_unconfirmed'


_NEXT_HREF_JS = """
(visited) => {
  const seen = new Set((visited || []).map((item) => String(item).replace(/\\/$/, '')));
  const current = location.href.replace(/\\/$/, '');
  seen.add(current);
  const abs = (el) => {
    if (!el) return null;
    const raw = el.href || el.getAttribute('href');
    if (!raw || raw.startsWith('javascript:') || raw.startsWith('#')) return null;
    try { return new URL(raw, location.href).href; } catch (e) { return null; }
  };
  const rel = document.querySelector('a[rel~="next"], link[rel~="next"]');
  const relHref = abs(rel);
  if (relHref && !seen.has(relHref.replace(/\\/$/, ''))) return relHref;
  const nodes = [...document.querySelectorAll('a[href], button[href]')];
  for (const el of nodes) {
    const text = ((el.getAttribute('aria-label') || el.innerText || '') + '').trim().toLowerCase();
    if (text === 'next' || text === 'next page' || text.includes('load more') || text.includes('show more') || text.includes('view more')) {
      const href = abs(el);
      if (href && !seen.has(href.replace(/\\/$/, ''))) return href;
    }
  }
  let currentPage = 1;
  try {
    const query = new URL(location.href).searchParams;
    const raw = query.get('page') || query.get('paged');
    if (raw && /^[0-9]+$/.test(raw)) currentPage = parseInt(raw, 10);
  } catch (e) {}
  const wanted = String(currentPage + 1);
  for (const el of document.querySelectorAll('a[href]')) {
    let nextUrl;
    try { nextUrl = new URL(el.href, location.href); } catch (e) { continue; }
    const page = nextUrl.searchParams.get('page') || nextUrl.searchParams.get('paged');
    if (page === wanted && !seen.has(nextUrl.href.replace(/\\/$/, ''))) return nextUrl.href;
  }
  return null;
}
"""


async def _next_collection_href(page, *, visited: set[str]) -> str | None:
    try:
        href = await page.evaluate(_NEXT_HREF_JS, list(visited))
    except Exception:
        return None
    if not isinstance(href, str) or not href.startswith(('http://', 'https://')):
        return None
    return href


async def _try_load_more(page) -> bool:
    selectors = [
        'button:has-text("Load more")',
        'button:has-text("Show more")',
        'button:has-text("View more")',
        'a:has-text("Load more")',
        'a:has-text("Show more")',
        'a:has-text("View more")',
        'a:has-text("Next")',
        'button[aria-label*="next" i]',
        'a[rel="next"]',
        '[data-testid*="load-more" i]',
        '[data-testid*="pagination-next" i]',
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            if not await locator.is_visible():
                continue
            await locator.click(timeout=3000)
            return True
        except Exception:
            continue
    return False


async def _try_scroll_for_more(page) -> bool:
    try:
        before_height, before_links = await page.evaluate(
            '() => [document.body.scrollHeight, document.querySelectorAll("a[href]").length]'
        )
        await page.evaluate('() => window.scrollTo(0, document.body.scrollHeight)')
        await asyncio.sleep(1.2)
        after_height, after_links = await page.evaluate(
            '() => [document.body.scrollHeight, document.querySelectorAll("a[href]").length]'
        )
        return after_height > before_height or after_links > before_links
    except Exception:
        return False


async def interactive_extractor_result(
    url: str,
    *,
    max_products: int = 5,
    profile_dir: Path | None = None,
    human_timeout: float = 300.0,
    wait_callback=None,
    session_out: dict | None = None,
) -> ExtractorResult:
    session = await run_interactive_collection(
        url,
        max_products=max_products,
        profile_dir=profile_dir,
        human_timeout=human_timeout,
        wait_callback=wait_callback,
    )
    session.products = merge_colorway_records(session.products)
    if session_out is not None:
        session_out.clear()
        session_out.update({
            'blocked': session.blocked,
            'block_message': session.block_message,
            'collection_url': session.collection_url,
            'product_urls': list(session.product_urls),
            'pages_visited': session.pages_visited,
            'failed_pages': session.failed_pages,
            'pagination_end_confirmed': session.pagination_end_confirmed,
            'pagination_notes': list(session.pagination_notes),
            'evidence': dict(session.evidence),
        })
    if session.blocked:
        return ExtractorResult(
            products=session.products,
            complete=False,
            error=session.block_message or ACCESS_BLOCKED,
            completeness_reason=ACCESS_BLOCKED,
            pages_completed=session.pages_visited,
        )
    complete = False
    reason = 'interactive_coverage_unverified'
    extracted_all = bool(session.products) and len(session.products) == len(session.product_urls)
    if session.pagination_end_confirmed is True and extracted_all and session.failed_pages == 0:
        complete = True
        reason = None
    elif session.failed_pages:
        reason = 'interactive_product_pages_failed'
    elif session.pagination_notes:
        reason = session.pagination_notes[-1]
    return ExtractorResult(
        products=session.products,
        complete=complete,
        error=None,
        completeness_reason=reason,
        pages_completed=session.pages_visited,
    )
