# PDF to Excel/Mongo (Ubuntu Batch)

This project converts many voter-list PDFs into:
- one combined Excel file
- optional MongoDB documents
- raw OCR text files for debugging

## Visual Flow

```text
pdfs/*.pdf
   |
   v
PDF page render (pdf2image or pypdfium2)
   |
   v
Image preprocess (OpenCV: grayscale + threshold)
   |
   v
OCR (Tesseract: mar+eng)
   |
   v
Regex parsing -> structured voter records
   |                     |
   |                     +--> output/raw_text/<pdf_name>.txt
   v
output/all_voters.xlsx
   |
   +--> optional MongoDB upsert
```

## Folder Structure

```text
PDFtoWebsite/
├── pdfs/                  # Put all input PDF files here
├── output/
│   └── raw_text/          # OCR text dump per PDF
├── setup_ubuntu.sh        # One-time Ubuntu setup
├── run_batch.py           # Main batch runner
├── pdfToMongo.py          # Existing single-file runner (kept)
└── README.md
```

## 1) Ubuntu Initial Setup

Run once:

```bash
chmod +x setup_ubuntu.sh
./setup_ubuntu.sh
```

## 2) Add Your PDFs

Copy all PDFs into `pdfs/`.

Example:

```bash
cp /path/to/your/*.pdf ./pdfs/
```

## 3) Run Batch Conversion

Activate env:

```bash
source .venv/bin/activate
```

Quick test (fast):

```bash
python run_batch.py --input-dir pdfs --excel output/all_voters.xlsx --max-pages 3 --dpi 180
```

Full run:

```bash
python run_batch.py --input-dir pdfs --excel output/all_voters.xlsx
```

## 4) Optional MongoDB Save

If MongoDB is running:

```bash
python run_batch.py \
  --input-dir pdfs \
  --excel output/all_voters.xlsx \
  --mongo-uri "mongodb://localhost:27017/" \
  --mongo-db voter_db \
  --mongo-collection voters
```

## Outputs

- Combined Excel: `output/all_voters.xlsx`
- Raw OCR text: `output/raw_text/<pdfname>.txt`
- MongoDB records (optional): `voter_db.voters`

## Performance Notes

- Start with `--max-pages 3` to validate pipeline quickly.
- Use `--dpi 150` or `--dpi 180` for faster throughput.
- `mar+eng` is slower than `eng` only, but needed for Marathi PDFs.

## Troubleshooting

- If OCR language error appears:
  - install Marathi pack: `sudo apt install tesseract-ocr-mar`
- If Poppler missing:
  - install: `sudo apt install poppler-utils`
- If no rows parsed:
  - check corresponding `output/raw_text/*.txt` and adjust parser regex.
