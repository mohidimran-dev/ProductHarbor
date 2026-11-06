"""Background image download / Shopify media upload jobs for Product Harbor."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .image_assets import (
    apply_content_dedupe_to_products,
    collect_image_assets,
    download_assets,
    load_products_json,
    rewrite_products_urls,
    save_asset_checkpoint,
)
from .shopify_connection import load_connection
from .shopify_csv import write_shopify_csv
from .shopify_files import upload_assets_to_shopify
from .zip_export import build_csv_images_zip, write_download_report

MEDIA_TERMINAL = frozenset({'succeeded', 'failed', 'cancelled', 'partial'})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_scope_hash(products: list[dict]) -> str:
    blob = json.dumps(products, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]


@dataclass
class MediaJob:
    media_job_id: str
    parent_job_id: str
    kind: str  # zip | shopify_ready
    state: str = 'queued'
    created_at: str = field(default_factory=_utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_seconds: float = 0.0
    message: str | None = None
    shop: str | None = None
    content_hash: str | None = None
    download_total: int = 0
    downloaded: int = 0
    download_failed: int = 0
    upload_total: int = 0
    uploaded: int = 0
    upload_failed: int = 0
    partial: bool = False
    zip_path: str | None = None
    shopify_ready_csv_path: str | None = None
    previous_shopify_ready_csv_path: str | None = None
    failure_report_path: str | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    _started_mono: float | None = None
    _work_dir: Path | None = None
    resumable: bool = False

    def public(self) -> dict:
        elapsed = self.elapsed_seconds
        if self.state == 'running' and self._started_mono is not None:
            elapsed = round(time.monotonic() - self._started_mono, 2)
        return {
            'media_job_id': self.media_job_id,
            'parent_job_id': self.parent_job_id,
            'kind': self.kind,
            'state': self.state,
            'created_at': self.created_at,
            'started_at': self.started_at,
            'finished_at': self.finished_at,
            'elapsed_seconds': elapsed,
            'message': self.message,
            'shop': self.shop,
            'content_hash': self.content_hash,
            'partial': self.partial,
            'resumable': self.resumable and self.state in ('cancelled', 'failed', 'partial'),
            'download': {
                'total': self.download_total,
                'downloaded': self.downloaded,
                'failed': self.download_failed,
                'pending': max(0, self.download_total - self.downloaded - self.download_failed),
            },
            'upload': {
                'total': self.upload_total,
                'uploaded': self.uploaded,
                'failed': self.upload_failed,
                'pending': max(0, self.upload_total - self.uploaded - self.upload_failed),
            },
            'downloads': {
                'csv_images_zip': bool(self.zip_path and Path(self.zip_path).is_file()),
                'shopify_ready_csv': bool(
                    self.shopify_ready_csv_path and Path(self.shopify_ready_csv_path).is_file()
                ),
                'failure_report': bool(
                    self.failure_report_path and Path(self.failure_report_path).is_file()
                ),
                'previous_shopify_ready_csv': bool(
                    self.previous_shopify_ready_csv_path
                    and Path(self.previous_shopify_ready_csv_path).is_file()
                ),
            },
            'labels': {
                'zip': 'Product Harbor CSV + Images ZIP (backup — local images are not auto-imported by Shopify)',
                'shopify_ready': 'Product Harbor Shopify-ready CSV (CDN URLs)',
                'partial': 'Partial export — some images failed; see failure report',
            },
        }


class MediaJobManager:
    def __init__(self, export_root: Path, artifacts_root: Path | None = None):
        self.export_root = Path(export_root)
        self.artifacts_root = Path(artifacts_root or self.export_root.parent)
        self.jobs: dict[str, MediaJob] = {}
        self.by_parent: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def get(self, media_job_id: str) -> MediaJob | None:
        return self.jobs.get(media_job_id)

    def for_parent(self, parent_job_id: str) -> MediaJob | None:
        mid = self.by_parent.get(parent_job_id)
        return self.jobs.get(mid) if mid else None

    async def start_zip(self, parent_job, *, resume: bool = False) -> MediaJob:
        return await self._start(parent_job, kind='zip', resume=resume)

    async def start_shopify_ready(self, parent_job, *, resume: bool = False) -> MediaJob:
        conn = load_connection(self.artifacts_root)
        if conn is None:
            raise ValueError(
                'Shopify store is not connected. Configure Product Harbor Shopify connection first.'
            )
        return await self._start(parent_job, kind='shopify_ready', resume=resume, shop=conn.shop)

    async def cancel(self, media_job_id: str) -> MediaJob:
        job = self.jobs.get(media_job_id)
        if job is None:
            raise KeyError(media_job_id)
        if job.state in MEDIA_TERMINAL:
            job.resumable = True
            return job
        job.cancel_event.set()
        job.message = 'Cancellation requested…'
        return job

    async def _start(
        self,
        parent_job,
        *,
        kind: str,
        resume: bool = False,
        shop: str | None = None,
    ) -> MediaJob:
        if not parent_job.products_path or not Path(parent_job.products_path).is_file():
            raise ValueError('Products export is required before image packaging.')
        products = load_products_json(Path(parent_job.products_path))
        if not products:
            raise ValueError('No products available for image packaging.')

        existing = self.for_parent(parent_job.job_id)
        if existing is not None and existing.state == 'running':
            raise ValueError('A media job is already running for this extraction.')

        if resume and existing is not None and existing.kind == kind and existing.resumable:
            job = existing
            job.cancel_event = asyncio.Event()
            job.state = 'queued'
            job.message = 'Resuming Product Harbor media job…'
            job.finished_at = None
            job.partial = False
        else:
            import uuid
            job = MediaJob(
                media_job_id=uuid.uuid4().hex,
                parent_job_id=parent_job.job_id,
                kind=kind,
                shop=shop,
                content_hash=_content_scope_hash(products),
            )
            # Preserve prior artifacts across re-runs / kind switches.
            if existing is not None:
                if existing.zip_path and Path(existing.zip_path).is_file():
                    job.zip_path = existing.zip_path
                if existing.shopify_ready_csv_path and Path(existing.shopify_ready_csv_path).is_file():
                    job.previous_shopify_ready_csv_path = existing.shopify_ready_csv_path
                    if kind != 'shopify_ready':
                        job.shopify_ready_csv_path = existing.shopify_ready_csv_path
                if existing.failure_report_path and Path(existing.failure_report_path).is_file():
                    job.failure_report_path = existing.failure_report_path

        work_dir = self.export_root / parent_job.job_id / 'media' / job.content_hash
        job._work_dir = work_dir
        job.shop = shop
        async with self._lock:
            self.jobs[job.media_job_id] = job
            self.by_parent[parent_job.job_id] = job.media_job_id
            job.task = asyncio.create_task(self._run(job, products))
        return job

    async def _run(self, job: MediaJob, products: list[dict]) -> None:
        job.state = 'running'
        job.started_at = _utc_now()
        job._started_mono = time.monotonic()
        work_dir = job._work_dir or (self.export_root / job.parent_job_id / 'media' / (job.content_hash or 'run'))
        images_dir = work_dir / 'images'
        checkpoint_path = work_dir / 'download-checkpoint.json'
        from .._dedupe import dedupe_product_dicts
        products, product_stats = dedupe_product_dicts(products, store='')
        assets, image_stats = collect_image_assets(products)
        job.download_total = len(assets)
        job.message = (
            f'Product Harbor downloading {job.download_total} unique image(s)…'
            if job.kind == 'zip'
            else f'Product Harbor preparing Shopify-ready CSV for {job.shop}…'
        )

        def on_download(progress: dict) -> None:
            job.downloaded = int(progress.get('downloaded') or 0)
            job.download_failed = int(progress.get('failed') or 0)
            job.message = (
                f'Downloading images: {job.downloaded}/{job.download_total}'
                f' ({job.download_failed} failed)'
            )

        try:
            await download_assets(
                assets,
                images_dir,
                concurrency=4,
                cancel_event=job.cancel_event,
                on_progress=on_download,
                checkpoint_path=checkpoint_path,
            )
            job.downloaded = sum(1 for a in assets if a.status == 'downloaded')
            job.download_failed = sum(1 for a in assets if a.status == 'failed')
            if job.cancel_event.is_set():
                job.state = 'cancelled'
                job.resumable = True
                job.message = 'Image download cancelled. You can resume from the checkpoint.'
                report = write_download_report(assets, work_dir / 'download-report.json', cancelled=True)
                job.failure_report_path = str(work_dir / 'download-report.json')
                job.partial = True
                save_asset_checkpoint(assets, checkpoint_path)
                return

            products, content_stats = apply_content_dedupe_to_products(products, assets)
            image_stats.repeated_image_references_removed += content_stats.repeated_image_references_removed
            image_stats.unique_downloaded_images = content_stats.unique_downloaded_images
            image_stats.files_reused = max(image_stats.files_reused, content_stats.files_reused)

            if job.kind == 'zip':
                zip_path = self.export_root / job.parent_job_id / 'product-harbor-csv-images.zip'
                report = build_csv_images_zip(
                    products=products,
                    assets=assets,
                    work_dir=work_dir / 'zip_stage',
                    zip_path=zip_path,
                    cancelled=False,
                    product_stats=product_stats.as_dict(),
                    image_stats=image_stats.as_dict(),
                )
                job.zip_path = str(zip_path)
                job.failure_report_path = str(work_dir / 'zip_stage' / 'download-report.json')
                job.partial = bool(report.get('partial'))
                if job.partial:
                    job.state = 'partial'
                    job.message = (
                        'Product Harbor ZIP ready (partial). Local images are a backup; '
                        'Shopify native CSV import still needs accessible HTTPS image URLs.'
                    )
                else:
                    job.state = 'succeeded'
                    job.message = (
                        'Product Harbor CSV + Images ZIP ready (backup package). '
                        'Local images are not auto-imported by Shopify’s native CSV importer.'
                    )
                job.resumable = job.partial
                return

            # Shopify-ready path
            conn = load_connection(self.artifacts_root)
            if conn is None:
                raise ValueError('Shopify connection disappeared before upload.')
            if job.shop and job.shop != conn.shop:
                raise ValueError('Destination store changed; refusing to reuse mappings.')
            job.shop = conn.shop
            job.upload_total = sum(1 for a in assets if a.status == 'downloaded')
            mapping_path = work_dir / f'shopify-map-{conn.shop.replace(".", "_")}.json'

            def on_upload(progress: dict) -> None:
                job.uploaded = int(progress.get('uploaded') or 0)
                job.upload_failed = int(progress.get('failed') or 0)
                job.message = (
                    f'Uploading to {conn.shop}: {job.uploaded}/{job.upload_total}'
                    f' ({job.upload_failed} failed)'
                )

            upload_report = await upload_assets_to_shopify(
                conn,
                assets,
                concurrency=2,
                cancel_event=job.cancel_event,
                on_progress=on_upload,
                checkpoint_path=checkpoint_path,
                mapping_path=mapping_path,
            )
            image_stats.uploads_reused = int(upload_report.get('uploads_reused') or 0)
            job.uploaded = sum(1 for a in assets if a.shopify_cdn_url)
            job.upload_failed = sum(
                1 for a in assets
                if a.status == 'downloaded' and not a.shopify_cdn_url and a.error and str(a.error).startswith('upload:')
            )

            if job.cancel_event.is_set():
                job.state = 'cancelled'
                job.resumable = True
                job.message = f'Upload to {conn.shop} cancelled. Resume will skip completed CDN mappings.'
                failure = {
                    'package': 'Product Harbor',
                    'shop': conn.shop,
                    'cancelled': True,
                    'download': {
                        'total': job.download_total,
                        'downloaded': job.downloaded,
                        'failed': job.download_failed,
                    },
                    'upload': {
                        'total': job.upload_total,
                        'uploaded': job.uploaded,
                        'failed': job.upload_failed,
                    },
                    'failures': [
                        {
                            'source_url': a.source_url,
                            'error': a.error,
                            'content_hash': a.content_hash,
                        }
                        for a in assets if a.error
                    ],
                }
                fail_path = work_dir / 'media-failure-report.json'
                fail_path.write_text(json.dumps(failure, indent=2), encoding='utf-8')
                job.failure_report_path = str(fail_path)
                return

            url_map: dict[str, str] = {}
            for asset in assets:
                if not asset.shopify_cdn_url:
                    continue
                if asset.source_url:
                    url_map[asset.source_url] = asset.shopify_cdn_url
                for alt in asset.alternate_urls:
                    url_map[alt] = asset.shopify_cdn_url
            rewritten = rewrite_products_urls(products, url_map)
            ready_name = 'products-shopify-ready.csv'
            if job.download_failed or job.upload_failed or any(
                a.status == 'downloaded' and not a.shopify_cdn_url for a in assets
            ):
                ready_name = 'products-shopify-ready-PARTIAL.csv'
                job.partial = True

            ready_path = self.export_root / job.parent_job_id / ready_name
            # Keep prior successful export if this run is worse/failed.
            previous = job.previous_shopify_ready_csv_path
            write_shopify_csv(rewritten, ready_path)
            job.shopify_ready_csv_path = str(ready_path)

            failure = {
                'package': 'Product Harbor',
                'shop': conn.shop,
                'partial': job.partial,
                'unique_products': product_stats.unique_products,
                'duplicate_product_observations_merged': (
                    product_stats.duplicate_product_observations_merged
                ),
                'unique_downloaded_images': image_stats.unique_downloaded_images,
                'repeated_image_references_removed': (
                    image_stats.repeated_image_references_removed
                ),
                'files_reused': image_stats.files_reused,
                'uploads_reused': image_stats.uploads_reused,
                'download': {
                    'total': job.download_total,
                    'downloaded': job.downloaded,
                    'failed': job.download_failed,
                },
                'upload': {
                    'total': job.upload_total,
                    'uploaded': job.uploaded,
                    'failed': job.upload_failed,
                },
                'unmapped_source_urls': [
                    a.source_url for a in assets
                    if a.source_url and a.source_url not in url_map
                ],
                'failures': [
                    {
                        'source_url': a.source_url,
                        'error': a.error,
                        'content_hash': a.content_hash,
                        'associations': [
                            {
                                'product_handle': x.product_handle,
                                'role': x.role,
                                'position': x.position,
                                'variant_sku': x.variant_sku,
                            }
                            for x in a.associations
                        ],
                    }
                    for a in assets if a.error or (a.status != 'downloaded') or not a.shopify_cdn_url
                ],
            }
            fail_path = self.export_root / job.parent_job_id / 'shopify-ready-failure-report.json'
            fail_path.write_text(json.dumps(failure, indent=2), encoding='utf-8')
            job.failure_report_path = str(fail_path)

            if job.partial:
                job.state = 'partial'
                job.resumable = True
                job.message = (
                    f'Partial Shopify-ready CSV for {conn.shop}. '
                    'Failed images were not substituted. See the failure report.'
                )
            else:
                job.state = 'succeeded'
                job.resumable = False
                job.message = (
                    f'Shopify-ready CSV prepared for {conn.shop}. '
                    'Image columns use Shopify CDN URLs. Media only — no products were created.'
                )
            # If somehow write failed we'd have raised; prior path remains on job.previous_*
            if previous:
                job.previous_shopify_ready_csv_path = previous

        except asyncio.CancelledError:
            job.state = 'cancelled'
            job.resumable = True
            job.message = 'Media job cancelled.'
        except Exception as exc:
            job.state = 'failed'
            job.resumable = True
            job.message = f'Product Harbor media job failed: {type(exc).__name__}: {exc}'[:400]
            # Do not delete previous_shopify_ready_csv_path
        finally:
            if job._started_mono is not None:
                job.elapsed_seconds = round(time.monotonic() - job._started_mono, 2)
            job.finished_at = _utc_now()
