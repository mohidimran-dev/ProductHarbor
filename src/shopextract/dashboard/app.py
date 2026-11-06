"""Localhost extraction dashboard (aiohttp) — Product Harbor."""
from __future__ import annotations

from pathlib import Path

from aiohttp import web

from .jobs import DashboardJobManager
from .media_jobs import MediaJobManager
from .shopify_connection import (
    clear_connection,
    load_connection,
    public_connection_status,
    save_connection,
)

STATIC_DIR = Path(__file__).with_name('static')
DEFAULT_EXPORT_ROOT = Path('artifacts') / 'dashboard-jobs'
DEFAULT_ARTIFACTS_ROOT = Path('artifacts')


def create_app(
    *,
    export_root: Path | None = None,
    artifacts_root: Path | None = None,
) -> web.Application:
    export = Path(export_root or DEFAULT_EXPORT_ROOT)
    artifacts = Path(artifacts_root or DEFAULT_ARTIFACTS_ROOT)
    manager = DashboardJobManager(export)
    media = MediaJobManager(export, artifacts_root=artifacts)
    app = web.Application()
    app['jobs'] = manager
    app['media'] = media
    app['artifacts_root'] = artifacts

    def _attach_media(job) -> None:
        media_job = media.for_parent(job.job_id)
        job.media = media_job

    async def index(_request: web.Request) -> web.Response:
        return web.FileResponse(STATIC_DIR / 'index.html')

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({'ok': True, 'service': 'productharbor-dashboard', 'name': 'Product Harbor'})

    async def shopify_status(_request: web.Request) -> web.Response:
        status = public_connection_status(_request.app['artifacts_root'])
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
        return web.json_response({'ok': True, 'shopify': conn.public_status})

    async def shopify_disconnect(request: web.Request) -> web.Response:
        clear_connection(request.app['artifacts_root'])
        return web.json_response({
            'ok': True,
            'shopify': public_connection_status(request.app['artifacts_root']),
        })

    async def start_job(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({'ok': False, 'error': 'Expected JSON body.'}, status=400)
        url = body.get('url')
        tab = body.get('tab', 'auto')
        mode = body.get('mode', 'test')
        interactive = bool(body.get('interactive_browser'))
        if not isinstance(url, str) or not url.strip():
            return web.json_response({'ok': False, 'error': 'Collection URL is required.'}, status=400)
        try:
            job = await request.app['jobs'].start(
                url=url, tab=str(tab), mode=str(mode), interactive_browser=interactive,
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
    app.router.add_get('/api/shopify/status', shopify_status)
    app.router.add_post('/api/shopify/connect', shopify_connect)
    app.router.add_post('/api/shopify/disconnect', shopify_disconnect)
    app.router.add_post('/api/jobs', start_job)
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
