"""Background Shopify CSV import jobs for Product Harbor."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .shopify_connection import load_connection, verify_connection_readonly
from .shopify_csv_import import (
    csv_fingerprint,
    import_csv_product,
    parse_shopify_product_csv,
)
from .shopify_files import (
    ShopifyStoreAccessError,
    file_create_access_denied_message,
)

logger = logging.getLogger(__name__)
TERMINAL = frozenset({'succeeded', 'failed', 'cancelled', 'partial'})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CsvImportJob:
    job_id: str
    shop: str
    filename: str
    fingerprint: str
    state: str = 'queued'
    created_at: str = field(default_factory=_utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_seconds: float = 0.0
    message: str | None = None
    total: int = 0
    processed: int = 0
    imported: int = 0
    skipped: int = 0
    network_skipped: int = 0
    results: list[dict] = field(default_factory=list)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    _started_mono: float | None = None
    _csv_text: str = ''

    def _progress_text(self, *, prefix: str | None = None) -> str:
        base = (
            f'{self.processed} of {self.total} processed · '
            f'{self.imported} imported · {self.skipped} skipped'
        )
        if self.network_skipped:
            base += f' · {self.network_skipped} network'
        if prefix:
            return f'{prefix}{base}'
        return base

    def public(self) -> dict:
        elapsed = self.elapsed_seconds
        if self.state == 'running' and self._started_mono is not None:
            elapsed = round(time.monotonic() - self._started_mono, 2)
        return {
            'job_id': self.job_id,
            'shop': self.shop,
            'filename': self.filename,
            'fingerprint': self.fingerprint,
            'state': self.state,
            'created_at': self.created_at,
            'started_at': self.started_at,
            'finished_at': self.finished_at,
            'elapsed_seconds': elapsed,
            'message': self.message,
            'counts': {
                'total': self.total,
                'processed': self.processed,
                'imported': self.imported,
                'skipped': self.skipped,
                'network_skipped': self.network_skipped,
            },
            'progress_label': self._progress_text(),
            'results': list(self.results[-40:]),
        }


class CsvImportJobManager:
    def __init__(self, artifacts_root: Path, export_root: Path | None = None):
        self.artifacts_root = Path(artifacts_root)
        self.export_root = Path(export_root or (self.artifacts_root / 'csv-imports'))
        self.export_root.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, CsvImportJob] = {}
        self._lock = asyncio.Lock()

    def get(self, job_id: str) -> CsvImportJob | None:
        return self.jobs.get(job_id)

    def latest(self) -> CsvImportJob | None:
        if not self.jobs:
            return None
        return max(self.jobs.values(), key=lambda job: job.created_at)

    async def start(self, *, csv_text: str, filename: str) -> CsvImportJob:
        conn = load_connection(self.artifacts_root)
        if conn is None:
            raise ValueError('Connect a Shopify store before importing a CSV.')
        products = parse_shopify_product_csv(csv_text)
        if not products:
            raise ValueError('The CSV did not contain any products.')
        async with self._lock:
            for job in self.jobs.values():
                if job.state == 'running':
                    raise ValueError('A CSV import is already running. Wait for it to finish.')
            job = CsvImportJob(
                job_id=uuid.uuid4().hex,
                shop=conn.shop,
                filename=filename or 'products.csv',
                fingerprint=csv_fingerprint(csv_text),
                total=len(products),
                message=f'Importing into {conn.shop}…',
                _csv_text=csv_text,
            )
            self.jobs[job.job_id] = job
            job.task = asyncio.create_task(self._run(job, products))
            return job

    async def cancel(self, job_id: str) -> CsvImportJob:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.state in TERMINAL:
            return job
        job.cancel_event.set()
        if job.task is not None and not job.task.done():
            job.task.cancel()
        return job

    async def _run(self, job: CsvImportJob, products) -> None:
        job.state = 'running'
        job.started_at = _utc_now()
        job._started_mono = time.monotonic()
        conn = load_connection(self.artifacts_root)
        if conn is None:
            job.state = 'failed'
            job.message = 'Shopify connection disappeared before import started.'
            job.finished_at = _utc_now()
            return
        directory = self.export_root / job.job_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'source.csv').write_text(job._csv_text, encoding='utf-8-sig')
        logger.info('CSV import start shop=%s products=%s file=%s', job.shop, job.total, job.filename)
        try:
            verification = await verify_connection_readonly(conn)
            missing = list(verification.get('missing_scopes') or [])
            if missing:
                raise ShopifyStoreAccessError(
                    file_create_access_denied_message(scopes_granted=False)
                    if 'write_files' in missing
                    else (
                        f'Missing required Admin API scopes: {", ".join(missing)}. '
                        'Enable them on the custom app, reinstall for a new token, then reconnect.'
                    )
                )
            for product in products:
                if job.cancel_event.is_set():
                    raise asyncio.CancelledError()
                try:
                    result = await import_csv_product(
                        conn,
                        product,
                        artifacts_root=self.artifacts_root,
                    )
                except ShopifyStoreAccessError as exc:
                    scopes_ok = 'write_files' not in (verification.get('missing_scopes') or [])
                    message = file_create_access_denied_message(scopes_granted=scopes_ok)
                    if str(exc).strip():
                        message = str(exc)
                    job.state = 'failed'
                    job.message = message
                    job.results.append({
                        'status': 'failed',
                        'reason': message,
                        'handle': product.handle,
                        'shopify_id': None,
                        'admin_url': None,
                        'source_rows': list(product.source_rows),
                        'store_level': True,
                    })
                    logger.info('CSV import stopped: store-level fileCreate denial')
                    return
                job.processed += 1
                if result.get('status') == 'imported':
                    job.imported += 1
                else:
                    job.skipped += 1
                    if result.get('skip_kind') == 'network':
                        job.network_skipped += 1
                job.results.append(result)
                job.message = f'{job._progress_text()} · {job.shop}'
                logger.info(
                    'CSV import product handle=%s status=%s kind=%s',
                    product.handle, result.get('status'), result.get('skip_kind'),
                )
            job.state = 'succeeded' if job.skipped == 0 else 'partial'
            job.message = f'Done on {job.shop}. {job._progress_text()}.'
            if job.network_skipped:
                job.message += (
                    f' {job.network_skipped} product(s) hit a network problem downloading '
                    'images from the source CDN — retry those when the connection is stable.'
                )
        except asyncio.CancelledError:
            job.state = 'cancelled'
            job.message = f'Cancelled on {job.shop}. {job._progress_text()}.'
        except ShopifyStoreAccessError as exc:
            job.state = 'failed'
            job.message = str(exc)
            job.results.append({
                'status': 'failed',
                'reason': str(exc),
                'handle': None,
                'shopify_id': None,
                'admin_url': None,
                'source_rows': [],
                'store_level': True,
            })
            logger.info('CSV import stopped before products: %s', exc)
        except Exception as exc:
            job.state = 'failed'
            job.message = f'CSV import failed: {type(exc).__name__}: {exc}'
            logger.info('CSV import failed: %s', exc)
        finally:
            if job._started_mono is not None:
                job.elapsed_seconds = round(time.monotonic() - job._started_mono, 2)
            job.finished_at = _utc_now()
            summary = directory / 'summary.json'
            summary.write_text(
                __import__('json').dumps(job.public(), indent=2),
                encoding='utf-8',
            )
