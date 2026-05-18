import os
import re
import fitz  # PyMuPDF
from flask import Flask, request, jsonify, send_file, render_template
from werkzeug.utils import secure_filename
import tempfile
import zipfile
from collections import defaultdict

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200MB max upload

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/process', methods=['POST'])
def process():
    issued_raw   = request.form.get('issued', '')
    produced_raw = request.form.get('produced', '')
    delivered_raw = request.form.get('delivered', '')
    files        = request.files.getlist('pdfs')

    delivered_map = defaultdict(list)
    for line in delivered_raw.splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in re.split(r'\t+|\s{2,}', line.strip()) if p.strip()]
        if len(parts) >= 2:
            ref = parts[0]
            load = parts[1]
        elif len(parts) == 1:
            ref = parts[0]
            load = ""
        else:
            continue
            
        if ref:
            delivered_map[ref].append(load)

    issued_set   = {l.strip() for l in issued_raw.splitlines()   if l.strip()}
    produced_set = {l.strip() for l in produced_raw.splitlines() if l.strip()}
    actual_delivered = set(delivered_map.keys())

    actual_produced  = produced_set - actual_delivered
    actual_issued    = issued_set - produced_set - actual_delivered

    all_searched = actual_delivered | actual_produced | actual_issued

    if not all_searched:
        return jsonify({'error': 'All lists are empty. Paste references first.'}), 400
    if not files or files[0].filename == '':
        return jsonify({'error': 'No PDF files selected.'}), 400

    detected_prefixes = set()
    for item in all_searched:
        m = re.match(r'^([A-Z]+)', item)
        if m:
            detected_prefixes.add(m.group(1))

    if detected_prefixes:
        sorted_prefixes = sorted(detected_prefixes, key=len, reverse=True)
        prefix_str = "|".join(re.escape(p) for p in sorted_prefixes)
        has_hyphen = any('-' in item for item in all_searched)
        if has_hyphen:
            unit_pattern = re.compile(r'\b(?:' + prefix_str + r')\-\d+\b')
        else:
            unit_pattern = re.compile(r'\b(?:' + prefix_str + r')\d+\b')
    else:
        unit_pattern = re.compile(r'\b[A-Z]{2,4}\-\d+\b')

    logs = []
    logs.append(f"Tiers loaded — {len(actual_delivered)} delivered, {len(actual_produced)} produced, {len(actual_issued)} issued")

    found_units = set()
    unsearched_units_found = set()
    output_files = []

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

                delivered_instances = []
                elev_regex = re.compile(r'\+(\d+)')

                for page_idx in range(len(doc)):
                    page = doc[page_idx]
                    page_blocks = page.get_text("blocks")
                    
                    elevations = []
                    for block in page_blocks:
                        text = block[4]
                        for m in elev_regex.findall(text):
                            elevations.append({
                                'value': int(m),
                                'y': (block[1] + block[3]) / 2
                            })

                    for ref in delivered_map.keys():
                        matches = page.search_for(ref)
                        for inst in matches:
                            match_y = (inst.y0 + inst.y1) / 2
                            match_x = (inst.x0 + inst.x1) / 2

                            if elevations:
                                closest_elev = min(elevations, key=lambda e: abs(e['y'] - match_y))
                                elev_val = closest_elev['value']
                            else:
                                elev_val = 0

                            delivered_instances.append({
                                'ref': ref,
                                'page_idx': page_idx,
                                'rect': inst,
                                'elevation': elev_val,
                                'y': match_y,
                                'x': match_x,
                                'load_no': None
                            })

                instances_by_ref = defaultdict(list)
                for inst in delivered_instances:
                    instances_by_ref[inst['ref']].append(inst)

                assigned_delivered_highlights = defaultdict(list)
                
                for ref, inst_list in instances_by_ref.items():
                    inst_list.sort(key=lambda i: (i['elevation'], i['page_idx'], i['y'], i['x']))
                    
                    loads = delivered_map[ref]
                    for idx, inst in enumerate(inst_list):
                        if idx < len(loads) and loads[idx]:
                            inst['load_no'] = loads[idx]
                        assigned_delivered_highlights[inst['page_idx']].append(inst)

                for page_idx in range(len(doc)):
                    page = doc[page_idx]
                    page_protected_rects = []

                    page_delivered = assigned_delivered_highlights.get(page_idx, [])
                    for inst_data in page_delivered:
                        inst = inst_data['rect']
                        ref = inst_data['ref']
                        found_units.add(ref)

                        annot = page.add_highlight_annot(inst)
                        annot.set_colors(stroke=(0.1, 0.6, 1.0))
                        annot.update()
                        total_highlights += 1
                        page_protected_rects.append(inst)

                        if inst_data['load_no']:
                            font_size = max(7, inst.height * 0.85)
                            point = fitz.Point(inst.x1 + 4, inst.y0 + (inst.height / 2) + (font_size / 3))
                            page.insert_text(point, f"L-{inst_data['load_no']}", fontsize=font_size, color=(0.0, 0.2, 0.65), overlay=True)

                    for ref in actual_produced:
                        matches = page.search_for(ref)
                        if matches:
                            for inst in matches:
                                inst_area = abs(inst.width * inst.height)
                                overlap = False
                                if inst_area > 0:
                                    for p_rect in page_protected_rects:
                                        intersect = inst & p_rect
                                        if not intersect.is_empty:
                                            if abs(intersect.width * intersect.height) > inst_area * 0.7:
                                                overlap = True
                                                break
                                if overlap:
                                    continue
                                found_units.add(ref)
                                annot = page.add_highlight_annot(inst)
                                annot.set_colors(stroke=(1.0, 0.647, 0.0))
                                annot.update()
                                total_highlights += 1
                                page_protected_rects.append(inst)

                    for ref in actual_issued:
                        matches = page.search_for(ref)
                        if matches:
                            for inst in matches:
                                inst_area = abs(inst.width * inst.height)
                                overlap = False
                                if inst_area > 0:
                                    for p_rect in page_protected_rects:
                                        intersect = inst & p_rect
                                        if not intersect.is_empty:
                                            if abs(intersect.width * intersect.height) > inst_area * 0.7:
                                                overlap = True
                                                break
                                if overlap:
                                    continue
                                found_units.add(ref)
                                annot = page.add_highlight_annot(inst)
                                annot.set_colors(stroke=(1.0, 1.0, 0.0))
                                annot.update()
                                total_highlights += 1

                    page_text = page.get_text("text")
                    for mark in unit_pattern.findall(page_text):
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

        not_found = sorted(all_searched - found_units)
        unsearched = sorted(unsearched_units_found)

        if output_files:
            zip_path = os.path.join(tmpdir, 'marked_drawings.zip')
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for name, path in output_files:
                    zf.write(path, name)

            with open(zip_path, 'rb') as f:
                zip_bytes = f.read()
        else:
            zip_bytes = None

    result = {
        'logs': logs,
        'not_found': not_found,
        'unsearched': unsearched,
        'stats': {
            'delivered': len(actual_delivered),
            'produced': len(actual_produced),
            'issued': len(actual_issued),
        },
        'has_output': zip_bytes is not None,
    }

    if zip_bytes:
        import base64
        result['zip_b64'] = base64.b64encode(zip_bytes).decode('utf-8')
        result['zip_filename'] = 'marked_drawings.zip'

    return jsonify(result)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
