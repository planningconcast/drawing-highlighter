import os
import re
import fitz  # PyMuPDF
from flask import Flask, request, jsonify, send_file, render_template
from werkzeug.utils import secure_filename
import tempfile
import zipfile

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

    issued_set   = {l.strip() for l in issued_raw.splitlines()   if l.strip()}
    produced_set = {l.strip() for l in produced_raw.splitlines() if l.strip()}
    delivered_set = {l.strip() for l in delivered_raw.splitlines() if l.strip()}

    all_searched = issued_set | produced_set | delivered_set

    if not all_searched:
        return jsonify({'error': 'All lists are empty. Paste references first.'}), 400
    if not files or files[0].filename == '':
        return jsonify({'error': 'No PDF files selected.'}), 400

    # Progressive tier deduplication
    actual_delivered = delivered_set
    actual_produced  = produced_set - actual_delivered
    actual_issued    = issued_set - produced_set - actual_delivered

    # Dynamic regex inference from input prefixes
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

                for page in doc:
                    page_protected_rects = []

                    # PHASE 1: BLUE (DELIVERED)
                    for ref in actual_delivered:
                        matches = page.search_for(ref)
                        if matches:
                            found_units.add(ref)
                            for inst in matches:
                                annot = page.add_highlight_annot(inst)
                                annot.set_colors(stroke=(0.1, 0.6, 1.0))
                                annot.update()
                                total_highlights += 1
                                page_protected_rects.append(inst)

                    # PHASE 2: ORANGE (PRODUCED)
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

                    # PHASE 3: YELLOW (ISSUED)
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

                    # PHASE 4: AUDIT
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

        # Package outputs into a zip
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
        # Store temporarily for download
        import base64
        result['zip_b64'] = base64.b64encode(zip_bytes).decode('utf-8')
        result['zip_filename'] = 'marked_drawings.zip'

    return jsonify(result)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
