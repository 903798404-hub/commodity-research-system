"""Collect publicly linked HiSugar freight tables into an isolated candidate, never publish."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import time
import unicodedata
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = 'https://www.hisugar.com'


def packed(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'


def fetch(url, limit=4 * 1024 * 1024):
    if urlparse(url).hostname != 'www.hisugar.com' or not url.startswith(BASE + '/'):
        raise ValueError('Only the public HiSugar source is allowed')
    last = None
    for attempt in range(3):
        try:
            with requests.get(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': BASE + '/'},
                              timeout=(5, 15), stream=True, allow_redirects=False) as response:
                response.raise_for_status()
                if response.status_code != 200: raise ValueError('Unexpected source redirect')
                data = bytearray()
                deadline = time.monotonic() + 25
                for chunk in response.iter_content(16384):
                    data.extend(chunk)
                    if len(data) > limit or time.monotonic() > deadline:
                        raise ValueError('Source response exceeded bound')
            return bytes(data)
        except requests.RequestException as exc:
            last = type(exc).__name__
            time.sleep(.3 * (attempt + 1))
    raise ValueError('Source request failed: ' + str(last))


def discover(root):
    url = BASE + '/home/searchCatetoryAndArticle?keyWord=' + requests.utils.quote('食糖进口成本')
    path = root / 'search-all.json'
    raw = path.read_bytes() if path.exists() else fetch(url)
    response = json.loads(raw.decode('utf-8'))
    if response.get('code') != 0: raise ValueError('Public search failed')
    path.write_bytes(raw)
    rows = [r for group in response['data'] for r in group['list']
            if re.match(r'\d{4}', r.get('title', '')) and '食糖进口成本' in r['title']]
    if len({r['id'] for r in rows}) != len(rows): raise ValueError('Duplicate article identity')
    (root / 'articles.json').write_text(packed(rows), encoding='utf-8')
    return rows


def article(root, row):
    identity = row['id']
    if not re.fullmatch(r'\d{16,30}', identity): raise ValueError('Invalid public article ID')
    receipt = root / 'articles' / (identity + '.json')
    if receipt.exists(): return json.loads(receipt.read_text(encoding='utf-8'))
    url = BASE + '/home/articleContent?id=' + identity
    raw = fetch(url)
    soup = BeautifulSoup(raw.decode('utf-8', errors='strict'), 'html.parser')
    title = soup.select_one('#articleTitle')
    content = soup.select_one('.news-left .content')
    if not title or title.get_text(strip=True) != row['title'] or content is None:
        raise ValueError('Article identity or public content missing')
    html_path = root / 'articles' / (identity + '.html')
    html_path.write_bytes(raw)
    images = content.find_all('img', src=True)
    body_text = content.get_text(' ', strip=True)
    stamp = re.search(r'(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日', body_text)
    if stamp:
        article_date = datetime(*map(int, stamp.groups())).date().strftime('%Y%m%d')
    else:
        # Retain malformed title dates for review rather than guessing the correction.
        article_date = re.match(r'\d+', row['title'])[0]
    result = dict(id=identity, title=row['title'], article_date=article_date,
        published_at=row.get('publishTime'), article_url=url,
        retrieved_at=datetime.now().astimezone().isoformat(), html_sha256=hashlib.sha256(raw).hexdigest(),
        images=[], text=body_text)
    # These reports place the numerical source table first; graph-only images are not observations.
    for image in images[:1]:
        image_url = urljoin(BASE, image['src'])
        data = fetch(image_url)
        digest = hashlib.sha256(data).hexdigest()
        from PIL import Image
        from io import BytesIO
        with Image.open(BytesIO(data)) as picture:
            picture.verify()
        image_path = root / 'images' / (digest + '.image')
        if not image_path.exists(): image_path.write_bytes(data)
        result['images'].append(dict(url=image_url, sha256=digest, bytes=len(data), file=image_path.name))
    receipt.write_text(packed(result), encoding='utf-8')
    return result


def collect(root, workers=4):
    root.mkdir(parents=True, exist_ok=True)
    for folder in ('articles', 'images'): (root / folder).mkdir(exist_ok=True)
    rows = discover(root)
    failures, receipts = [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(article, root, row): row for row in rows}
        for i, future in enumerate(as_completed(futures), 1):
            row = futures[future]
            try: receipts.append(future.result())
            except Exception as exc:
                failures.append(dict(id=row['id'], title=row['title'], error=type(exc).__name__ + ': ' + str(exc)))
            if i % 50 == 0: print(f'Fetched {i}/{len(rows)} articles; failures {len(failures)}', flush=True)
    manifest = dict(schema_version='hisugar-public-capture/1', search_url=BASE + '/home/searchCatetoryAndArticle',
        keyword='食糖进口成本', search_sha256=hashlib.sha256((root / 'search-all.json').read_bytes()).hexdigest(),
        discovered=len(rows), collected=len(receipts), failures=failures,
        articles=sorted(receipts, key=lambda r: r['article_date']))
    (root / 'capture.json').write_text(packed(manifest), encoding='utf-8')
    print(f'Captured {len(receipts)} of {len(rows)} public reports; {len(failures)} unavailable', flush=True)


def prepare_ocr(root):
    """Produce a number-only review panel, retaining exact source cell geometry."""
    from PIL import Image
    import numpy as np
    manifest = json.loads((root / 'capture.json').read_text(encoding='utf-8'))
    (root / 'ocr-panels').mkdir(exist_ok=True)
    jobs, skipped = [], []
    for row in manifest['articles']:
        if not row['images']:
            skipped.append(dict(id=row['id'], reason='No source table image')); continue
        evidence = row['images'][0]
        with Image.open(root / 'images' / evidence['file']) as source:
            source = source.convert('RGB')
            pixels = np.asarray(source)
            gold = (pixels[:, :, 0] > 190) & (pixels[:, :, 1] > 90) & (pixels[:, :, 1] < 240) & (pixels[:, :, 2] < 110)
            def centers(values):
                groups = []
                for n in values:
                    if not groups or n > groups[-1][-1] + 2: groups.append([int(n)])
                    else: groups[-1].append(int(n))
                return [int(sum(g)/len(g)) for g in groups]
            ys = centers(np.where(gold.mean(axis=1) > .65)[0])
            xs = centers(np.where(gold.mean(axis=0) > .35)[0])
            if len(xs) < 5 or len(ys) < 4:
                # A small number of genuine reports use black rather than gold grid lines.
                black = (pixels < 90).all(axis=2)
                ys = centers(np.where(black.mean(axis=1) > .65)[0])
                xs = centers(np.where(black.mean(axis=0) > .50)[0])
            if len(xs) < 5 or len(ys) < 4:
                skipped.append(dict(id=row['id'], reason='Unrecognized table layout', width=source.width, height=source.height)); continue
            cells = []
            for top, bottom in zip(ys, ys[1:]):
                if not 15 <= bottom - top <= 75: continue
                cells.append(dict(top=top, bottom=bottom, date_left=xs[0], date_right=xs[1], freight_left=xs[3], freight_right=xs[4]))
            if not cells:
                skipped.append(dict(id=row['id'], reason='No candidate rows')); continue
            # Separate the two columns with ample whitespace; larger black glyphs improve native OCR.
            header = source.crop((0, 0, source.width, min(200, source.height)))
            header.thumbnail((1600, 240))
            panel = Image.new('RGB', (1600, 100 * len(cells) + header.height + 10), 'white')
            for i, cell in enumerate(cells):
                for field, x, width in (('date', 10, 570), ('freight', 650, 330)):
                    crop = source.crop((cell[field+'_left']+2, cell['top']+2, cell[field+'_right']-2, cell['bottom']-2))
                    crop.thumbnail((width // 3, 28))
                    crop = crop.resize((crop.width*3, crop.height*3))
                    panel.paste(crop, (x, i * 100 + 5))
            panel.paste(header, (0, 100 * len(cells) + 10))
            path = root / 'ocr-panels' / (row['id'] + '.png')
            panel.save(path)
            jobs.append(dict(id=row['id'], path=str(path.resolve()), cells=cells, source_sha256=evidence['sha256']))
    (root / 'ocr-jobs.json').write_text(packed(jobs), encoding='utf-8')
    (root / 'ocr-layout-skips.json').write_text(packed(skipped), encoding='utf-8')
    print(f'OCR panels: {len(jobs)}; layout skips: {len(skipped)}')


def parse_ocr(root):
    from datetime import date
    from collections import defaultdict
    jobs = {r['id']: r for r in json.loads((root / 'ocr-jobs.json').read_text(encoding='utf-8'))}
    articles = {r['id']: r for r in json.loads((root / 'capture.json').read_text(encoding='utf-8'))['articles']}
    corrections_path = root / 'cell-reviews.json'
    corrections = {(r['id'], r['row']):r for r in json.loads(corrections_path.read_text(encoding='utf-8'))} if corrections_path.exists() else {}
    header_path = root / 'header-reviews.json'
    headers = json.loads(header_path.read_text(encoding='utf-8')) if header_path.exists() else []
    occurrences, rejects = defaultdict(list), []
    for line in (root / 'ocr-results.jsonl').read_text(encoding='utf-8').splitlines():
        ocr = json.loads(line)
        identity = ocr['id']
        source = articles[identity]
        text = re.sub(r'\s', '', unicodedata.normalize('NFKC', ocr.get('text', '')))
        # Prove the selected source column is a freight column, never infer from costs.
        if ('海运费' not in text or '美元' not in text or '巴西' not in text) and identity not in headers:
            rejects.append(dict(id=identity, reason='Brazil freight header or USD unit not recognized')); continue
        for index, cell in enumerate(jobs[identity]['cells']):
            words = [w for w in ocr.get('words', []) if int((w['y'] + w['height']/2)//100) == index]
            strings = [''.join(w['text'] for w in sorted(words, key=lambda w: w['x']) if (w['x'] >= 600) == right) for right in (False, True)]
            raw_date, raw_value = [unicodedata.normalize('NFKC', v).replace(' ', '') for v in strings]
            reviewed = corrections.get((identity, index))
            if reviewed:
                if reviewed['source_sha256'] != jobs[identity]['source_sha256']:
                    raise ValueError('Reviewed cell source identity changed')
                raw_date, raw_value = reviewed['date'].replace('-', ''), str(reviewed['value'])
            if not re.fullmatch(r'20\d{6}', raw_date):
                if any(c.isdigit() for c in raw_date):
                    rejects.append(dict(id=identity, row=index, reason='Unrecognized source date', raw_date=raw_date, raw_value=raw_value))
                continue
            try:
                day = datetime.strptime(raw_date, '%Y%m%d').date()
                if day > date.fromisoformat(source['published_at'][:10]): raise ValueError('Future source date')
                if not re.fullmatch(r'\d{1,3}(?:\.\d{1,3})?', raw_value): raise ValueError('Unrecognized freight number')
                number = float(raw_value)
                if number > 300: raise ValueError('Freight requires range review')
            except ValueError as exc:
                rejects.append(dict(id=identity, row=index, reason=str(exc), raw_date=raw_date, raw_value=raw_value)); continue
            evidence = dict(article_url=source['article_url'], article_title=source['title'],
                published_at=source['published_at'].replace(' ', 'T') + '+08:00',
                retrieved_at=source['retrieved_at'], image_url=source['images'][0]['url'],
                image_sha256=source['images'][0]['sha256'], html_sha256=source['html_sha256'], row_index=index,
                source_cell=cell, article_id=identity)
            if reviewed: evidence['cell_visual_review'] = True
            occurrences[day.isoformat()].append(dict(value=number, source=evidence))
    observations, conflicts = [], []
    for day, records in sorted(occurrences.items()):
        values = {r['value'] for r in records}
        if len(values) != 1:
            conflicts.append(dict(date=day, occurrences=records)); continue
        observations.append(dict(date=day, country='巴西', freight_usd_per_tonne=records[0]['value'],
            extraction_status='cross_report_agreement' if len(records) > 1 else 'single_source',
            sources=sorted([r['source'] for r in records], key=lambda r: (not r.get('cell_visual_review', False), r['published_at']))))
    review = dict(observations=observations, conflicts=conflicts, rejected=rejects)
    (root / 'extracted-review.json').write_text(packed(review), encoding='utf-8')
    print(f'Unique observations {len(observations)}; conflicting dates {len(conflicts)}; rejected cells/tables {len(rejects)}')


def review_candidates(root):
    """Show source cells even when numeric OCR is untrusted; never infer missing digits."""
    from PIL import Image, ImageDraw, ImageFont
    jobs = {r['id']: r for r in json.loads((root / 'ocr-jobs.json').read_text(encoding='utf-8'))}
    candidates, rejected = {}, []
    for line in (root / 'ocr-results.jsonl').read_text(encoding='utf-8').splitlines():
        result = json.loads(line)
        identity = result['id']
        for index, cell in enumerate(jobs[identity]['cells']):
            words = [w for w in result.get('words', []) if int((w['y']+w['height']/2)//100) == index]
            parts = [''.join(w['text'] for w in sorted(words, key=lambda w:w['x']) if (w['x'] >= 600) == right) for right in (False, True)]
            day, value = [unicodedata.normalize('NFKC', p).replace(' ', '') for p in parts]
            if sum(c.isdigit() for c in day) < 5: continue
            item = dict(id=identity, row=index, raw_date=day, raw_value=value,
                source_sha256=jobs[identity]['source_sha256'], cell=cell)
            try:
                if not re.fullmatch(r'20\d{6}', day): raise ValueError()
                parsed = datetime.strptime(day, '%Y%m%d').date().isoformat()
            except ValueError:
                rejected.append(item); continue
            item['date'] = parsed
            candidates.setdefault(parsed, item)
    candidates = sorted(candidates.values(), key=lambda r:r['date'])
    (root / 'candidate-cells.json').write_text(packed(candidates), encoding='utf-8')
    (root / 'date-cell-rejects.json').write_text(packed(rejected), encoding='utf-8')
    font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 18)
    for label, rows in [('all-cell-sheets', candidates), ('bad-date-sheets', rejected)]:
        folder = root / label
        folder.mkdir(exist_ok=True)
        for start in range(0, len(rows), 40):
            sheet = Image.new('RGB', (1200, 1725), 'white')
            draw = ImageDraw.Draw(sheet)
            draw.text((10, 6), 'Untrusted OCR | original date cell | original Brazil freight cell', fill='black', font=font)
            for offset, row in enumerate(rows[start:start+40]):
                y = 45 + offset * 42
                draw.text((10, y+6), f"{start+offset:04} {row.get('date',row['raw_date'])} {row['raw_value']}", fill='black', font=font)
                with Image.open(root / 'images' / (row['source_sha256']+'.image')) as picture:
                    c = row['cell']
                    for field, x in [('date',380), ('freight',780)]:
                        crop = picture.crop((c[field+'_left']+2,c['top']+2,c[field+'_right']-2,c['bottom']-2)).convert('RGB')
                        crop.thumbnail((350,36))
                        sheet.paste(crop,(x,y))
                draw.line((0,y+40,1200,y+40),fill='#dddddd')
            sheet.save(folder / f'{start//40+1:03}.png')
    print(f'Candidate dates {len(candidates)}; date OCR rejects {len(rejected)}')


def review_sheets(root):
    """Render exact source date/freight cell crops beside the extracted numbers."""
    from PIL import Image, ImageDraw, ImageFont
    value = json.loads((root / 'extracted-review.json').read_text(encoding='utf-8'))
    rows = value['observations']
    target = root / 'review-sheets'
    target.mkdir(exist_ok=True)
    font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 18)
    for start in range(0, len(rows), 40):
        sheet = Image.new('RGB', (1200, 40 * 42 + 45), 'white')
        draw = ImageDraw.Draw(sheet)
        draw.text((10, 6), 'Extracted date / USD per tonne | original date cell | original freight cell', fill='black', font=font)
        for i, row in enumerate(rows[start:start+40]):
            y = i * 42 + 45
            src = row['sources'][0]
            cell = src['source_cell']
            draw.text((10, y+6), f"{row['date']}   {row['freight_usd_per_tonne']:g}   n={len(row['sources'])}", fill='black', font=font)
            with Image.open(root / 'images' / (src['image_sha256'] + '.image')) as image:
                for field, x in (('date', 380), ('freight', 780)):
                    crop = image.crop((cell[field+'_left']+2, cell['top']+2, cell[field+'_right']-2, cell['bottom']-2)).convert('RGB')
                    crop.thumbnail((350, 36))
                    sheet.paste(crop, (x, y))
            draw.line((0, y+40, 1200, y+40), fill='#dddddd')
        sheet.save(target / f'{start//40+1:03}.png')
    print(f'Review sheets: {(len(rows)+39)//40}')


def seal(root):
    """Seal only visually reviewed source observations; unresolved records stay absent."""
    from datetime import date, timedelta
    import csv
    review_path = root / 'extracted-review.json'
    review = json.loads(review_path.read_text(encoding='utf-8'))
    approval = json.loads((root / 'review-approval.json').read_text(encoding='utf-8'))
    digest = hashlib.sha256(review_path.read_bytes()).hexdigest()
    if (approval.get('extracted_sha256') != digest
            or approval.get('checked_observations') != len(review['observations'])
            or not approval.get('reviewer') or not approval.get('reviewed_sheets')):
        raise ValueError('Exact extracted observations require a source-cell visual review receipt')
    capture = json.loads((root / 'capture.json').read_text(encoding='utf-8'))
    rows = review['observations']
    if not rows: raise ValueError('No reviewed public observations')
    if any(not any(s.get('cell_visual_review') for s in r['sources']) for r in rows):
        raise ValueError('Every published observation requires a visually checked original source cell')
    bundle = dict(schema_version='hisugar-freight/1', provider='HiSugar', unit='USD/tonne',
        definition='泛糖测算采用运费，非SYCAC00或逐月船期成交报价',
        captured_at=max(r['retrieved_at'] for r in capture['articles']),
        extraction='Windows native OCR; exact source date/freight cells visually reviewed',
        observations=rows)
    raw = packed(bundle).encode('utf-8')
    sha = hashlib.sha256(raw).hexdigest()
    folder = root / 'sealed'
    folder.mkdir(exist_ok=True)
    name = 'freight-' + sha[:16] + '.json'
    target = folder / name
    if target.exists() and target.read_bytes() != raw: raise ValueError('Immutable artifact collision')
    target.write_bytes(raw)
    manifest = dict(schema_version=bundle['schema_version'], file=name, sha256=sha, row_count=len(rows),
        first_date=rows[0]['date'], last_date=rows[-1]['date'], source_report_count=capture['collected'],
        review_sha256=digest)
    (folder / 'current.json').write_text(packed(manifest), encoding='utf-8')
    lookup = {r['date']: r for r in rows}
    with (folder / 'hisugar-freight-history.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['date', 'freight_usd_per_tonne', 'country', 'definition', 'status', 'published_at', 'article_url', 'image_url', 'image_sha256', 'source_count'])
        stamp = date.fromisoformat(rows[0]['date'])
        end = date.fromisoformat(bundle['captured_at'][:10])
        while stamp <= end:
            row = lookup.get(stamp.isoformat())
            if row:
                src = row['sources'][0]
                writer.writerow([stamp.isoformat(), row['freight_usd_per_tonne'], '巴西', bundle['definition'],
                    row['extraction_status'], src['published_at'], src['article_url'], src['image_url'], src['image_sha256'], len(row['sources'])])
            else:
                writer.writerow([stamp.isoformat(), '', '巴西', bundle['definition'], 'unavailable', '', '', '', '', ''])
            stamp += timedelta(days=1)
    audit = dict(manifest=manifest, discovered=capture['discovered'], collected=capture['collected'],
        download_failures=capture['failures'],
        layout_skips=json.loads((root / 'ocr-layout-skips.json').read_text(encoding='utf-8')),
        unresolved_conflicts=review['conflicts'], unparsed_cells_or_tables=review['rejected'],
        review=approval, missing_policy='No interpolation, forward/backward fill, zero fill or profit inversion',
        methodology='Brazil freight column only; overlapping source tables must agree; unresolved conflicts excluded')
    (folder / 'audit.json').write_text(packed(audit), encoding='utf-8')
    print(packed(manifest))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate-root', type=Path, required=True)
    parser.add_argument('--stage', choices=('fetch', 'prepare-ocr', 'parse-ocr', 'review-candidates', 'review-sheets', 'seal'), required=True)
    args = parser.parse_args()
    root = args.candidate_root.resolve()
    repo = Path(__file__).resolve().parents[2]
    if not root.is_relative_to(repo / '01_data/candidates/sugar_import_profit'):
        raise ValueError('Collection writes only to the isolated sugar candidate root')
    if args.stage == 'fetch': collect(root)
    elif args.stage == 'prepare-ocr': prepare_ocr(root)
    elif args.stage == 'parse-ocr': parse_ocr(root)
    elif args.stage == 'review-candidates': review_candidates(root)
    elif args.stage == 'review-sheets': review_sheets(root)
    else: seal(root)


if __name__ == '__main__': main()
