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
# Handles: +4000  +7.600  +7 600  +11,235  -500  EL+4000
# ===========================================================================
ELEV_MARKER_RE = re.compile(
    r'(?:EL\.?\s*)?([+\-])\s*(\d[\d\s]*(?:[.,]\d+)?)',
    re.IGNORECASE
)

def parse_elev_value(sign, digits):
    cleaned = digits.replace(' ', '').replace(',', '.')
    try:
        val = float(cleaned)
        # Heuristic: +7.600 is metres — convert to mm
        if val < 500 and '.' in cleaned:
            val *= 1000
        return val if sign == '+' else -val
    except ValueError:
        return None

def extract_elevations(page):
    """Return [(elev_mm, y_on_page), ...] sorted ascending."""
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
    """Closest elevation marker to this rect's y-centre."""
    if not elevations:
        return 0.0
    ry = (rect.y0 + rect.y1) / 2
    return min(elevations, key=lambda e: abs(e[1] - ry))[0]

# ===========================================================================
# DRAWING TYPE DETECTION
# Checks filename first (most reliable), then first-page text
# ===========================================================================
PLAN_RE = re.compile(r'\b(PLAN|PLN)\b',              re.IGNORECASE)
ELEV_RE = re.compile(r'\b(ELEVATION|ELEV)\b',         re.IGNORECASE)
SECT_RE = re.compile(r'\b(SECTION|SECT|SEC)\b',        re.IGNORECASE)

def detect_drawing_type(filename, first_page_text=''):
    # Normalise filename separators → spaces
    name = re.sub(r'[-_]', ' ', os.path.splitext(filename)[0])
    combined = name.upper() + '  ' + first_page_text[:2000].upper()
    if SECT_RE.search(combined): return 'SECTION'
    if ELEV_RE.search(combined): return 'ELEVATION'
    if PLAN_RE.search(combined): return 'PLAN'
    return 'UNKNOWN'

# ===========================================================================
# HEAT AREA DETECTION  (plan drawings)
# ===========================================================================
def compute_heat_centroid(positions, page_w, page_h):
    """
    Divide page into 8×8 grid, find densest cluster centroid.
    Returns None when distribution is too uniform.
    """
    if not positions:
        return None
    GRID = 8
    cw, ch = page_w / GRID, page_h / GRID
    grid = Counter()
    for x, y in positions:
        grid[(min(int(x / cw), GRID-1), min(int(y / ch), GRID-1))] += 1

    max_cnt = max(grid.values())
    avg_cnt = len(positions) / (GRID * GRID)

    # Require peak to be at least 2× average and at least 2 units
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
    """Greedy nearest-neighbour — keeps spatially adjacent instances together."""
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
        nearest = min(remaining, key=lambda i: pdist((i['cx'], i['cy']), (last['cx'], last['cy'])))
        result.append(nearest)
        remaining.remove(nearest)
    return result

# ===========================================================================
# INPUT PARSERS
# ===========================================================================
def parse_count_list(raw):
    """Each line = one unit. Duplicate lines = multiple units of same ref."""
    counts = Counter()
    for line in raw.splitlines():
        ref = line.strip()
        if ref:
            counts[ref] += 1
    return counts

def parse_delivered(raw):
    """
    Tab-separated: REF <TAB> LOAD_NO
    Returns {ref: [load_no, ...]} — length = count of that ref.
    """
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
# LOAD LABEL
# ===========================================================================
def insert_load_label(page, rect, load_no):
    font_size = max(7, rect.height * 0.85)
    pt = fitz.Point(rect.x1 + 4, rect.y0 + rect.height / 2 + font_size / 3)
    page.insert_text(pt, load_no, fontsize=font_size,
                     color=(0.0, 0.2, 0.65), overlay=True)

# ===========================================================================
# OVERLAP CHECK
# ===========================================================================
def overlaps_protected(inst, protected):
    area = abs(inst.width * inst.height)
    if area == 0:
        return False
    for p in protected:
        inter = inst & p
        if not inter.is_empty and abs(inter.width * inter.height) > area * 0.7:
            return True
    return False

# ===========================================================================
# FLASK ROUTES
# ===========================================================================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/process', methods=['POST'])
def process():
    issued_raw    = request.form.get('issued', '')
    produced_raw  = request.form.get('produced', '')
    delivered_raw = request.form.get('delivered', '')
    files         = request.files.getlist('pdfs')

    # --- Count-aware parsing ---
    issued_counts   = parse_count_list(issued_raw)
    produced_counts = parse_count_list(produced_raw)
    delivered_map   = parse_delivered(delivered_raw)
    delivered_counts = Counter({ref: len(loads) for ref, loads in delivered_map.items()})

    # Tier deduplication (higher tier wins)
    delivered_refs = set(delivered_map.keys())
    produced_refs  = set(produced_counts.keys()) - delivered_refs
    issued_refs    = set(issued_counts.keys()) - set(produced_counts.keys()) - delivered_refs
    all_searched   = delivered_refs | produced_refs | issued_refs

    def quota(ref):
        if ref in delivered_refs: return delivered_counts[ref]
        if ref in produced_refs:  return produced_counts[ref]
        if ref in issued_refs:    return issued_counts[ref]
        return 0

    def tier_of(ref):
        if ref in delivered_refs: return 0  # highest priority
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

    total_quotas = sum(quota(r) for r in all_searched)
    logs = [
        f"Tiers loaded — {len(delivered_refs)} delivered refs "
        f"({sum(delivered_counts.values())} units), "
        f"{len(produced_refs)} produced refs "
        f"({sum(produced_counts[r] for r in produced_refs)} units), "
        f"{len(issued_refs)} issued refs "
        f"({sum(issued_counts[r] for r in issued_refs)} units) "
        f"— {total_quotas} total units to mark"
    ]

    found_units:            set = set()
    unsearched_units_found: set = set()
    output_files:          list = []
    # Global quota tracking across all files
    marked_counts: Counter = Counter()

    with tempfile.TemporaryDirectory() as tmpdir:
        for upload in files:
            filename = secure_filename(upload.filename)
            if not filename.lower().endswith('.pdf'):
                logs.append(f"SKIP {filename} — not a PDF")
                continue

            in_path = os.path.join(tmpdir, filename)
            upload.save(in_path)

            try:
                logs.append(f"Analyzing: {filename}")
                doc = fitz.open(in_path)
                total_highlights = 0

                # Detect drawing type
                first_text  = doc[0].get_text("text") if len(doc) > 0 else ''
                draw_type   = detect_drawing_type(filename, first_text)
                logs.append(f"  ↳ Drawing type: {draw_type}")

                # -------------------------------------------------------
                # PRE-PASS: collect all candidate instances
                # Only collect refs that still have remaining quota
                # -------------------------------------------------------
                # { ref: [instance_dict, ...] }
                candidates = defaultdict(list)
                all_unit_positions = []  # for heat map

                for page_idx in range(len(doc)):
                    page = doc[page_idx]
                    elevations = extract_elevations(page)

                    for ref in all_searched:
                        if quota(ref) - marked_counts[ref] <= 0:
                            continue  # quota already met from previous files
                        for inst in page.search_for(ref):
                            cx = (inst.x0 + inst.x1) / 2
                            cy = (inst.y0 + inst.y1) / 2
                            candidates[ref].append({
                                'ref':       ref,
                                'page_idx':  page_idx,
                                'rect':      inst,
                                'elevation': elevation_for_rect(inst, elevations),
                                'cx': cx, 'cy': cy,
                            })
                            all_unit_positions.append((cx, cy))

                # Heat centroid for plan drawings
                heat_centroid = None
                if draw_type == 'PLAN' and len(doc) > 0:
                    pw, ph = doc[0].rect.width, doc[0].rect.height
                    heat_centroid = compute_heat_centroid(all_unit_positions, pw, ph)
                    if heat_centroid:
                        logs.append(f"  ↳ Heat area found at ({heat_centroid[0]:.0f}, {heat_centroid[1]:.0f})")
                    else:
                        logs.append(f"  ↳ No dominant heat area — using proximity grouping")

                # -------------------------------------------------------
                # SORT + QUOTA SELECTION per ref
                # -------------------------------------------------------
                assigned: list = []

                for ref, instances in candidates.items():
                    remaining = quota(ref) - marked_counts[ref]
                    if remaining <= 0 or not instances:
                        continue

                    if draw_type in ('ELEVATION', 'SECTION'):
                        all_zero = all(i['elevation'] == 0.0 for i in instances)
                        if all_zero:
                            # No elevation markers — fall back to bottom-of-page first
                            instances.sort(key=lambda i: (i['page_idx'], -i['cy']))
                            logs.append(f"  ↳ '{ref}': no elevation markers, using Y-position fallback")
                        else:
                            instances.sort(key=lambda i: (i['elevation'], i['page_idx'], i['cy']))

                    elif draw_type == 'PLAN':
                        if heat_centroid:
                            instances.sort(
                                key=lambda i: pdist((i['cx'], i['cy']), heat_centroid)
                            )
                        else:
                            instances = sort_by_proximity(instances)

                    else:  # UNKNOWN — try elevation, fall back to proximity
                        all_zero = all(i['elevation'] == 0.0 for i in instances)
                        if not all_zero:
                            instances.sort(key=lambda i: (i['elevation'], i['page_idx'], i['cy']))
                        else:
                            instances = sort_by_proximity(instances)

                    selected = instances[:remaining]

                    # Assign load numbers to delivered instances
                    if ref in delivered_refs:
                        loads  = delivered_map[ref]
                        offset = marked_counts[ref]
                        for k, inst in enumerate(selected):
                            load_idx = offset + k
                            inst['load_no'] = (
                                loads[load_idx]
                                if load_idx < len(loads) and loads[load_idx]
                                else None
                            )
                    else:
                        for inst in selected:
                            inst['load_no'] = None

                    if len(instances) > remaining:
                        logs.append(
                            f"  ↳ '{ref}': {len(instances)} instances found, "
                            f"marking {remaining} per quota "
                            f"(drawing type: {draw_type})"
                        )

                    if len(selected) < remaining:
                        logs.append(
                            f"  ⚠ '{ref}': quota {quota(ref)}, "
                            f"only {marked_counts[ref] + len(selected)} instance(s) found in total so far"
                        )

                    assigned.extend(selected)

                # -------------------------------------------------------
                # ANNOTATION PASS — priority order per page
                # -------------------------------------------------------
                by_page = defaultdict(list)
                for inst in assigned:
                    by_page[inst['page_idx']].append(inst)

                for page_idx in range(len(doc)):
                    page = doc[page_idx]
                    # Sort so delivered annotations are placed before produced/issued
                    # (ensures overlap protection works correctly by tier)
                    page_instances = sorted(by_page.get(page_idx, []), key=lambda i: tier_of(i['ref']))
                    page_protected_rects = []

                    for inst_data in page_instances:
                        inst = inst_data['rect']
                        ref  = inst_data['ref']

                        if overlaps_protected(inst, page_protected_rects):
                            continue

                        t = tier_of(ref)
                        if t == 0:   colour = (0.1, 0.6, 1.0)   # blue
                        elif t == 1: colour = (1.0, 0.647, 0.0)  # orange
                        else:        colour = (1.0, 1.0, 0.0)    # yellow

                        annot = page.add_highlight_annot(inst)
                        annot.set_colors(stroke=colour)
                        annot.update()
                        total_highlights += 1
                        page_protected_rects.append(inst)

                        found_units.add(ref)
                        marked_counts[ref] += 1

                        if inst_data['load_no']:
                            insert_load_label(page, inst, inst_data['load_no'])

                    # Audit unsearched units
                    for mark in unit_pattern.findall(page.get_text("text")):
                        if mark not in all_searched:
                            unsearched_units_found.add(mark)

                if total_highlights > 0:
                    out_name = f"MARKED_{filename}"
                    out_path = os.path.join(tmpdir, out_name)
                    doc.save(out_path, garbage=4, deflate=True, clean=True)
                    doc.close()
                    output_files.append((out_name, out_path))
                    logs.append(f"OK: {out_name} ({total_highlights} highlights)")
                else:
                    doc.close()
                    logs.append(f"WARN: No matches in {filename}")

            except Exception as e:
                logs.append(f"ERROR: {filename} — {e}")
                logs.append(traceback.format_exc()[:500])

        # Final quota summary
        logs.append("─" * 40)
        for ref in sorted(all_searched):
            q = quota(ref)
            m = marked_counts.get(ref, 0)
            if m == 0:
                pass  # captured in not_found
            elif m < q:
                logs.append(f"⚠ PARTIAL: '{ref}' — marked {m} of {q}")
            else:
                logs.append(f"✓ '{ref}' — {m}/{q} marked")

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
            'delivered': sum(delivered_counts.values()),
            'produced':  sum(produced_counts[r] for r in produced_refs),
            'issued':    sum(issued_counts[r] for r in issued_refs),
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
