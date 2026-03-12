#!/usr/bin/env python3
"""
arcology-hashdb — import/export hash databases from an Arcology instance.

Usage:
    arcology-hashdb list [options]
    arcology-hashdb export <id> <output-file> [--format json|csv] [options]
    arcology-hashdb import <input-file> [--format json|csv] [--name NAME] [--merge] [options]

Options:
    --api-url URL   Arcology API base URL (default: $ARCOLOGY_API or http://localhost:5000/api)
    --api-key KEY   API key for authentication (default: $WORKER_API_KEY)
    --format FMT    Export/import format: json (default) or csv
    --name NAME     Override database name on import
    --merge         Add products to an existing database instead of creating a new one

Examples:
    arcology-hashdb list
    arcology-hashdb export 1 riscos_apps.json
    arcology-hashdb export 1 riscos_apps.csv --format csv
    arcology-hashdb import riscos_apps.json
    arcology-hashdb import riscos_apps.json --name "RISC OS Apps (imported)" --merge
"""

import argparse
import csv
import json
import os
import sys
from io import StringIO

import requests


def build_client(api_url: str, api_key: str):
    session = requests.Session()
    session.headers.update({'X-API-Key': api_key})
    return session, api_url.rstrip('/')


def cmd_list(args, session, api_url):
    resp = session.get(f'{api_url}/hash-databases')
    resp.raise_for_status()
    databases = resp.json()
    if not databases:
        print('No hash databases found.')
        return
    print(f'{"ID":>4}  {"Name":<40}  {"Files":>6}  {"Recognition":<12}  Version')
    print('-' * 80)
    for db in databases:
        recognition = 'enabled' if db.get('enable_product_recognition') else '-'
        print(f'{db["id"]:>4}  {db["name"]:<40}  {db.get("file_count", 0):>6}  {recognition:<12}  {db.get("version") or ""}')


def cmd_export(args, session, api_url):
    db_id = args.id
    output_file = args.output_file
    fmt = (args.format or 'json').lower()

    resp = session.get(f'{api_url}/hash-databases/{db_id}')
    if resp.status_code == 404:
        print(f'Error: hash database {db_id} not found.', file=sys.stderr)
        sys.exit(1)
    resp.raise_for_status()
    data = resp.json()

    if fmt == 'csv':
        out = StringIO()
        writer = csv.writer(out)
        writer.writerow(['product_title', 'filename', 'file_size', 'md5', 'sha1', 'sha256',
                         'crc32', 'is_required', 'relative_path', 'description'])
        for product in data.get('products', []):
            for f in product.get('files', []):
                writer.writerow([
                    product['title'],
                    f.get('filename', ''),
                    f.get('file_size', '') or '',
                    f.get('md5', '') or '',
                    f.get('sha1', '') or '',
                    f.get('sha256', '') or '',
                    f.get('crc32', '') or '',
                    '1' if f.get('is_required', True) else '0',
                    f.get('relative_path', '') or '',
                    f.get('description', '') or '',
                ])
        content = out.getvalue()
    else:
        export_data = {
            'schema_version': 1,
            'database': {
                'name': data['name'],
                'description': data.get('description'),
                'version': data.get('version'),
                'source_url': data.get('source_url'),
            },
            'products': [
                {
                    'title': p['title'],
                    'description': p.get('description'),
                    'path_match_enabled': p.get('path_match_enabled', False),
                    'files': [
                        {
                            'filename': f.get('filename'),
                            'file_size': f.get('file_size'),
                            'md5': f.get('md5'),
                            'sha1': f.get('sha1'),
                            'sha256': f.get('sha256'),
                            'crc32': f.get('crc32'),
                            'is_required': f.get('is_required', True),
                            'relative_path': f.get('relative_path'),
                            'description': f.get('description'),
                        }
                        for f in p.get('files', [])
                    ],
                }
                for p in data.get('products', [])
            ],
        }
        content = json.dumps(export_data, indent=2)

    with open(output_file, 'w', encoding='utf-8') as fh:
        fh.write(content)

    total_files = sum(len(p.get('files', [])) for p in data.get('products', []))
    print(f'Exported "{data["name"]}" ({len(data.get("products", []))} product(s), '
          f'{total_files} file(s)) to {output_file}')


def cmd_import(args, session, api_url):
    input_file = args.input_file
    fmt = (args.format or 'json').lower()
    if fmt == 'auto':
        fmt = 'csv' if input_file.lower().endswith('.csv') else 'json'

    with open(input_file, 'r', encoding='utf-8') as fh:
        content = fh.read()

    if fmt == 'csv':
        reader = csv.DictReader(StringIO(content))
        products_by_title: dict[str, list] = {}
        for row in reader:
            title = row.get('product_title', '').strip() or 'Uncategorised'
            if title not in products_by_title:
                products_by_title[title] = []
            products_by_title[title].append({
                'filename': row.get('filename', ''),
                'file_size': int(row['file_size']) if row.get('file_size', '').isdigit() else None,
                'md5': row.get('md5') or None,
                'sha1': row.get('sha1') or None,
                'sha256': row.get('sha256') or None,
                'crc32': row.get('crc32') or None,
                'is_required': row.get('is_required', '1') == '1',
                'relative_path': row.get('relative_path') or None,
                'description': row.get('description') or None,
            })
        import_data = {
            'database': {'name': args.name or os.path.splitext(os.path.basename(input_file))[0]},
            'products': [
                {'title': title, 'files': files}
                for title, files in products_by_title.items()
            ],
        }
    else:
        import_data = json.loads(content)
        if import_data.get('schema_version', 1) != 1:
            print(f'Warning: schema_version {import_data["schema_version"]} is not 1; proceeding anyway.',
                  file=sys.stderr)

    db_name = args.name or import_data['database']['name']
    products = import_data.get('products', [])

    # Find or create database
    db_id = None
    if args.merge:
        resp = session.get(f'{api_url}/hash-databases')
        resp.raise_for_status()
        for existing in resp.json():
            if existing['name'] == db_name:
                db_id = existing['id']
                print(f'Merging into existing database "{db_name}" (id={db_id})')
                break
        if db_id is None:
            print(f'Warning: --merge specified but no database named "{db_name}" found; creating new.',
                  file=sys.stderr)

    if db_id is None:
        resp = session.post(f'{api_url}/hash-databases', json={
            'name': db_name,
            'description': import_data.get('database', {}).get('description'),
            'version': import_data.get('database', {}).get('version'),
            'source_url': import_data.get('database', {}).get('source_url'),
        })
        if resp.status_code == 409:
            print(f'Error: a database named "{db_name}" already exists. Use --merge to add to it.',
                  file=sys.stderr)
            sys.exit(1)
        resp.raise_for_status()
        db_id = resp.json()['id']
        print(f'Created database "{db_name}" (id={db_id})')

    # Import products and files
    total_files = 0
    for product in products:
        title = product.get('title', 'Untitled')
        resp = session.post(f'{api_url}/hash-databases/{db_id}/products', json={
            'title': title,
            'description': product.get('description'),
            'path_match_enabled': product.get('path_match_enabled', False),
        })
        resp.raise_for_status()
        product_id = resp.json()['id']

        files = product.get('files', [])
        if files:
            resp = session.post(
                f'{api_url}/hash-databases/{db_id}/products/{product_id}/files',
                json=files
            )
            resp.raise_for_status()
            added = resp.json().get('added', len(files))
            total_files += added
            print(f'  Product "{title}": {added} file(s) added')

    print(f'\nImport complete: {len(products)} product(s), {total_files} file(s) into "{db_name}"')


def main():
    parser = argparse.ArgumentParser(
        description='Import/export Arcology hash databases',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--api-url', default=os.environ.get('ARCOLOGY_API', 'http://localhost:5000/api'),
                        help='Arcology API base URL')
    parser.add_argument('--api-key', default=os.environ.get('WORKER_API_KEY', ''),
                        help='API key for authentication')

    subparsers = parser.add_subparsers(dest='command', required=True)

    # list
    subparsers.add_parser('list', help='List all hash databases')

    # export
    exp = subparsers.add_parser('export', help='Export a hash database to a file')
    exp.add_argument('id', type=int, help='Database ID')
    exp.add_argument('output_file', help='Output file path')
    exp.add_argument('--format', choices=['json', 'csv'], default='json')

    # import
    imp = subparsers.add_parser('import', help='Import a hash database from a file')
    imp.add_argument('input_file', help='Input file path')
    imp.add_argument('--format', choices=['json', 'csv', 'auto'], default='auto',
                     help='File format (default: auto-detect from extension)')
    imp.add_argument('--name', help='Override database name')
    imp.add_argument('--merge', action='store_true',
                     help='Add to an existing database with the same name')

    args = parser.parse_args()

    if not args.api_key:
        print('Warning: no API key set. Set --api-key or $WORKER_API_KEY.', file=sys.stderr)

    session, api_url = build_client(args.api_url, args.api_key)

    try:
        if args.command == 'list':
            cmd_list(args, session, api_url)
        elif args.command == 'export':
            cmd_export(args, session, api_url)
        elif args.command == 'import':
            cmd_import(args, session, api_url)
    except requests.HTTPError as e:
        print(f'API error: {e}', file=sys.stderr)
        try:
            print(f'Response: {e.response.json()}', file=sys.stderr)
        except Exception:
            pass
        sys.exit(1)
    except requests.ConnectionError as e:
        print(f'Connection error: {e}', file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as e:
        print(f'File error: {e}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()

# vim: ts=4 sw=4 et
