"""Read printed CONAB chart labels; reject unfamiliar glyphs or chart layouts.

Values come from the printed decimal percentages, never from slice geometry.
An absent label remains absent. Templates retain their official source hashes.
"""
from __future__ import annotations

from collections import Counter
from io import BytesIO
import json
from pathlib import Path
import re

from PIL import Image

PALETTE = {(248, 203, 173): 'EMERGENCE', (0, 128, 0): 'VEGETATIVE',
           (175, 171, 171): 'FLOWERING', (255, 204, 0): 'GRAIN_FILLING',
           (222, 134, 4): 'MATURING', (91, 155, 213): 'STAGE_HARVEST'}
TEMPLATES = Path(__file__).resolve().parents[3] / '02_configs/conab_growth_glyphs.json'


def runs(values):
    groups = []
    for value in sorted(values):
        if not groups or value > groups[-1][-1] + 1:
            groups.append([])
        groups[-1].append(value)
    return groups


def signature(image, size=(20, 32)):
    rgb = image.convert('RGB')
    mask = Image.new('1', rgb.size)
    mask.putdata([max(pixel) < 65 for pixel in rgb.get_flattened_data()])
    box = mask.getbbox()
    if box is None:
        raise ValueError('empty CONAB label glyph')
    return ''.join('1' if pixel else '0' for pixel in mask.crop(box).resize(size, Image.Resampling.NEAREST).get_flattened_data())


def labels(image):
    image = image.convert('RGB')
    width, height = image.size
    if not (900 <= width <= 1200 and 950 <= height <= 1250):
        raise ValueError('CONAB chart dimensions changed')
    ink = {(x, y) for y in range(20, int(height * .84)) for x in range(20, width - 20)
           if max(image.getpixel((x, y))) < 65}
    result = []
    for band in runs({y for _, y in ink}):
        if len(band) < 12:
            continue
        columns = runs({x for x, y in ink if band[0] <= y <= band[-1]})
        words = []
        for column in columns:
            if not words or column[0] - words[-1][-1][-1] > 20:
                words.append([])
            words[-1].append(column)
        for word in words:
            boxes = [(c[0], band[0], c[-1] + 1, band[-1] + 1) for c in word]
            left, right = boxes[0][0], boxes[-1][2]
            pixels = Counter(image.crop((left - 4, band[0] - 4, right + 4, band[-1] + 5)).get_flattened_data())
            colors = [(n, color) for color, n in pixels.items() if color in PALETTE]
            if not colors or max(colors)[0] < 100:
                raise ValueError('CONAB printed label has unrecognized slice color')
            color = max(colors)[1]
            result.append((PALETTE[color], [image.crop(box) for box in boxes]))
    if not result or len(result) > 6:
        raise ValueError('CONAB percentage label count changed')
    return result


def read_growth_chart(raw: bytes, *, templates: Path = TEMPLATES):
    with Image.open(BytesIO(raw)) as opened:
        image = opened.convert('RGB')
    proof = json.loads(templates.read_text(encoding='utf-8'))
    if proof.get('schema_version') != 'conab-printed-glyphs/1':
        raise ValueError('CONAB glyph provenance unavailable')
    # Legend text/order is part of the accepted image format, independent of
    # the changing slices and values. Refuse an unfamiliar source legend.
    legend = image.crop((0, int(image.height * .875), image.width, image.height))
    legend_mask = signature(legend, (160, 48))
    difference = lambda a, b: sum(x != y for x, y in zip(a, b)) / len(a)
    if min(difference(legend_mask, x) for x in proof['legends']) > .06:
        raise ValueError('CONAB growth legend changed; review required')
    output = {}
    for stage, glyphs in labels(image):
        word = ''
        for glyph in glyphs:
            key = signature(glyph)
            candidates = sorted((min(difference(key, item) for item in variants), char)
                                for char, variants in proof['glyphs'].items())
            score, char = candidates[0]
            if score > .12 or candidates[1][0] - score < .025:
                raise ValueError('unrecognized or ambiguous CONAB printed digit')
            word += char
        if not re.fullmatch(r'(?:\d{1,2}|100),\d%', word):
            raise ValueError('CONAB printed percentage format changed')
        value = float(word[:-1].replace(',', '.'))
        if stage in output or not 0 <= value <= 100:
            raise ValueError('duplicate or invalid CONAB stage')
        output[stage] = value
    if not 99.5 <= sum(output.values()) <= 100.5:
        raise ValueError('CONAB printed labels incomplete; review required')
    return output
