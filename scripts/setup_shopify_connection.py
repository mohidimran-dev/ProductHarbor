#!/usr/bin/env python3
"""Interactive Shopify connection setup for Product Harbor (media-only).

- Prompts for my-store.myshopify.com
- Reads Admin API token via getpass (hidden; never printed or passed as argv)
- Saves with shopextract.dashboard.shopify_connection.save_connection
- Verifies auth + read_files/write_files via read-only GraphQL
- Runs verify_shopify_media_upload.py --dry-run to print the upload plan
- Does NOT start uploads

Exact command:

  cd D:\\Scraper\\shopextract
  .\\.venv\\Scripts\\python.exe scripts\\setup_shopify_connection.py
"""
from __future__ import annotations

import asyncio
import getpass
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from shopextract.dashboard.shopify_connection import (  # noqa: E402
    REQUIRED_SCOPES,
    SHOPIFY_API_VERSION,
    connection_path,
    normalize_shop,
    save_connection,
    verify_connection_readonly,
)

ARTIFACTS_ROOT = ROOT / 'artifacts'
VERIFY_SCRIPT = ROOT / 'scripts' / 'verify_shopify_media_upload.py'


def _prompt_shop() -> str:
    print('Product Harbor — Shopify connection setup (Files API only)')
    print(f'Required scopes : {", ".join(REQUIRED_SCOPES)}')
    print(f'API version     : {SHOPIFY_API_VERSION}')
    print('Uploads will NOT start from this script.')
    print()
    raw = input('Shop hostname (my-store.myshopify.com): ').strip()
    if not raw:
        raise SystemExit('Aborted: shop hostname is required.')
    try:
        return normalize_shop(raw)
    except ValueError as exc:
        raise SystemExit(f'Aborted: {exc}') from exc


def _prompt_token() -> str:
    # getpass hides input on supporting terminals; never echo or log the value.
    token = getpass.getpass('Admin API access token (input hidden): ')
    token = (token or '').strip()
    if not token:
        raise SystemExit('Aborted: access token is required.')
    if token.startswith('shpat_') is False and token.startswith('shpca_') is False:
        # Soft warning only — do not reject alternate valid token forms.
        print('Note: token does not start with shpat_/shpca_; continuing anyway.')
    return token


async def _verify_and_report(conn) -> dict:
    print()
    print(f'Verifying connection to {conn.shop} (read-only GraphQL)…')
    result = await verify_connection_readonly(conn)
    if result.get('ok'):
        print(f'  OK  shop_name={result.get("shop_name")!r}')
        print(f'  OK  domain={result.get("myshopify_domain")}')
        print(f'  OK  files_readable={result.get("files_readable")}')
        print(f'  OK  scopes include {", ".join(REQUIRED_SCOPES)}')
    else:
        print(f'  FAILED: {result.get("error")}')
        missing = result.get('missing_scopes') or []
        if missing:
            print(f'  Missing scopes: {", ".join(missing)}')
            print('  Enable them on the custom app, reinstall if required, then re-run setup.')
        granted = result.get('granted_scopes') or []
        if granted:
            print(f'  Granted scopes observed: {", ".join(granted)}')
    return result


def _run_dry_run_plan() -> int:
    print()
    print('Running media-upload dry-run plan (no uploads)…')
    print('-' * 60)
    # Invoke by path only — never pass the token as an argument or env override here.
    completed = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), '--dry-run', '--artifacts-root', str(ARTIFACTS_ROOT)],
        cwd=str(ROOT),
        check=False,
    )
    print('-' * 60)
    return int(completed.returncode)


def main() -> int:
    shop = _prompt_shop()
    token = _prompt_token()
    # Drop local reference after save; do not print.
    conn = save_connection(ARTIFACTS_ROOT, shop=shop, access_token=token)
    del token
    path = connection_path(ARTIFACTS_ROOT)
    print()
    print(f'Saved connection for {conn.shop}')
    print(f'Secrets file (gitignored): {path}')
    print('Token was not printed.')

    result = asyncio.run(_verify_and_report(conn))
    if not result.get('ok'):
        print()
        print('Connection saved but verification failed. Fix scopes/token, then re-run setup.')
        print('Dry-run plan skipped until verification succeeds.')
        return 1

    code = _run_dry_run_plan()
    if code != 0:
        print(f'Dry-run exited with code {code}. Connection is saved; fix the plan fixture if needed.')
        return code
    print()
    print('Setup complete. Uploads were not started.')
    print('When ready for the gated live verify (still requires typing the shop hostname):')
    print(f'  {sys.executable} {VERIFY_SCRIPT}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
