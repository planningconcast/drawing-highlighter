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

def detect_drawing_type(filename, pages):
    name = re.sub(r'[-_.()[\]]', ' ',
                  os.path.splitext(os.path.basename(filename))[0]).upper()
    if SECT_RE.search(name): return 'SECTION'
    if ELEV_RE.search(name): return 'ELEVATION'
    if PLAN_RE.search(name): return 'PLAN'
    for page in pages:
        tb = extract_title_block_text(page).upper()
        if SECT_RE.search(tb): return 'SECTION'
        if ELEV_RE.search(tb): return 'ELEVATION'
        if PLAN_RE.search(tb): return 'PLAN'
    for page in pages:
        full = page.get_text("text").upper()
        if SECT_RE.search(full): return 'SECTION'
        if ELEV_RE.search(full): return 'ELEVATION'
        if PLAN_RE.search(full): return 'PLAN'
    return 'UNKNOWN'

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
                    })
    return pairs


def apply_boundary_strip(plan_insts, sheet_pairs, logs):
    """
    For each sheet pair, mark instances on sheet B (the right-hand sheet) whose
    text label falls inside the boundary strip as outline-only.

    The strip is centred on the detected split gridline x on sheet B.
    Sheet A is untouched — it owns the boundary.

    Since docs are closed by this point, we use the instance cx values
    (collected during the scan pass) to check strip membership.
    """
    if not sheet_pairs:
        return plan_insts

    # Build lookup: fn_b -> (gridline_x_b, strip_width)
    b_strips = {}
    for pair in sheet_pairs:
        b_strips[pair["fn_b"]] = (pair["gridline_x_b"], pair["strip_width"])

    marked = 0
    for inst in plan_insts:
        fn = inst["filename"]
        if fn not in b_strips:
            continue
        gx_b, sw = b_strips[fn]
        # Check if this instance's text position is inside the boundary strip
        if abs(inst["cx"] - gx_b) <= sw:
            if inst["ann_type"] != "outline":   # don't double-mark
                inst["ann_type"] = "outline"
                marked += 1

    if marked > 0:
        logs.append(
            f"  ↳ Boundary strip: {marked} instance(s) on sheet-2 side "
            f"→ outline-only (sheet 1 owns the gridline)"
        )
    return plan_insts


def deduplicate_sheet_overlaps(plan_insts, sheet_pairs, logs):
    """Legacy stub — boundary strip logic replaced this function."""
    return plan_insts
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
    return 'ok', 200

@app.route('/')
def index():
    return render_template('index.html')

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

    found_units:            set  = set()
    unsearched_units_found: set  = set()
    output_files:           list = []

    with tempfile.TemporaryDirectory() as tmpdir:

        # ===================================================================
        # SCAN PASS — collect all instances, page dims, draw types
        # ===================================================================
        all_candidates   = defaultdict(list)   # ref → [instance_dict]
        file_draw_types  = {}
        file_heat_centroids = {}
        file_page_dims   = {}                  # filename → (width, height)
        all_unit_pos_by_file = defaultdict(list)

        saved_paths = []
        for upload in files:
            filename = secure_filename(upload.filename)
            if not filename.lower().endswith('.pdf'):
                logs.append(f"SKIP {filename} — not a PDF")
                continue
            in_path = os.path.join(tmpdir, filename)
            upload.save(in_path)
            saved_paths.append((filename, in_path))

        file_docs = {}  # keep docs open for sheet-pair gridline detection

        for filename, in_path in saved_paths:
            try:
                doc  = fitz.open(in_path)
                pages = [doc[i] for i in range(len(doc))]
                draw_type = detect_drawing_type(filename, pages)
                file_draw_types[filename] = draw_type
                floor_lvl = extract_floor_level(filename)
                logs.append(f"Scanning: {filename} [{draw_type}, floor {floor_lvl}]")

                for page_idx, page in enumerate(pages):
                    file_page_dims[filename] = (page.rect.width, page.rect.height)
                    elevations = extract_elevations(page)
                    for ref in all_searched:
                        for inst in page.search_for(ref):
                            cx = (inst.x0 + inst.x1) / 2
                            cy = (inst.y0 + inst.y1) / 2
                            all_candidates[ref].append({
                                'ref':        ref,
                                'filename':   filename,
                                'in_path':    in_path,
                                'page_idx':   page_idx,
                                'rect':       inst,
                                'elevation':  elevation_for_rect(inst, elevations),
                                'cx': cx, 'cy': cy,
                                'draw_type':  draw_type,
                                'floor_lvl':  floor_lvl,
                                'load_no':    None,
                                'ann_type':   'highlight',  # or 'outline'
                            })
                            all_unit_pos_by_file[filename].append((cx, cy))
                file_docs[filename] = doc  # keep open for pair detection
            except Exception as e:
                logs.append(f"ERROR scanning {filename}: {e}")

        # Compute heat centroids for plan drawings
        for filename, in_path in saved_paths:
            if file_draw_types.get(filename) == 'PLAN':
                try:
                    pw, ph = file_page_dims.get(filename, (1000, 1000))
                    positions = all_unit_pos_by_file[filename]
                    centroid  = compute_heat_centroid(positions, pw, ph)
                    file_heat_centroids[filename] = centroid
                    if centroid:
                        logs.append(f"  ↳ {filename}: heat area at ({centroid[0]:.0f}, {centroid[1]:.0f})")
                    else:
                        logs.append(f"  ↳ {filename}: no heat area — proximity grouping")
                except Exception:
                    file_heat_centroids[filename] = None

        # Build split-sheet pairs for overlap deduplication
        sheet_pairs = build_sheet_pairs(saved_paths, file_draw_types, file_docs)
        if sheet_pairs:
            for p in sheet_pairs:
                logs.append(
                    f"  ↳ Split-sheet pair [{p['method']}]: "
                    f"{os.path.basename(p['fn_a'])} ↔ {os.path.basename(p['fn_b'])} "
                    f"| gridline x: sheet1={p['gridline_x_a']:.0f}, "
                    f"sheet2={p['gridline_x_b']:.0f}, strip±{p['strip_width']:.0f}"
                )

        # Close docs after pair detection — no longer needed until annotation pass
        for doc in file_docs.values():
            try:
                doc.close()
            except Exception:
                pass
        file_docs = {}

        # ===================================================================
        # PHASE 2: QUOTA SELECTION
        #
        # PLAN / UNKNOWN:
        #   - Deduplicate overlapping split-sheet instances
        #   - Sort by floor level ascending (lowest = built first)
        #   - Within same floor, sort by heat centroid distance
        #   - First quota(ref) instances → highlight
        #   - Remaining spotted instances → outline rectangle
        #
        # ELEVATION / SECTION:
        #   - Global quota across ALL elevation drawings
        #   - Sort bottom-up (largest cy first = ground level)
        #   - First quota(ref) instances → highlight
        #   - Remaining → outline rectangle
        # ===================================================================
        selected_instances = []  # both highlights and outlines

        for ref, instances in all_candidates.items():
            q = quota(ref)
            if not instances:
                continue

            elev_insts = [i for i in instances if i['draw_type'] in ('ELEVATION', 'SECTION')]
            plan_insts = [i for i in instances if i['draw_type'] in ('PLAN', 'UNKNOWN')]

            # ---- ELEVATION / SECTION ----------------------------------------
            if elev_insts:
                elev_insts.sort(key=lambda i: (i['page_idx'], -i['cy']))
                for k, inst in enumerate(elev_insts):
                    if k < q:
                        inst['ann_type'] = 'highlight'
                    else:
                        inst['ann_type'] = 'outline'
                if len(elev_insts) > q:
                    logs.append(
                        f"  ↳ '{ref}': {len(elev_insts)} elev/sect instances, "
                        f"marking {q} bottom-up + {len(elev_insts)-q} outlined"
                    )
                elif len(elev_insts) < q:
                    logs.append(
                        f"  ⚠ '{ref}': elev/sect quota {q}, "
                        f"only {len(elev_insts)} instance(s) found"
                    )
                selected_instances.extend(elev_insts)

            # ---- PLAN / UNKNOWN ---------------------------------------------
            if plan_insts:
                # Apply boundary strip — sheet 2 instances near split gridline → outline
                plan_insts = apply_boundary_strip(plan_insts, sheet_pairs, logs)

                # Sort: floor level ascending, then heat centroid distance within floor
                def plan_key(i):
                    centroid = file_heat_centroids.get(i['filename'])
                    dist = pdist((i['cx'], i['cy']), centroid) if centroid else 0
                    return (i['floor_lvl'], dist)

                plan_insts.sort(key=plan_key)

                # Apply quota — respect boundary-strip outlines already assigned.
                # Only instances still marked 'highlight' compete for the quota.
                highlight_count = 0
                for inst in plan_insts:
                    if inst['ann_type'] == 'outline':
                        continue  # boundary strip or prior assignment — leave as outline
                    if highlight_count < q:
                        inst['ann_type'] = 'highlight'
                        highlight_count += 1
                    else:
                        inst['ann_type'] = 'outline'

                outline_count = len(plan_insts) - highlight_count
                if outline_count > 0 or highlight_count < q:
                    logs.append(
                        f"  ↳ '{ref}': {len(plan_insts)} plan instances "
                        f"→ {highlight_count} highlighted, {outline_count} outlined"
                        + (f" (quota {q} not fully met)" if highlight_count < q else "")
                    )
                selected_instances.extend(plan_insts)

            # Assign load numbers for delivered refs
            if ref in delivered_refs:
                loads = delivered_map[ref]
                # Assign to highlights only, in order: elev first then plan
                highlights = [i for i in selected_instances
                              if i['ref'] == ref and i['ann_type'] == 'highlight']
                for k, inst in enumerate(highlights):
                    inst['load_no'] = loads[k] if k < len(loads) and loads[k] else None

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
