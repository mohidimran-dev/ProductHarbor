"""Shopify CSV import parser and idempotent product write path."""
from __future__ import annotations

import pytest

from shopextract.dashboard.shopify_connection import ShopifyConnection
from shopextract.dashboard.shopify_csv_import import (
    CsvProduct,
    CsvVariant,
    import_csv_product,
    ledger_get,
    merge_csv_products_by_title,
    parse_shopify_product_csv,
)


MULTI_VARIANT_CSV = """Handle,Title,Body (HTML),Option1 Name,Option1 Value,Option2 Name,Option2 Value,Variant SKU,Variant Price,Variant Compare At Price,Image Src,Variant Image
trainer,LV Trainer,,Color,Black,Size,40,SKU-B-40,1360,1500,https://cdn.example/black.jpg,https://cdn.example/black-40.jpg
trainer,,,Color,Black,Size,41,SKU-B-41,1360,1500,,https://cdn.example/black-41.jpg
trainer,,,Color,Brown,Size,40,SKU-BR-40,1680,,,https://cdn.example/brown-40.jpg
trainer,,,,,,,,,,https://cdn.example/gallery-2.jpg,
"""


LEGACY_AND_BROKEN_CSV = """URL handle,Title,Description,Option1 name,Option1 value,SKU,Price,Compare-at price,Product image URL,Variant image URL
broken-shoe,Broken Shoe,Desc,Color,Black,BR-1,10,12,https://cdn.example/missing.jpg,
good-bag,Good Bag,Bag body,Color,Tan,GB-1,20,,https://cdn.example/bag.jpg,https://cdn.example/bag-v.jpg
good-bag,,,,,,,,https://cdn.example/bag-2.jpg,
"""


def test_parse_groups_variants_gallery_and_legacy_headers():
    products = parse_shopify_product_csv(MULTI_VARIANT_CSV)
    assert len(products) == 1
    product = products[0]
    assert product.handle == 'trainer'
    assert product.title == 'LV Trainer'
    assert product.gallery[0] == 'https://cdn.example/black.jpg'
    assert 'https://cdn.example/gallery-2.jpg' in product.gallery
    assert len(product.variants) == 3
    colors = {variant.options.get('Color') for variant in product.variants}
    sizes = {variant.options.get('Size') for variant in product.variants}
    assert colors == {'Black', 'Brown'}
    assert sizes == {'40', '41'}
    assert product.variants[0].compare_at == '1500'
    assert product.variants[0].image_url == 'https://cdn.example/black-40.jpg'


def test_parse_accepts_current_product_harbor_headers():
    products = parse_shopify_product_csv(LEGACY_AND_BROKEN_CSV)
    assert [item.handle for item in products] == ['broken-shoe', 'good-bag']
    bag = products[1]
    assert bag.title == 'Good Bag'
    assert bag.gallery == ['https://cdn.example/bag.jpg', 'https://cdn.example/bag-2.jpg']
    assert bag.variants[0].options == {'Color': 'Tan'}
    assert bag.variants[0].image_url == 'https://cdn.example/bag-v.jpg'


SAME_TITLE_COLORWAY_CSV = """Handle,Title,Option1 Name,Option1 Value,Option2 Name,Option2 Value,Variant SKU,Variant Price,Image Src,Variant Image
lv-trainer-black,LV Trainer Sneaker,Color,Black,Size,40,SKU-B-40,1360,https://cdn.example/black.jpg,https://cdn.example/black-40.jpg
lv-trainer-black,,Color,Black,Size,41,SKU-B-41,1360,,https://cdn.example/black-41.jpg
lv-trainer-brown,LV Trainer Sneaker,Color,Brown,Size,40,SKU-BR-40,1680,https://cdn.example/brown.jpg,https://cdn.example/brown-40.jpg
lv-trainer-brown,,Color,Brown,Size,41,SKU-BR-41,1680,,https://cdn.example/brown-41.jpg
"""


def test_parse_merges_same_title_colorways_into_one_product():
    products = parse_shopify_product_csv(SAME_TITLE_COLORWAY_CSV)
    assert len(products) == 1
    product = products[0]
    assert product.title == 'LV Trainer Sneaker'
    assert product.handle == 'lv-trainer-sneaker'
    assert set(product.source_handles) == {'lv-trainer-black', 'lv-trainer-brown'}
    assert len(product.variants) == 4
    assert {variant.options.get('Color') for variant in product.variants} == {'Black', 'Brown'}
    assert 'https://cdn.example/black.jpg' in product.gallery
    assert 'https://cdn.example/brown.jpg' in product.gallery


COLLIDING_BEIGE_CSV = """Handle,Title,Option1 Name,Option1 Value,Option2 Name,Option2 Value,Variant SKU,Variant Price,Image Src,Variant Image
lv-trainer-nvprod111,LV Trainer Sneaker,Color,Beige,Size,40,SKU-A-40,1360,https://cdn.example/a.jpg,https://cdn.example/a-40.jpg
lv-trainer-nvprod111,,Color,Beige,Size,41,SKU-A-41,1360,,https://cdn.example/a-41.jpg
lv-trainer-nvprod222,LV Trainer Sneaker,Color,Beige,Size,40,SKU-B-40,1400,https://cdn.example/b.jpg,https://cdn.example/b-40.jpg
lv-trainer-nvprod222,,Color,Beige,Size,41,SKU-B-41,1400,,https://cdn.example/b-41.jpg
"""


def test_merge_disambiguates_duplicate_color_names_across_colorways():
    products = parse_shopify_product_csv(COLLIDING_BEIGE_CSV)
    assert len(products) == 1
    product = products[0]
    colors = {variant.options.get('Color') for variant in product.variants}
    assert 'Beige' in colors
    assert any(color.startswith('Beige (') for color in colors if color)
    option_keys = [
        tuple(sorted((variant.options or {}).items()))
        for variant in product.variants
    ]
    assert len(option_keys) == len(set(option_keys))


def test_merge_keeps_products_separate_when_variants_exceed_100():
    items = []
    for color_index in range(6):
        variants = [
            CsvVariant(
                sku=f'{color_index}-{size}',
                price='10',
                options={'Color': f'C{color_index}', 'Size': str(size)},
            )
            for size in range(20)
        ]
        items.append(CsvProduct(
            handle=f'h-{color_index}',
            title='Wide Shoe',
            variants=variants,
            source_handles=[f'h-{color_index}'],
        ))
    merged = merge_csv_products_by_title(items)
    assert len(merged) == 6


@pytest.mark.asyncio
async def test_import_skips_broken_image_and_records_csv_rows(tmp_path, monkeypatch):
    products = parse_shopify_product_csv(LEGACY_AND_BROKEN_CSV)
    broken = products[0]
    conn = ShopifyConnection(shop='demo-store.myshopify.com', access_token='shpat_unit')

    async def boom(*_a, **_k):
        raise RuntimeError('404 not found')

    monkeypatch.setattr('shopextract.dashboard.shopify_csv_import._stage_urls', boom)
    result = await import_csv_product(conn, broken, artifacts_root=tmp_path)
    assert result['status'] == 'skipped'
    assert 'Image upload failed' in result['reason']
    assert broken.source_rows[0] in result['source_rows']
    saved = ledger_get(tmp_path, conn.shop, broken.handle)
    assert saved['status'] == 'skipped'


@pytest.mark.asyncio
async def test_import_marks_network_image_failures_separately(tmp_path, monkeypatch):
    from shopextract.dashboard.shopify_csv_import import CsvNetworkError

    products = parse_shopify_product_csv(LEGACY_AND_BROKEN_CSV)
    bag = products[1]
    conn = ShopifyConnection(shop='demo-store.myshopify.com', access_token='shpat_unit')

    async def boom(*_a, **_k):
        raise CsvNetworkError('Network: could not download image after 4 tries (ConnectError:)')

    monkeypatch.setattr('shopextract.dashboard.shopify_csv_import._stage_urls', boom)
    result = await import_csv_product(conn, bag, artifacts_root=tmp_path)
    assert result['status'] == 'skipped'
    assert result['skip_kind'] == 'network'
    assert result['reason'].startswith('Network:')


@pytest.mark.asyncio
async def test_import_attaches_images_when_existing_product_has_none(tmp_path, monkeypatch):
    products = parse_shopify_product_csv(LEGACY_AND_BROKEN_CSV)
    bag = products[1]
    conn = ShopifyConnection(shop='demo-store.myshopify.com', access_token='shpat_unit')

    async def fake_stage(_conn, urls, *, alt, handle='product'):
        return {url: f'https://cdn.shopify.com/{index}.jpg' for index, url in enumerate(urls)}

    async def fake_find(_conn, handle):
        assert handle == 'good-bag'
        return {'id': 'gid://shopify/Product/55', 'handle': 'good-bag', 'media': {'nodes': []}}

    attached = {}

    async def fake_attach(_conn, *, product_id, resource_by_url, alt):
        attached['product_id'] = product_id
        attached['count'] = len(resource_by_url)
        return ['gid://shopify/MediaImage/1']

    monkeypatch.setattr('shopextract.dashboard.shopify_csv_import._stage_urls', fake_stage)
    monkeypatch.setattr('shopextract.dashboard.shopify_csv_import.find_existing_product', fake_find)
    monkeypatch.setattr('shopextract.dashboard.shopify_csv_import._attach_images_to_existing', fake_attach)

    result = await import_csv_product(conn, bag, artifacts_root=tmp_path)
    assert result['status'] == 'imported'
    assert attached['product_id'] == 'gid://shopify/Product/55'
    assert attached['count'] == 3
    assert result['admin_url'].endswith('/admin/products/55')

    # Second run must not create or re-attach.
    async def explode(*_a, **_k):
        raise AssertionError('Shopify should not be called again')

    monkeypatch.setattr('shopextract.dashboard.shopify_csv_import._stage_urls', explode)
    again = await import_csv_product(conn, bag, artifacts_root=tmp_path)
    assert again['status'] == 'skipped'
    assert again['shopify_id'] == 'gid://shopify/Product/55'


@pytest.mark.asyncio
async def test_import_creates_draft_when_product_is_new(tmp_path, monkeypatch):
    products = parse_shopify_product_csv(MULTI_VARIANT_CSV)
    product = products[0]
    conn = ShopifyConnection(shop='demo-store.myshopify.com', access_token='shpat_unit')

    async def fake_stage(_conn, urls, *, alt, handle='product'):
        return {url: f'https://cdn.shopify.com/{index}.jpg' for index, url in enumerate(urls)}

    async def fake_find(_conn, _handle):
        return None

    captured = {}

    async def fake_graphql(_conn, query, variables=None):
        captured['input'] = variables['input']
        return {
            'productSet': {
                'product': {'id': 'gid://shopify/Product/99', 'handle': 'trainer', 'status': 'DRAFT'},
                'userErrors': [],
            }
        }

    monkeypatch.setattr('shopextract.dashboard.shopify_csv_import._stage_urls', fake_stage)
    monkeypatch.setattr('shopextract.dashboard.shopify_csv_import.find_existing_product', fake_find)
    monkeypatch.setattr('shopextract.dashboard.shopify_csv_import.graphql', fake_graphql)

    result = await import_csv_product(conn, product, artifacts_root=tmp_path)
    assert result['status'] == 'imported'
    assert captured['input']['status'] == 'DRAFT'
    assert captured['input']['handle'] == 'trainer'
    assert len(captured['input']['variants']) == 3
    assert any(row.get('compareAtPrice') == '1500.00' or row.get('compareAtPrice') == '1500' for row in captured['input']['variants'])
    assert ledger_get(tmp_path, conn.shop, 'trainer')['shopify_id'] == 'gid://shopify/Product/99'


@pytest.mark.asyncio
async def test_csv_import_job_reports_live_progress(tmp_path, monkeypatch):
    from shopextract.dashboard.csv_import_jobs import CsvImportJobManager

    monkeypatch.setattr(
        'shopextract.dashboard.csv_import_jobs.load_connection',
        lambda _root: ShopifyConnection(shop='demo-store.myshopify.com', access_token='shpat_unit'),
    )

    async def fake_verify(_conn):
        return {
            'ok': True,
            'granted_scopes': ['write_files', 'read_files', 'write_products', 'read_products'],
            'missing_scopes': [],
        }

    async def fake_import(_conn, product, *, artifacts_root):
        await __import__('asyncio').sleep(0)
        if product.handle == 'broken-shoe':
            return {
                'status': 'skipped',
                'reason': 'Image upload failed',
                'handle': product.handle,
                'shopify_id': None,
                'admin_url': None,
                'source_rows': product.source_rows,
            }
        return {
            'status': 'imported',
            'reason': 'ok',
            'handle': product.handle,
            'shopify_id': 'gid://shopify/Product/1',
            'admin_url': 'https://demo-store.myshopify.com/admin/products/1',
            'source_rows': product.source_rows,
        }

    monkeypatch.setattr('shopextract.dashboard.csv_import_jobs.verify_connection_readonly', fake_verify)
    monkeypatch.setattr('shopextract.dashboard.csv_import_jobs.import_csv_product', fake_import)
    manager = CsvImportJobManager(tmp_path, export_root=tmp_path / 'jobs')
    job = await manager.start(csv_text=LEGACY_AND_BROKEN_CSV, filename='sample.csv')
    await job.task
    public = job.public()
    assert public['shop'] == 'demo-store.myshopify.com'
    assert public['counts']['total'] == 2
    assert public['counts']['processed'] == 2
    assert public['counts']['imported'] == 1
    assert public['counts']['skipped'] == 1
    assert '1 imported · 1 skipped' in public['progress_label']


@pytest.mark.asyncio
async def test_file_create_access_denied_stops_job_store_level(tmp_path, monkeypatch):
    from shopextract.dashboard.csv_import_jobs import CsvImportJobManager
    from shopextract.dashboard.shopify_files import ShopifyStoreAccessError

    monkeypatch.setattr(
        'shopextract.dashboard.csv_import_jobs.load_connection',
        lambda _root: ShopifyConnection(shop='demo-store.myshopify.com', access_token='shpat_unit'),
    )

    async def fake_verify(_conn):
        return {
            'ok': True,
            'granted_scopes': ['write_files', 'read_files', 'write_products', 'read_products'],
            'missing_scopes': [],
        }

    async def deny_import(_conn, product, *, artifacts_root):
        raise ShopifyStoreAccessError(
            'Shopify denied fileCreate even though write_files is granted on the app.'
        )

    monkeypatch.setattr('shopextract.dashboard.csv_import_jobs.verify_connection_readonly', fake_verify)
    monkeypatch.setattr('shopextract.dashboard.csv_import_jobs.import_csv_product', deny_import)
    manager = CsvImportJobManager(tmp_path, export_root=tmp_path / 'jobs')
    job = await manager.start(csv_text=LEGACY_AND_BROKEN_CSV, filename='sample.csv')
    await job.task
    public = job.public()
    assert public['state'] == 'failed'
    assert public['counts']['processed'] == 0
    assert public['counts']['skipped'] == 0
    assert public['counts']['imported'] == 0
    assert len(public['results']) == 1
    assert public['results'][0]['store_level'] is True
    assert 'fileCreate' in public['message']


@pytest.mark.asyncio
async def test_import_raises_store_access_on_filecreate_denial(tmp_path, monkeypatch):
    from shopextract.dashboard.shopify_files import ShopifyStoreAccessError

    products = parse_shopify_product_csv(LEGACY_AND_BROKEN_CSV)
    bag = products[1]
    conn = ShopifyConnection(shop='demo-store.myshopify.com', access_token='shpat_unit')

    async def deny(*_a, **_k):
        raise RuntimeError(
            'GraphQL error: Access denied for fileCreate field. '
            'Required access: `write_files` access scope. '
            'Also: Users must have create files permissions.'
        )

    monkeypatch.setattr('shopextract.dashboard.shopify_csv_import._stage_urls', deny)
    with pytest.raises(ShopifyStoreAccessError):
        await import_csv_product(conn, bag, artifacts_root=tmp_path)
    assert ledger_get(tmp_path, conn.shop, bag.handle) is None


@pytest.mark.asyncio
async def test_csv_import_endpoint_accepts_over_1mb_upload(tmp_path, monkeypatch):
    from aiohttp import FormData
    from aiohttp.test_utils import TestClient, TestServer

    from shopextract.dashboard.app import CSV_UPLOAD_MAX_BYTES, create_app
    from shopextract.dashboard.shopify_connection import save_connection

    artifacts = tmp_path / 'artifacts'
    save_connection(artifacts, shop='demo-store.myshopify.com', access_token='shpat_unit')
    monkeypatch.setattr(
        'shopextract.dashboard.csv_import_jobs.load_connection',
        lambda _root: ShopifyConnection(shop='demo-store.myshopify.com', access_token='shpat_unit'),
    )
    monkeypatch.setattr(
        'shopextract.dashboard.app.load_connection',
        lambda _root: ShopifyConnection(shop='demo-store.myshopify.com', access_token='shpat_unit'),
    )

    async def fake_verify(_conn):
        return {
            'ok': True,
            'granted_scopes': ['write_files', 'read_files', 'write_products', 'read_products'],
            'missing_scopes': [],
        }

    started = {}

    async def fake_import(_conn, product, *, artifacts_root):
        started['handle'] = product.handle
        return {
            'status': 'imported',
            'reason': 'ok',
            'handle': product.handle,
            'shopify_id': 'gid://shopify/Product/1',
            'admin_url': 'https://demo-store.myshopify.com/admin/products/1',
            'source_rows': product.source_rows,
        }

    monkeypatch.setattr('shopextract.dashboard.csv_import_jobs.verify_connection_readonly', fake_verify)
    monkeypatch.setattr('shopextract.dashboard.csv_import_jobs.import_csv_product', fake_import)

    # Many normal rows so multipart body clears the old 1 MiB default.
    lines = ['Handle,Title,Option1 Name,Option1 Value,Variant Price,Image Src']
    while sum(len(line) + 1 for line in lines) < (1100 * 1024):
        index = len(lines)
        lines.append(
            f'pad-product-{index},Pad Product {index},Title,Default Title,10,'
            f'https://cdn.example.com/images/product-{index}.jpg'
        )
    csv_text = '\n'.join(lines) + '\n'
    assert len(csv_text.encode('utf-8')) > 1024 * 1024
    assert len(csv_text.encode('utf-8')) < CSV_UPLOAD_MAX_BYTES

    app = create_app(export_root=tmp_path / 'jobs', artifacts_root=artifacts)
    assert app._client_max_size == CSV_UPLOAD_MAX_BYTES
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        form = FormData()
        form.add_field('file', csv_text, filename='big.csv', content_type='text/csv')
        response = await client.post('/api/shopify/csv-import', data=form)
        payload = await response.json()
        assert response.status == 200, payload
        assert payload['ok'] is True
        job = app['csv_imports'].get(payload['import']['job_id'])
        assert job is not None
        await job.task
        assert started['handle'].startswith('pad-product-')
    finally:
        await client.close()
