"""Run pace and denial cooldown.

Shopify is the source of truth for products already uploaded. This module
stores only the last access denial so a new run can wait out the cooldown.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

COOLDOWN_HOURS = 6
MAX_PRODUCTS_PER_RUN = 15
TEST_PRODUCTS_PER_RUN = 5
DELAY_MIN_SECONDS = 20
DELAY_MAX_SECONDS = 60
SCHEDULE_INTERVAL_HOURS = 24

_PACE: ContextVar['RunPace | None'] = ContextVar('productharbor_run_pace', default=None)
_KNOWN_FAMILIES: ContextVar[frozenset[str] | None] = ContextVar(
    'productharbor_known_families', default=None,
)


@dataclass(frozen=True)
class RunPace:
    delay_min_seconds: float = DELAY_MIN_SECONDS
    delay_max_seconds: float = DELAY_MAX_SECONDS
    max_products: int = MAX_PRODUCTS_PER_RUN


def products_this_run(mode: str) -> int:
    """Test stays a 5-product sample. Every other mode uses the batch cap."""
    if mode == 'test':
        return TEST_PRODUCTS_PER_RUN
    return MAX_PRODUCTS_PER_RUN


def control_path(artifacts_root: Path) -> Path:
    return Path(artifacts_root) / 'run-control.json'


def _now(moment: datetime | None = None) -> datetime:
    if moment is None:
        return datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def record_denial(
    artifacts_root: Path,
    *,
    url: str,
    reference: str | None,
    completed: int,
    when: datetime | None = None,
) -> dict:
    moment = _now(when)
    payload = {
        'denied_at': moment.isoformat(),
        'denied_url': url,
        'reference': reference,
        'completed_products': int(completed),
    }
    path = control_path(artifacts_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    temporary.replace(path)
    return payload


def cooldown_status(
    artifacts_root: Path,
    *,
    now: datetime | None = None,
    cooldown_hours: float = COOLDOWN_HOURS,
) -> dict:
    """Return whether a new run is allowed, and how long is left."""
    moment = _now(now)
    path = control_path(artifacts_root)
    if not path.is_file():
        return {'active': False, 'remaining_seconds': 0, 'message': '', 'denied_at': None, 'reference': None}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {'active': False, 'remaining_seconds': 0, 'message': '', 'denied_at': None, 'reference': None}
    raw = data.get('denied_at')
    if not isinstance(raw, str) or not raw:
        return {'active': False, 'remaining_seconds': 0, 'message': '', 'denied_at': None, 'reference': None}
    try:
        denied_at = datetime.fromisoformat(raw)
    except ValueError:
        return {'active': False, 'remaining_seconds': 0, 'message': '', 'denied_at': None, 'reference': None}
    if denied_at.tzinfo is None:
        denied_at = denied_at.replace(tzinfo=timezone.utc)
    opens_at = denied_at + timedelta(hours=float(cooldown_hours))
    remaining = (opens_at - moment).total_seconds()
    reference = data.get('reference') if isinstance(data.get('reference'), str) else None
    if remaining <= 0:
        return {
            'active': False,
            'remaining_seconds': 0,
            'message': '',
            'denied_at': denied_at.isoformat(),
            'reference': reference,
        }
    hours = int(remaining // 3600)
    minutes = int((remaining % 3600) // 60)
    left = f'{hours}h {minutes}m' if hours else f'{minutes}m'
    ref = f' Reference {reference}.' if reference else ''
    return {
        'active': True,
        'remaining_seconds': int(remaining),
        'message': f'The store denied this browser. A new run can start in {left}.{ref}',
        'denied_at': denied_at.isoformat(),
        'reference': reference,
        'denied_url': data.get('denied_url') if isinstance(data.get('denied_url'), str) else None,
    }


@contextmanager
def activate_run_pace(pace: RunPace | None = None):
    token = _PACE.set(pace or RunPace())
    try:
        yield
    finally:
        _PACE.reset(token)


@contextmanager
def activate_known_families(family_ids: set[str] | frozenset[str]):
    token = _KNOWN_FAMILIES.set(frozenset(family_id.lower() for family_id in family_ids))
    try:
        yield
    finally:
        _KNOWN_FAMILIES.reset(token)


def current_pace() -> RunPace | None:
    return _PACE.get()


def current_known_families() -> frozenset[str]:
    return _KNOWN_FAMILIES.get() or frozenset()
