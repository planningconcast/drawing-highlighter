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
    if not elevations:
        return 0.0
    ry = (rect.y0 + rect.y1) / 2
    return min(elevations, key=lambda e: abs(e[1] - ry))[0]

# ===========================================================================
# DRAWING TYPE DETECTION
# ===========================================================================
PLAN_RE = re.compile(r'\b(PLAN|PLN)\b',         re.IGNORECASE)
ELEV_RE = re.compile(r'\b(ELEVATION|ELEV)\b',    re.IGNORECASE)
SECT_RE = re.compile(r'\b(SECTION|SECT|SEC)\b',  re.IGNORECASE)

def detect_drawing_type(filename, first_page_text=''):
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
    """Tab-separated: REF <TAB> LOAD_NO. Returns {ref: [load_no, ...]}."""
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

    # --- Parse inputs (count-aware) ---
    issued_counts   = parse_count_list(issued_raw)
    produced_counts = parse_count_list(produced_raw)
    delivered_map   = parse_delivered(delivered_raw)
    delivered_counts = Counter({ref: len(loads) for ref, loads in delivered_map.items()})

    # Tier colouring: delivered=blue, produced-not-delivered=orange, issued-not-produced=yellow
    # Marking priority is based on highest tier a ref appears in.
    delivered_refs = set(delivered_map.keys())
    produced_refs  = set(produced_counts.keys()) - delivered_refs
    issued_refs    = set(issued_counts.keys()) - set(produced_counts.keys()) - delivered_refs
    all_searched   = delivered_refs | produced_refs | issued_refs

    def quota(ref):
        """How many instances of this ref should be marked (count from its list)."""
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

    # Stats show TOTAL units per tier (not exclusive counts)
    total_delivered = sum(delivered_counts.values())
    total_produced  = sum(produced_counts.values())
    total_issued    = sum(issued_counts.values())

    logs = [
        f"Tiers loaded — "
        f"{len(delivered_refs)} delivered refs ({total_delivered} units), "
        f"{len(produced_refs)} produced-only refs, "
        f"{len(issued_refs)} issued-only refs — "
        f"{sum(quota(r) for r in all_searched)} total units to mark"
    ]

    found_units:             set   = set()
    unsearched_units_found:  set   = set()
    output_files:            list  = []

    with tempfile.TemporaryDirectory() as tmpdir:

        # ===================================================================
        # PHASE 1: COLLECT ALL INSTANCES ACROSS ALL FILES
        # We must scan every file before applying quota selection,
        # so that quota is distributed globally (not consumed file-by-file).
        # ===================================================================
        # Structure: { ref: [ {file_path, filename, page_idx, rect, elevation, cx, cy, draw_type}, ... ] }
        all_candidates = defaultdict(list)
        file_draw_types = {}   # filename -> draw_type
        file_heat_centroids = {}  # filename -> centroid or None
        all_unit_positions_by_file = defaultdict(list)

        saved_paths = []
        for upload in files:
            filename = secure_filename(upload.filename)
            if not filename.lower().endswith('.pdf'):
                logs.append(f"SKIP {filename} — not a PDF")
                continue
            in_path = os.path.join(tmpdir, filename)
            upload.save(in_path)
            saved_paths.append((filename, in_path))

        for filename, in_path in saved_paths:
            try:
                doc = fitz.open(in_path)
                first_text  = doc[0].get_text("text") if len(doc) > 0 else ''
                draw_type   = detect_drawing_type(filename, first_text)
                file_draw_types[filename] = draw_type
                logs.append(f"Scanning: {filename} [{draw_type}]")

                for page_idx in range(len(doc)):
                    page       = doc[page_idx]
                    elevations = extract_elevations(page)

                    for ref in all_searched:
                        for inst in page.search_for(ref):
                            cx = (inst.x0 + inst.x1) / 2
                            cy = (inst.y0 + inst.y1) / 2
                            all_candidates[ref].append({
                                'ref':       ref,
                                'filename':  filename,
                                'in_path':   in_path,
                                'page_idx':  page_idx,
                                'rect':      inst,
                                'elevation': elevation_for_rect(inst, elevations),
                                'cx': cx, 'cy': cy,
                                'draw_type': draw_type,
                                'load_no':   None,
                            })
                            all_unit_positions_by_file[filename].append((cx, cy))

                doc.close()

            except Exception as e:
                logs.append(f"ERROR scanning {filename}: {e}")

        # Compute heat centroids for plan drawings
        for filename, in_path in saved_paths:
            draw_type = file_draw_types.get(filename, 'UNKNOWN')
            if draw_type == 'PLAN':
                try:
                    doc = fitz.open(in_path)
                    pw, ph = doc[0].rect.width, doc[0].rect.height
                    doc.close()
                    positions = all_unit_positions_by_file[filename]
                    centroid  = compute_heat_centroid(positions, pw, ph)
                    file_heat_centroids[filename] = centroid
                    if centroid:
                        logs.append(f"  ↳ {filename}: heat area at ({centroid[0]:.0f}, {centroid[1]:.0f})")
                    else:
                        logs.append(f"  ↳ {filename}: no heat area — proximity grouping")
                except Exception:
                    file_heat_centroids[filename] = None
            else:
                file_heat_centroids[filename] = None

        # ===================================================================
        # PHASE 2: GLOBAL QUOTA SELECTION per ref
        # Sort all instances of each ref across all files, then take first N.
        # ===================================================================
        selected_instances = []  # all instances that will be annotated

        for ref, instances in all_candidates.items():
            q = quota(ref)
            if not instances:
                continue

            # Determine dominant draw type across instances of this ref
            type_counts = Counter(i['draw_type'] for i in instances)
            dominant_type = type_counts.most_common(1)[0][0]

            if dominant_type in ('ELEVATION', 'SECTION'):
                all_zero = all(i['elevation'] == 0.0 for i in instances)
                if all_zero:
                    instances.sort(key=lambda i: (i['filename'], i['page_idx'], -i['cy']))
                    logs.append(f"  ↳ '{ref}': no elevation markers — Y-position fallback")
                else:
                    instances.sort(key=lambda i: (i['elevation'], i['page_idx'], i['cy']))

            elif dominant_type == 'PLAN':
                # Use per-file heat centroid
                def plan_sort_key(i):
                    centroid = file_heat_centroids.get(i['filename'])
                    if centroid:
                        return pdist((i['cx'], i['cy']), centroid)
                    return i['cx'] + i['cy']  # proximity fallback
                instances = sort_by_proximity(instances) if not any(
                    file_heat_centroids.get(i['filename']) for i in instances
                ) else sorted(instances, key=plan_sort_key)

            else:
                # UNKNOWN — try elevation sort first
                all_zero = all(i['elevation'] == 0.0 for i in instances)
                if not all_zero:
                    instances.sort(key=lambda i: (i['elevation'], i['page_idx'], i['cy']))
                else:
                    instances = sort_by_proximity(instances)

            selected = instances[:q]

            if len(instances) > q:
                logs.append(
                    f"  ↳ '{ref}': {len(instances)} instances across all drawings, "
                    f"marking {q} per quota [{dominant_type}]"
                )
            if len(selected) < q:
                logs.append(
                    f"  ⚠ '{ref}': quota {q}, only {len(selected)} instance(s) found in drawings"
                )

            # Assign load numbers for delivered refs
            if ref in delivered_refs:
                loads = delivered_map[ref]
                for k, inst in enumerate(selected):
                    inst['load_no'] = (
                        loads[k] if k < len(loads) and loads[k] else None
                    )

            selected_instances.extend(selected)

        # ===================================================================
        # PHASE 3: ANNOTATE — open each file once and apply highlights
        # ===================================================================
        # Group selected instances by file
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
                total_highlights = 0

                # Group by page
                by_page = defaultdict(list)
                for inst in file_instances:
                    by_page[inst['page_idx']].append(inst)

                for page_idx in range(len(doc)):
                    page = doc[page_idx]
                    # Process in priority order: delivered first, then produced, then issued
                    page_instances = sorted(
                        by_page.get(page_idx, []),
                        key=lambda i: tier_of(i['filename'])  # bug fix below
                    )
                    # ↑ sort key should be tier_of(ref), not filename
                    page_instances = sorted(
                        by_page.get(page_idx, []),
                        key=lambda i: tier_of(i.get('ref', ''))
                    )
                    page_protected = []

                    for inst_data in page_instances:
                        ref  = inst_data.get('ref', '')
                        inst = inst_data['rect']

                        if overlaps_protected(inst, page_protected):
                            continue

                        t = tier_of(ref)
                        colour = (0.1, 0.6, 1.0) if t == 0 else \
                                 (1.0, 0.647, 0.0) if t == 1 else \
                                 (1.0, 1.0, 0.0)

                        annot = page.add_highlight_annot(inst)
                        annot.set_colors(stroke=colour)
                        annot.update()
                        total_highlights += 1
                        page_protected.append(inst)
                        found_units.add(ref)

                        if inst_data['load_no']:
                            insert_load_label(page, inst, inst_data['load_no'])

                    # Audit unsearched units
                    for mark in unit_pattern.findall(page.get_text("text")):
                        if mark not in all_searched:
                            unsearched_units_found.add(mark)

                out_name = f"MARKED_{filename}"
                out_path = os.path.join(tmpdir, out_name)
                doc.save(out_path, garbage=4, deflate=True, clean=True)
                doc.close()
                output_files.append((out_name, out_path))
                logs.append(f"OK: {out_name} ({total_highlights} highlights)")

            except Exception as e:
                logs.append(f"ERROR: {filename} — {e}")
                logs.append(traceback.format_exc()[:500])

        # Final quota summary
        logs.append("─" * 40)
        marked_by_ref = Counter()
        for inst in selected_instances:
            marked_by_ref[inst.get('ref', '')] += 1
        for ref in sorted(all_searched):
            q = quota(ref)
            m = marked_by_ref.get(ref, 0)
            if m == 0:
                pass  # captured in not_found
            elif m < q:
                logs.append(f"⚠ PARTIAL: '{ref}' — marked {m} of {q}")
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
            # Show TOTAL counts per list, not exclusive
            'delivered': total_delivered,
            'produced':  total_produced,
            'issued':    total_issued,
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
