"""Background extraction jobs for the local dashboard."""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .._access import ACCESS_BLOCKED, access_blocked_reason
from .._cli_data import extraction_data
from .._execution import ExecutionPolicy, ProgressCheckpoint, execution_policy
from .._models import Platform
from .._outcomes import assess_extraction_outcome

TERMINAL = frozenset({'succeeded', 'failed', 'cancelled', 'blocked', 'partial'})
MODE_MAX = {'test': 5, 'full': 1000}
TAB_PLATFORM = {
    'auto': None,
    'shopify': Platform.SHOPIFY,
    'woocommerce': Platform.WOOCOMMERCE,
    'custom': Platform.GENERIC,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _money(value) -> str | None:
    if value is None:
        return None
    return str(value)


def preview_products(products, *, limit: int = 48) -> list[dict]:
    rows = []
    for product in products[:limit]:
        rows.append({
            'external_id': product.external_id or None,
            'title': product.title or None,
            'image_url': product.image_url or None,
            'price': _money(product.price),
            'currency': product.currency or None,
            'variant_count': len(product.variants or []),
            'product_url': product.product_url or None,
        })
    return rows


@dataclass
class DashboardJob:
    job_id: str
    url: str
    tab: str
    mode: str
    interactive_browser: bool = False
    browser_profile_dir: str | None = None
    state: str = 'queued'
    created_at: str = field(default_factory=_utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_seconds: float = 0.0
    discovered: int = 0
    extracted: int = 0
    failed: int = 0
    variant_count: int = 0
    image_count: int = 0
    platform: str | None = None
    outcome: str | None = None
    catalog_complete: bool | None = None
    incompleteness_reasons: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    message: str | None = None
    preview: list[dict] = field(default_factory=list)
    products_path: str | None = None
    report_path: str | None = None
    shopify_csv_path: str | None = None
    live_snapshot_path: str | None = None
    live_snapshot_label: str | None = None
    live_snapshot_at: str | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    _started_mono: float | None = None
    _result: Any = None
    _progress: ProgressCheckpoint | None = None
    awaiting_human: bool = False

    def public(self) -> dict:
        elapsed = self.elapsed_seconds
        if self.state == 'running' and self._started_mono is not None:
            elapsed = round(time.monotonic() - self._started_mono, 2)
        return {
            'job_id': self.job_id,
            'url': self.url,
            'tab': self.tab,
            'mode': self.mode,
            'interactive_browser': self.interactive_browser,
            'browser_profile_dir': self.browser_profile_dir,
            'awaiting_human': self.awaiting_human,
            'state': self.state,
            'created_at': self.created_at,
            'started_at': self.started_at,
            'finished_at': self.finished_at,
            'elapsed_seconds': elapsed,
            'counts': {
                'discovered': self.discovered,
                'extracted': self.extracted,
                'failed': self.failed,
                'variants': self.variant_count,
                'images': self.image_count,
            },
            'platform': self.platform,
            'outcome': self.outcome,
            'catalog_complete': self.catalog_complete,
            'incompleteness_reasons': list(self.incompleteness_reasons),
            'errors': list(self.errors),
            'message': self.message,
            'preview': list(self.preview),
            'live_preview': {
                'available': bool(self.live_snapshot_path and Path(self.live_snapshot_path).is_file()),
                'label': self.live_snapshot_label,
                'updated_at': self.live_snapshot_at,
            },
            'downloads': {
                'products_json': bool(self.products_path and Path(self.products_path).is_file()),
                'report_json': bool(self.report_path and Path(self.report_path).is_file()),
                'shopify_csv': bool(self.shopify_csv_path and Path(self.shopify_csv_path).is_file()),
            },
            'max_urls': MODE_MAX[self.mode],
        }


class DashboardJobManager:
    def __init__(self, export_root: Path):
        self.export_root = Path(export_root)
        self.export_root.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, DashboardJob] = {}
        self._lock = asyncio.Lock()

    def get(self, job_id: str) -> DashboardJob | None:
        return self.jobs.get(job_id)

    async def start(self, *, url: str, tab: str, mode: str, interactive_browser: bool = False) -> DashboardJob:
        if tab not in TAB_PLATFORM:
            raise ValueError('Unsupported tab; use auto, shopify, woocommerce, or custom.')
        if mode not in MODE_MAX:
            raise ValueError('Unsupported mode; use test or full.')
        if interactive_browser and tab != 'custom':
            raise ValueError('Interactive browser mode is only available on the Custom / Other tab.')
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ValueError('Expected an HTTP(S) collection URL.')
        from ..extractors._interactive_browser import default_profile_dir
        profile = str(default_profile_dir(url.strip())) if interactive_browser else None
        job = DashboardJob(
            job_id=uuid.uuid4().hex,
            url=url.strip(),
            tab=tab,
            mode=mode,
            interactive_browser=interactive_browser,
            browser_profile_dir=profile,
        )
        async with self._lock:
            self.jobs[job.job_id] = job
            job.task = asyncio.create_task(self._run(job))
        return job

    async def cancel(self, job_id: str) -> DashboardJob:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.state in TERMINAL:
            return job
        job.cancel_event.set()
        if job.task is not None and not job.task.done():
            job.task.cancel()
        return job

    async def continue_interactive(self, job_id: str) -> DashboardJob:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if not job.interactive_browser:
            raise ValueError('Job is not an interactive browser run.')
        if job.state in TERMINAL:
            return job
        if not job.browser_profile_dir:
            raise ValueError('Interactive profile directory is missing.')
        from ..extractors._interactive_browser import signal_continue
        signal_continue(Path(job.browser_profile_dir))
        job.awaiting_human = False
        job.message = 'Continue signal sent; waiting for page re-check.'
        return job

    def _progress_counts(self, job: DashboardJob) -> tuple[int, int]:
        progress = job._progress
        if progress is None:
            return job.discovered, job.extracted
        discovered = len(progress.attempted) if progress.attempted else 0
        extracted = 0
        for source in progress.sources:
            extracted = max(extracted, len(source.get('rows') or []))
        return discovered or extracted, extracted

    async def _run(self, job: DashboardJob) -> None:
        from .._extract import extract
        job.state = 'running'
        job.started_at = _utc_now()
        job._started_mono = time.monotonic()
        max_urls = MODE_MAX[job.mode]
        platform = TAB_PLATFORM[job.tab]
        # Custom/other sites use the generic crawl path; browser is explicit and supported.
        browser = job.tab == 'custom' or job.interactive_browser
        policy = ExecutionPolicy(
            browser=browser,
            interactive_browser=job.interactive_browser,
            browser_profile_dir=job.browser_profile_dir,
        )
        # Interactive runs need room for a human gate; full collections need more budget.
        if job.interactive_browser:
            timeout = 900.0
            job.message = (
                'Interactive browser opened. Complete any challenge in the Chromium window, '
                'then click Continue here (or create .productharbor_continue in the profile dir).'
            )
            job.awaiting_human = True
        else:
            timeout = 300.0 if job.mode == 'full' else 120.0
        try:
            async def operation():
                from ..extractors._interactive_browser import reset_live_hooks, set_live_hooks
                tokens = (None, None)
                if job.interactive_browser:
                    tokens = set_live_hooks(
                        on_product=lambda record: self._on_live_product(job, record),
                        on_snapshot=lambda png, label: self._on_live_snapshot(job, png, label),
                    )
                try:
                    with execution_policy(policy, timeout) as state:
                        state.progress = ProgressCheckpoint(max_products=max_urls)
                        job._progress = state.progress
                        if job.cancel_event.is_set():
                            raise asyncio.CancelledError()
                        return await extract(
                            job.url,
                            platform=platform,
                            max_urls=max_urls,
                            shop_url=job.url,
                        )
                finally:
                    if tokens[0] is not None:
                        reset_live_hooks(*tokens)

            poll = asyncio.create_task(self._poll_progress(job))
            try:
                result = await asyncio.wait_for(operation(), timeout=timeout)
            finally:
                poll.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await poll
            if job.cancel_event.is_set():
                raise asyncio.CancelledError()
            self._finalize_result(job, result)
        except asyncio.CancelledError:
            job.state = 'cancelled'
            job.message = 'Job cancelled.'
            job.outcome = 'failed'
            # Preserve any existing export files; do not write empty replacements.
        except asyncio.TimeoutError:
            job.state = 'failed'
            job.message = 'Operation timed out.'
            job.errors = ['operation_timeout']
            job.outcome = 'failed'
        except Exception as exc:
            job.state = 'failed'
            job.message = f'Extraction failed: {type(exc).__name__}'
            job.errors = [str(exc)]
            job.outcome = 'failed'
        finally:
            if job._started_mono is not None:
                job.elapsed_seconds = round(time.monotonic() - job._started_mono, 2)
            job.finished_at = _utc_now()

    async def _poll_progress(self, job: DashboardJob) -> None:
        while job.state == 'running':
            discovered, extracted = self._progress_counts(job)
            # Prefer live interactive counts when preview is already streaming.
            if job.preview:
                job.extracted = max(extracted, len(job.preview))
                job.discovered = max(discovered, job.extracted)
            else:
                job.discovered = discovered
                job.extracted = extracted
            await asyncio.sleep(0.4)

    def _on_live_product(self, job: DashboardJob, record: dict) -> None:
        row = {
            'external_id': None,
            'title': record.get('title') if isinstance(record.get('title'), str) else None,
            'image_url': record.get('image_url') if isinstance(record.get('image_url'), str) else None,
            'price': str(record['price']) if record.get('price') not in (None, '') else None,
            'currency': record.get('currency') if isinstance(record.get('currency'), str) else None,
            'variant_count': len(record.get('variants') or []),
            'product_url': record.get('product_url') if isinstance(record.get('product_url'), str) else None,
        }
        job.preview = list(job.preview) + [row]
        job.extracted = len(job.preview)
        job.discovered = max(job.discovered, job.extracted)
        job.variant_count = sum(int(item.get('variant_count') or 0) for item in job.preview)
        job.image_count = sum(1 for item in job.preview if item.get('image_url'))
        job.message = f'Live preview: extracted {job.extracted} product(s)…'

    def _on_live_snapshot(self, job: DashboardJob, png: bytes, label: str) -> None:
        directory = self.export_root / job.job_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'live-preview.jpg'
        path.write_bytes(png)
        job.live_snapshot_path = str(path)
        job.live_snapshot_label = label
        job.live_snapshot_at = _utc_now()

    def _finalize_result(self, job: DashboardJob, result) -> None:
        job._result = result
        job.platform = result.platform.value if result.platform else None
        job.catalog_complete = result.catalog_complete
        job.incompleteness_reasons = list(result.incompleteness_reasons)
        job.errors = list(result.errors)
        job.extracted = result.product_count
        job.discovered = max(result.urls_attempted, result.product_count)
        job.failed = max(0, job.discovered - job.extracted) if result.urls_attempted else 0
        job.variant_count = sum(len(p.variants or []) for p in result.products)
        job.image_count = sum(
            (1 if p.image_url else 0) + len(p.additional_images or []) for p in result.products
        )
        job.preview = preview_products(result.products)
        blocked = access_blocked_reason(result.errors, result.incompleteness_reasons)
        outcome = assess_extraction_outcome(
            product_count=result.product_count,
            catalog_complete=result.catalog_complete,
            errors=result.errors,
            incompleteness_reasons=result.incompleteness_reasons,
        )
        job.outcome = outcome
        if blocked is not None:
            job.state = 'blocked'
            job.message = blocked if blocked != ACCESS_BLOCKED else (
                f'{ACCESS_BLOCKED} url={job.url} status=unknown reason=access_blocked'
            )
            return
        if outcome == 'complete':
            job.state = 'succeeded'
            job.message = 'Collection extraction completed with confirmed completeness evidence.'
            self._write_exports(job, result)
            return
        if result.product_count > 0:
            job.state = 'partial'
            job.message = 'Partial results retained; completeness not confirmed.'
            self._write_exports(job, result)
            return
        job.state = 'failed'
        job.message = 'No usable products were extracted; source failed or was incomplete.'

    def _write_exports(self, job: DashboardJob, result) -> None:
        directory = self.export_root / job.job_id
        directory.mkdir(parents=True, exist_ok=True)
        products_path = directory / 'products.json'
        report_path = directory / 'run-report.json'
        data = extraction_data(result)
        products_payload = {
            'collection_url': job.url,
            'tab': job.tab,
            'mode': job.mode,
            'extracted_at': job.finished_at or _utc_now(),
            'product_count': result.product_count,
            'products': [
                {
                    'external_id': p.external_id or None,
                    'title': p.title or None,
                    'description': p.description or None,
                    'price': _money(p.price),
                    'compare_at_price': _money(p.compare_at_price),
                    'currency': p.currency or None,
                    'sku': p.sku,
                    'image_url': p.image_url or None,
                    'additional_images': list(p.additional_images or []),
                    'product_url': p.product_url or None,
                    'variants': [
                        {
                            'variant_id': v.variant_id or None,
                            'title': v.title or None,
                            'price': _money(v.price),
                            'sku': v.sku,
                            'options': dict(v.attributes or {}),
                            'featured_image': v.featured_image,
                            'featured_image_id': v.featured_image_id,
                        }
                        for v in (p.variants or [])
                    ],
                }
                for p in result.products
            ],
        }
        report_payload = {
            'job_id': job.job_id,
            'collection_url': job.url,
            'tab': job.tab,
            'mode': job.mode,
            'platform': job.platform,
            'state': job.state,
            'outcome': job.outcome,
            'catalog_complete': job.catalog_complete,
            'incompleteness_reasons': job.incompleteness_reasons,
            'errors': job.errors,
            'message': job.message,
            'counts': {
                'discovered': job.discovered,
                'extracted': job.extracted,
                'failed': job.failed,
                'variants': job.variant_count,
                'images': job.image_count,
            },
            'elapsed_seconds': job.elapsed_seconds,
            'pipeline': data,
        }
        # Atomic-ish write: temp then replace so failures don't clobber prior exports.
        self._atomic_write(products_path, products_payload)
        self._atomic_write(report_path, report_payload)
        from .shopify_csv import write_shopify_csv
        shopify_path = directory / 'products-shopify.csv'
        write_shopify_csv(result.products, shopify_path)
        job.products_path = str(products_path)
        job.report_path = str(report_path)
        job.shopify_csv_path = str(shopify_path)

    @staticmethod
    def _atomic_write(path: Path, payload: dict) -> None:
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
        temporary.replace(path)
