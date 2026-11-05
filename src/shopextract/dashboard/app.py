"""Localhost extraction dashboard (aiohttp)."""
from __future__ import annotations

import json
from pathlib import Path

from aiohttp import web

from .jobs import DashboardJobManager

STATIC_DIR = Path(__file__).with_name('static')
DEFAULT_EXPORT_ROOT = Path('artifacts') / 'dashboard-jobs'


def create_app(*, export_root: Path | None = None) -> web.Application:
    manager = DashboardJobManager(Path(export_root or DEFAULT_EXPORT_ROOT))
    app = web.Application()
    app['jobs'] = manager

    async def index(_request: web.Request) -> web.Response:
        return web.FileResponse(STATIC_DIR / 'index.html')

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({'ok': True, 'service': 'productharbor-dashboard'})

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
        return web.json_response({'ok': True, 'job': job.public()})

    async def job_status(request: web.Request) -> web.Response:
        job = request.app['jobs'].get(request.match_info['job_id'])
        if job is None:
            return web.json_response({'ok': False, 'error': 'Job not found.'}, status=404)
        return web.json_response({'ok': True, 'job': job.public()})

    async def cancel_job(request: web.Request) -> web.Response:
        try:
            job = await request.app['jobs'].cancel(request.match_info['job_id'])
        except KeyError:
            return web.json_response({'ok': False, 'error': 'Job not found.'}, status=404)
        return web.json_response({'ok': True, 'job': job.public()})

    async def continue_job(request: web.Request) -> web.Response:
        try:
            job = await request.app['jobs'].continue_interactive(request.match_info['job_id'])
        except KeyError:
            return web.json_response({'ok': False, 'error': 'Job not found.'}, status=404)
        except ValueError as exc:
            return web.json_response({'ok': False, 'error': str(exc)}, status=400)
        return web.json_response({'ok': True, 'job': job.public()})

    async def download(request: web.Request) -> web.Response:
        job = request.app['jobs'].get(request.match_info['job_id'])
        if job is None:
            return web.json_response({'ok': False, 'error': 'Job not found.'}, status=404)
        kind = request.match_info['kind']
        if kind == 'products':
            path, filename = job.products_path, 'products.json'
        elif kind == 'report':
            path, filename = job.report_path, 'run-report.json'
        elif kind == 'shopify':
            path, filename = job.shopify_csv_path, 'products-shopify.csv'
        else:
            return web.json_response({'ok': False, 'error': 'Unknown download kind.'}, status=404)
        if not path or not Path(path).is_file():
            return web.json_response({
                'ok': False,
                'error': 'Download unavailable. Exports are written only for successful or partial jobs with products; failed/blocked/cancelled jobs preserve prior files and do not write empty replacements.',
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
    app.router.add_post('/api/jobs', start_job)
    app.router.add_get('/api/jobs/{job_id}', job_status)
    app.router.add_post('/api/jobs/{job_id}/cancel', cancel_job)
    app.router.add_post('/api/jobs/{job_id}/continue', continue_job)
    app.router.add_get('/api/jobs/{job_id}/download/{kind}', download)
    app.router.add_get('/api/jobs/{job_id}/live-preview', live_preview)
    app.router.add_static('/static/', STATIC_DIR, show_index=False)
    return app


def main(host: str = '127.0.0.1', port: int = 8765) -> None:
    app = create_app()
    web.run_app(app, host=host, port=port, print=lambda msg: print(msg, flush=True))
