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
    detect_access_block,
    format_access_blocked,
    human_action_guidance,
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
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(1.0, timeout)
    stdin_task = None
    try:
        import sys
        if sys.stdin is not None and sys.stdin.isatty():
            stdin_task = asyncio.create_task(asyncio.to_thread(input, ''))
    except Exception:
        stdin_task = None
    try:
        while loop.time() < deadline:
            if continue_path(profile_dir).is_file():
                clear_continue(profile_dir)
                return True
            if stdin_task is not None and stdin_task.done():
                with contextlib.suppress(Exception):
                    stdin_task.result()
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
        path = parts.path.lower()
        # Require a product path segment. Category facet URLs also end in /_/N-...
        # and must not be treated as products.
        if '/products/' not in path and '/product/' not in path:
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
        image = product.get('image')
        if isinstance(image, str):
            images.append(image)
        elif isinstance(image, list):
            images.extend(i for i in image if isinstance(i, str))
        elif isinstance(image, dict) and isinstance(image.get('url'), str):
            images.append(image['url'])
        offers = product.get('offers')
        offer_list = offers if isinstance(offers, list) else [offers] if isinstance(offers, dict) else []
        for offer in offer_list:
            if not isinstance(offer, dict):
                continue
            if price is None and offer.get('price') is not None:
                price = str(offer.get('price'))
            if currency is None and isinstance(offer.get('priceCurrency'), str):
                currency = offer.get('priceCurrency')
            if sku is None and isinstance(offer.get('sku'), str):
                sku = offer.get('sku')
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
                variants.append(variant)

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


async def run_interactive_collection(
    url: str,
    *,
    max_products: int = 5,
    profile_dir: Path | None = None,
    human_timeout: float = 300.0,
    navigation_timeout_ms: int = 60000,
    wait_callback=None,
) -> InteractiveSessionResult:
    """Open headed persistent Chromium, wait for human, then scrape if cleared."""
    from .._execution import browser_allowed
    if not browser_allowed():
        raise RuntimeError('Browser execution is disabled by the operation policy')

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
            args=['--disable-blink-features=AutomationControlled'],
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
            response = await page.goto(url, wait_until='domcontentloaded', timeout=navigation_timeout_ms)
            status = response.status if response is not None else None
            html = await page.content()
            await _emit_snapshot(page, label='collection_open')
            block = detect_access_block(url=page.url, status=status, body=html)
            if block is not None:
                await _emit_snapshot(page, label=f'human_gate_{block.reason}')
                ready = await _human_gate(
                    profile, block=block, human_timeout=human_timeout, wait_callback=wait_callback,
                )
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
            # Attempt in-page load-more / infinite-scroll while under budget; dedupe URLs.
            pagination_notes = []
            load_rounds = 0
            while len(product_urls) < max_products and load_rounds < 8:
                before = set(product_urls)
                clicked = await _try_load_more(page)
                if clicked:
                    pagination_notes.append(f'clicked_load_more_or_next_round_{load_rounds + 1}')
                else:
                    scrolled = await _try_scroll_for_more(page)
                    if not scrolled:
                        pagination_notes.append('no_load_more_control_found')
                        break
                    pagination_notes.append(f'scrolled_for_more_round_{load_rounds + 1}')
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
                if added == 0:
                    pagination_notes.append('load_more_yielded_no_additional_unique_urls')
                    break
            if load_rounds == 0 and 'no_load_more_control_found' not in pagination_notes:
                pagination_notes.append('single_page_no_pagination_attempt')

            result.product_urls = product_urls[:max_products]
            result.pagination_notes = pagination_notes
            if not product_urls:
                result.pagination_end_confirmed = False
                result.pagination_notes.append('no_product_urls_discovered')
            elif 'load_more_yielded_no_additional_unique_urls' in pagination_notes:
                result.pagination_end_confirmed = True
                result.pagination_notes.append('collection_end_confirmed_via_exhausted_load_more')
            elif len(product_urls) < max_products and 'no_load_more_control_found' in pagination_notes:
                result.pagination_end_confirmed = None
                result.pagination_notes.append('fewer_than_requested_urls_end_unconfirmed')
            elif len(result.product_urls) >= max_products:
                result.pagination_end_confirmed = False
                result.pagination_notes.append('hit_product_budget_end_unconfirmed')
            else:
                result.pagination_end_confirmed = None
                result.pagination_notes.append('pagination_end_unconfirmed')

            result.evidence.update({
                'final_url': page.url,
                'discovered_url_count': len(product_urls),
                'network_samples': network_samples[:12],
                'pagination_notes': list(pagination_notes),
                'pagination_end_confirmed': result.pagination_end_confirmed,
                'html_prefix': (html or '')[:4000],
                'html_length': len(html or ''),
            })

            for product_url in result.product_urls:
                try:
                    resp = await page.goto(product_url, wait_until='domcontentloaded', timeout=navigation_timeout_ms)
                    body = await page.content()
                    page_status = resp.status if resp is not None else None
                    page_block = detect_access_block(url=page.url, status=page_status, body=body)
                    result.pages_visited += 1
                    await _emit_snapshot(page, label=f'product_{result.pages_visited}')
                    if page_block is not None:
                        await _emit_snapshot(page, label=f'product_human_gate_{page_block.reason}')
                        ready = await _human_gate(
                            profile,
                            block=page_block,
                            human_timeout=min(human_timeout, 180.0),
                            wait_callback=wait_callback,
                        )
                        if not ready:
                            result.blocked = True
                            result.block_message = (
                                format_access_blocked(page_block) + ' (product human gate timed out)'
                            )
                            result.evidence['blocked_product_url'] = page.url
                            break
                        try:
                            await page.wait_for_load_state('domcontentloaded', timeout=15000)
                        except Exception:
                            pass
                        with contextlib.suppress(Exception):
                            await page.reload(wait_until='domcontentloaded', timeout=navigation_timeout_ms)
                        body = await page.content()
                        page_block = detect_access_block(url=page.url, status=None, body=body)
                        await _emit_snapshot(page, label=f'product_after_human_{result.pages_visited}')
                        if page_block is not None:
                            result.blocked = True
                            result.block_message = format_access_blocked(page_block)
                            result.evidence['blocked_product_url'] = page.url
                            result.evidence['human_action'] = human_action_guidance(page_block.reason)
                            break
                    record = extract_product_record(body, page.url)
                    if record is not None:
                        record['_capture'] = {
                            **(record.get('_capture') or {}),
                            'collection_url': result.collection_url,
                            'network_hit_count': len(network_samples),
                        }
                        result.products.append(record)
                        await _emit_product(record)
                except Exception as exc:
                    logger.info('Interactive product navigation failed for %s: %s', product_url, exc)
                    continue
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


async def _try_load_more(page) -> bool:
    selectors = [
        'button:has-text("Load more")',
        'button:has-text("Show more")',
        'a:has-text("Load more")',
        'a:has-text("Next")',
        'button[aria-label*="next" i]',
        'a[rel="next"]',
        '[data-testid*="load-more" i]',
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
        before = await page.evaluate('() => document.body.scrollHeight')
        await page.evaluate('() => window.scrollTo(0, document.body.scrollHeight)')
        await asyncio.sleep(1.2)
        after = await page.evaluate('() => document.body.scrollHeight')
        return after > before
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
    if session_out is not None:
        session_out.clear()
        session_out.update({
            'blocked': session.blocked,
            'block_message': session.block_message,
            'collection_url': session.collection_url,
            'product_urls': list(session.product_urls),
            'pages_visited': session.pages_visited,
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
    if session.pagination_end_confirmed is True:
        # End of visible pagination confirmed; catalog_complete still needs product budget care.
        complete = len(session.products) > 0 and max_products >= len(session.product_urls)
        reason = None if complete else 'interactive_coverage_unverified'
    elif session.pagination_notes:
        reason = session.pagination_notes[-1]
    return ExtractorResult(
        products=session.products,
        complete=complete,
        error=None,
        completeness_reason=reason,
        pages_completed=session.pages_visited,
    )
