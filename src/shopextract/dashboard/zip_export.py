"""Build Product Harbor CSV + images ZIP backup packages."""
from __future__ import annotations

import json
import zipfile
from dataclasses import asdict
from pathlib import Path

from .image_assets import ImageAsset
from .shopify_csv import write_shopify_csv

BACKUP_README = """Product Harbor — CSV + Images ZIP (BACKUP PACKAGE)

This archive is a local backup / handoff package. It is NOT a complete
Shopify native product CSV import for local images.

Shopify's native product CSV importer requires publicly accessible image
URLs (HTTPS). Local paths, localhost, file:// URLs, and base64 data URLs
are not accepted in Shopify image columns.

Contents
--------
- products.csv          Shopify-format product CSV with SOURCE image URLs retained
- images/               Downloaded gallery and variant images (original bytes)
- image-manifest.json   Filename, associations, alt text, source URLs
- download-report.json  Download counts, dedupe stats, and failures
- README-BACKUP.txt     This file

To import products into Shopify with images automatically:
use Product Harbor → Prepare Shopify-ready CSV (uploads images to your
store Files library, then remaps CSV rows to Shopify CDN URLs).
"""


def write_image_manifest(assets: list[ImageAsset], path: Path) -> None:
    payload = {
        'package': 'Product Harbor CSV + Images ZIP',
        'note': 'Backup package. Local images are not auto-imported by Shopify native CSV.',
        'images': [
            {
                'source_url': asset.source_url,
                'alternate_urls': list(asset.alternate_urls),
                'normalized_url': asset.normalized_url,
                'filename': asset.filename,
                'content_hash': asset.content_hash,
                'pixel_hash': asset.pixel_hash,
                'mime_type': asset.mime_type,
                'byte_size': asset.byte_size,
                'status': asset.status,
                'error': asset.error,
                'reused_file': asset.reused_file,
                'associations': [asdict(item) for item in asset.associations],
            }
            for asset in assets
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding='utf-8')


def write_download_report(
    assets: list[ImageAsset],
    path: Path,
    *,
    cancelled: bool = False,
    product_stats: dict | None = None,
    image_stats: dict | None = None,
) -> dict:
    unique_files = {a.local_path for a in assets if a.status == 'downloaded' and a.local_path}
    report = {
        'package': 'Product Harbor',
        'cancelled': cancelled,
        'total': len(assets),
        'downloaded': sum(1 for a in assets if a.status == 'downloaded'),
        'failed': sum(1 for a in assets if a.status == 'failed'),
        'pending': sum(1 for a in assets if a.status == 'pending'),
        'partial': any(a.status == 'failed' for a in assets),
        'unique_products': (product_stats or {}).get('unique_products'),
        'duplicate_product_observations_merged': (product_stats or {}).get(
            'duplicate_product_observations_merged'
        ),
        'unique_downloaded_images': (image_stats or {}).get(
            'unique_downloaded_images', len(unique_files)
        ),
        'repeated_image_references_removed': (image_stats or {}).get(
            'repeated_image_references_removed', 0
        ),
        'files_reused': (image_stats or {}).get(
            'files_reused', sum(1 for a in assets if a.reused_file)
        ),
        'uploads_reused': (image_stats or {}).get('uploads_reused', 0),
        'failures': [
            {
                'source_url': a.source_url,
                'error': a.error,
                'associations': [asdict(x) for x in a.associations],
            }
            for a in assets if a.status == 'failed'
        ],
        'product_stats': product_stats or {},
        'image_stats': image_stats or {},
    }
    path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def build_csv_images_zip(
    *,
    products: list[dict],
    assets: list[ImageAsset],
    work_dir: Path,
    zip_path: Path,
    cancelled: bool = False,
    product_stats: dict | None = None,
    image_stats: dict | None = None,
) -> dict:
    """Create ZIP with products.csv (source URLs), images/, manifests."""
    work_dir.mkdir(parents=True, exist_ok=True)
    images_dir = work_dir / 'images'
    images_dir.mkdir(parents=True, exist_ok=True)

    csv_path = work_dir / 'products.csv'
    write_shopify_csv(products, csv_path)

    manifest_path = work_dir / 'image-manifest.json'
    report_path = work_dir / 'download-report.json'
    readme_path = work_dir / 'README-BACKUP.txt'
    write_image_manifest(assets, manifest_path)
    report = write_download_report(
        assets,
        report_path,
        cancelled=cancelled,
        product_stats=product_stats,
        image_stats=image_stats,
    )
    readme_path.write_text(BACKUP_README, encoding='utf-8')

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = zip_path.with_suffix(zip_path.suffix + '.tmp')
    with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(csv_path, arcname='products.csv')
        archive.write(manifest_path, arcname='image-manifest.json')
        archive.write(report_path, arcname='download-report.json')
        archive.write(readme_path, arcname='README-BACKUP.txt')
        written: set[str] = set()
        for asset in assets:
            if asset.status != 'downloaded' or not asset.local_path or not asset.filename:
                continue
            if asset.filename in written:
                continue
            source = Path(asset.local_path)
            if source.is_file():
                archive.write(source, arcname=f'images/{asset.filename}')
                written.add(asset.filename)
    temporary.replace(zip_path)
    report['zip_path'] = str(zip_path)
    report['csv_path'] = str(csv_path)
    return report
