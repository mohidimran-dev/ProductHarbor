"""Background extraction jobs for the local dashboard."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
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
from ..run_control import (
    SCHEDULE_INTERVAL_HOURS,
    activate_known_families,
    cooldown_status,
    products_this_run,
    record_denial,
)

logger = logging.getLogger(__name__)

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
    # Measured counters only. Use None when not measured — never infer.
    http_pages: int | None = None
    discovered: int | None = None
    extracted: int | None = None
    failed: int | None = None
    duplicates_merged: int | None = None
    pending: int | None = None
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
    human_action: dict | None = None
    upload_to_shopify: bool = False
    upload_queued: int = 0
    upload_done: int = 0
    upload_skipped: int = 0
    upload_failed: int = 0
    upload_message: str | None = None
    schedule_daily: bool = False
    duplicate_report: str | None = None
    media: Any = None  # optional MediaJob public snapshot helper
    _upload_keys: set[str] = field(default_factory=set)

    def public(self) -> dict:
        elapsed = self.elapsed_seconds
        if self.state == 'running' and self._started_mono is not None:
            elapsed = round(time.monotonic() - self._started_mono, 2)
        limited_run = self.mode == 'test' or (
            'product_budget_reached' in (self.incompleteness_reasons or [])
        )
        return {
            'job_id': self.job_id,
            'url': self.url,
            'tab': self.tab,
            'mode': self.mode,
            'interactive_browser': self.interactive_browser,
            'browser_profile_dir': self.browser_profile_dir,
            'awaiting_human': self.awaiting_human,
            'human_action': dict(self.human_action) if self.human_action else None,
            'state': self.state,
            'created_at': self.created_at,
            'started_at': self.started_at,
            'finished_at': self.finished_at,
            'elapsed_seconds': elapsed,
            'counts': {
                'http_pages': self.http_pages,
                'discovered': self.discovered,
                'extracted': self.extracted,
                'failed': self.failed,
                'duplicates_merged': self.duplicates_merged,
                'pending': self.pending,
                'variants': self.variant_count,
                'images': self.image_count,
            },
            'platform': self.platform,
            'outcome': self.outcome,
            'catalog_complete': self.catalog_complete,
            'completeness_label': (
                'confirmed complete' if self.catalog_complete is True
                else ('incomplete / partial' if self.catalog_complete is False else 'unverified')
            ),
            'limited_run': limited_run,
            'run_limit': products_this_run(self.mode),
            'schedule_daily': self.schedule_daily,
            'duplicate_report': self.duplicate_report,
            'incompleteness_reasons': list(self.incompleteness_reasons),
            'errors': list(self.errors),
            'message': self.message,
            'upload': {
                'enabled': self.upload_to_shopify,
                'queued': self.upload_queued,
                'uploaded': self.upload_done,
                'skipped': self.upload_skipped,
                'failed': self.upload_failed,
                'message': self.upload_message,
            },
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
                'csv_images_zip': bool(
                    getattr(self.media, 'zip_path', None)
                    and Path(getattr(self.media, 'zip_path')).is_file()
                ) if self.media is not None else False,
                'shopify_ready_csv': bool(
                    getattr(self.media, 'shopify_ready_csv_path', None)
                    and Path(getattr(self.media, 'shopify_ready_csv_path')).is_file()
                ) if self.media is not None else False,
                'media_failure_report': bool(
                    getattr(self.media, 'failure_report_path', None)
                    and Path(getattr(self.media, 'failure_report_path')).is_file()
                ) if self.media is not None else False,
            },
            'media': self.media.public() if self.media is not None and hasattr(self.media, 'public') else None,
            'max_urls': products_this_run(self.mode),
        }


class DashboardJobManager:
    def __init__(self, export_root: Path, artifacts_root: Path | None = None):
        self.export_root = Path(export_root)
        self.export_root.mkdir(parents=True, exist_ok=True)
        self.artifacts_root = Path(artifacts_root) if artifacts_root is not None else self.export_root.parent
        self.jobs: dict[str, DashboardJob] = {}
        self._lock = asyncio.Lock()
        self.on_upload_record = None
        self.catalog_uploader = None

    def get(self, job_id: str) -> DashboardJob | None:
        job = self.jobs.get(job_id)
        if job is not None:
            return job
        return self.restore_saved(job_id)

    def restore_saved(self, job_id: str) -> DashboardJob | None:
        """Reload a finished export so downloads still work after a dashboard restart."""
        directory = self.export_root / job_id
        products_path = directory / 'products.json'
        if not products_path.is_file():
            return None
        try:
            payload = json.loads(products_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return None
        report: dict = {}
        report_path = directory / 'run-report.json'
        if report_path.is_file():
            with contextlib.suppress(OSError, json.JSONDecodeError):
                report = json.loads(report_path.read_text(encoding='utf-8'))
        url = str(payload.get('collection_url') or report.get('collection_url') or '')
        tab = str(payload.get('tab') or report.get('tab') or 'custom')
        mode = str(payload.get('mode') or report.get('mode') or 'full')
        if tab not in TAB_PLATFORM:
            tab = 'custom'
        if mode not in MODE_MAX:
            mode = 'full'
        profile = None
        if url:
            from ..extractors._interactive_browser import default_profile_dir
            profile = str(default_profile_dir(url))
        products = payload.get('products') if isinstance(payload.get('products'), list) else []
        job = DashboardJob(
            job_id=job_id,
            url=url,
            tab=tab,
            mode=mode,
            interactive_browser=bool(profile and (directory / 'live-preview.jpg').is_file()),
            browser_profile_dir=profile,
            state=str(report.get('state') or 'partial'),
            finished_at=str(payload.get('extracted_at') or ''),
            platform=report.get('platform'),
            outcome=report.get('outcome'),
            catalog_complete=report.get('catalog_complete'),
            incompleteness_reasons=list(report.get('incompleteness_reasons') or []),
            errors=list(report.get('errors') or []),
            message=report.get('message') or 'Saved export restored.',
            extracted=len(products),
            preview=[
                {
                    'external_id': item.get('external_id'),
                    'title': item.get('title'),
                    'image_url': item.get('image_url'),
                    'price': item.get('price'),
                    'currency': item.get('currency'),
                    'variant_count': len(item.get('variants') or []),
                    'product_url': item.get('product_url'),
                }
                for item in products[:48]
                if isinstance(item, dict)
            ],
        )
        counts = report.get('counts') if isinstance(report.get('counts'), dict) else {}
        job.http_pages = counts.get('http_pages')
        job.discovered = counts.get('discovered')
        job.failed = counts.get('failed')
        job.duplicates_merged = counts.get('duplicates_merged')
        job.variant_count = sum(len(item.get('variants') or []) for item in products if isinstance(item, dict))
        job.image_count = sum(
            (1 if item.get('image_url') else 0) + len(item.get('additional_images') or [])
            for item in products if isinstance(item, dict)
        )
        job.products_path = str(products_path)
        job.report_path = str(report_path) if report_path.is_file() else None
        shopify_csv = directory / 'products-shopify.csv'
        if shopify_csv.is_file():
            job.shopify_csv_path = str(shopify_csv)
        snapshot = directory / 'live-preview.jpg'
        if snapshot.is_file():
            job.live_snapshot_path = str(snapshot)
            job.live_snapshot_label = 'saved'
        self.jobs[job_id] = job
        return job

    def latest_saved(self) -> DashboardJob | None:
        if not self.export_root.is_dir():
            return None
        newest: tuple[float, str] | None = None
        for directory in self.export_root.iterdir():
            products = directory / 'products.json'
            if not directory.is_dir() or not products.is_file():
                continue
            try:
                stamp = products.stat().st_mtime
            except OSError:
                continue
            if newest is None or stamp > newest[0]:
                newest = (stamp, directory.name)
        if newest is None:
            return None
        return self.get(newest[1])

    async def start(
        self,
        *,
        url: str,
        tab: str,
        mode: str,
        interactive_browser: bool = False,
        upload_to_shopify: bool = False,
        schedule_daily: bool = False,
    ) -> DashboardJob:
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
        cooling = cooldown_status(self.artifacts_root)
        if cooling['active']:
            raise ValueError(cooling['message'])
        from ..extractors._interactive_browser import default_profile_dir
        from .._access import human_action_guidance
        profile = str(default_profile_dir(url.strip())) if interactive_browser else None
        job = DashboardJob(
            job_id=uuid.uuid4().hex,
            url=url.strip(),
            tab=tab,
            mode=mode,
            interactive_browser=interactive_browser,
            browser_profile_dir=profile,
            human_action=human_action_guidance('unknown') if interactive_browser else None,
            upload_to_shopify=upload_to_shopify,
            schedule_daily=bool(schedule_daily),
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
        job.human_action = None
        job.message = (
            'Continue received. Re-checking the Chromium page, then scraping visible products…'
        )
        return job

    def _progress_counts(self, job: DashboardJob) -> tuple[int | None, int | None]:
        """Return (discovered, extracted). Never infer discovered from URL attempts."""
        progress = job._progress
        if progress is None:
            return job.discovered, job.extracted
        extracted = 0
        for source in progress.sources:
            extracted = max(extracted, len(source.get('rows') or []))
        # Mid-run we only know accepted product rows; URL attempts are not discoveries.
        return None, (extracted if extracted else None)

    async def _run(self, job: DashboardJob) -> None:
        job.state = 'running'
        job.started_at = _utc_now()
        job._started_mono = time.monotonic()
        max_urls = products_this_run(job.mode)
        platform = TAB_PLATFORM[job.tab]
        browser = job.tab == 'custom' or job.interactive_browser
        policy = ExecutionPolicy(
            browser=browser,
            interactive_browser=job.interactive_browser,
            browser_profile_dir=job.browser_profile_dir,
        )
        # Interactive runs need room for delays between pages and a human gate.
        if job.interactive_browser:
            from ..run_control import DELAY_MAX_SECONDS
            pages = 4 + max_urls * 8
            crawl_budget = pages * (DELAY_MAX_SECONDS + 30) + 300.0
            timeout = crawl_budget + 120.0
        else:
            crawl_budget = 300.0 if job.mode == 'full' else 120.0
            timeout = crawl_budget
        if job.interactive_browser:
            from .._access import human_action_guidance
            job.human_action = human_action_guidance('unknown')
            job.message = (
                'Opening Chromium. If a waiting room, CAPTCHA, or login appears, '
                'complete it in that window — steps will show here — then click Continue.'
            )
            job.awaiting_human = False
        try:
            while not job.cancel_event.is_set():
                await self._run_batch(job, policy, crawl_budget, timeout, max_urls, platform)
                if not job.schedule_daily or job.state in ('failed', 'cancelled', 'blocked'):
                    break
                wait_seconds = self._next_batch_wait()
                logger.info('Scheduled mode waiting %s seconds before the next batch', wait_seconds)
                job.message = (
                    f'Batch finished. Next batch in {self._format_wait(wait_seconds)}. '
                    f'{job.message or ""}'
                ).strip()
                if not await self._sleep_cancellable(job, wait_seconds):
                    job.state = 'cancelled'
                    job.message = 'Scheduled run cancelled.'
                    break
                cooling = cooldown_status(self.artifacts_root)
                if cooling['active']:
                    job.message = cooling['message']
                    if not await self._sleep_cancellable(job, cooling['remaining_seconds']):
                        job.state = 'cancelled'
                        job.message = 'Scheduled run cancelled during cooldown.'
                        break
                job.state = 'running'
                job.message = 'Starting the next daily batch.'
                job.awaiting_human = False
                job.human_action = None
                job.preview = []
                job.extracted = 0
                job.upload_queued = 0
                job.upload_done = 0
                job.upload_skipped = 0
                job.upload_failed = 0
                job._upload_keys.clear()
        except asyncio.CancelledError:
            job.state = 'cancelled'
            job.message = 'Job cancelled.'
            job.outcome = 'failed'
            job.awaiting_human = False
        except asyncio.TimeoutError:
            job.catalog_complete = False
            job.incompleteness_reasons = ['operation_timeout']
            job.errors = ['operation_timeout']
            job.awaiting_human = False
            if job.preview:
                job.state = 'partial'
                job.outcome = 'partial'
                job.message = 'Operation timed out. Products already extracted were kept.'
            else:
                job.state = 'failed'
                job.outcome = 'failed'
                job.message = 'Operation timed out.'
        except Exception as exc:
            job.state = 'failed'
            job.message = f'Extraction failed: {type(exc).__name__}: {exc}'
            job.errors = [str(exc)]
            job.outcome = 'failed'
            job.awaiting_human = False
        finally:
            if job._started_mono is not None:
                job.elapsed_seconds = round(time.monotonic() - job._started_mono, 2)
            job.finished_at = _utc_now()

    async def _run_batch(
        self,
        job: DashboardJob,
        policy: ExecutionPolicy,
        crawl_budget: float,
        timeout: float,
        max_urls: int,
        platform,
    ) -> None:
        from .._extract import extract
        from ..extractors._interactive_browser import reset_live_hooks, set_live_hooks
        from .shopify_connection import load_connection
        from .shopify_catalog import list_family_index

        known_families: set[str] = set()
        uploader = self.catalog_uploader
        if job.upload_to_shopify:
            conn = load_connection(self.artifacts_root)
            if conn is not None:
                logger.info('Fetching existing Shopify family ids once for this run')
                index = await list_family_index(conn)
                if uploader is not None:
                    uploader.catalog_index = index
                known_families = set(index.families)
                job.duplicate_report = index.duplicate_report() or None
                if job.duplicate_report:
                    logger.info('Duplicate Shopify family report: %s', job.duplicate_report)
                if index.error:
                    logger.info('Shopify family index incomplete: %s', index.error)

        tokens = (None, None, None)

        def on_human_gate(action: dict, _block) -> None:
            job.awaiting_human = True
            job.human_action = dict(action)
            job.message = str(action.get('summary') or job.message)

        async def operation():
            nonlocal tokens
            if job.interactive_browser:
                tokens = set_live_hooks(
                    on_product=lambda record: self._on_live_product(job, record),
                    on_snapshot=lambda png, label: self._on_live_snapshot(job, png, label),
                    on_human_gate=on_human_gate,
                )
            with activate_known_families(known_families):
                with execution_policy(policy, crawl_budget) as state:
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

        poll = asyncio.create_task(self._poll_progress(job))
        try:
            try:
                result = await asyncio.wait_for(operation(), timeout=timeout)
            finally:
                if tokens[0] is not None:
                    reset_live_hooks(*tokens)
                poll.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await poll
            if job.cancel_event.is_set():
                raise asyncio.CancelledError()
            self._record_denial_if_needed(job, result)
            self._finalize_result(job, result)
            job.awaiting_human = False
            if job.state not in ('blocked',):
                job.human_action = None
            note = getattr(result, 'batch_note', '') or ''
            if note:
                job.message = f'{job.message or ""} {note}'.strip()
        except asyncio.TimeoutError:
            raise
        except asyncio.CancelledError:
            raise

    def _record_denial_if_needed(self, job: DashboardJob, result) -> None:
        denial = getattr(result, 'denial', None)
        blocked = access_blocked_reason(result.errors, result.incompleteness_reasons)
        if not isinstance(denial, dict) and blocked is None:
            return
        url = job.url
        reference = None
        completed = int(getattr(result, 'product_count', 0) or 0)
        if isinstance(denial, dict):
            url = str(denial.get('url') or url)
            reference = denial.get('reference') if isinstance(denial.get('reference'), str) else None
            completed = int(denial.get('completed_products') or completed)
        elif isinstance(blocked, str) and 'url=' in blocked:
            url = blocked.split('url=', 1)[-1].split()[0]
            if 'reference=' in blocked:
                reference = blocked.split('reference=', 1)[-1].split()[0]
        record_denial(
            self.artifacts_root,
            url=url,
            reference=reference,
            completed=completed,
        )
        logger.info(
            'Denial recorded completed=%s url=%s reference=%s',
            completed, url, reference or '',
        )

    def _next_batch_wait(self) -> float:
        return float(SCHEDULE_INTERVAL_HOURS) * 3600.0

    @staticmethod
    def _format_wait(seconds: float) -> str:
        total = max(0, int(seconds))
        hours = total // 3600
        minutes = (total % 3600) // 60
        if hours:
            return f'{hours}h {minutes}m'
        return f'{minutes}m'

    async def _sleep_cancellable(self, job: DashboardJob, seconds: float) -> bool:
        """Sleep until the wait ends. Return False when the operator cancelled."""
        deadline = time.monotonic() + max(0.0, float(seconds))
        while time.monotonic() < deadline:
            if job.cancel_event.is_set():
                return False
            await asyncio.sleep(min(5.0, max(0.1, deadline - time.monotonic())))
        return not job.cancel_event.is_set()

    async def _poll_progress(self, job: DashboardJob) -> None:
        while job.state == 'running':
            discovered, extracted = self._progress_counts(job)
            # Prefer live interactive extracted counts when preview is streaming.
            # Do not invent discovered from preview length or URL attempts.
            if job.preview:
                job.extracted = len(job.preview)
            elif extracted is not None:
                job.extracted = extracted
            if discovered is not None:
                job.discovered = discovered
            limit = products_this_run(job.mode)
            if job.extracted is not None and job.state == 'running':
                job.pending = max(0, limit - job.extracted)
            await asyncio.sleep(0.4)

    def _on_live_product(self, job: DashboardJob, record: dict) -> None:
        family = None
        if isinstance(record, dict):
            family = record.get('family_id') or record.get('external_id')
            if not isinstance(family, str) or not family:
                from ..extractors._interactive_browser import product_family_key
                family = product_family_key(record.get('product_url'))
        row = {
            'external_id': family if isinstance(family, str) else None,
            'title': record.get('title') if isinstance(record.get('title'), str) else None,
            'image_url': record.get('image_url') if isinstance(record.get('image_url'), str) else None,
            'price': str(record['price']) if record.get('price') not in (None, '') else None,
            'currency': record.get('currency') if isinstance(record.get('currency'), str) else None,
            'variant_count': len(record.get('variants') or []),
            'product_url': record.get('product_url') if isinstance(record.get('product_url'), str) else None,
        }
        job.preview = list(job.preview) + [row]
        job.extracted = len(job.preview)
        # Discovered stays null until finalize reports a measured discovery count.
        job.pending = max(0, products_this_run(job.mode) - job.extracted)
        job.variant_count = sum(int(item.get('variant_count') or 0) for item in job.preview)
        job.image_count = sum(1 for item in job.preview if item.get('image_url'))
        job.awaiting_human = False
        job.human_action = None
        job.message = f'Live preview: extracted {job.extracted} product(s)…'
        # Interactive mode emits a family only after every color page succeeded.
        if job.upload_to_shopify and self.on_upload_record is not None:
            self.on_upload_record(job, record)

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
        # Measured counters only — never infer discovered/failed from urls_attempted.
        job.extracted = result.product_count
        job.http_pages = getattr(result, 'http_pages', None)
        job.discovered = getattr(result, 'products_discovered', None)
        job.failed = getattr(result, 'products_failed', None)
        job.duplicates_merged = getattr(result, 'duplicates_merged', None)
        job.pending = 0
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
        limited = job.mode == 'test' or 'product_budget_reached' in job.incompleteness_reasons
        if blocked is not None and result.product_count == 0:
            job.state = 'blocked'
            job.message = blocked if blocked != ACCESS_BLOCKED else (
                f'{ACCESS_BLOCKED} url={job.url} status=unknown reason=access_blocked'
            )
            if job.human_action is None:
                from .._access import human_action_guidance
                reason = 'unknown'
                text = job.message or ''
                if 'reason=' in text:
                    reason = text.rsplit('reason=', 1)[-1].strip().split()[0]
                job.human_action = human_action_guidance(reason)
            return
        if blocked is not None:
            job.state = 'partial'
            job.outcome = 'partial'
            job.message = (
                f'Kept {result.product_count} scraped product(s). '
                f'A later page was blocked and was not allowed to discard them. {blocked}'
            )
            self._write_exports(job, result)
            return
        if outcome == 'complete' and result.catalog_complete is True:
            job.state = 'succeeded'
            job.message = 'Collection extraction completed with confirmed completeness evidence.'
            self._write_exports(job, result)
            return
        if result.product_count > 0:
            job.state = 'partial'
            if 'operation_timeout' in job.incompleteness_reasons:
                job.message = 'Operation timed out. Products already extracted were kept.'
            elif limited:
                job.message = (
                    f'Limited/partial run (cap {products_this_run(job.mode)}). '
                    'Completeness not confirmed for the full collection.'
                )
            else:
                job.message = 'Partial results retained; completeness not confirmed.'
            self._write_exports(job, result)
            return
        job.state = 'failed'
        job.message = 'No usable products were extracted; source failed or was incomplete.'

    def _write_exports(self, job: DashboardJob, result) -> None:
        from .._dedupe import dedupe_products
        products, dedupe_stats = dedupe_products(list(result.products), store=job.url)
        result.products = products
        prior_dupes = job.duplicates_merged or 0
        job.duplicates_merged = prior_dupes + dedupe_stats.duplicate_product_observations_merged
        job.extracted = len(products)
        job.variant_count = sum(len(p.variants or []) for p in products)
        job.image_count = sum(
            (1 if p.image_url else 0) + len(p.additional_images or []) for p in products
        )
        job.preview = preview_products(products)

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
            'product_count': len(products),
            'dedupe': dedupe_stats.as_dict(),
            'products': [
                {
                    'external_id': p.external_id or None,
                    'family_id': p.external_id if isinstance(p.external_id, str) and p.external_id.lower().startswith('nvprod') else None,
                    'title': p.title or None,
                    'description': p.description or None,
                    'description_html': getattr(p, 'description_html', '') or None,
                    'price': _money(p.price),
                    'compare_at_price': _money(p.compare_at_price),
                    'currency': p.currency or None,
                    'sku': p.sku,
                    'image_url': p.image_url or None,
                    'additional_images': list(p.additional_images or []),
                    'product_url': p.product_url or None,
                    'option_names': list(getattr(p, 'option_names', None) or []),
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
                for p in products
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
            'limited_run': job.mode == 'test' or 'product_budget_reached' in job.incompleteness_reasons,
            'incompleteness_reasons': job.incompleteness_reasons,
            'errors': job.errors,
            'message': job.message,
            'counts': {
                'http_pages': job.http_pages,
                'discovered': job.discovered,
                'extracted': job.extracted,
                'failed': job.failed,
                'duplicates_merged': job.duplicates_merged,
                'pending': job.pending,
                'variants': job.variant_count,
                'images': job.image_count,
            },
            'dedupe': dedupe_stats.as_dict(),
            'elapsed_seconds': job.elapsed_seconds,
            'pipeline': data,
        }
        # Atomic-ish write: temp then replace so failures don't clobber prior exports.
        self._atomic_write(products_path, products_payload)
        self._atomic_write(report_path, report_payload)
        from .shopify_csv import write_shopify_csv
        shopify_path = directory / 'products-shopify.csv'
        write_shopify_csv(products, shopify_path)
        job.products_path = str(products_path)
        job.report_path = str(report_path)
        job.shopify_csv_path = str(shopify_path)
        self._enqueue_uploads(job, products)

    def _enqueue_uploads(self, job: DashboardJob, products) -> None:
        if not job.upload_to_shopify or self.on_upload_record is None:
            return
        from .shopify_catalog import upload_record_from_product
        for product in products:
            self.on_upload_record(job, upload_record_from_product(product))

    @staticmethod
    def _atomic_write(path: Path, payload: dict) -> None:
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
        temporary.replace(path)
