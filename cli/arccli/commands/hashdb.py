"""
arco hashdb — Import, export, and manage Arcology hash databases.
"""

import csv
import json
import os
import sys
from io import StringIO
from ..client import ArcologyClient, ArcologyError


def cmd_hashdb_list(client: ArcologyClient, args):
    """List all hash databases."""
    databases = client.list_hash_databases()
    if args.json:
        from ..formatting import print_json
        print_json(databases)
        return

    if not databases:
        print('No hash databases found.')
        return

    from ..formatting import print_table
    print_table(
        ['ID', 'Name', 'Files', 'Recognition', 'Version'],
        [
            [
                db['id'],
                db['name'],
                db.get('file_count', 0),
                'enabled' if db.get('enable_product_recognition') else '-',
                db.get('version') or '',
            ]
            for db in databases
        ],
    )


def cmd_hashdb_export(client: ArcologyClient, args):
    """Export a hash database to a file."""
    try:
        data = client.get_hash_database(args.id)
    except ArcologyError as e:
        if e.status_code == 404:
            print(f'Error: hash database {args.id} not found.', file=sys.stderr)
            sys.exit(1)
        raise

    fmt = (args.format or 'json').lower()

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

    with open(args.output_file, 'w', encoding='utf-8') as fh:
        fh.write(content)

    products = data.get('products', [])
    total_files = sum(len(p.get('files', [])) for p in products)
    if args.json:
        from ..formatting import print_json
        print_json({
            'database': data['name'],
            'products': len(products),
            'files': total_files,
            'format': fmt,
            'output_file': args.output_file,
        })
    else:
        print(f'Exported "{data["name"]}" ({len(products)} product(s), '
              f'{total_files} file(s)) to {args.output_file}')


def cmd_hashdb_import(client: ArcologyClient, args):
    """Import a hash database from a file."""
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
            'database': {'name': args.name or os.path.splitext(
                os.path.basename(input_file))[0]},
            'products': [
                {'title': title, 'files': files}
                for title, files in products_by_title.items()
            ],
        }
    else:
        import_data = json.loads(content)
        if import_data.get('schema_version', 1) != 1:
            print(f'Warning: schema_version {import_data["schema_version"]} is not 1; '
                  f'proceeding anyway.', file=sys.stderr)

    db_name = args.name or import_data['database']['name']
    products = import_data.get('products', [])

    # Find or create database
    db_id = None
    if args.merge:
        for existing in client.list_hash_databases():
            if existing['name'] == db_name:
                db_id = existing['id']
                if not args.json:
                    print(f'Merging into existing database "{db_name}" (id={db_id})')
                break
        if db_id is None:
            print(f'Warning: --merge specified but no database named "{db_name}" found; '
                  f'creating new.', file=sys.stderr)

    if db_id is None:
        try:
            result = client.create_hash_database(
                name=db_name,
                description=import_data.get('database', {}).get('description'),
                version=import_data.get('database', {}).get('version'),
                source_url=import_data.get('database', {}).get('source_url'),
            )
            db_id = result['id']
            if not args.json:
                print(f'Created database "{db_name}" (id={db_id})')
        except ArcologyError as e:
            if e.status_code == 409:
                print(f'Error: a database named "{db_name}" already exists. '
                      f'Use --merge to add to it.', file=sys.stderr)
                sys.exit(1)
            raise

    # Import products and files
    total_files = 0
    for product in products:
        title = product.get('title', 'Untitled')
        result = client.create_hash_database_product(
            db_id,
            title=title,
            description=product.get('description'),
            path_match_enabled=product.get('path_match_enabled', False),
        )
        product_id = result['id']

        files = product.get('files', [])
        if files:
            result = client.add_product_files(db_id, product_id, files)
            added = result.get('added', len(files))
            total_files += added
            if not args.json:
                print(f'  Product "{title}": {added} file(s) added')

    if args.json:
        from ..formatting import print_json
        print_json({
            'database': db_name,
            'db_id': db_id,
            'products': len(products),
            'files': total_files,
        })
    else:
        print(f'\nImport complete: {len(products)} product(s), {total_files} file(s) into "{db_name}"')

# vim: ts=4 sw=4 et
