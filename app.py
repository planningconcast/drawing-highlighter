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
# SPLIT-SHEET DETECTION & OVERLAP DEDUPLICATION
#
# When one floor plan is split across 2 (or more) drawings, units near the
# shared gridline appear on both sheets and must only be marked once.
#
# Detection strategy (in order of reliability):
#   1. Filename contains "1 OF 2" / "2 OF 2" / "SHEET_1_OF_2" etc.
#   2. Title block text contains the same pattern
#   3. Shared gridline: grid refs (letters/numbers) found on both the right
#      margin of sheet A and the left margin of sheet B confirm the join edge.
#
# Once the shared gridline x-position is known for each sheet, any instance
# that falls on the "wrong" side (overlap zone) of that line is a duplicate
# and is removed — keeping only the more-central instance.
# ===========================================================================

SHEET_OF_RE = re.compile(
    r'(\d+)\s*(?:OF|_OF_)\s*(\d+)', re.IGNORECASE
)
# Grid refs: single/double uppercase letters or 1-3 digit numbers
GRID_REF_RE = re.compile(r'^[A-Z]{1,2}$|^\d{1,3}$')

def get_sheet_number(filename):
    """Return (sheet_num, total_sheets) from filename, or None."""
    name = re.sub(r'[-.]', '_', os.path.basename(filename).upper())
    m = SHEET_OF_RE.search(name)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None

def extract_margin_grid_refs(page, side, margin=0.08):
    """
    Extract grid reference labels from one vertical margin of the page.
    side: 'left' or 'right'
    Returns dict {ref_text: x_centre_on_page}
    """
    pw, ph = page.rect.width, page.rect.height
    # Exclude top/bottom 5% to avoid title block and sheet border text
    if side == 'right':
        zone = fitz.Rect(pw * (1.0 - margin), ph * 0.05, pw, ph * 0.95)
    else:
        zone = fitz.Rect(0, ph * 0.05, pw * margin, ph * 0.95)

    refs = {}
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        br = fitz.Rect(block["bbox"])
        if not zone.intersects(br):
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span["text"].strip()
                if GRID_REF_RE.match(text):
                    cx = (span["bbox"][0] + span["bbox"][2]) / 2
                    refs[text] = cx
    return refs


def find_shared_gridline(page_a, page_b):
    """
    Find the x-position of the shared gridline between two adjacent sheets.
    sheet A's right margin grid refs are compared with sheet B's left margin.
    Returns (x_in_a, x_in_b) or None if no shared refs found.
    """
    right_of_a = extract_margin_grid_refs(page_a, 'right')
    left_of_b  = extract_margin_grid_refs(page_b, 'left')
    shared = set(right_of_a.keys()) & set(left_of_b.keys())
    if shared:
        # Use the rightmost shared ref in A and leftmost in B
        x_a = max(right_of_a[r] for r in shared)
        x_b = min(left_of_b[r]  for r in shared)
        return x_a, x_b
    return None


def build_sheet_pairs(saved_paths, file_draw_types, file_docs):
    """
    Identify pairs of split-sheet plan drawings for the same floor.
    Returns list of dicts:
      { 'fn_a': filename, 'fn_b': filename,
        'boundary_x_a': float, 'boundary_x_b': float }
    boundary_x_a = x in sheet A beyond which is the overlap zone (right side)
    boundary_x_b = x in sheet B below which is the overlap zone (left side)
    """
    pairs = []
    plan_files = [(fn, ip) for fn, ip in saved_paths
                  if file_draw_types.get(fn) in ('PLAN', 'UNKNOWN')]

    # Group by floor level
    by_floor = defaultdict(list)
    for fn, ip in plan_files:
        lvl = extract_floor_level(fn)
        by_floor[lvl].append((fn, ip))

    for lvl, floor_files in by_floor.items():
        if len(floor_files) < 2:
            continue

        # Find files that declare themselves as part of a multi-sheet set
        sheet_numbered = []
        for fn, ip in floor_files:
            sn = get_sheet_number(fn)
            if sn:
                sheet_numbered.append((fn, ip, sn[0], sn[1]))

        if len(sheet_numbered) >= 2:
            # Sort by sheet number and pair consecutive sheets
            sheet_numbered.sort(key=lambda x: x[2])
            for k in range(len(sheet_numbered) - 1):
                fn_a, ip_a, sn_a, _ = sheet_numbered[k]
                fn_b, ip_b, sn_b, _ = sheet_numbered[k + 1]

                doc_a = file_docs.get(fn_a)
                doc_b = file_docs.get(fn_b)
                if not doc_a or not doc_b:
                    continue

                page_a = doc_a[0]
                page_b = doc_b[0]
                result = find_shared_gridline(page_a, page_b)

                if result:
                    bx_a, bx_b = result
                    pairs.append({
                        'fn_a': fn_a, 'fn_b': fn_b,
                        'boundary_x_a': bx_a,
                        'boundary_x_b': bx_b,
                        'method': 'gridline'
                    })
                else:
                    # Fallback: use page-width fraction as boundary
                    pw_a = doc_a[0].rect.width
                    pw_b = doc_b[0].rect.width
                    pairs.append({
                        'fn_a': fn_a, 'fn_b': fn_b,
                        'boundary_x_a': pw_a * 0.85,
                        'boundary_x_b': pw_b * 0.15,
                        'method': 'fallback'
                    })
        else:
            # No sheet numbers — try all pairs using gridline detection only
            for i in range(len(floor_files)):
                for j in range(i + 1, len(floor_files)):
                    fn_a, ip_a = floor_files[i]
                    fn_b, ip_b = floor_files[j]
                    doc_a = file_docs.get(fn_a)
                    doc_b = file_docs.get(fn_b)
                    if not doc_a or not doc_b:
                        continue
                    result = find_shared_gridline(doc_a[0], doc_b[0])
                    if result:
                        bx_a, bx_b = result
                        pairs.append({
                            'fn_a': fn_a, 'fn_b': fn_b,
                            'boundary_x_a': bx_a,
                            'boundary_x_b': bx_b,
                            'method': 'gridline'
                        })
    return pairs


def deduplicate_sheet_overlaps(plan_insts, sheet_pairs, logs):
    """
    Remove duplicate instances in the overlap zone between paired sheets.
    For each pair, instances past the boundary in sheet A or before the
    boundary in sheet B are in the overlap zone.  When the same ref appears
    in the overlap zone of both sheets, keep the instance that is further
    from the shared edge (i.e. more central to its own sheet).
    """
    if not sheet_pairs or len({i['filename'] for i in plan_insts}) < 2:
        return plan_insts

    to_remove = set()
    by_file = defaultdict(list)
    for inst in plan_insts:
        by_file[inst['filename']].append(inst)

    for pair in sheet_pairs:
        fn_a = pair['fn_a']
        fn_b = pair['fn_b']
        bx_a = pair['boundary_x_a']   # right-side boundary in sheet A
        bx_b = pair['boundary_x_b']   # left-side boundary in sheet B
        method = pair.get('method', 'fallback')

        insts_a = by_file.get(fn_a, [])
        insts_b = by_file.get(fn_b, [])

        overlap_a = [i for i in insts_a if i['cx'] >= bx_a and id(i) not in to_remove]
        overlap_b = [i for i in insts_b if i['cx'] <= bx_b and id(i) not in to_remove]

        removed = 0
        for ia in overlap_a:
            for ib in overlap_b:
                if ia['ref'] != ib['ref']:
                    continue
                # Same ref in overlap zone of both sheets.
                # Tiebreaker rule: sheet A (lower sheet number) always owns
                # the boundary — remove the sheet B instance on any tie.
                # Only keep sheet B instance if it is STRICTLY further from
                # the boundary than sheet A (meaning it is clearly more central
                # to sheet B and sheet A's instance is closer to the edge).
                dist_a = abs(ia['cx'] - bx_a)  # distance from boundary in A
                dist_b = abs(ib['cx'] - bx_b)  # distance from boundary in B
                if dist_a < dist_b:
                    # Sheet A instance is closer to boundary — remove it
                    to_remove.add(id(ia))
                else:
                    # Sheet A instance is further from (or equal to) boundary
                    # → it is more central to sheet A, keep it, remove sheet B
                    to_remove.add(id(ib))
                removed += 1

        if removed > 0:
            logs.append(
                f"  ↳ Split-sheet dedup [{method}]: "
                f"{os.path.basename(fn_a)} / {os.path.basename(fn_b)} "
                f"— removed {removed} overlap duplicate(s)"
            )

    return [i for i in plan_insts if id(i) not in to_remove]

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
        unit_pattern = re.compile(
            r'\b(?:' + pfx_str + (r')\-\d+\b' if has_hyphen else r')\d+\b')
        )
    else:
        unit_pattern = re.compile(r'\b[A-Z]{2,4}\-\d+\b')

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
                    f"  ↳ Split-sheet pair detected [{p['method']}]: "
                    f"{os.path.basename(p['fn_a'])} ↔ {os.path.basename(p['fn_b'])} "
                    f"(boundary x={p['boundary_x_a']:.0f} / {p['boundary_x_b']:.0f})"
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
                # Deduplicate overlapping split-sheet instances using detected pairs
                plan_insts = deduplicate_sheet_overlaps(plan_insts, sheet_pairs, logs)

                # Sort: floor level ascending, then heat centroid distance within floor
                def plan_key(i):
                    centroid = file_heat_centroids.get(i['filename'])
                    dist = pdist((i['cx'], i['cy']), centroid) if centroid else 0
                    return (i['floor_lvl'], dist)

                plan_insts.sort(key=plan_key)

                for k, inst in enumerate(plan_insts):
                    if k < q:
                        inst['ann_type'] = 'highlight'
                    else:
                        inst['ann_type'] = 'outline'

                if len(plan_insts) > q:
                    logs.append(
                        f"  ↳ '{ref}': {len(plan_insts)} plan instances "
                        f"(after dedup), marking {q} lowest-floor-first "
                        f"+ {len(plan_insts)-q} outlined"
                    )
                elif len(plan_insts) < q:
                    logs.append(
                        f"  ⚠ '{ref}': plan quota {q}, "
                        f"only {len(plan_insts)} instance(s) found after dedup"
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
