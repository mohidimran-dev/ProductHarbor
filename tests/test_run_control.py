"""Denial cooldown and batch pace. No product database."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from shopextract.run_control import (
    COOLDOWN_HOURS,
    MAX_PRODUCTS_PER_RUN,
    cooldown_status,
    products_this_run,
    record_denial,
)


def test_products_this_run_keeps_test_small_and_caps_batches():
    assert products_this_run('test') == 5
    assert products_this_run('full') == MAX_PRODUCTS_PER_RUN == 15


def test_cooldown_blocks_until_window_passes(tmp_path):
    denied_at = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
    record_denial(
        tmp_path,
        url='https://us.louisvuitton.com/eng-us/men/shoes',
        reference='#0.75dac817.1794819823.200497c4',
        completed=3,
        when=denied_at,
    )
    active = cooldown_status(tmp_path, now=denied_at + timedelta(hours=1), cooldown_hours=COOLDOWN_HOURS)
    assert active['active'] is True
    assert active['remaining_seconds'] > 0
    assert 'Reference #0.75dac817.1794819823.200497c4' in active['message']

    cleared = cooldown_status(
        tmp_path,
        now=denied_at + timedelta(hours=COOLDOWN_HOURS, minutes=1),
        cooldown_hours=COOLDOWN_HOURS,
    )
    assert cleared['active'] is False
    assert cleared['remaining_seconds'] == 0
