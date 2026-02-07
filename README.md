# Image Folder to Excel Extractor

A local web app that:
- lets you browse and upload a folder of `.jpg/.jpeg/.png` images,
- extracts structured data from each image,
- writes one consolidated Excel sheet.

It supports two OCR modes:
- `Local OCR` (offline, EasyOCR)
- `Gemini API` (cloud)

## Features
- Single Excel output for all uploaded images
- User-defined field extraction into consistent columns (same headers across images)
- Works with handwritten + printed text (best results typically with Gemini)
- Simple local UI with preview and download link

## Setup (uv)

```bash
uv venv
.venv\Scripts\activate
uv sync
```

## Run (uv)

```bash
uv run uvicorn app:app --reload
```

Open `http://127.0.0.1:8000`.

## Gemini mode
- Select `Gemini API` in the UI.
- Enter your Gemini API key in the API key placeholder field (or set env var):

```bash
set GEMINI_API_KEY=your_key_here
```

- Default model is `gemini-2.5-flash` (change in `app.py` if needed).
- In `Fields to extract`, enter comma-separated or one-per-line fields (example: `name, date_of_birth, phone_number`).

## Notes
- Browser folder upload uses `webkitdirectory`; subfolders are ignored logically by this app workflow.
- Only `.jpg/.jpeg/.png` files are processed.
- Output files are saved in `outputs/`.
