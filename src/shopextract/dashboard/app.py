"""Localhost extraction dashboard (aiohttp) — Product Harbor."""
from __future__ import annotations

from pathlib import Path

from aiohttp import web

from .csv_import_jobs import CsvImportJobManager
from .jobs import DashboardJobManager
from .media_jobs import MediaJobManager
from .shopify_catalog import LiveProductUploader
from .shopify_connection import (
    clear_connection,
    load_connection,
    public_connection_status,
    save_connection,
    verify_connection_readonly,
)
from ..run_control import (
    COOLDOWN_HOURS,
    DELAY_MAX_SECONDS,
    DELAY_MIN_SECONDS,
    MAX_PRODUCTS_PER_RUN,
    cooldown_status,
)

STATIC_DIR = Path(__file__).with_name('static')
DEFAULT_EXPORT_ROOT = Path('artifacts') / 'dashboard-jobs'
DEFAULT_ARTIFACTS_ROOT = Path('artifacts')
# Product CSV uploads (multipart) routinely exceed aiohttp's 1 MiB default.
CSV_UPLOAD_MAX_BYTES = 32 * 1024 * 1024


def create_app(
    *,
    export_root: Path | None = None,
    artifacts_root: Path | None = None,
) -> web.Application:
    export = Path(export_root or DEFAULT_EXPORT_ROOT)
    artifacts = Path(artifacts_root or DEFAULT_ARTIFACTS_ROOT)
    manager = DashboardJobManager(export, artifacts_root=artifacts)
    media = MediaJobManager(export, artifacts_root=artifacts)
    csv_imports = CsvImportJobManager(artifacts, export_root=artifacts / 'csv-imports')
    uploader = LiveProductUploader(artifacts)
    manager.on_upload_record = uploader.enqueue
    manager.catalog_uploader = uploader
    app = web.Application(client_max_size=CSV_UPLOAD_MAX_BYTES)
    app['jobs'] = manager
    app['media'] = media
    app['csv_imports'] = csv_imports
    app['uploader'] = uploader
    app['artifacts_root'] = artifacts
    app['live_upload_enabled'] = False

    @web.middleware
    async def json_http_errors(request: web.Request, handler):
        try:
            return await handler(request)
        except web.HTTPRequestEntityTooLarge:
            return web.json_response({
                'ok': False,
                'error': (
                    f'That CSV is larger than the {CSV_UPLOAD_MAX_BYTES // (1024 * 1024)} MB upload limit. '
                    'Split the file or raise CSV_UPLOAD_MAX_BYTES.'
                ),
            }, status=413)

    app.middlewares.append(json_http_errors)

    def _attach_media(job) -> None:
        media_job = media.for_parent(job.job_id)
        job.media = media_job

    async def index(_request: web.Request) -> web.Response:
        return web.FileResponse(
            STATIC_DIR / 'index.html',
            headers={'Cache-Control': 'no-store, max-age=0'},
        )

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({'ok': True, 'service': 'productharbor-dashboard', 'name': 'Product Harbor'})

    async def run_control_status(request: web.Request) -> web.Response:
        cooling = cooldown_status(request.app['artifacts_root'])
        return web.json_response({
            'ok': True,
            'run_control': {
                'cooldown': cooling,
                'max_products_per_run': MAX_PRODUCTS_PER_RUN,
                'delay_min_seconds': DELAY_MIN_SECONDS,
                'delay_max_seconds': DELAY_MAX_SECONDS,
                'cooldown_hours': COOLDOWN_HOURS,
            },
        })

    async def shopify_status(request: web.Request) -> web.Response:
        status = public_connection_status(request.app['artifacts_root'])
        status['live_upload'] = bool(request.app.get('live_upload_enabled'))
        conn = load_connection(request.app['artifacts_root'])
        if conn is not None:
            verification = await verify_connection_readonly(conn)
            status['granted_scopes'] = verification.get('granted_scopes') or []
            status['missing_scopes'] = verification.get('missing_scopes') or []
            status['files_readable'] = bool(verification.get('files_readable'))
            status['shop_name'] = verification.get('shop_name')
            status['scopes_ok'] = bool(verification.get('ok'))
            if verification.get('reauthorize_hint'):
                status['reauthorize_hint'] = verification['reauthorize_hint']
            if verification.get('error'):
                status['verify_error'] = verification['error']
        cooling = cooldown_status(request.app['artifacts_root'])
        return web.json_response({'ok': True, 'shopify': status, 'cooldown': cooling})

    async def shopify_live_upload(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        enabled = bool(body.get('enabled'))
        if enabled and load_connection(request.app['artifacts_root']) is None:
            return web.json_response({
                'ok': False,
                'error': 'Connect a Shopify store before turning on product upload.',
            }, status=400)
        request.app['live_upload_enabled'] = enabled
        job_id = body.get('job_id')
        if enabled and isinstance(job_id, str):
            job = request.app['jobs'].get(job_id)
            if job is not None and job.state == 'running':
                job.upload_to_shopify = True
        status = public_connection_status(request.app['artifacts_root'])
        status['live_upload'] = enabled
        return web.json_response({'ok': True, 'shopify': status})

    async def shopify_connect(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({'ok': False, 'error': 'Expected JSON body.'}, status=400)
        shop = body.get('shop')
        token = body.get('access_token')
        api_version = body.get('api_version')
        if not isinstance(shop, str) or not shop.strip():
            return web.json_response({'ok': False, 'error': 'Shop domain is required.'}, status=400)
        if not isinstance(token, str) or not token.strip():
            return web.json_response({'ok': False, 'error': 'Admin API access token is required.'}, status=400)
        try:
            conn = save_connection(
                request.app['artifacts_root'],
                shop=shop,
                access_token=token,
                api_version=api_version if isinstance(api_version, str) else None,
            )
        except ValueError as exc:
            return web.json_response({'ok': False, 'error': str(exc)}, status=400)
        # Never echo the token back to the browser.
        status = dict(conn.public_status)
        status['live_upload'] = bool(request.app.get('live_upload_enabled'))
        verification = await verify_connection_readonly(conn)
        status['granted_scopes'] = verification.get('granted_scopes') or []
        status['missing_scopes'] = verification.get('missing_scopes') or []
        status['files_readable'] = bool(verification.get('files_readable'))
        status['shop_name'] = verification.get('shop_name')
        status['scopes_ok'] = bool(verification.get('ok'))
        if verification.get('reauthorize_hint'):
            status['reauthorize_hint'] = verification['reauthorize_hint']
        if verification.get('error'):
            status['verify_error'] = verification['error']
            return web.json_response({
                'ok': False,
                'error': verification['error'],
                'shopify': status,
                'reauthorize_hint': verification.get('reauthorize_hint') or status.get('reauthorize_hint'),
            }, status=400)
        return web.json_response({'ok': True, 'shopify': status})

    async def shopify_disconnect(request: web.Request) -> web.Response:
        clear_connection(request.app['artifacts_root'])
        request.app['live_upload_enabled'] = False
        status = public_connection_status(request.app['artifacts_root'])
        status['live_upload'] = False
        return web.json_response({'ok': True, 'shopify': status})

    async def start_csv_import(request: web.Request) -> web.Response:
        if load_connection(request.app['artifacts_root']) is None:
            return web.json_response({
                'ok': False,
                'error': 'Connect a Shopify store before importing a CSV.',
            }, status=400)
        reader = await request.multipart()
        csv_text = None
        filename = 'products.csv'
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name in ('file', 'csv', 'upload'):
                raw = await part.read(decode=False)
                filename = Path(part.filename or filename).name or filename
                try:
                    csv_text = raw.decode('utf-8-sig')
                except UnicodeDecodeError:
                    csv_text = raw.decode('latin-1')
        if not csv_text or not csv_text.strip():
            return web.json_response({'ok': False, 'error': 'Choose a CSV file to import.'}, status=400)
        try:
            job = await request.app['csv_imports'].start(csv_text=csv_text, filename=filename)
        except ValueError as exc:
            return web.json_response({'ok': False, 'error': str(exc)}, status=400)
        except Exception as exc:
            return web.json_response({
                'ok': False,
                'error': f'Could not start CSV import: {type(exc).__name__}: {exc}',
            }, status=400)
        return web.json_response({'ok': True, 'import': job.public()})

    async def csv_import_status(request: web.Request) -> web.Response:
        job = request.app['csv_imports'].get(request.match_info['job_id'])
        if job is None:
            return web.json_response({'ok': False, 'error': 'Import job not found.'}, status=404)
        return web.json_response({'ok': True, 'import': job.public()})

    async def csv_import_latest(request: web.Request) -> web.Response:
        job = request.app['csv_imports'].latest()
        if job is None:
            return web.json_response({'ok': True, 'import': None})
        return web.json_response({'ok': True, 'import': job.public()})

    async def cancel_csv_import(request: web.Request) -> web.Response:
        try:
            job = await request.app['csv_imports'].cancel(request.match_info['job_id'])
        except KeyError:
            return web.json_response({'ok': False, 'error': 'Import job not found.'}, status=404)
        return web.json_response({'ok': True, 'import': job.public()})

    async def start_job(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({'ok': False, 'error': 'Expected JSON body.'}, status=400)
        url = body.get('url')
        tab = body.get('tab', 'auto')
        mode = body.get('mode', 'test')
        interactive = bool(body.get('interactive_browser'))
        upload = bool(body.get('upload_products')) or bool(request.app.get('live_upload_enabled'))
        schedule_daily = bool(body.get('schedule_daily'))
        if not isinstance(url, str) or not url.strip():
            return web.json_response({'ok': False, 'error': 'Collection URL is required.'}, status=400)
        if upload and load_connection(request.app['artifacts_root']) is None:
            return web.json_response({
                'ok': False,
                'error': 'Connect a Shopify store before uploading scraped products.',
            }, status=400)
        try:
            job = await request.app['jobs'].start(
                url=url,
                tab=str(tab),
                mode=str(mode),
                interactive_browser=interactive,
                upload_to_shopify=upload,
                schedule_daily=schedule_daily,
            )
        except ValueError as exc:
            return web.json_response({'ok': False, 'error': str(exc)}, status=400)
        _attach_media(job)
        return web.json_response({'ok': True, 'job': job.public()})

    async def job_status(request: web.Request) -> web.Response:
        job = request.app['jobs'].get(request.match_info['job_id'])
        if job is None:
            return web.json_response({'ok': False, 'error': 'Job not found.'}, status=404)
        _attach_media(job)
        return web.json_response({'ok': True, 'job': job.public()})

    async def cancel_job(request: web.Request) -> web.Response:
        try:
            job = await request.app['jobs'].cancel(request.match_info['job_id'])
        except KeyError:
            return web.json_response({'ok': False, 'error': 'Job not found.'}, status=404)
        _attach_media(job)
        return web.json_response({'ok': True, 'job': job.public()})

    async def continue_job(request: web.Request) -> web.Response:
        try:
            job = await request.app['jobs'].continue_interactive(request.match_info['job_id'])
        except KeyError:
            return web.json_response({'ok': False, 'error': 'Job not found.'}, status=404)
        except ValueError as exc:
            return web.json_response({'ok': False, 'error': str(exc)}, status=400)
        _attach_media(job)
        return web.json_response({'ok': True, 'job': job.public()})

    async def start_media(request: web.Request) -> web.Response:
        job = request.app['jobs'].get(request.match_info['job_id'])
        if job is None:
            return web.json_response({'ok': False, 'error': 'Job not found.'}, status=404)
        try:
            body = await request.json()
        except Exception:
            body = {}
        kind = str(body.get('kind') or request.match_info.get('kind') or '').strip()
        resume = bool(body.get('resume'))
        media_mgr: MediaJobManager = request.app['media']
        try:
            if kind in ('zip', 'csv_images_zip'):
                media_job = await media_mgr.start_zip(job, resume=resume)
            elif kind in ('shopify_ready', 'prepare_shopify'):
                conn = load_connection(request.app['artifacts_root'])
                if conn is None:
                    return web.json_response({
                        'ok': False,
                        'error': 'Connect a Shopify store before preparing a Shopify-ready CSV.',
                        'shopify': public_connection_status(request.app['artifacts_root']),
                    }, status=400)
                media_job = await media_mgr.start_shopify_ready(job, resume=resume)
            else:
                return web.json_response({
                    'ok': False,
                    'error': 'Unknown media kind. Use zip or shopify_ready.',
                }, status=400)
        except ValueError as exc:
            return web.json_response({'ok': False, 'error': str(exc)}, status=400)
        job.media = media_job
        return web.json_response({
            'ok': True,
            'job': job.public(),
            'media': media_job.public(),
            'destination_shop': media_job.shop,
        })

    async def cancel_media(request: web.Request) -> web.Response:
        job = request.app['jobs'].get(request.match_info['job_id'])
        if job is None:
            return web.json_response({'ok': False, 'error': 'Job not found.'}, status=404)
        media_mgr: MediaJobManager = request.app['media']
        media_job = media_mgr.for_parent(job.job_id)
        if media_job is None:
            return web.json_response({'ok': False, 'error': 'No media job for this extraction.'}, status=404)
        await media_mgr.cancel(media_job.media_job_id)
        job.media = media_job
        return web.json_response({'ok': True, 'job': job.public(), 'media': media_job.public()})

    async def download(request: web.Request) -> web.Response:
        job = request.app['jobs'].get(request.match_info['job_id'])
        if job is None:
            return web.json_response({'ok': False, 'error': 'Job not found.'}, status=404)
        _attach_media(job)
        kind = request.match_info['kind']
        media_job = job.media
        if kind == 'products':
            path, filename = job.products_path, 'products.json'
        elif kind == 'report':
            path, filename = job.report_path, 'run-report.json'
        elif kind == 'shopify':
            path, filename = job.shopify_csv_path, 'products-shopify.csv'
        elif kind == 'csv_images_zip':
            path = getattr(media_job, 'zip_path', None) if media_job else None
            filename = 'product-harbor-csv-images-BACKUP.zip'
        elif kind == 'shopify_ready':
            path = getattr(media_job, 'shopify_ready_csv_path', None) if media_job else None
            filename = Path(path).name if path else 'products-shopify-ready.csv'
        elif kind == 'media_failure_report':
            path = getattr(media_job, 'failure_report_path', None) if media_job else None
            filename = 'product-harbor-media-failure-report.json'
        elif kind == 'previous_shopify_ready':
            path = getattr(media_job, 'previous_shopify_ready_csv_path', None) if media_job else None
            filename = 'products-shopify-ready-previous.csv'
        else:
            return web.json_response({'ok': False, 'error': 'Unknown download kind.'}, status=404)
        if not path or not Path(path).is_file():
            return web.json_response({
                'ok': False,
                'error': (
                    'Download unavailable. Exports are written only when Product Harbor has '
                    'retained files for this job; failed runs preserve prior successful exports.'
                ),
            }, status=404)
        return web.FileResponse(path, headers={'Content-Disposition': f'attachment; filename="{filename}"'})

    async def live_preview(request: web.Request) -> web.Response:
        job = request.app['jobs'].get(request.match_info['job_id'])
        if job is None:
            return web.json_response({'ok': False, 'error': 'Job not found.'}, status=404)
        path = job.live_snapshot_path
        if not path or not Path(path).is_file():
            return web.json_response({'ok': False, 'error': 'Live preview not available yet.'}, status=404)
        return web.FileResponse(
            path,
            headers={
                'Content-Type': 'image/jpeg',
                'Cache-Control': 'no-store, max-age=0',
            },
        )

    app.router.add_get('/', index)
    app.router.add_get('/api/health', health)
    app.router.add_get('/api/run-control', run_control_status)
    app.router.add_get('/api/shopify/status', shopify_status)
    app.router.add_post('/api/shopify/connect', shopify_connect)
    app.router.add_post('/api/shopify/disconnect', shopify_disconnect)
    app.router.add_post('/api/shopify/live-upload', shopify_live_upload)
    app.router.add_post('/api/shopify/csv-import', start_csv_import)
    app.router.add_get('/api/shopify/csv-import/latest', csv_import_latest)
    app.router.add_get('/api/shopify/csv-import/{job_id}', csv_import_status)
    app.router.add_post('/api/shopify/csv-import/{job_id}/cancel', cancel_csv_import)
    async def latest_job(_request: web.Request) -> web.Response:
        # Prefer an in-memory active run. Do not resurrect finished scrapes into the
        # workspace UI — those left Access Denied / old counts on screen after reload.
        active = None
        for candidate in manager.jobs.values():
            if candidate.state in ('queued', 'running', 'awaiting_human'):
                if active is None or (candidate.created_at or '') > (active.created_at or ''):
                    active = candidate
        job = active or None
        if job is None:
            return web.json_response({'ok': True, 'job': None})
        _attach_media(job)
        return web.json_response({'ok': True, 'job': job.public()})

    app.router.add_post('/api/jobs', start_job)
    app.router.add_get('/api/jobs/latest', latest_job)
    app.router.add_get('/api/jobs/{job_id}', job_status)
    app.router.add_post('/api/jobs/{job_id}/cancel', cancel_job)
    app.router.add_post('/api/jobs/{job_id}/continue', continue_job)
    app.router.add_post('/api/jobs/{job_id}/media', start_media)
    app.router.add_post('/api/jobs/{job_id}/media/cancel', cancel_media)
    app.router.add_get('/api/jobs/{job_id}/download/{kind}', download)
    app.router.add_get('/api/jobs/{job_id}/live-preview', live_preview)
    app.router.add_static('/static/', STATIC_DIR, show_index=False)
    return app


def main(host: str = '127.0.0.1', port: int = 8765) -> None:
    app = create_app()
    web.run_app(app, host=host, port=port, print=lambda msg: print(msg, flush=True))
