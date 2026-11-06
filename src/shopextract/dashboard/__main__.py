"""python -m shopextract.dashboard"""
from __future__ import annotations

import argparse

from .app import main


def cli() -> None:
    parser = argparse.ArgumentParser(description='Product Harbor local extraction dashboard')
    parser.add_argument('--host', default='127.0.0.1', help='Bind host (default localhost only)')
    parser.add_argument('--port', type=int, default=8765, help='Bind port')
    args = parser.parse_args()
    print(f'Product Harbor dashboard: http://{args.host}:{args.port}/', flush=True)
    main(host=args.host, port=args.port)


if __name__ == '__main__':
    cli()
