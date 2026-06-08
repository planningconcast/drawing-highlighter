import os
import re
import fitz
import math
import base64
import tempfile
import zipfile
import traceback
from collections import defaultdict, Counter
from flask import Flask, request, jsonify, render_template
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024

# ---------------------------------------------------------------------------
APP_VERSION = '1.7.3'
APP_BUILD   = '2025-06-08'
APP_NOTES   = (
    'Multi-priority PDF annotation & audit | '
    'Universal view-type detection (font-size titles + CAD viewport frames) | '
    'Project-type-agnostic quota rules: plan quota / elev quota / detail=outline | '
    'Auto column-sort for side-by-side elevation views | '
    'Stitch-line detection: quota-exempt boundary marks on both drawings'
)
# ---------------------------------------------------------------------------

# ===========================================================================
# ELEVATION MARKER EXTRACTION
# ===========================================================================
ELEV_MARKER_RE = re.compile(
    r'(?:EL\.?\s*)?([+\-])\s*(\d[\d\s]*(?:[.,]\d+)?)',
    re.IGNORECASE
)

def parse_elev_value(sign, digits):
    cleaned = digits.replace(' ', '').replace(',', '.')
    try:
        val = float(cleaned)
        if val < 500 and '.' in cleaned:
            val *= 1000
        return val if sign == '+' else -val
    except ValueError:
        return None

def extract_elevations(page):
    elevations = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for m in ELEV_MARKER_RE.finditer(span["text"]):
                    val = parse_elev_value(m.group(1), m.group(2))
                    if val is not None:
                        bbox = span["bbox"]
                        elevations.append((val, (bbox[1] + bbox[3]) / 2))
    elevations.sort(key=lambda e: e[0])
    return elevations

def elevation_for_rect(rect, elevations):
    if not elevations:
        return 0.0
    ry = (rect.y0 + rect.y1) / 2
    return min(elevations, key=lambda e: abs(e[1] - ry))[0]

# ===========================================================================
# FLOOR LEVEL EXTRACTION FROM FILENAME
# Used to prioritise lower floors first in plan drawings.
# ===========================================================================
FLOOR_KEYWORDS = [
    ('GROUND', 0), ('BASEMENT', -1), ('LOWER GROUND', -1),
    ('FIRST', 1), ('SECOND', 2), ('THIRD', 3), ('FOURTH', 4),
    ('FIFTH', 5), ('SIXTH', 6), ('SEVENTH', 7), ('EIGHTH', 8),
    ('NINTH', 9), ('TENTH', 10), ('ROOF', 99),
]

def extract_floor_level(filename):
    """Return numeric floor level. Lower number = built first."""
    name = re.sub(r'[-_.]', ' ', filename.upper())
    # L00, L01, L02 ... pattern (most common in your files)
    m = re.search(r'\bL(\d{2})\b', name)
    if m:
        return int(m.group(1))
    # Level/Floor number: LEVEL 1, FLOOR 2 etc.
    m = re.search(r'\b(?:LEVEL|FLOOR)\s*(\d+)\b', name)
    if m:
        return int(m.group(1))
    # Word keywords
    for keyword, level in FLOOR_KEYWORDS:
        if keyword in name:
            return level
    return 50  # unknown — goes after known floors

# ===========================================================================
# DRAWING TYPE DETECTION — coordinates-based title block
# ===========================================================================
PLAN_RE = re.compile(r'\b(PLAN|PLN)\b',         re.IGNORECASE)
ELEV_RE = re.compile(r'\b(ELEVATION|ELEV)\b',    re.IGNORECASE)
SECT_RE = re.compile(r'\b(SECTION|SECT|SEC)\b',  re.IGNORECASE)

def extract_title_block_text(page):
    """Extract text from bottom-right 40%×25% of page — the title block zone."""
    pw, ph = page.rect.width, page.rect.height
    zone = fitz.Rect(pw * 0.60, ph * 0.75, pw, ph)
    parts = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        br = fitz.Rect(block["bbox"])
        if not zone.intersects(br):
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                parts.append(span["text"])
    return " ".join(parts)

# ---------------------------------------------------------------------------
# FONT-SIZE-RANKED TITLE DETECTION & VIEWPORT FRAME HARVESTING
# ---------------------------------------------------------------------------

def get_large_text_spans(page):
    """
    Return all text spans sorted by font size descending, filtered to those
    with meaningful alphabetic content (excludes pure dimension numbers).
    View titles are almost always the largest text on the page.
    """
    spans = []
    for block in page.get_text('dict')['blocks']:
        if block.get('type') != 0:
            continue
        for line in block.get('lines', []):
            for span in line.get('spans', []):
                text = span['text'].strip()
                # Must contain letters and be at least 4 chars
                if text and len(text) >= 4 and any(c.isalpha() for c in text):
                    spans.append({
                        'text': text,
                        'size': span['size'],
                        'bbox': span['bbox'],
                        'cx':   (span['bbox'][0] + span['bbox'][2]) / 2,
                        'cy':   (span['bbox'][1] + span['bbox'][3]) / 2,
                    })
    spans.sort(key=lambda s: -s['size'])
    return spans


def detect_viewport_frames(page, min_frac=0.03, max_frac=0.92):
    """
    Harvest large rectangular closed paths from CAD vector content.
    AutoCAD and Tekla output viewport bounding boxes as distinct rectangles.
    Returns list of fitz.Rect sorted by area descending.
    """
    pw, ph    = page.rect.width, page.rect.height
    page_area = pw * ph
    frames    = []
    try:
        for path in page.get_drawings():
            r = path.get('rect')
            if r is None:
                continue
            frac = (r.width * r.height) / page_area
            if (min_frac <= frac <= max_frac
                    and r.width  > pw * 0.08
                    and r.height > ph * 0.08):
                frames.append(r)
    except Exception:
        pass
    frames.sort(key=lambda r: -(r.width * r.height))
    return frames


def find_view_titles(page):
    """
    Identify view title spans by combining font-size ranking with keyword matching.

    Strategy:
      1. Collect all meaningful alphabetic spans ranked by font size.
      2. Take spans in the top 25% by size — these are headings/titles.
      3. Among those, keep only spans matching view-type keywords.
      4. Deduplicate nearby titles (CAD sometimes repeats a title string).

    Returns list of {'text', 'size', 'cx', 'cy', 'type', 'bbox'}
    sorted top-to-bottom (ascending cy).
    """
    all_spans = get_large_text_spans(page)
    if not all_spans:
        return []

    sizes    = sorted({s['size'] for s in all_spans}, reverse=True)
    cutoff   = sizes[max(0, int(len(sizes) * 0.25) - 1)]
    cands    = [s for s in all_spans if s['size'] >= cutoff]

    # Priority-ordered keyword table (first match wins)
    VIEW_KWS = [
        ('DETAIL',               'DETAIL'),
        ('DOCK FOUNDATION PLAN', 'PLAN'),
        ('DOCK SLAB PLAN',       'PLAN'),
        ('YARD WALL PLAN',       'PLAN'),
        ('WING WALL',            'PLAN'),
        ('HUB PLAN',             'PLAN'),
        ('FLOOR PLAN',           'PLAN'),
        ('PLAN',                 'PLAN'),
        ('DOCK ELEVATION',       'ELEVATION'),
        ('WING WALLS ELEVATION', 'ELEVATION'),
        ('YARD WALL ELEVATION',  'ELEVATION'),
        ('HUB ELEVATION',        'ELEVATION'),
        ('ELEVATION',            'ELEVATION'),
        ('SECTION',              'SECTION'),
    ]

    titles = []
    for span in cands:
        tu = span['text'].upper()
        matched = None
        for kw, vtype in VIEW_KWS:
            if kw in tu:
                matched = vtype
                break
        if matched is None:
            continue
        titles.append({**span, 'type': matched})

    # Deduplicate: merge spans within 60px vertically / 250px horizontally
    merged = []
    for t in sorted(titles, key=lambda x: x['cy']):
        close = [m for m in merged
                 if abs(m['cy'] - t['cy']) < 60
                 and abs(m['cx'] - t['cx']) < 250]
        if close:
            if t['size'] > close[0]['size']:
                merged.remove(close[0])
                merged.append(t)
        else:
            merged.append(t)

    return sorted(merged, key=lambda t: t['cy'])


def detect_drawing_type(filename, pages):
    """
    Detect drawing type in priority order:
      1. Filename keywords (fastest, most reliable for well-named files)
      2. Font-size-ranked title spans (largest text = view titles)
      3. Title block zone (bottom-right 40x25% coordinate region)
      4. Full page text fallback
    """
    name = re.sub(r'[-_.()[\]]', ' ',
                  os.path.splitext(os.path.basename(filename))[0]).upper()
    if SECT_RE.search(name): return 'SECTION'
    if ELEV_RE.search(name): return 'ELEVATION'
    if PLAN_RE.search(name): return 'PLAN'

    # Font-size-ranked: view titles are the largest alphabetic text on the page
    for page in pages:
        for title in find_view_titles(page)[:6]:
            tu = title['text'].upper()
            if SECT_RE.search(tu): return 'SECTION'
            if ELEV_RE.search(tu): return 'ELEVATION'
            if PLAN_RE.search(tu): return 'PLAN'

    # Title block zone (coordinate-based)
    for page in pages:
        tb = extract_title_block_text(page).upper()
        if SECT_RE.search(tb): return 'SECTION'
        if ELEV_RE.search(tb): return 'ELEVATION'
        if PLAN_RE.search(tb): return 'PLAN'

    # Full page text fallback
    for page in pages:
        full = page.get_text('text').upper()
        if SECT_RE.search(full): return 'SECTION'
        if ELEV_RE.search(full): return 'ELEVATION'
        if PLAN_RE.search(full): return 'PLAN'

    return 'UNKNOWN' 


# ===========================================================================
# DOCK PROJECT DETECTION & MULTI-VIEW SECTION PARSING
# ===========================================================================
DOCK_RE       = re.compile(r'\bDOCK\b', re.IGNORECASE)
PLAN_TITLE_RE = re.compile(
    r'(dock\s+(?:foundation\s+)?(?:slab\s+)?plan'
    r'|yard\s+wall\s+plan'
    r'|wing\s+walls?\s+plan'
    r'|hub\s+plan)',
    re.IGNORECASE
)
ELEV_TITLE_RE = re.compile(
    r'(dock\s+elevation'
    r'|wing\s+walls?\s+elevation'
    r'|hub\s+elevation'
    r'|yard\s+walls?\s+elevation)',
    re.IGNORECASE
)
# Bay range patterns — handles various title formats:
# 'GL C/4-8', 'GL C/4-8 to C/8-11'
# 'Grid W 8-14', 'Grid W/8-14'
# 'Dock Elevation Grid W 8-14'
DOCK_BAY_RE = re.compile(
    r'(?:GL|GRID)\s+([A-Z]+)[/\s](\d+)[-\s]+(\d+)',
    re.IGNORECASE
)


def get_dock_view_sections(page):
    """
    Detect view boundaries on dock/yard-wall drawings.
    Uses font-size-ranked title detection first (more robust than keyword-only).
    Falls back to PLAN_TITLE_RE / ELEV_TITLE_RE keyword matching if font-size
    approach finds no titles (e.g. very uniform font-size drawings).
    """
    # --- Primary: font-size-ranked titles ---
    view_titles = find_view_titles(page)
    plan_elev   = [t for t in view_titles if t['type'] in ('PLAN', 'ELEVATION')]

    if plan_elev:
        sections = []
        y_start  = 0.0
        for t in plan_elev:   # already sorted top-to-bottom by cy
            sections.append({'type': t['type'], 'y_min': y_start, 'y_max': t['cy']})
            y_start = t['cy']
        return sections

    # --- Fallback: keyword block matching ---
    titles = []
    for block in page.get_text('dict')['blocks']:
        if block.get('type') != 0:
            continue
        text  = ' '.join(
            span['text']
            for line in block.get('lines', [])
            for span in line.get('spans', [])
        )
        y_bot = block['bbox'][3]
        if PLAN_TITLE_RE.search(text):
            titles.append(('PLAN', y_bot))
        elif ELEV_TITLE_RE.search(text):
            titles.append(('ELEVATION', y_bot))
    if not titles:
        return None
    titles.sort(key=lambda t: t[1])
    sections = []
    y_start  = 0.0
    for kind, y_bot in titles:
        sections.append({'type': kind, 'y_min': y_start, 'y_max': y_bot})
        y_start = y_bot
    return sections


def section_type_at(cy, sections):
    if not sections:
        return 'PLAN'
    for sec in sections:
        if sec['y_min'] <= cy <= sec['y_max']:
            return sec['type']
    return 'PLAN'


def get_dock_bay_range(filename, page_text):
    combined = filename + ' ' + page_text[:1000]
    m = DOCK_BAY_RE.search(combined)
    if m:
        return m.group(1).upper(), int(m.group(2)), int(m.group(3))
    return None


def build_dock_sheet_pairs(saved_paths, file_draw_types, file_docs):
    STRIP_WIDTH   = 150
    FALLBACK_FRAC = 0.08
    by_gridline = defaultdict(list)
    for fn, ip in saved_paths:
        doc = file_docs.get(fn)
        if not doc:
            continue
        page_text = get_drawing_page(doc).get_text('text')
        bay_range = get_dock_bay_range(fn, page_text)
        if bay_range:
            gl, bstart, bend = bay_range
            by_gridline[gl].append((fn, bstart, bend))
    pairs = []
    for gl, drawings in by_gridline.items():
        drawings.sort(key=lambda x: x[1])
        for k in range(len(drawings) - 1):
            fn_a, sa, ea = drawings[k]
            fn_b, sb, eb = drawings[k + 1]
            if ea != sb:
                continue
            doc_a = file_docs.get(fn_a)
            doc_b = file_docs.get(fn_b)
            if not doc_a or not doc_b:
                continue
            pa   = get_drawing_page(doc_a)
            pb   = get_drawing_page(doc_b)
            pw_a = pa.rect.width
            pw_b = pb.rect.width
            approx_a = find_continuation_x(pa, 'right') or pw_a * (1 - FALLBACK_FRAC)
            approx_b = find_continuation_x(pb, 'left')  or pw_b * FALLBACK_FRAC
            gx_a = refine_gridline_x(pa, approx_a)
            gx_b = refine_gridline_x(pb, approx_b)
            pairs.append({
                'fn_a':         fn_a,
                'fn_b':         fn_b,
                'gridline_x_a': gx_a,
                'gridline_x_b': gx_b,
                'strip_width':  STRIP_WIDTH,
                'method':       'dock_gl_{}_bay_{}'.format(gl, ea),
                'dock_mode':    True,  # dock split: both drawings highlight, quota-exempt
            })
    return pairs


# ===========================================================================
# HEAT AREA DETECTION (plan drawings)
# ===========================================================================
def compute_heat_centroid(positions, page_w, page_h):
    if not positions:
        return None
    GRID = 8
    cw, ch = page_w / GRID, page_h / GRID
    grid = Counter()
    for x, y in positions:
        grid[(min(int(x / cw), GRID-1), min(int(y / ch), GRID-1))] += 1
    max_cnt = max(grid.values())
    avg_cnt = len(positions) / (GRID * GRID)
    if max_cnt < avg_cnt * 2.0 or max_cnt < 2:
        return None
    threshold = max_cnt * 0.75
    hot = [(c, r) for (c, r), cnt in grid.items() if cnt >= threshold]
    cx = sum((c + 0.5) * cw for c, r in hot) / len(hot)
    cy = sum((r + 0.5) * ch for c, r in hot) / len(hot)
    return (cx, cy)

def pdist(p1, p2):
    return math.sqrt((p1[0]-p2[0])**2 + (p1[1]-p2[1])**2)

def sort_by_proximity(instances):
    if len(instances) <= 1:
        return instances[:]
    remaining = instances[:]
    cx = sum(i['cx'] for i in remaining) / len(remaining)
    cy = sum(i['cy'] for i in remaining) / len(remaining)
    start = min(remaining, key=lambda i: pdist((i['cx'], i['cy']), (cx, cy)))
    result = [start]
    remaining.remove(start)
    while remaining:
        last = result[-1]
        nearest = min(remaining,
                      key=lambda i: pdist((i['cx'], i['cy']), (last['cx'], last['cy'])))
        result.append(nearest)
        remaining.remove(nearest)
    return result

# ===========================================================================
# SPLIT-SHEET DETECTION & BOUNDARY STRIP APPROACH
#
# When one floor plan is split across 2+ sheets (e.g. gridlines 1-8 and 8-14),
# the shared gridline appears on both sheets. Any unit whose callout label sits
# within a narrow strip around that gridline on sheet 2 is a boundary duplicate
# — sheet 1 owns the boundary, sheet 2 gets outline-only for those units.
#
# Detection pipeline:
#   1. Filename "SHEET_1_OF_2" / "SHEET_2_OF_2" → confirmed pair
#   2. "FOR CONTINUATION SEE DRAWING" text → approximate boundary x
#   3. Vector drawing scan → long vertical line near approx_x → exact gridline x
#   4. Strip on sheet 2 centred on that x → refs inside get outline-only
# ===========================================================================

SHEET_OF_RE = re.compile(r'(\d+)\s*(?:OF|_OF_)\s*(\d+)', re.IGNORECASE)

def get_sheet_number(filename):
    """Return (sheet_num, total_sheets) from filename, or None."""
    name = re.sub(r'[-.]', '_', os.path.basename(filename).upper())
    m = SHEET_OF_RE.search(name)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def find_continuation_x(page, side):
    """
    Find approximate x of the shared edge from 'FOR CONTINUATION SEE DRAWING' text.
    side: 'right' (sheet A) or 'left' (sheet B).
    Returns x float or None.
    """
    pw, ph = page.rect.width, page.rect.height
    search_zone = fitz.Rect(pw * 0.5, 0, pw, ph) if side == 'right'              else fitz.Rect(0, 0, pw * 0.5, ph)

    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        br = fitz.Rect(block["bbox"])
        if not search_zone.intersects(br):
            continue
        text = " ".join(
            span["text"]
            for line in block.get("lines", [])
            for span in line.get("spans", [])
        ).upper()
        if "FOR CONTINUATION" in text or "SEE DRAWING" in text:
            return block["bbox"][0] if side == "right" else block["bbox"][2]
    return None


def refine_gridline_x(page, approx_x, search_margin=400):
    """
    Find the exact x-position of the split gridline by scanning vector paths.
    Looks for a long near-vertical line segment close to approx_x.
    Returns refined x or approx_x if nothing found.
    """
    ph = page.rect.height
    min_span = ph * 0.45   # must span at least 45% of page height

    best_x      = None
    best_length = 0

    try:
        for path in page.get_drawings():
            items = path.get("items", [])
            pts   = []
            for item in items:
                if item[0] in ("m", "l"):
                    pts.append(item[1])
                elif item[0] == "c":
                    pts.extend([item[1], item[3]])   # control + end

            if len(pts) < 2:
                continue

            xs = [p.x for p in pts]
            ys = [p.y for p in pts]
            x_range = max(xs) - min(xs)
            y_range = max(ys) - min(ys)
            avg_x   = (max(xs) + min(xs)) / 2

            # Long near-vertical line close to approx_x
            if (x_range < 20 and y_range > min_span
                    and abs(avg_x - approx_x) < search_margin
                    and y_range > best_length):
                best_length = y_range
                best_x      = avg_x
    except Exception:
        pass   # get_drawings can fail on some PDFs

    return best_x if best_x is not None else approx_x


def has_continuation_note(page):
    """True if page contains a FOR CONTINUATION note."""
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        text = " ".join(
            span["text"]
            for line in block.get("lines", [])
            for span in line.get("spans", [])
        ).upper()
        if "FOR CONTINUATION" in text:
            return True
    return False


def get_drawing_page(doc):
    """
    Return the main drawing page from a multi-page PDF.
    Skips cover/title pages by finding the page with the most text blocks,
    which correlates with dense CAD drawing content.
    Falls back to page 0 if only one page or all equal.
    """
    if len(doc) == 1:
        return doc[0]
    best_page  = doc[0]
    best_count = 0
    for i in range(len(doc)):
        count = len(doc[i].get_text("dict").get("blocks", []))
        if count > best_count:
            best_count = count
            best_page  = doc[i]
    return best_page


def build_sheet_pairs(saved_paths, file_draw_types, file_docs):
    """
    Identify pairs of split-sheet plan drawings and determine the exact
    x-position of the shared gridline on each sheet.

    Returns list of dicts:
      { 'fn_a': sheet1_filename,  'fn_b': sheet2_filename,
        'gridline_x_a': float,    'gridline_x_b': float,
        'strip_width': float,     'method': str }

    gridline_x_b is the x on sheet B where the strip centred around
    it defines the no-markup zone.
    """
    STRIP_WIDTH   = 150   # half-width of no-markup strip in PDF units (~15mm at 1:100)
    FALLBACK_FRAC = 0.08  # 8% from edge if all detection fails

    pairs = []
    plan_files = [(fn, ip) for fn, ip in saved_paths
                  if file_draw_types.get(fn) in ("PLAN", "UNKNOWN")]

    by_floor = defaultdict(list)
    for fn, ip in plan_files:
        by_floor[extract_floor_level(fn)].append((fn, ip))

    for lvl, floor_files in by_floor.items():
        if len(floor_files) < 2:
            continue

        sheet_numbered = []
        for fn, ip in floor_files:
            sn = get_sheet_number(fn)
            if sn:
                sheet_numbered.append((fn, ip, sn[0], sn[1]))

        if len(sheet_numbered) >= 2:
            sheet_numbered.sort(key=lambda x: x[2])
            for k in range(len(sheet_numbered) - 1):
                fn_a, _,  sn_a, _ = sheet_numbered[k]
                fn_b, _,  sn_b, _ = sheet_numbered[k + 1]
                doc_a = file_docs.get(fn_a)
                doc_b = file_docs.get(fn_b)
                if not doc_a or not doc_b:
                    continue

                page_a = get_drawing_page(doc_a)
                page_b = get_drawing_page(doc_b)
                pw_a   = page_a.rect.width
                pw_b   = page_b.rect.width

                # Step 1: approximate boundary from continuation note
                approx_a = find_continuation_x(page_a, "right") or pw_a * (1 - FALLBACK_FRAC)
                approx_b = find_continuation_x(page_b, "left")  or pw_b * FALLBACK_FRAC

                # Step 2: refine using actual vector gridline
                gx_a = refine_gridline_x(page_a, approx_a)
                gx_b = refine_gridline_x(page_b, approx_b)

                method = "sheet_number"
                if has_continuation_note(page_a) or has_continuation_note(page_b):
                    method = "sheet_number+continuation+vector"

                pairs.append({
                    "fn_a":        fn_a,
                    "fn_b":        fn_b,
                    "gridline_x_a": gx_a,
                    "gridline_x_b": gx_b,
                    "strip_width": STRIP_WIDTH,
                    "method":      method,
                    "dock_mode":   False,  # building split: sheet B outlines
                })

        else:
            # No sheet numbers — use continuation notes to identify pairs
            for i in range(len(floor_files)):
                for j in range(i + 1, len(floor_files)):
                    fn_a, _ = floor_files[i]
                    fn_b, _ = floor_files[j]
                    doc_a   = file_docs.get(fn_a)
                    doc_b   = file_docs.get(fn_b)
                    if not doc_a or not doc_b:
                        continue
                    pa_main = get_drawing_page(doc_a)
                    pb_main = get_drawing_page(doc_b)
                    if not (has_continuation_note(pa_main) or
                            has_continuation_note(pb_main)):
                        continue
                    pw_a  = pa_main.rect.width
                    pw_b  = pb_main.rect.width
                    approx_a = find_continuation_x(pa_main, "right") or pw_a * (1 - FALLBACK_FRAC)
                    approx_b = find_continuation_x(pb_main, "left")  or pw_b * FALLBACK_FRAC
                    gx_a  = refine_gridline_x(pa_main, approx_a)
                    gx_b  = refine_gridline_x(pb_main, approx_b)

                    pairs.append({
                        "fn_a":        fn_a,
                        "fn_b":        fn_b,
                        "gridline_x_a": gx_a,
                        "gridline_x_b": gx_b,
                        "strip_width": STRIP_WIDTH,
                        "method":      "continuation+vector",
                        "dock_mode":   False,
                    })
    return pairs


def apply_boundary_strip(plan_insts, sheet_pairs, logs, dock_mode=False):
    """
    Handle instances near the shared gridline between paired drawings.

    BUILDING mode (dock_mode=False):
      Sheet A owns the boundary. Sheet B instances in the strip -> outline.

    DOCK mode (dock_mode=True):
      Both sheets show the boundary unit highlighted — erectors need to see
      the unit in context on both adjacent drawings.
      Boundary instances are tagged 'boundary' so the quota loop skips them
      (they never consume from the quota pool).
    """
    if not sheet_pairs:
        return plan_insts

    # Build lookup: filename -> list of (gridline_x, strip_width, is_a_side, pair_dock_mode)
    # Per-pair dock_mode overrides the function-level dock_mode argument.
    strips = defaultdict(list)
    for pair in sheet_pairs:
        pair_dm = pair.get('dock_mode', dock_mode)
        strips[pair['fn_a']].append((pair['gridline_x_a'], pair['strip_width'], True,  pair_dm))
        strips[pair['fn_b']].append((pair['gridline_x_b'], pair['strip_width'], False, pair_dm))

    marked_outline = 0
    marked_boundary = 0

    for inst in plan_insts:
        fn = inst['filename']
        if fn not in strips:
            continue
        for gx, sw, is_a, pair_dm in strips[fn]:
            if abs(inst['cx'] - gx) > sw:
                continue
            if pair_dm:
                # Dock mode: both sheets highlight, boundary is quota-exempt
                inst['is_boundary'] = True
                inst['ann_type']    = 'highlight'
                marked_boundary += 1
            else:
                # Building mode: sheet A highlights normally, sheet B outlines
                if not is_a and inst['ann_type'] != 'outline':
                    inst['ann_type'] = 'outline'
                    marked_outline += 1
            break

    if marked_outline > 0:
        logs.append(
            f"  Boundary strip: {marked_outline} sheet-B instance(s) -> outline"
        )
    if marked_boundary > 0:
        logs.append(
            f"  Dock boundary: {marked_boundary} instance(s) at shared gridline "
            f"-> highlighted on both drawings (quota-exempt)"
        )
    return plan_insts


def deduplicate_sheet_overlaps(plan_insts, sheet_pairs, logs):
    """Legacy stub — boundary strip logic replaced this function."""
    return plan_insts
# ===========================================================================
# LIFT / STAIR / SHAFT DRAWINGS
#
# These drawings show multiple elevation views SIDE BY SIDE horizontally
# (e.g. Elevation 01, 02, 03 on one sheet).  Units repeat across faces.
# Strategy: identify vertical columns by x-gaps, fill right column first
# (bottom-to-top within each column), then move left.
# ===========================================================================
LIFT_RE = re.compile(
    r'\b(LIFT|SHAFT|LIFT[_\s]CORE|LIFT[_\s]SHAFT)\b',
    re.IGNORECASE
)


STAIRCORE_RE = re.compile(
    r'\b(STAIRCORE|STAIR[\s_-]+CORE|STAIR[\s_-]+SHAFT|STAIR[\s_-]+FLIGHT|STAIRCASE)\b',
    re.IGNORECASE
)
STAIR_DETAIL_RE = re.compile(r'\bDETAIL\b', re.IGNORECASE)


def sort_by_columns_right_to_left(instances, gap_fraction=0.04):
    """
    Group instances into vertical columns by detecting x-gaps in their
    cx distribution.  Returns instances sorted:
      1. Column order: rightmost column first
      2. Within each column: bottom-to-top (largest cy first, since
         y=0 is top of page in PDF space)

    gap_fraction: minimum gap between adjacent instance cx values
    (as a fraction of total x-range) to be considered a new column.
    """
    if len(instances) <= 1:
        return instances[:]

    sorted_x = sorted(instances, key=lambda i: i['cx'])
    xs       = [i['cx'] for i in sorted_x]
    x_range  = xs[-1] - xs[0]
    if x_range == 0:
        return sorted(instances, key=lambda i: -i['cy'])

    gap_min = x_range * gap_fraction

    # Build columns by splitting at gaps
    columns      = []
    current_col  = [sorted_x[0]]
    for k in range(1, len(sorted_x)):
        if xs[k] - xs[k-1] > gap_min:
            columns.append(current_col)
            current_col = []
        current_col.append(sorted_x[k])
    columns.append(current_col)

    # Right column first; within each column sort bottom-to-top
    result = []
    for col in reversed(columns):        # reversed = rightmost first
        col_sorted = sorted(col, key=lambda i: -i['cy'])  # largest cy = bottom
        result.extend(col_sorted)
    return result


# ===========================================================================
# INPUT PARSERS
# ===========================================================================
def parse_count_list(raw):
    counts = Counter()
    for line in raw.splitlines():
        ref = line.strip()
        if ref:
            counts[ref] += 1
    return counts

def parse_delivered(raw):
    result = defaultdict(list)
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in re.split(r'\t+|\s{2,}', line) if p.strip()]
        if not parts:
            continue
        ref  = parts[0]
        load = parts[1] if len(parts) >= 2 else ''
        result[ref].append(load)
    return dict(result)

# ===========================================================================
# ANNOTATIONS
# ===========================================================================
def insert_load_label(page, rect, load_no):
    """
    Insert a load label to the right of the highlight.
    Prepends 'Load ' if the value is purely a number/code (e.g. 1, 10, 1A, 30b).
    Draws a blue-bordered rectangle behind the text for visibility.
    """
    # Prepend 'Load ' unless the user already included a word prefix
    # (e.g. 'LOAD-01' stays as-is; '1A' becomes 'Load 1A')
    label = load_no.strip()
    if re.match(r'^[0-9]', label):
        label = 'Load ' + label

    # Cap font size: use min dimension scaled down, then hard-cap at 10pt
    # Diagonal or large rects would otherwise produce oversized labels
    font_size = min(10, max(7, min(rect.width, rect.height) * 0.85))
    # Estimate text width: ~0.55 * font_size per character (Helvetica)
    text_w = len(label) * font_size * 0.55
    text_h = font_size * 1.2
    pad = 2

    x0 = rect.x1 + 3
    y0 = rect.y0 + (rect.height - text_h) / 2
    x1 = x0 + text_w + pad * 2
    y1 = y0 + text_h

    label_rect = fitz.Rect(x0, y0, x1, y1)

    # Draw rectangle directly into the page content stream so it sits
    # BEHIND the text (annotations always render on top of insert_text).
    page.draw_rect(
        label_rect,
        color=(0.1, 0.4, 0.85),   # border colour
        fill=(0.85, 0.93, 1.0),   # light blue fill
        width=1.0,
        overlay=True
    )

    # Text drawn after — sits on top of the rectangle
    pt = fitz.Point(x0 + pad, y1 - pad - 1)
    page.insert_text(pt, label, fontsize=font_size,
                     color=(0.0, 0.15, 0.55), overlay=True)

def add_highlight(page, rect, colour):
    annot = page.add_highlight_annot(rect)
    annot.set_colors(stroke=colour)
    annot.update()

def add_outline_rect(page, rect, colour):
    """Rectangle outline for out-of-quota spotted instances."""
    expanded = fitz.Rect(rect.x0 - 2, rect.y0 - 2, rect.x1 + 2, rect.y1 + 2)
    annot = page.add_rect_annot(expanded)
    annot.set_colors(stroke=colour, fill=None)
    annot.set_border(width=1.5)
    annot.update()

def overlaps_protected(inst, protected):
    area = abs(inst.width * inst.height)
    if area == 0:
        return False
    for p in protected:
        inter = inst & p
        if not inter.is_empty and abs(inter.width * inter.height) > area * 0.7:
            return True
    return False

def tier_colour(tier):
    if tier == 0: return (0.1, 0.6, 1.0)   # blue  — delivered
    if tier == 1: return (1.0, 0.647, 0.0)  # orange — produced
    return              (1.0, 1.0, 0.0)      # yellow — issued

# ===========================================================================
# FLASK ROUTES
# ===========================================================================
@app.route('/health')
def health():
    return f'ok | v{APP_VERSION} ({APP_BUILD})', 200


@app.route('/version')
def version():
    return {
        'version': APP_VERSION,
        'build':   APP_BUILD,
        'notes':   APP_NOTES,
    }

@app.route('/')
def index():
    return render_template('index.html',
                           app_version=APP_VERSION,
                           app_build=APP_BUILD)

@app.route('/process', methods=['POST'])
def process():
    issued_raw    = request.form.get('issued', '')
    produced_raw  = request.form.get('produced', '')
    delivered_raw = request.form.get('delivered', '')
    files         = request.files.getlist('pdfs')

    issued_counts    = parse_count_list(issued_raw)
    produced_counts  = parse_count_list(produced_raw)
    delivered_map    = parse_delivered(delivered_raw)
    delivered_counts = Counter({ref: len(loads) for ref, loads in delivered_map.items()})

    delivered_refs = set(delivered_map.keys())
    produced_refs  = set(produced_counts.keys()) - delivered_refs
    issued_refs    = set(issued_counts.keys()) - set(produced_counts.keys()) - delivered_refs
    all_searched   = delivered_refs | produced_refs | issued_refs

    # Tier consistency checks (Delivered ⊆ Produced ⊆ Issued)
    _tier_warns = []
    delivered_not_in_produced = set(delivered_map.keys()) - set(produced_counts.keys())
    if delivered_not_in_produced:
        _tier_warns.append(
            f"  ⚠ Tier inconsistency: {len(delivered_not_in_produced)} Delivered ref(s) "
            f"not in Produced list: "
            f"{', '.join(sorted(delivered_not_in_produced)[:5])}"
            + (f' (+{len(delivered_not_in_produced)-5} more)' if len(delivered_not_in_produced) > 5 else '')
        )
    produced_not_in_issued = set(produced_counts.keys()) - set(issued_counts.keys())
    if produced_not_in_issued:
        _tier_warns.append(
            f"  ⚠ Tier inconsistency: {len(produced_not_in_issued)} Produced ref(s) "
            f"not in Issued list: "
            f"{', '.join(sorted(produced_not_in_issued)[:5])}"
            + (f' (+{len(produced_not_in_issued)-5} more)' if len(produced_not_in_issued) > 5 else '')
        )

    def quota(ref):
        if ref in delivered_refs:  return delivered_counts[ref]
        if ref in produced_refs:   return produced_counts[ref]
        if ref in issued_refs:     return issued_counts[ref]
        return 0

    def tier_of(ref):
        if ref in delivered_refs: return 0
        if ref in produced_refs:  return 1
        return 2

    if not all_searched:
        return jsonify({'error': 'All lists are empty. Paste references first.'}), 400
    if not files or files[0].filename == '':
        return jsonify({'error': 'No PDF files selected.'}), 400

    # Audit regex
    detected_prefixes = set()
    for item in all_searched:
        m = re.match(r'^([A-Z]+)', item)
        if m:
            detected_prefixes.add(m.group(1))
    if detected_prefixes:
        sorted_pfx = sorted(detected_prefixes, key=len, reverse=True)
        pfx_str    = '|'.join(re.escape(p) for p in sorted_pfx)
        has_hyphen = any('-' in item for item in all_searched)
        if has_hyphen:
            unit_pattern = re.compile(rf'\b(?:{pfx_str})-\d+\b')
        else:
            unit_pattern = re.compile(rf'\b(?:{pfx_str})\d+\b')
    else:
        unit_pattern = re.compile(r'\b[A-Z]{2,4}-\d+\b')

    total_issued    = sum(1 for l in issued_raw.splitlines()    if l.strip())
    total_produced  = sum(1 for l in produced_raw.splitlines()  if l.strip())
    total_delivered = sum(len(v) for v in delivered_map.values())

    logs = [
        f"Tiers loaded — {len(delivered_refs)} delivered refs ({total_delivered} units), "
        f"{len(produced_refs)} produced-only refs, "
        f"{len(issued_refs)} issued-only refs — "
        f"{sum(quota(r) for r in all_searched)} total units to mark"
    ]
    logs.extend(_tier_warns)

    found_units:            set  = set()
    unsearched_units_found: set  = set()
    output_files:           list = []

    with tempfile.TemporaryDirectory() as tmpdir:

        # ===================================================================
        # SCAN PASS — collect all instances, page dims, draw types
        # ===================================================================
        all_candidates       = defaultdict(list)   # ref → [instance_dict]
        file_draw_types      = {}
        file_heat_centroids  = {}
        file_page_dims       = {}              # filename → (width, height)
        all_unit_pos_by_file = defaultdict(list)
        file_vp_frames       = defaultdict(list)  # filename → [fitz.Rect]

        saved_paths = []
        for upload in files:
            filename = secure_filename(upload.filename)
            if not filename.lower().endswith('.pdf'):
                logs.append(f"SKIP {filename} — not a PDF")
                continue
            in_path = os.path.join(tmpdir, filename)
            upload.save(in_path)
            saved_paths.append((filename, in_path))

        file_docs = {}   # keep open for stitch-line gridline detection

        for filename, in_path in saved_paths:
            try:
                doc        = fitz.open(in_path)
                pages      = [doc[i] for i in range(len(doc))]
                page_texts = [p.get_text('text') for p in pages]

                # File-level draw type is a fallback hint only.
                # Per-instance view type is derived from page layout below.
                file_draw_type = detect_drawing_type(filename, pages)
                file_draw_types[filename] = file_draw_type
                floor_lvl = extract_floor_level(filename)
                logs.append(f"Scanning: {filename}")

                for page_idx, page in enumerate(pages):
                    pw, ph = page.rect.width, page.rect.height
                    file_page_dims[filename] = (pw, ph)
                    elevations  = extract_elevations(page)

                    # ── VIEW LAYOUT ANALYSIS ─────────────────────────────
                    # Run on every drawing regardless of project type.
                    # Font-size-ranked titles + CAD viewport frames give us
                    # the view-type of each area of the page.
                    # Detect view layout for this specific page
                    _titles   = find_view_titles(page)
                    _frames   = detect_viewport_frames(page)
                    # Accumulate viewport frames for elevation grouping
                    file_vp_frames[filename].extend(
                        f for f in _frames if f not in file_vp_frames[filename]
                    )

                    # Build section bands: each title sits at bottom of its view
                    _sections = []
                    if _titles:
                        y0 = 0.0
                        for t in _titles:
                            _sections.append({'type': t['type'],
                                              'y_min': y0, 'y_max': t['cy']})
                            y0 = t['cy']

                    def _view_type(cx, cy,
                                   _s=_sections, _f=_frames, _t=_titles,
                                   _dt=file_draw_type):
                        """
                        Resolve view type for an instance.
                        Explicit default args bind current values, avoiding
                        late-binding closure issues with loop variables.
                        """
                        # 1. Section bands (vertically stacked views)
                        if _s:
                            vt = section_type_at(cy, _s)
                            if vt in ('PLAN', 'ELEVATION', 'SECTION', 'DETAIL'):
                                return vt

                        # 2. Viewport frame containment (side-by-side views)
                        pt = fitz.Point(cx, cy)
                        for frame in _f:
                            if frame.contains(pt) and _t:
                                nearest = min(_t, key=lambda t: abs(t['cy'] - frame.y1))
                                return nearest['type']

                        # 3. File-level hint as final fallback
                        if _dt in ('ELEVATION', 'SECTION'):
                            return 'ELEVATION'
                        return 'PLAN'

                    # Build search variants to handle common PDF hyphen
                    # formatting differences.
                    def _variants(r):
                        v = {r}
                        # Space after hyphen: 'NLB- 2106'
                        v.add(re.sub(r'-(\d)', r'- \1', r))
                        # En-dash: 'NLB–2106' and 'NLB– 2106'
                        ed = r.replace('-', '–')
                        v.add(ed)
                        v.add(re.sub(r'–(\d)', '– \\1', ed))
                        # Non-breaking hyphen: 'NLB‑2106'
                        v.add(r.replace('-', '‑'))
                        return list(v)

                    for ref in all_searched:
                        seen_rects = set()   # dedup if both formats on same page
                        for search_text in _variants(ref):
                            for inst in page.search_for(search_text):
                                key = (round(inst.x0), round(inst.y0))
                                if key in seen_rects:
                                    continue
                                seen_rects.add(key)
                                cx = (inst.x0 + inst.x1) / 2
                                cy = (inst.y0 + inst.y1) / 2
                                vtype = _view_type(cx, cy)
                                all_candidates[ref].append({
                                    'ref':       ref,
                                    'filename':  filename,
                                    'in_path':   in_path,
                                    'page_idx':  page_idx,
                                    'rect':      inst,
                                    'elevation': elevation_for_rect(inst, elevations),
                                    'cx': cx, 'cy': cy,
                                    'page_w': pw, 'page_h': ph,
                                    'draw_type': file_draw_type,
                                    'floor_lvl': floor_lvl,
                                    'sec_type':  vtype,
                                    'load_no':   None,
                                    'ann_type':  'highlight',
                                })
                                if vtype in ('PLAN', 'UNKNOWN'):
                                    all_unit_pos_by_file[filename].append((cx, cy))

                file_docs[filename] = doc
            except Exception as e:
                logs.append(f"ERROR scanning {filename}: {e}")

        # Scan summary — helps diagnose missing refs
        scan_total = sum(len(v) for v in all_candidates.values())
        scan_refs  = sum(1 for v in all_candidates.values() if v)
        logs.append(
            f"  Scan complete: {scan_total} instance(s) found "
            f"for {scan_refs}/{len(all_searched)} refs"
        )
        if scan_total == 0:
            logs.append(
                "  HINT: No text matches found. Check that reference codes "
                "in the input exactly match the text in the drawings "
                "(case-sensitive, no extra spaces)."
            )

        # Heat centroids — any file with plan-zone instances
        for filename, in_path in saved_paths:
            positions = all_unit_pos_by_file.get(filename, [])
            if not positions:
                continue
            try:
                pw, ph   = file_page_dims.get(filename, (1000, 1000))
                centroid = compute_heat_centroid(positions, pw, ph)
                file_heat_centroids[filename] = centroid
                if centroid:
                    logs.append(
                        f"  {filename}: heat area ({centroid[0]:.0f}, {centroid[1]:.0f})")
                else:
                    logs.append(f"  {filename}: no heat area — proximity grouping")
            except Exception:
                file_heat_centroids[filename] = None

        # Stitch-line detection (file continuity — not project-type specific)
        sheet_pairs = build_sheet_pairs(saved_paths, file_draw_types, file_docs)
        bay_pairs   = build_dock_sheet_pairs(saved_paths, file_draw_types, file_docs)
        sheet_pairs += bay_pairs
        for p in sheet_pairs:
            logs.append(
                f"  Stitch [{p['method']}]: "
                f"{os.path.basename(p['fn_a'])} <-> {os.path.basename(p['fn_b'])} "
                f"boundary x: A={p['gridline_x_a']:.0f} B={p['gridline_x_b']:.0f}"
            )

        for doc in file_docs.values():
            try: doc.close()
            except Exception: pass
        file_docs = {}

        # ===================================================================
        # PHASE 2 — UNIFIED QUOTA SELECTION
        #
        # sec_type (per-instance, from layout analysis) is the sole driver.
        # Project type is not consulted.
        #
        # DETAIL        → outline only, no quota consumed
        # PLAN          → quota A  (floor level asc, heat centroid dist)
        # ELEV / SECTION→ quota B  (bottom-up, or column-right-to-left when
        #                           views are horizontally arranged)
        #
        # If ref exists in BOTH plan and elev zones:
        #   plan highlighted (quota A), elev outlined
        # If ref exists ONLY in elev zones:
        #   elev highlighted (quota B)
        #
        # Stitch-line instances are quota-exempt on both drawings.
        # ===================================================================
        selected_instances = []

        def effective_type(inst):
            st = inst.get('sec_type')
            if st in ('PLAN', 'ELEVATION', 'SECTION', 'DETAIL'):
                return st
            dt = inst.get('draw_type', 'UNKNOWN')
            if dt in ('ELEVATION', 'SECTION'): return 'ELEVATION'
            if dt == 'PLAN':                   return 'PLAN'
            return 'PLAN'

        def views_are_horizontal(insts):
            """
            Detect side-by-side viewport layout (lift core / shaft style).
            True when x-spread is wide AND y-spread is narrow relative to
            page dimensions, with at least one detectable x-gap between columns.
            """
            if len(insts) < 4:
                return False
            xs  = [i['cx'] for i in insts]
            ys  = [i['cy'] for i in insts]
            pw  = sum(i.get('page_w', 1000) for i in insts) / len(insts)
            ph  = sum(i.get('page_h', 1000) for i in insts) / len(insts)
            xr  = (max(xs) - min(xs)) / pw
            yr  = (max(ys) - min(ys)) / ph
            sxs = sorted(xs)
            xrng = sxs[-1] - sxs[0]
            gaps = sum(1 for k in range(1, len(sxs))
                       if sxs[k] - sxs[k-1] > xrng * 0.04)
            return xr > 0.35 and yr < 0.25 and gaps >= 1

        def run_quota(insts, q, sort_key_fn):
            """Sort insts, apply quota, return highlight count."""
            insts.sort(key=sort_key_fn)
            hl = 0
            for inst in insts:
                if inst.get('is_boundary') or inst['ann_type'] == 'outline':
                    continue
                if hl < q:
                    inst['ann_type'] = 'highlight'
                    hl += 1
                else:
                    inst['ann_type'] = 'outline'
            return hl


            # Check if we actually have multiple floor levels
            floors = {i['floor_lvl'] for i in plan_insts}
            if len(floors) < 2:
                return plan_insts   # single floor — nothing to dedup

            TOL = 200   # PDF units

            # Sort lowest floor first
            by_floor = sorted(plan_insts, key=lambda i: i['floor_lvl'])
            claimed  = []   # [(cx, floor_lvl)] of already-primary instances

            for inst in by_floor:
                if inst['ann_type'] == 'outline':
                    continue   # already excluded (e.g. boundary strip)
                cx = inst['cx']
                fl = inst['floor_lvl']
                # Check if a lower-floor instance already claimed this x-position
                is_upper_dup = any(
                    cfl < fl and abs(cx - ccx) < TOL
                    for ccx, cfl in claimed
                )
                if is_upper_dup:
                    inst['ann_type'] = 'outline'
                else:
                    claimed.append((cx, fl))

            return plan_insts

        def sort_elevations_by_viewports(elev_insts, file_vp_frames):
            """
            Group elevation instances by viewport frame.
            Process frames right-to-left (exhausting each before moving left),
            bottom-to-top within each frame.

            Falls back to sort_by_columns_right_to_left if no viewport frames
            are available for the files involved.
            """
            if not file_vp_frames or not elev_insts:
                return sort_by_columns_right_to_left(elev_insts)

            # Build per-instance frame assignment
            frame_groups = defaultdict(list)  # frame_key -> [inst]
            unframed     = []

            for inst in elev_insts:
                fn     = inst['filename']
                frames = file_vp_frames.get(fn, [])
                pt     = fitz.Point(inst['cx'], inst['cy'])
                placed = False
                for frame in frames:
                    if frame.contains(pt):
                        # Key: file + frame top-left rounded to 10 units
                        fk = (fn, round(frame.x0 / 10), round(frame.y0 / 10))
                        frame_groups[fk].append((frame, inst))
                        placed = True
                        break
                if not placed:
                    unframed.append(inst)

            if not frame_groups:
                # No viewport frame data — use column sort
                return sort_by_columns_right_to_left(elev_insts)

            # Sort frames: rightmost (largest x0) first, then top-to-bottom
            def frame_order(fk):
                _, _, item = next(iter(frame_groups[fk]))
                frame = next(iter(frame_groups[fk]))[0]
                return (-frame.x0, frame.y0)

            try:
                sorted_keys = sorted(
                    frame_groups.keys(),
                    key=lambda fk: (
                        -frame_groups[fk][0][0].x0,
                         frame_groups[fk][0][0].y0
                    )
                )
            except Exception:
                return sort_by_columns_right_to_left(elev_insts)

            result = []
            for fk in sorted_keys:
                group_insts = [item[1] for item in frame_groups[fk]]
                # Within each viewport: bottom-up
                group_insts.sort(key=lambda i: (i['page_idx'], -i['cy']))
                result.extend(group_insts)

            # Unframed instances appended at end, bottom-up
            unframed.sort(key=lambda i: (i['page_idx'], -i['cy']))
            result.extend(unframed)
            return result

        for ref, instances in all_candidates.items():
            q = quota(ref)
            if not instances:
                continue

            detail_insts = [i for i in instances if effective_type(i) == 'DETAIL']
            elev_insts   = [i for i in instances
                            if effective_type(i) in ('ELEVATION', 'SECTION')]
            plan_insts   = [i for i in instances if effective_type(i) == 'PLAN']

            # Details → always outline
            for inst in detail_insts:
                inst['ann_type'] = 'outline'

            # Stair flights and landings are best represented in section
            # views — their geometry is only clearly visible there.
            # For these refs, section is primary even when plan instances
            # also exist. All other refs: plan is primary.
            STAIR_PREFIX_RE = re.compile(
                r'^(?:[A-Z]{2,4})(SF|SL|LS|LF|SF|STAIR|FLIGHT|LANDING)',
                re.IGNORECASE
            )
            section_primary = bool(STAIR_PREFIX_RE.search(ref))

            # When plan instances exist, elevation instances become outlines
            # (plan is the primary record for most units).
            # Exception: stair/landing refs where section is primary.
            if plan_insts and elev_insts and not section_primary:
                for inst in elev_insts:
                    inst['ann_type'] = 'outline'
            elif plan_insts and elev_insts and section_primary:
                # Section primary — plan instances become outlines
                for inst in plan_insts:
                    inst['ann_type'] = 'outline'

            # ── ELEVATION / SECTION (only when NO plan instances,
            #    or when section is primary for stair/landing refs) ────────
            if elev_insts and (not plan_insts or section_primary):
                elev_insts = apply_boundary_strip(
                    elev_insts, sheet_pairs, logs)

                # Sort strategy — right-to-left across viewport frames.
                # Exhausts the rightmost elevation fully before moving left.
                # Degrades gracefully: single view → bottom-up;
                # no frame data → column-gap detection.
                elev_insts = sort_elevations_by_viewports(
                    elev_insts, file_vp_frames)
                hl   = run_quota(elev_insts, q, lambda i: 0)
                b_hl = sum(1 for i in elev_insts if i.get('is_boundary'))
                # Describe sort method used
                n_frames = len({(i['filename'], round(i['cx']/10))
                                for i in elev_insts})
                sort_desc = f'{n_frames} viewport(s) R-to-L' if n_frames > 1 \
                            else 'bottom-up'
                logs.append(
                    f"  '{ref}': elev [{hl} highlighted {sort_desc}, "
                    f"{b_hl} stitch-exempt, "
                    f"{len(elev_insts)-hl-b_hl} outlined]"
                )

            # ── PLAN ──────────────────────────────────────────────────────
            if plan_insts and not section_primary:
                plan_insts = apply_boundary_strip(
                    plan_insts, sheet_pairs, logs)

                def plan_sort(i):
                    c = file_heat_centroids.get(i['filename'])
                    return (i['floor_lvl'],
                            pdist((i['cx'], i['cy']), c) if c else 0)

                hl   = run_quota(plan_insts, q, plan_sort)
                b_hl = sum(1 for i in plan_insts if i.get('is_boundary'))
                ev_o = len(elev_insts) if elev_insts else 0
                logs.append(
                    f"  '{ref}': plan [{hl} highlighted, "
                    f"{b_hl} stitch-exempt, "
                    f"{len(plan_insts)-hl-b_hl} outlined"
                    + (f" | {ev_o} elev → outline]" if ev_o else "]")
                )

            # Assign load numbers (delivered refs only)
            if ref in delivered_refs:
                loads = delivered_map[ref]
                highlights = [i for i in selected_instances + plan_insts + elev_insts
                              if i['ref'] == ref and i['ann_type'] == 'highlight']
                for k, inst in enumerate(highlights):
                    inst['load_no'] = loads[k] if k < len(loads) and loads[k] else None

            selected_instances.extend(plan_insts)
            selected_instances.extend(elev_insts)
            selected_instances.extend(detail_insts)

                # ===================================================================
        # PHASE 3: ANNOTATE — open each file and apply highlights + outlines
        # ===================================================================
        by_file = defaultdict(list)
        for inst in selected_instances:
            by_file[inst['filename']].append(inst)

        for filename, in_path in saved_paths:
            file_instances = by_file.get(filename, [])
            if not file_instances:
                logs.append(f"WARN: No matches in {filename}")
                continue

            try:
                doc = fitz.open(in_path)
                total_marks = 0

                by_page = defaultdict(list)
                for inst in file_instances:
                    by_page[inst['page_idx']].append(inst)

                for page_idx in range(len(doc)):
                    page = doc[page_idx]
                    # Process highlights before outlines; within highlights, by tier
                    page_insts = sorted(
                        by_page.get(page_idx, []),
                        key=lambda i: (0 if i['ann_type'] == 'highlight' else 1,
                                       tier_of(i['ref']))
                    )
                    page_protected = []

                    for inst_data in page_insts:
                        ref   = inst_data['ref']
                        inst  = inst_data['rect']
                        colour = tier_colour(tier_of(ref))

                        if inst_data['ann_type'] == 'highlight':
                            if overlaps_protected(inst, page_protected):
                                continue
                            add_highlight(page, inst, colour)
                            page_protected.append(inst)
                            found_units.add(ref)
                            total_marks += 1
                            if inst_data['load_no']:
                                insert_load_label(page, inst, inst_data['load_no'])

                        else:  # outline — always draw, even if overlapping
                            add_outline_rect(page, inst, colour)
                            total_marks += 1

                    # Audit unsearched units
                    for mark in unit_pattern.findall(page.get_text("text")):
                        if mark not in all_searched:
                            unsearched_units_found.add(mark)

                out_name = f"MARKED_{filename}"
                out_path = os.path.join(tmpdir, out_name)
                doc.save(out_path, garbage=4, deflate=True, clean=True)
                doc.close()
                output_files.append((out_name, out_path))
                logs.append(f"OK: {out_name} ({total_marks} annotations)")

            except Exception as e:
                logs.append(f"ERROR: {filename} — {e}")
                logs.append(traceback.format_exc()[:500])

        # Summary
        logs.append("─" * 40)
        marked_by_ref = Counter()
        for inst in selected_instances:
            if inst['ann_type'] == 'highlight':
                marked_by_ref[inst['ref']] += 1
        for ref in sorted(all_searched):
            q = quota(ref)
            m = marked_by_ref.get(ref, 0)
            if m == 0:
                pass  # captured in not_found
            elif m < q:
                logs.append(f"⚠ PARTIAL: '{ref}' — highlighted {m} of {q}")
            else:
                logs.append(f"✓ '{ref}' — {m}/{q}")

        not_found  = sorted(all_searched - found_units)
        unsearched = sorted(unsearched_units_found)

        zip_bytes = None
        if output_files:
            zip_path = os.path.join(tmpdir, 'marked_drawings.zip')
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for name, path in output_files:
                    zf.write(path, name)
            with open(zip_path, 'rb') as f:
                zip_bytes = f.read()

    result = {
        'logs':       logs,
        'not_found':  not_found,
        'unsearched': unsearched,
        'stats': {
            'issued':    total_issued,
            'produced':  total_produced,
            'delivered': total_delivered,
        },
        'has_output': zip_bytes is not None,
    }
    if zip_bytes:
        result['zip_b64']      = base64.b64encode(zip_bytes).decode('utf-8')
        result['zip_filename'] = 'marked_drawings.zip'

    return jsonify(result)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
