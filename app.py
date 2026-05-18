import os
import re
import fitz  # PyMuPDF
from flask import Flask, request, jsonify, render_template
from werkzeug.utils import secure_filename
import tempfile
import zipfile
import base64
from collections import defaultdict

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200MB max upload

# ---------------------------------------------------------------------------
# ELEVATION DETECTION
# Handles: +4000  +7.600  +7 600  +11,235  -500  EL+4000  EL.+7600
# ---------------------------------------------------------------------------
ELEV_REGEX = re.compile(
    r'(?:EL\.?\s*)?([+\-])\s*(\d[\d\s]*(?:[.,]\d+)?)',
    re.IGNORECASE
)

def parse_elevation_value(sign: str, digits: str) -> float | None:
    """Normalise varied elevation formats to a float in mm."""
    cleaned = digits.replace(' ', '').replace(',', '.')
    try:
        val = float(cleaned)
        # Heuristic: values like +7.600 are metres — convert to mm
        if val < 500 and '.' in cleaned:
            val *= 1000
        return val if sign == '+' else -val
    except ValueError:
        return None

def extract_elevations(page) -> list[tuple[float, float]]:
    """
    Return [(elevation_mm, y_centre_on_page), ...] sorted ascending (ground up).
    Uses block-level text with bbox so we know where on the page each value sits.
    """
    elevations = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for m in ELEV_REGEX.finditer(span["text"]):
                    val = parse_elevation_value(m.group(1), m.group(2))
                    if val is not None:
                        bbox = span["bbox"]
                        y_centre = (bbox[1] + bbox[3]) / 2
                        elevations.append((val, y_centre))
    elevations.sort(key=lambda e: e[0])
    return elevations

def elevation_for_rect(rect, elevations: list[tuple[float, float]]) -> float:
    """Find the elevation value whose y-position is closest to the rect's centre."""
    if not elevations:
        return 0.0
    rect_y = (rect.y0 + rect.y1) / 2
    return min(elevations, key=lambda e: abs(e[1] - rect_y))[0]

# ---------------------------------------------------------------------------
# DELIVERED INPUT PARSER  (tab-separated: REF <TAB> LOAD_NO)
# Also accepts lines with just a ref (no load number).
# Returns delivered_map: { ref: [load1, load2, ...] }
# ---------------------------------------------------------------------------
def parse_delivered(raw: str) -> dict[str, list[str]]:
    delivered_map = defaultdict(list)
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # Split on tab first; fall back to 2+ spaces
        parts = [p.strip() for p in re.split(r'\t+|\s{2,}', line) if p.strip()]
        if not parts:
            continue
        ref  = parts[0]
        load = parts[1] if len(parts) >= 2 else ""
        delivered_map[ref].append(load)
    return delivered_map

# ---------------------------------------------------------------------------
# LOAD LABEL  — inserted as a text overlay to the right of the highlight
# ---------------------------------------------------------------------------
def insert_load_label(page, rect, load_no: str):
    font_size = max(7, rect.height * 0.85)
    # Vertically centre the text inside the highlight band
    point = fitz.Point(
        rect.x1 + 4,
        rect.y0 + rect.height / 2 + font_size / 3
    )
    page.insert_text(
        point,
        load_no,          # show exactly what was pasted — no extra prefix
        fontsize=font_size,
        color=(0.0, 0.2, 0.65),
        overlay=True
    )

# ---------------------------------------------------------------------------
# FLASK ROUTES
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/process', methods=['POST'])
def process():
    issued_raw    = request.form.get('issued', '')
    produced_raw  = request.form.get('produced', '')
    delivered_raw = request.form.get('delivered', '')
    files         = request.files.getlist('pdfs')

    # --- Parse inputs ---
    delivered_map = parse_delivered(delivered_raw)
    actual_delivered = set(delivered_map.keys())
    issued_set    = {l.strip() for l in issued_raw.splitlines()  if l.strip()}
    produced_set  = {l.strip() for l in produced_raw.splitlines() if l.strip()}
    actual_produced = produced_set - actual_delivered
    actual_issued   = issued_set - produced_set - actual_delivered
    all_searched  = actual_delivered | actual_produced | actual_issued

    if not all_searched:
        return jsonify({'error': 'All lists are empty. Paste references first.'}), 400
    if not files or files[0].filename == '':
        return jsonify({'error': 'No PDF files selected.'}), 400

    # Refs that appear more than once in the delivered list are duplicates
    duplicate_refs = {ref for ref, loads in delivered_map.items() if len(loads) > 1}

    # Refs pasted twice accidentally (same ref, same load both times)
    # — warn but keep only unique loads per ref
    cleaned_map: dict[str, list[str]] = {}
    for ref, loads in delivered_map.items():
        seen = []
        for l in loads:
            if l not in seen:
                seen.append(l)
        cleaned_map[ref] = seen
    delivered_map = defaultdict(list, cleaned_map)

    # Dynamic regex for unsearched-unit audit
    detected_prefixes = set()
    for item in all_searched:
        m = re.match(r'^([A-Z]+)', item)
        if m:
            detected_prefixes.add(m.group(1))

    if detected_prefixes:
        sorted_pfx = sorted(detected_prefixes, key=len, reverse=True)
        pfx_str    = "|".join(re.escape(p) for p in sorted_pfx)
        has_hyphen = any('-' in item for item in all_searched)
        unit_pattern = re.compile(
            r'\b(?:' + pfx_str + (r')\-\d+\b' if has_hyphen else r')\d+\b')
        )
    else:
        unit_pattern = re.compile(r'\b[A-Z]{2,4}\-\d+\b')

    logs = []
    logs.append(
        f"Tiers loaded — {len(actual_delivered)} delivered "
        f"({len(duplicate_refs)} duplicate refs), "
        f"{len(actual_produced)} produced, {len(actual_issued)} issued"
    )

    if duplicate_refs:
        logs.append(f"Duplicate refs detected: {', '.join(sorted(duplicate_refs))}")
        logs.append("  ↳ Will assign load numbers bottom-up by elevation on drawings")

    found_units:           set[str] = set()
    unsearched_units_found: set[str] = set()
    output_files: list[tuple[str, str]] = []

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

                # ------------------------------------------------------------
                # PRE-PASS: collect every delivered instance across the doc,
                # detect elevation for each, then assign load numbers.
                # ------------------------------------------------------------
                # { ref: [ {page_idx, rect, elevation, load_no}, ... ] }
                all_delivered_instances: dict[str, list[dict]] = defaultdict(list)

                for page_idx in range(len(doc)):
                    page       = doc[page_idx]
                    elevations = extract_elevations(page)

                    for ref in actual_delivered:
                        for inst in page.search_for(ref):
                            elev = elevation_for_rect(inst, elevations)
                            all_delivered_instances[ref].append({
                                'page_idx': page_idx,
                                'rect':     inst,
                                'elevation': elev,
                                'load_no':  None,
                            })

                # Assign load numbers
                for ref, instances in all_delivered_instances.items():
                    loads = delivered_map[ref]

                    if len(instances) == 1:
                        # Unambiguous — assign first load directly
                        instances[0]['load_no'] = loads[0] if loads else None

                    else:
                        # Multiple instances — sort bottom-up by elevation.
                        # If all elevations are 0 (no markers found), fall back to
                        # bottom-of-page-first (highest y value = lowest physical position).
                        all_zero = all(i['elevation'] == 0.0 for i in instances)

                        if all_zero:
                            # Density fallback: sort by page then descending y
                            # (bottom of page = likely lower floor)
                            instances.sort(key=lambda i: (i['page_idx'], -i['rect'].y1))
                            logs.append(
                                f"  ↳ No elevation markers found for '{ref}' — "
                                f"using page/position fallback for load assignment"
                            )
                        else:
                            instances.sort(key=lambda i: (i['elevation'], i['page_idx'], i['rect'].y1))

                        for idx, inst in enumerate(instances):
                            inst['load_no'] = loads[idx] if idx < len(loads) and loads[idx] else None

                        # Warn if there are more instances than load entries
                        if len(instances) > len(loads):
                            logs.append(
                                f"  ⚠ '{ref}' has {len(instances)} instances on drawings "
                                f"but only {len(loads)} load entries — "
                                f"{len(instances) - len(loads)} instance(s) left unlabelled"
                            )

                # Group by page for the annotation pass
                by_page: dict[int, list[dict]] = defaultdict(list)
                for instances in all_delivered_instances.values():
                    for inst in instances:
                        by_page[inst['page_idx']].append(inst)

                # ------------------------------------------------------------
                # ANNOTATION PASS
                # ------------------------------------------------------------
                for page_idx in range(len(doc)):
                    page = doc[page_idx]
                    page_protected_rects = []

                    # --- PHASE 1: BLUE (DELIVERED) with load labels ---
                    for inst_data in by_page.get(page_idx, []):
                        inst = inst_data['rect']
                        found_units.add(inst_data.get('ref', ''))

                        annot = page.add_highlight_annot(inst)
                        annot.set_colors(stroke=(0.1, 0.6, 1.0))
                        annot.update()
                        total_highlights += 1
                        page_protected_rects.append(inst)

                        if inst_data['load_no']:
                            insert_load_label(page, inst, inst_data['load_no'])

                    # --- PHASE 2: ORANGE (PRODUCED) ---
                    for ref in actual_produced:
                        for inst in page.search_for(ref):
                            inst_area = abs(inst.width * inst.height)
                            if inst_area > 0 and any(
                                not (inst & p).is_empty and
                                abs((inst & p).width * (inst & p).height) > inst_area * 0.7
                                for p in page_protected_rects
                            ):
                                continue
                            found_units.add(ref)
                            annot = page.add_highlight_annot(inst)
                            annot.set_colors(stroke=(1.0, 0.647, 0.0))
                            annot.update()
                            total_highlights += 1
                            page_protected_rects.append(inst)

                    # --- PHASE 3: YELLOW (ISSUED) ---
                    for ref in actual_issued:
                        for inst in page.search_for(ref):
                            inst_area = abs(inst.width * inst.height)
                            if inst_area > 0 and any(
                                not (inst & p).is_empty and
                                abs((inst & p).width * (inst & p).height) > inst_area * 0.7
                                for p in page_protected_rects
                            ):
                                continue
                            found_units.add(ref)
                            annot = page.add_highlight_annot(inst)
                            annot.set_colors(stroke=(1.0, 1.0, 0.0))
                            annot.update()
                            total_highlights += 1

                    # --- PHASE 4: AUDIT unsearched units ---
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
                import traceback
                logs.append(f"ERROR: {filename} — {e}")
                logs.append(traceback.format_exc()[:400])

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
            'delivered': len(actual_delivered),
            'produced':  len(actual_produced),
            'issued':    len(actual_issued),
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
