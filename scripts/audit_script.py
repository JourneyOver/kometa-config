import os, glob

script_dir = os.path.dirname(os.path.abspath(__file__))
base_root = os.path.dirname(script_dir)
base = os.path.join(base_root, 'data', 'metadata')
collections_base = os.path.join(base_root, 'data', 'collections')
reports_dir = os.path.join(base_root, 'config', 'reports')
os.makedirs(reports_dir, exist_ok=True)
out = os.path.join(reports_dir, 'poster_audit_report.txt')

yml_files = glob.glob(os.path.join(base, '**', '*.yml'), recursive=True)
collection_files = glob.glob(os.path.join(collections_base, '**', '*.yml'), recursive=True)

missing_header = []
missing_url = []
has_posterdb = []
collection_missing_header = []
collection_missing_url = []
collection_has_posterdb = []

def normalize(path_str):
    return path_str.replace('\\', '/')

def check_file(f, base_dir):
    rel = normalize(os.path.relpath(f, base_dir))
    try:
        with open(f, 'r', encoding='utf-8', errors='replace') as fh:
            lines = [fh.readline().rstrip() for _ in range(5)]
    except:
        return None, None, False

    has_header = any('# Posters from:' in l or '# Poster from:' in l for l in lines)
    if not has_header:
        return rel, 'missing_header', False

    hash_lines = [l for l in lines if l.startswith('#')]
    has_url = any('http' in l.lower() for l in hash_lines)
    if not has_url:
        return rel, 'missing_url', False

    try:
        with open(f, 'r', encoding='utf-8', errors='replace') as fh:
            full_content = fh.read()
    except:
        return None, None, False

    is_posterdb = 'theposterdb.com' in full_content
    is_anime = rel.startswith('anime/') or rel.startswith('anime-movies/')

    if is_posterdb and not is_anime:
        return rel, 'has_posterdb', True

    return None, None, False

for f in yml_files:
    rel, category, _ = check_file(f, base)
    if category == 'missing_header':
        missing_header.append(f'metadata/{rel}')
    elif category == 'missing_url':
        missing_url.append(f'metadata/{rel}')
    elif category == 'has_posterdb':
        has_posterdb.append(f'metadata/{rel}')

for f in collection_files:
    rel, category, _ = check_file(f, collections_base)
    if category == 'missing_header':
        collection_missing_header.append(f'collections/{rel}')
    elif category == 'missing_url':
        collection_missing_url.append(f'collections/{rel}')
    elif category == 'has_posterdb':
        collection_has_posterdb.append(f'collections/{rel}')

with open(out, 'w', encoding='utf-8') as fh:
    fh.write('POSTER AUDIT REPORT\n')
    fh.write('=' * 60 + '\n')
    fh.write(f'Total .yml files scanned (metadata): {len(yml_files)}\n')
    fh.write(f'Total .yml files scanned (collections): {len(collection_files)}\n\n')

    fh.write('=' * 60 + '\n')
    fh.write('SECTION 1: METADATA - MISSING "# Posters from:" HEADER\n')
    fh.write(f'Count: {len(missing_header)}\n')
    fh.write('=' * 60 + '\n')
    for f in sorted(missing_header):
        fh.write(f'{f}\n')

    fh.write('\n' + '=' * 60 + '\n')
    fh.write('SECTION 2: METADATA - MISSING URL AFTER SECOND "#" LINE\n')
    fh.write(f'Count: {len(missing_url)}\n')
    fh.write('=' * 60 + '\n')
    for f in sorted(missing_url):
        fh.write(f'{f}\n')

    fh.write('\n' + '=' * 60 + '\n')
    fh.write('SECTION 3: METADATA - FILES USING theposterdb.com\n')
    fh.write('(Excludes anime/ and anime-movies/ directories)\n')
    fh.write(f'Count: {len(has_posterdb)}\n')
    fh.write('=' * 60 + '\n')
    for f in sorted(has_posterdb):
        fh.write(f'{f}\n')

    fh.write('\n' + '=' * 60 + '\n')
    fh.write('SECTION 4: COLLECTIONS - MISSING "# Poster from:" HEADER\n')
    fh.write(f'Count: {len(collection_missing_header)}\n')
    fh.write('=' * 60 + '\n')
    for f in sorted(collection_missing_header):
        fh.write(f'{f}\n')

    fh.write('\n' + '=' * 60 + '\n')
    fh.write('SECTION 5: COLLECTIONS - MISSING URL AFTER SECOND "#" LINE\n')
    fh.write(f'Count: {len(collection_missing_url)}\n')
    fh.write('=' * 60 + '\n')
    for f in sorted(collection_missing_url):
        fh.write(f'{f}\n')

    fh.write('\n' + '=' * 60 + '\n')
    fh.write('SECTION 6: COLLECTIONS - FILES USING theposterdb.com\n')
    fh.write('(Excludes anime/ and anime-movies/ directories)\n')
    fh.write(f'Count: {len(collection_has_posterdb)}\n')
    fh.write('=' * 60 + '\n')
    for f in sorted(collection_has_posterdb):
        fh.write(f'{f}\n')

total_posterdb = len(has_posterdb) + len(collection_has_posterdb)
total_missing_header = len(missing_header) + len(collection_missing_header)
total_missing_url = len(missing_url) + len(collection_missing_url)

print(f'Report written: {normalize(out)}')
print(f'Metadata files: {len(yml_files)}, Collection files: {len(collection_files)}')
print(f'Missing header: {total_missing_header}, Missing URL: {total_missing_url}, Has posterdb (non-anime): {total_posterdb}')
