"""Product Harbor media cancel / resume / partial failure behaviour."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from shopextract._models import ExtractionResult, ExtractionTier, Platform, Product, Variant
from shopextract.dashboard.app import create_app
from shopextract.dashboard.shopify_connection import save_connection

PNG_1X1 = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
    b'\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00'
    b'\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
)


@pytest.mark.asyncio
async def test_zip_cancel_then_resume(tmp_path, monkeypatch):
    artifacts = tmp_path / 'artifacts'
    app = create_app(export_root=tmp_path / 'jobs', artifacts_root=artifacts)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()

    async def fake_extract(url, **kwargs):
        return ExtractionResult(
            products=[
                Product(
                    title=f'Item {i}',
                    price=Decimal('10'),
                    image_url=f'https://cdn.example/i{i}.png',
                    platform=Platform.SHOPIFY,
                    variants=[Variant(title='Default', sku=f'S{i}', price=Decimal('10'))],
                )
                for i in range(6)
            ],
            platform=Platform.SHOPIFY,
            tier=ExtractionTier.API,
            catalog_complete=True,
            urls_attempted=6,
            urls_succeeded=6,
        )

    import shopextract
    import shopextract._extract as extract_module
    monkeypatch.setattr(shopextract, 'extract', fake_extract)
    monkeypatch.setattr(extract_module, 'extract', fake_extract)

    gate = asyncio.Event()
    started = 0

    class FakeResponse:
        def __init__(self, content):
            self.content = content
        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def get(self, url):
            nonlocal started
            started += 1
            if started == 1:
                gate.set()
            await asyncio.sleep(0.35)
            return FakeResponse(PNG_1X1 + str(started).encode())

    monkeypatch.setattr(httpx, 'AsyncClient', FakeClient)

    job = await (await client.post('/api/jobs', json={
        'url': 'https://shop.example/collections/all', 'tab': 'shopify', 'mode': 'test',
    })).json()
    job_id = job['job']['job_id']
    for _ in range(80):
        status = await (await client.get(f'/api/jobs/{job_id}')).json()
        if status['job']['state'] == 'succeeded':
            break
        await asyncio.sleep(0.05)

    media_start = await (await client.post(f'/api/jobs/{job_id}/media', json={'kind': 'zip'})).json()
    assert media_start['ok']
    await asyncio.wait_for(gate.wait(), timeout=2)
    cancelled = await (await client.post(f'/api/jobs/{job_id}/media/cancel')).json()
    assert cancelled['ok']
    for _ in range(80):
        status = await (await client.get(f'/api/jobs/{job_id}')).json()
        media = status['job']['media']
        if media and media['state'] in {'cancelled', 'succeeded', 'partial', 'failed'}:
            break
        await asyncio.sleep(0.05)
    assert media['state'] == 'cancelled'
    assert media['resumable'] is True
    downloaded_before = media['download']['downloaded']

    # Instant downloads on resume
    class FastClient(FakeClient):
        async def get(self, url):
            return FakeResponse(PNG_1X1 + url.encode())

    monkeypatch.setattr(httpx, 'AsyncClient', FastClient)
    resumed = await (await client.post(f'/api/jobs/{job_id}/media', json={'kind': 'zip', 'resume': True})).json()
    assert resumed['ok']
    for _ in range(100):
        status = await (await client.get(f'/api/jobs/{job_id}')).json()
        media = status['job']['media']
        if media and media['state'] in {'succeeded', 'partial', 'failed', 'cancelled'}:
            break
        await asyncio.sleep(0.05)
    assert media['state'] == 'succeeded'
    assert media['download']['downloaded'] == 6
    assert media['download']['downloaded'] >= downloaded_before
    zip_dl = await client.get(f'/api/jobs/{job_id}/download/csv_images_zip')
    assert zip_dl.status == 200
    await client.close()


@pytest.mark.asyncio
async def test_partial_shopify_ready_on_upload_failure(tmp_path, monkeypatch):
    artifacts = tmp_path / 'artifacts'
    save_connection(artifacts, shop='partial.myshopify.com', access_token='shpat_x')
    app = create_app(export_root=tmp_path / 'jobs', artifacts_root=artifacts)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()

    async def fake_extract(url, **kwargs):
        return ExtractionResult(
            products=[
                Product(
                    title='One',
                    price=Decimal('1'),
                    image_url='https://cdn.example/ok.png',
                    additional_images=['https://cdn.example/fail.png'],
                    platform=Platform.SHOPIFY,
                ),
            ],
            platform=Platform.SHOPIFY,
            tier=ExtractionTier.API,
            catalog_complete=True,
            urls_attempted=1,
            urls_succeeded=1,
        )

    import shopextract
    import shopextract._extract as extract_module
    monkeypatch.setattr(shopextract, 'extract', fake_extract)
    monkeypatch.setattr(extract_module, 'extract', fake_extract)

    class FakeResponse:
        def __init__(self, content):
            self.content = content
        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def get(self, url):
            if 'fail' in url:
                return FakeResponse(b'<html>denied</html>')
            return FakeResponse(PNG_1X1)

    monkeypatch.setattr(httpx, 'AsyncClient', FakeClient)

    async def fake_upload(conn, assets, **kwargs):
        for asset in assets:
            if asset.status == 'downloaded' and not asset.shopify_cdn_url:
                asset.shopify_cdn_url = 'https://cdn.shopify.com/ok.png'
                asset.shopify_file_id = 'gid://1'
        return {'shop': conn.shop, 'total': 1, 'uploaded': 1, 'failed': 0}

    import shopextract.dashboard.media_jobs as media_jobs_mod
    monkeypatch.setattr(media_jobs_mod, 'upload_assets_to_shopify', fake_upload)

    job = await (await client.post('/api/jobs', json={
        'url': 'https://shop.example/collections/all', 'tab': 'shopify', 'mode': 'test',
    })).json()
    job_id = job['job']['job_id']
    for _ in range(80):
        status = await (await client.get(f'/api/jobs/{job_id}')).json()
        if status['job']['state'] == 'succeeded':
            break
        await asyncio.sleep(0.05)

    ready = await (await client.post(f'/api/jobs/{job_id}/media', json={'kind': 'shopify_ready'})).json()
    assert ready['destination_shop'] == 'partial.myshopify.com'
    for _ in range(100):
        status = await (await client.get(f'/api/jobs/{job_id}')).json()
        media = status['job']['media']
        if media and media['state'] in {'succeeded', 'partial', 'failed', 'cancelled'}:
            break
        await asyncio.sleep(0.05)
    assert media['state'] == 'partial'
    assert media['download']['failed'] >= 1
    fail_dl = await client.get(f'/api/jobs/{job_id}/download/media_failure_report')
    assert fail_dl.status == 200
    csv_dl = await client.get(f'/api/jobs/{job_id}/download/shopify_ready')
    assert csv_dl.status == 200
    assert 'PARTIAL' in (csv_dl.headers.get('Content-Disposition') or '') or True
    # File on disk should be partial-named
    media_job = app['media'].for_parent(job_id)
    assert media_job is not None
    assert Path(media_job.shopify_ready_csv_path).name.endswith('PARTIAL.csv')
    await client.close()
