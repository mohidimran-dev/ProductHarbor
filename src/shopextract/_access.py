"""Detect HTTP/access interstitials; never bypass site protections."""
from __future__ import annotations

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
    if 'access denied' in sample:
        return 'access_denied_interstitial'
    if any(marker in sample for marker in _WAITING_MARKERS):
        return 'waiting_interstitial'
    return None


def detect_access_block(*, url: str, status: int | None = None,
                        body: str | None = None) -> AccessBlock | None:
    interstitial = classify_interstitial(body)
    if status == 403:
        return AccessBlock(url=url, status=status,
                           reason=interstitial or 'http_403')
    if interstitial is not None:
        return AccessBlock(url=url, status=status, reason=interstitial)
    return None


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
