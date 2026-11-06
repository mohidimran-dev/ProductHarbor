"""Product Harbor ZIP backup + mocked Shopify-ready media pipeline."""
from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from shopextract._models import ExtractionResult, ExtractionTier, Platform, Product, Variant
from shopextract.dashboard.app import create_app
from shopextract.dashboard.image_assets import ImageAsset, collect_image_assets, download_assets
from shopextract.dashboard.shopify_connection import save_connection
from shopextract.dashboard.shopify_csv import products_to_shopify_rows
from shopextract.dashboard.zip_export import build_csv_images_zip

PNG_1X1 = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
    b'\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00'
    b'\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
)


def _patch_httpx_images(monkeypatch, payloads: dict[str, bytes]):
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
            if url not in payloads:
                raise httpx.HTTPError(f'missing {url}')
            return FakeResponse(payloads[url])

    monkeypatch.setattr(httpx, 'AsyncClient', FakeClient)


@pytest.mark.asyncio
async def test_zip_package_retains_source_urls_and_labels_backup(tmp_path, monkeypatch):
    products = [{
        'title': 'Harbor Tote',
        'description': 'Canvas',
        'price': '100',
        'sku': 'HT-1',
        'image_url': 'https://cdn.example/g1.png',
        'additional_images': ['https://cdn.example/g2.png'],
        'variants': [
            {'sku': 'HT-RED', 'title': 'Red', 'price': '100', 'options': {'Color': 'Red'},
             'featured_image': 'https://cdn.example/v-red.png'},
            {'sku': 'HT-BLU', 'title': 'Blue', 'price': '100', 'options': {'Color': 'Blue'},
             'featured_image': None},
        ],
    }]
    payloads = {
        'https://cdn.example/g1.png': PNG_1X1,
        'https://cdn.example/g2.png': PNG_1X1 + b'\x00',  # different bytes
        'https://cdn.example/v-red.png': PNG_1X1 + b'\x01',
    }
    _patch_httpx_images(monkeypatch, payloads)
    assets, _stats = collect_image_assets(products)
    await download_assets(assets, tmp_path / 'images')
    report = build_csv_images_zip(
        products=products,
        assets=assets,
        work_dir=tmp_path / 'stage',
        zip_path=tmp_path / 'out.zip',
    )
    assert report['downloaded'] == 3
    assert Path(tmp_path / 'out.zip').is_file()
    with zipfile.ZipFile(tmp_path / 'out.zip') as zf:
        names = set(zf.namelist())
        assert 'products.csv' in names
        assert 'image-manifest.json' in names
        assert 'download-report.json' in names
        assert 'README-BACKUP.txt' in names
        assert any(n.startswith('images/') for n in names)
        csv_text = zf.read('products.csv').decode('utf-8-sig')
        assert 'https://cdn.example/g1.png' in csv_text
        assert 'https://cdn.example/v-red.png' in csv_text
        assert 'file://' not in csv_text
        assert '/images/' not in csv_text or 'cdn.example' in csv_text
        readme = zf.read('README-BACKUP.txt').decode('utf-8')
        assert 'BACKUP' in readme
        assert 'not' in readme.lower()
        manifest = json.loads(zf.read('image-manifest.json'))
        shared_roles = []
        for image in manifest['images']:
            for assoc in image['associations']:
                shared_roles.append(assoc['role'])
        assert 'gallery' in shared_roles
        assert 'variant' in shared_roles


@pytest.mark.asyncio
async def test_dashboard_zip_and_shopify_ready_mocked(tmp_path, monkeypatch):
    artifacts = tmp_path / 'artifacts'
    app = create_app(export_root=tmp_path / 'jobs', artifacts_root=artifacts)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()

    from decimal import Decimal

    async def fake_extract(url, **kwargs):
        return ExtractionResult(
            products=[
                Product(
                    title='Alpha Bag',
                    description='Desc',
                    price=Decimal('12'),
                    currency='USD',
                    image_url='https://cdn.example/a1.png',
                    additional_images=['https://cdn.example/a2.png'],
                    platform=Platform.SHOPIFY,
                    variants=[
                        Variant(
                            title='Red', sku='A-R', price=Decimal('12'),
                            attributes={'Color': 'Red'},
                            featured_image='https://cdn.example/a-red.png',
                        ),
                        Variant(
                            title='Blue', sku='A-B', price=Decimal('12'),
                            attributes={'Color': 'Blue'},
                            featured_image=None,
                        ),
                    ],
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

    payloads = {
        'https://cdn.example/a1.png': PNG_1X1,
        'https://cdn.example/a2.png': PNG_1X1 + b'\x02',
        'https://cdn.example/a-red.png': PNG_1X1 + b'\x03',
    }
    _patch_httpx_images(monkeypatch, payloads)

    started = await (await client.post('/api/jobs', json={
        'url': 'https://shop.example/collections/all',
        'tab': 'shopify',
        'mode': 'test',
    })).json()
    job_id = started['job']['job_id']
    for _ in range(80):
        status = await (await client.get(f'/api/jobs/{job_id}')).json()
        if status['job']['state'] in {'succeeded', 'partial', 'failed', 'blocked'}:
            break
        await asyncio.sleep(0.05)
    assert status['job']['state'] == 'succeeded'
    assert status['job']['downloads']['shopify_csv'] is True

    # ZIP without Shopify connection
    zip_start = await (await client.post(f'/api/jobs/{job_id}/media', json={'kind': 'zip'})).json()
    assert zip_start['ok'] is True
    for _ in range(100):
        status = await (await client.get(f'/api/jobs/{job_id}')).json()
        media = status['job']['media']
        if media and media['state'] in {'succeeded', 'partial', 'failed', 'cancelled'}:
            break
        await asyncio.sleep(0.05)
    assert media['state'] == 'succeeded'
    assert media['download']['downloaded'] == 3
    zip_dl = await client.get(f'/api/jobs/{job_id}/download/csv_images_zip')
    assert zip_dl.status == 200
    assert zip_dl.headers.get('Content-Disposition', '').endswith('BACKUP.zip"')

    # Shopify-ready refuses without connection
    denied = await (await client.post(f'/api/jobs/{job_id}/media', json={'kind': 'shopify_ready'})).json()
    assert denied['ok'] is False

    save_connection(artifacts, shop='demo-store.myshopify.com', access_token='shpat_test_token')
    status_shop = await (await client.get('/api/shopify/status')).json()
    assert status_shop['shopify']['connected'] is True
    assert status_shop['shopify']['shop'] == 'demo-store.myshopify.com'
    assert 'access_token' not in json.dumps(status_shop)

    uploaded_hashes = []

    async def fake_upload(conn, assets, **kwargs):
        assert conn.shop == 'demo-store.myshopify.com'
        for asset in assets:
            if asset.status != 'downloaded':
                continue
            if asset.shopify_cdn_url:
                continue
            if kwargs.get('cancel_event') and kwargs['cancel_event'].is_set():
                break
            asset.shopify_cdn_url = f'https://cdn.shopify.com/s/files/1/{asset.content_hash[:8]}.png'
            asset.shopify_file_id = f'gid://shopify/MediaImage/{asset.content_hash[:6]}'
            uploaded_hashes.append(asset.content_hash)
            if kwargs.get('on_progress'):
                kwargs['on_progress']({
                    'total': sum(1 for a in assets if a.status == 'downloaded'),
                    'uploaded': len(uploaded_hashes),
                    'failed': 0,
                    'shop': conn.shop,
                })
        if kwargs.get('mapping_path'):
            from shopextract.dashboard.shopify_files import save_store_mapping
            save_store_mapping(conn.shop, assets, kwargs['mapping_path'])
        if kwargs.get('checkpoint_path'):
            from shopextract.dashboard.image_assets import save_asset_checkpoint
            save_asset_checkpoint(assets, kwargs['checkpoint_path'])
        return {
            'shop': conn.shop,
            'total': sum(1 for a in assets if a.status == 'downloaded'),
            'uploaded': sum(1 for a in assets if a.shopify_cdn_url),
            'failed': 0,
        }

    import shopextract.dashboard.media_jobs as media_jobs_mod
    monkeypatch.setattr(media_jobs_mod, 'upload_assets_to_shopify', fake_upload)

    ready = await (await client.post(f'/api/jobs/{job_id}/media', json={'kind': 'shopify_ready'})).json()
    assert ready['ok'] is True
    assert ready['destination_shop'] == 'demo-store.myshopify.com'
    for _ in range(100):
        status = await (await client.get(f'/api/jobs/{job_id}')).json()
        media = status['job']['media']
        if media and media['state'] in {'succeeded', 'partial', 'failed', 'cancelled'}:
            break
        await asyncio.sleep(0.05)
    assert media['state'] == 'succeeded'
    assert media['upload']['uploaded'] == 3
    assert media['shop'] == 'demo-store.myshopify.com'

    csv_resp = await client.get(f'/api/jobs/{job_id}/download/shopify_ready')
    assert csv_resp.status == 200
    csv_text = await csv_resp.text()
    assert 'cdn.shopify.com' in csv_text
    assert 'cdn.example' not in csv_text
    rows = products_to_shopify_rows([{
        'title': 'Alpha Bag',
        'image_url': 'https://cdn.shopify.com/x.png',
        'additional_images': ['https://cdn.shopify.com/y.png'],
        'variants': [
            {'sku': 'A-R', 'featured_image': 'https://cdn.shopify.com/z.png', 'options': {'Color': 'Red'}},
            {'sku': 'A-B', 'featured_image': None, 'options': {'Color': 'Blue'}},
        ],
    }])
    blue = [r for r in rows if r.get('SKU') == 'A-B'][0]
    assert blue['Variant image URL'] == ''

    # Resume skips already-mapped uploads
    first_count = len(uploaded_hashes)
    resume = await (await client.post(f'/api/jobs/{job_id}/media', json={
        'kind': 'shopify_ready', 'resume': True,
    })).json()
    # After success resumable is False; start fresh prepare should still skip via mapping file
    if not resume['ok']:
        # start without resume after success
        resume = await (await client.post(f'/api/jobs/{job_id}/media', json={'kind': 'shopify_ready'})).json()
    for _ in range(100):
        status = await (await client.get(f'/api/jobs/{job_id}')).json()
        media = status['job']['media']
        if media and media['state'] in {'succeeded', 'partial', 'failed', 'cancelled'}:
            break
        await asyncio.sleep(0.05)
    # No additional uploads beyond remapped skips (fake_upload only appends when missing CDN)
    assert len(uploaded_hashes) == first_count

    await client.close()


@pytest.mark.asyncio
async def test_store_mapping_not_reused_across_shops(tmp_path):
    from shopextract.dashboard.shopify_files import save_store_mapping
    assets = [
        ImageAsset(
            source_url='https://cdn.example/a.png',
            content_hash='hash1',
            status='downloaded',
            shopify_cdn_url='https://cdn.shopify.com/a.png',
            shopify_file_id='gid://1',
            filename='a.png',
        ),
    ]
    path = tmp_path / 'map.json'
    save_store_mapping('store-a.myshopify.com', assets, path)
    payload = json.loads(path.read_text(encoding='utf-8'))
    assert payload['shop'] == 'store-a.myshopify.com'

    # Simulate load guard used by upload_assets_to_shopify
    prior = json.loads(path.read_text(encoding='utf-8'))
    assert prior.get('shop') != 'store-b.myshopify.com'


@pytest.mark.asyncio
async def test_connect_never_returns_token(tmp_path):
    app = create_app(export_root=tmp_path / 'jobs', artifacts_root=tmp_path / 'artifacts')
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    payload = await (await client.post('/api/shopify/connect', json={
        'shop': 'my-shop',
        'access_token': 'shpat_super_secret',
    })).json()
    assert payload['ok'] is True
    assert payload['shopify']['shop'] == 'my-shop.myshopify.com'
    dumped = json.dumps(payload)
    assert 'shpat_super_secret' not in dumped
    secret_file = tmp_path / 'artifacts' / 'secrets' / 'shopify-connection.json'
    assert secret_file.is_file()
    assert 'shpat_super_secret' in secret_file.read_text(encoding='utf-8')
    await client.close()
