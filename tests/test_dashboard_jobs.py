"""Local dashboard job lifecycle against the real extraction APIs (mocked)."""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from shopextract._access import ACCESS_BLOCKED
from shopextract._models import ExtractionResult, ExtractionTier, Platform, Product, Variant
from shopextract.dashboard.app import create_app
from shopextract.dashboard.jobs import MODE_MAX


@pytest.fixture
async def client(tmp_path, monkeypatch):
    app = create_app(export_root=tmp_path / 'jobs')
    server = TestServer(app)
    test_client = TestClient(server)
    await test_client.start_server()
    yield test_client, monkeypatch, app
    await test_client.close()


def patch_extract(monkeypatch, fake):
    import shopextract
    import shopextract._extract as extract_module
    monkeypatch.setattr(shopextract, 'extract', fake)
    monkeypatch.setattr(extract_module, 'extract', fake)


async def wait_terminal(client, job_id, *, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    status = None
    while asyncio.get_event_loop().time() < deadline:
        status = await (await client.get(f'/api/jobs/{job_id}')).json()
        if status['job']['state'] in {'succeeded', 'partial', 'failed', 'blocked', 'cancelled'}:
            return status
        await asyncio.sleep(0.05)
    return status


@pytest.mark.asyncio
async def test_start_preview_download_and_complete(client):
    test_client, monkeypatch, _app = client

    async def fake_extract(url, **kwargs):
        assert kwargs.get('max_urls') == MODE_MAX['test']
        assert kwargs.get('platform') is Platform.SHOPIFY
        products = [
            Product(
                title='Alpha', price=Decimal('12.00'), currency='USD',
                image_url='https://cdn.example/a.png', external_id='1',
                platform=Platform.SHOPIFY,
                variants=[Variant(
                    variant_id='11', title='Default', price=Decimal('12.00'),
                    featured_image='https://cdn.example/va.png', featured_image_id='9',
                )],
            ),
            Product(
                title='Beta', price=Decimal('15.00'), currency='USD',
                image_url='https://cdn.example/b.png', external_id='2',
                platform=Platform.SHOPIFY,
            ),
        ]
        return ExtractionResult(
            products=products, platform=Platform.SHOPIFY, tier=ExtractionTier.API,
            catalog_complete=True, urls_attempted=2, urls_succeeded=2,
        )

    patch_extract(monkeypatch, fake_extract)
    payload = await (await test_client.post('/api/jobs', json={
        'url': 'https://shop.example/collections/all',
        'tab': 'shopify',
        'mode': 'test',
    })).json()
    assert payload['ok'] is True
    status = await wait_terminal(test_client, payload['job']['job_id'])
    assert status['job']['state'] == 'succeeded'
    assert status['job']['catalog_complete'] is True
    assert len(status['job']['preview']) == 2
    assert status['job']['preview'][0]['title'] == 'Alpha'
    assert status['job']['downloads']['products_json'] is True

    download = await test_client.get(f"/api/jobs/{payload['job']['job_id']}/download/products")
    assert download.status == 200
    body = json.loads(await download.text())
    assert body['product_count'] == 2
    assert body['products'][0]['variants'][0]['featured_image'] == 'https://cdn.example/va.png'
    report = await test_client.get(f"/api/jobs/{payload['job']['job_id']}/download/report")
    assert report.status == 200
    assert status['job']['downloads']['shopify_csv'] is True
    shopify = await test_client.get(f"/api/jobs/{payload['job']['job_id']}/download/shopify")
    assert shopify.status == 200
    csv_text = await shopify.text()
    assert 'URL handle' in csv_text
    assert 'Alpha' in csv_text
    assert 'https://cdn.example/a.png' in csv_text


@pytest.mark.asyncio
async def test_cancel_job(client):
    test_client, monkeypatch, _app = client
    started = asyncio.Event()

    async def slow_extract(*a, **k):
        started.set()
        await asyncio.sleep(30)
        return ExtractionResult(products=[], platform=Platform.GENERIC)

    patch_extract(monkeypatch, slow_extract)
    payload = await (await test_client.post('/api/jobs', json={
        'url': 'https://shop.example/collections/all', 'tab': 'auto', 'mode': 'test',
    })).json()
    job_id = payload['job']['job_id']
    await asyncio.wait_for(started.wait(), timeout=2)
    cancelled = await (await test_client.post(f'/api/jobs/{job_id}/cancel')).json()
    assert cancelled['ok'] is True
    status = await wait_terminal(test_client, job_id)
    assert status['job']['state'] == 'cancelled'
    assert status['job']['downloads']['products_json'] is False


@pytest.mark.asyncio
async def test_access_blocked_outcome_preserves_export(client):
    test_client, monkeypatch, app = client

    async def blocked_extract(*a, **k):
        return ExtractionResult(
            products=[], platform=Platform.GENERIC, tier=ExtractionTier.UNIFIED_CRAWL,
            catalog_complete=False, incompleteness_reasons=[ACCESS_BLOCKED],
            errors=[f'{ACCESS_BLOCKED} url=https://blocked.example status=403 reason=waiting_interstitial'],
        )

    patch_extract(monkeypatch, blocked_extract)
    manager = app['jobs']
    prior = manager.export_root / 'prior' / 'products.json'
    prior.parent.mkdir(parents=True, exist_ok=True)
    prior.write_text(json.dumps({'product_count': 1, 'products': [{'title': 'Kept'}]}), encoding='utf-8')

    payload = await (await test_client.post('/api/jobs', json={
        'url': 'https://blocked.example/collection', 'tab': 'custom', 'mode': 'test',
    })).json()
    status = await wait_terminal(test_client, payload['job']['job_id'])
    assert status['job']['state'] == 'blocked'
    assert ACCESS_BLOCKED in status['job']['message']
    assert status['job']['downloads']['products_json'] is False
    assert json.loads(prior.read_text(encoding='utf-8'))['products'][0]['title'] == 'Kept'


@pytest.mark.asyncio
async def test_partial_outcome_writes_exports(client):
    test_client, monkeypatch, _app = client

    async def partial_extract(*a, **k):
        return ExtractionResult(
            products=[Product(title='Only one', price=Decimal('9.00'), currency='USD',
                              image_url='https://cdn.example/o.png', platform=Platform.SHOPIFY)],
            platform=Platform.SHOPIFY, tier=ExtractionTier.API,
            catalog_complete=False, incompleteness_reasons=['product_budget_reached'],
            urls_attempted=5, urls_succeeded=1,
        )

    patch_extract(monkeypatch, partial_extract)
    payload = await (await test_client.post('/api/jobs', json={
        'url': 'https://shop.example/collections/all', 'tab': 'shopify', 'mode': 'test',
    })).json()
    status = await wait_terminal(test_client, payload['job']['job_id'])
    assert status['job']['state'] == 'partial'
    assert status['job']['downloads']['products_json'] is True
    assert 'product_budget_reached' in status['job']['incompleteness_reasons']


@pytest.mark.asyncio
async def test_interactive_live_preview_endpoint(client):
    test_client, monkeypatch, app = client
    saw_preview = asyncio.Event()

    async def extract_with_live(url, **kwargs):
        from shopextract.extractors._interactive_browser import _product_hook, _snapshot_hook
        job = next(iter(app['jobs'].jobs.values()))
        product_hook = _product_hook.get()
        snapshot_hook = _snapshot_hook.get()
        assert product_hook is not None
        assert snapshot_hook is not None
        snapshot_hook(b'\xff\xd8\xff\xd9', 'collection_open')  # minimal jpeg-ish bytes
        product_hook({
            'title': 'Live Bag',
            'price': '100',
            'currency': 'USD',
            'image_url': 'https://cdn.example/live.jpg',
            'product_url': url,
            'variants': [],
        })
        # Give the status poll a chance to observe streaming preview.
        for _ in range(20):
            if job.preview:
                saw_preview.set()
                break
            await asyncio.sleep(0.05)
        return ExtractionResult(
            products=[Product(title='Live Bag', price=Decimal('100'), currency='USD',
                              image_url='https://cdn.example/live.jpg', platform=Platform.GENERIC)],
            platform=Platform.GENERIC, tier=ExtractionTier.UNIFIED_CRAWL,
            catalog_complete=False, incompleteness_reasons=['interactive_coverage_unverified'],
            urls_attempted=1, urls_succeeded=1,
        )

    patch_extract(monkeypatch, extract_with_live)
    payload = await (await test_client.post('/api/jobs', json={
        'url': 'https://us.louisvuitton.com/eng-us/women/handbags/new-in-handbags/_/N-t1dzbzff',
        'tab': 'custom',
        'mode': 'test',
        'interactive_browser': True,
    })).json()
    job_id = payload['job']['job_id']
    await asyncio.wait_for(saw_preview.wait(), timeout=3)
    status = await (await test_client.get(f'/api/jobs/{job_id}')).json()
    assert status['job']['preview'][0]['title'] == 'Live Bag'
    assert status['job']['live_preview']['available'] is True
    image = await test_client.get(f'/api/jobs/{job_id}/live-preview')
    assert image.status == 200
    assert image.headers.get('Content-Type', '').startswith('image/')
    await wait_terminal(test_client, job_id, timeout=5)


@pytest.mark.asyncio
async def test_interactive_continue_writes_sentinel(client):
    test_client, monkeypatch, _app = client
    started = asyncio.Event()
    cont_seen = asyncio.Event()

    async def waiting_extract(url, **kwargs):
        from shopextract.extractors._interactive_browser import continue_path, clear_continue
        # Resolve profile from the live job after start.
        job = next(iter(_app['jobs'].jobs.values()))
        profile = Path(job.browser_profile_dir)
        clear_continue(profile)
        started.set()
        deadline = asyncio.get_event_loop().time() + 4
        while asyncio.get_event_loop().time() < deadline:
            if continue_path(profile).is_file():
                cont_seen.set()
                clear_continue(profile)
                return ExtractionResult(
                    products=[Product(title='LV Bag', price=Decimal('100'), currency='USD',
                                      platform=Platform.GENERIC)],
                    platform=Platform.GENERIC, tier=ExtractionTier.UNIFIED_CRAWL,
                    catalog_complete=False, incompleteness_reasons=['interactive_coverage_unverified'],
                    urls_attempted=1, urls_succeeded=1,
                )
            await asyncio.sleep(0.05)
        return ExtractionResult(
            products=[], platform=Platform.GENERIC, catalog_complete=False,
            incompleteness_reasons=[ACCESS_BLOCKED],
            errors=[f'{ACCESS_BLOCKED} url={url} status=403 reason=waiting_interstitial'],
        )

    patch_extract(monkeypatch, waiting_extract)
    payload = await (await test_client.post('/api/jobs', json={
        'url': 'https://us.louisvuitton.com/eng-us/women/handbags/new-in-handbags/_/N-t1dzbzff',
        'tab': 'custom',
        'mode': 'test',
        'interactive_browser': True,
    })).json()
    assert payload['ok'] is True
    assert payload['job']['interactive_browser'] is True
    assert payload['job']['browser_profile_dir']
    job_id = payload['job']['job_id']
    await asyncio.wait_for(started.wait(), timeout=2)
    cont = await (await test_client.post(f'/api/jobs/{job_id}/continue')).json()
    assert cont['ok'] is True
    status = await wait_terminal(test_client, job_id, timeout=5)
    assert status['job']['state'] == 'partial'
    assert status['job']['preview'][0]['title'] == 'LV Bag'
    assert cont_seen.is_set()
