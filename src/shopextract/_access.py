"""Detect HTTP/access interstitials; never bypass site protections."""
from __future__ import annotations

import re
from dataclasses import dataclass

ACCESS_BLOCKED = 'ACCESS_BLOCKED'

_WAITING_MARKERS = (
    'lv-waiting',
    'access denied',
    'request blocked',
    'please wait while we verify',
    'checking your browser before accessing',
    'attention required! | cloudflare',
)


@dataclass(frozen=True)
class AccessBlock:
    url: str
    status: int | None
    reason: str


class AccessBlockedError(Exception):
    """Confirmed access block; callers must stop retries."""

    def __init__(self, block: AccessBlock):
        self.block = block
        super().__init__(format_access_blocked(block))


def format_access_blocked(block: AccessBlock) -> str:
    status = 'unknown' if block.status is None else str(block.status)
    return f'{ACCESS_BLOCKED} url={block.url} status={status} reason={block.reason}'


def classify_interstitial(body: str | None) -> str | None:
    if not body:
        return None
    sample = body[:65536].lower()
    if 'lv-waiting' in sample:
        return 'waiting_interstitial'
    if (
        'access denied' in sample
        or 'accès refusé' in sample
        or 'acces refuse' in sample
        or 'return at a later time' in sample
        or 'revenir plus tard' in sample
    ):
        return 'access_denied_interstitial'
    if any(marker in sample for marker in _WAITING_MARKERS):
        return 'waiting_interstitial'
    return None


_REFERENCE = re.compile(r'#\d+\.[0-9a-f]{4,}\.[0-9]+\.[0-9a-f]+', re.I)


def denial_reference(body: str | None) -> str | None:
    """Akamai reference printed on a denial page, when the page shows one."""
    if not body:
        return None
    match = _REFERENCE.search(body[:65536])
    return match.group(0) if match else None


def detect_access_block(*, url: str, status: int | None = None,
                        body: str | None = None) -> AccessBlock | None:
    interstitial = classify_interstitial(body)
    if status in (403, 429):
        return AccessBlock(url=url, status=status,
                           reason=interstitial or f'http_{status}')
    if interstitial is not None:
        return AccessBlock(url=url, status=status, reason=interstitial)
    return None


def is_hard_denial(block: AccessBlock | None) -> bool:
    """True when the store refused the browser. Callers must stop the run."""
    if block is None:
        return False
    if block.status in (403, 429):
        return True
    reason = (block.reason or '').lower()
    return 'access_denied' in reason or reason in ('http_403', 'http_429')


def detect_access_block_from_crawl(result: object, url: str) -> AccessBlock | None:
    status = getattr(result, 'status_code', None)
    html = getattr(result, 'html', None) or ''
    final = getattr(result, 'url', None) or url
    return detect_access_block(url=str(final), status=status if isinstance(status, int) else None,
                               body=html if isinstance(html, str) else None)


def is_access_blocked_message(value: object) -> bool:
    return isinstance(value, str) and value.startswith(ACCESS_BLOCKED)


def access_blocked_reason(errors=(), incompleteness_reasons=()) -> str | None:
    for item in errors:
        if is_access_blocked_message(item):
            return str(item)
    for item in incompleteness_reasons:
        if item == ACCESS_BLOCKED:
            return ACCESS_BLOCKED
    return None


def human_action_guidance(reason: str | None = None) -> dict:
    """Tell the operator what to do in the headed browser. Never bypasses the gate."""
    normalized = (reason or 'unknown').strip().lower()
    if 'access_denied' in normalized:
        return {
            'kind': normalized,
            'title': 'Access denied',
            'steps': [
                'The store refused this browser and asked you to come back later.',
                'This is not a waiting room or a CAPTCHA. Continue cannot open the catalog.',
                'Product Harbor does not bypass an access denial.',
            ],
            'summary': (
                'Access denied: the store refused this browser. '
                'Product Harbor will not try to get around that block.'
            ),
            'continue_label': 'Continue — start scraping',
        }
    if 'waiting' in normalized or 'interstitial' in normalized or normalized == 'http_403':
        title = 'Waiting room / access check'
        steps = [
            'Look at the Chromium window Product Harbor opened (not this dashboard).',
            'If the page shows Please wait or a brand waiting screen, leave it open.',
            'Accept cookies or click Continue/Verify if the site offers that.',
            'Wait until the real product collection (shoes, bags, etc.) is visible.',
            'Return here and click Continue so scraping can start.',
        ]
    elif 'captcha' in normalized or 'challenge' in normalized or 'cloudflare' in normalized:
        title = 'CAPTCHA / browser challenge'
        steps = [
            'In the Chromium window, complete the CAPTCHA or “I’m not a robot” check yourself.',
            'Wait until the real storefront content loads.',
            'Click Continue in Product Harbor.',
        ]
    elif 'login' in normalized or 'sign_in' in normalized or 'auth' in normalized:
        title = 'Login required'
        steps = [
            'Sign in or complete age/region confirmation in the Chromium window.',
            'Open the collection URL again if the site redirected away.',
            'When products are visible, click Continue here.',
        ]
    elif 'cookie' in normalized or 'consent' in normalized:
        title = 'Cookie / consent banner'
        steps = [
            'Accept or dismiss the cookie/consent banner in the Chromium window.',
            'Confirm the collection listing is visible.',
            'Click Continue in Product Harbor.',
        ]
    else:
        title = 'Human step required in browser'
        steps = [
            'Complete whatever the Chromium window asks (wait screen, CAPTCHA, login, or consent).',
            'Product Harbor does not solve challenges automatically.',
            'When the real product listing is visible, click Continue here.',
        ]
    return {
        'kind': normalized,
        'title': title,
        'steps': steps,
        'summary': (
            f'{title}: finish the step in the Chromium window, then click Continue. '
            'Product Harbor will scrape only after you confirm.'
        ),
        'continue_label': 'Continue — start scraping',
    }
