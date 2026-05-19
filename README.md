# Drawing Highlighter — Web App

Multi-priority PDF annotation & audit tool. Replaces the tkinter desktop app with a browser UI.

## Setup

```bash
pip install -r requirements.txt
python app.py
```

Then open **http://localhost:5000** in your browser.

## Usage

1. Paste unit references into the three columns (Issued / Produced / Delivered)
2. Click the file zone to select one or more PDF drawings
3. Hit **Run priority highlight & drawing audit**
4. When complete, click **Download marked drawings** to get a ZIP of the annotated PDFs

## Highlight colours

| Colour | Stage | Priority |
|--------|-------|----------|
| Blue   | Delivered | Highest — never overwritten |
| Orange | Produced (not delivered) | Middle |
| Yellow | Issued (not produced/delivered) | Lowest |

## Audit outputs

- **Not found in drawings** — references from your lists that had no match in any PDF
- **Unsearched units spotted** — unit codes detected on drawings that aren't in any of your lists

## Notes

- Output files are named `MARKED_<original>.pdf` and bundled into a ZIP for download
- The app infers a dynamic regex from your input prefixes — no manual pattern config needed
- Maximum upload size: 200 MB
