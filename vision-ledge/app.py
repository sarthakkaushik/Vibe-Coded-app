from __future__ import annotations

import base64
import io
import json
import os
import re
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Any, AsyncGenerator

import pandas as pd
import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.requests import Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png"}
OUTPUT_DIR = Path("outputs")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = Path("logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "extraction.log"

logger = logging.getLogger("extractor")
if not logger.handlers:
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(file_handler)
logger.setLevel(logging.INFO)


def sanitize_column_name(name: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_ ]+", "", name).strip().lower()
    clean = clean.replace(" ", "_")
    return clean if clean else "unknown_field"


def parse_requested_fields(raw_fields: str) -> Tuple[List[str], Dict[str, str]]:
    chunks = re.split(r"[\r\n,]+", raw_fields)
    ordered_unique: List[str] = []
    label_map: Dict[str, str] = {}
    seen = set()

    for chunk in chunks:
        raw_label = chunk.strip()
        field = sanitize_column_name(raw_label)
        if not field or field == "unknown_field":
            continue
        if field in seen:
            continue
        seen.add(field)
        ordered_unique.append(field)
        label_map[field] = raw_label or field

    return ordered_unique, label_map


def coerce_to_dict(data: object) -> Dict[str, str]:
    if not isinstance(data, dict):
        return {}
    result: Dict[str, str] = {}
    for k, v in data.items():
        key = sanitize_column_name(str(k))
        result[key] = "" if v is None else str(v).strip()
    return result


def parse_text_to_fields(raw_text: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        left, right = line.split(":", 1)
        key = sanitize_column_name(left)
        value = right.strip()
        if key and value:
            fields[key] = value
    return fields


def extract_json_from_markdown(text: str) -> Dict[str, str]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*", "", text)
        text = text.replace("```", "").strip()

    try:
        return coerce_to_dict(json.loads(text))
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return coerce_to_dict(json.loads(match.group(0)))
        except json.JSONDecodeError:
            return {}
    return {}


def enforce_requested_schema(parsed: Dict[str, str], requested_fields: List[str]) -> Dict[str, str]:
    return {field: parsed.get(field, "") for field in requested_fields}


def build_gemini_prompt(requested_fields: List[str], field_labels: Dict[str, str]) -> str:
    requested_lines = "\n".join(
        [f'- "{key}": capture value for "{field_labels.get(key, key)}"' for key in requested_fields]
    )
    schema_hint = "{\n" + "\n".join([f'  "{f}": ""' for f in requested_fields]) + "\n}"

    return (
        "You extract structured data from a single uploaded image.\n\n"
        "Task\n"
        "Extract only the requested fields and return one strict JSON object.\n\n"
        "Requested fields (JSON keys and meanings)\n"
        f"{requested_lines}\n\n"
        "Rules\n"
        "1) Read all visible text, including handwritten and printed text.\n"
        "2) Keep values exactly as seen where possible; avoid normalization unless necessary.\n"
        "3) If a value is missing, illegible, or uncertain, return an empty string.\n"
        "4) Do not invent values.\n"
        "5) Output must be valid JSON with exactly the requested keys and no extras.\n"
        "6) No markdown, no commentary, JSON only.\n\n"
        "Required JSON shape\n"
        f"{schema_hint}"
    )


def normalize_mode(ocr_mode: str) -> str:
    mode = ocr_mode.lower().strip()
    if mode not in {"local", "gemini"}:
        raise HTTPException(status_code=400, detail="Invalid OCR mode selected.")
    return mode


def get_provider(mode: str, gemini_api_key: str):
    if mode == "local":
        return LocalOCR()

    api_key = gemini_api_key.strip() or os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=400, detail="Gemini API key is required.")
    return GeminiOCR(api_key=api_key)


def make_output_dataframe(
    rows: List[Dict[str, str]], requested_fields: List[str], field_labels: Dict[str, str]
) -> Tuple[pd.DataFrame, List[str], Dict[str, str]]:
    df = pd.DataFrame(rows)
    rename_map = {key: field_labels.get(key, key) for key in requested_fields}
    df = df.rename(columns=rename_map)

    base_columns = ["file_name", "ocr_mode", "processed_at"]
    display_fields = [rename_map.get(field, field) for field in requested_fields]
    ordered_columns = base_columns + display_fields + [c for c in df.columns if c not in (base_columns + display_fields)]
    df = df[ordered_columns]
    return df, ordered_columns, rename_map


async def process_single_upload(
    file: UploadFile,
    mode: str,
    provider: Any,
    requested_fields: List[str],
    field_labels: Dict[str, str],
    run_id: str,
) -> Tuple[Dict[str, str] | None, str | None, str | None, str]:
    extension = Path(file.filename or "").suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        logger.info("file_skipped | run_id=%s | file=%s | reason=unsupported_extension", run_id, file.filename)
        return None, None, file.filename or "unknown", "skipped"

    image_bytes = await file.read()
    if not image_bytes:
        logger.info("file_skipped | run_id=%s | file=%s | reason=empty_file", run_id, file.filename)
        row = {
            "file_name": file.filename or "unknown",
            "ocr_mode": mode,
            "processed_at": datetime.now().isoformat(timespec="seconds"),
            **{field: "" for field in requested_fields},
            "error": "Empty file",
        }
        return row, None, row["file_name"], "error"

    mime_type = "image/jpeg" if extension in {".jpg", ".jpeg"} else "image/png"
    image_data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('utf-8')}"

    try:
        if mode == "gemini":
            extracted = provider.extract(image_bytes, mime_type, requested_fields, field_labels)
        else:
            raw = provider.extract(image_bytes)
            extracted = enforce_requested_schema(raw, requested_fields)
        status = "ok"
    except Exception as exc:
        logger.exception("file_failed | run_id=%s | file=%s", run_id, file.filename)
        extracted = {field: "" for field in requested_fields}
        extracted["error"] = str(exc)
        status = "error"

    row: Dict[str, str] = {
        "file_name": file.filename or "unknown",
        "ocr_mode": mode,
        "processed_at": datetime.now().isoformat(timespec="seconds"),
    }
    row.update(extracted)
    logger.info("file_processed | run_id=%s | file=%s", run_id, row["file_name"])
    return row, image_data_url, row["file_name"], status


def process_buffered_upload(
    filename: str,
    image_bytes: bytes,
    mode: str,
    provider: Any,
    requested_fields: List[str],
    field_labels: Dict[str, str],
    run_id: str,
) -> Tuple[Dict[str, str] | None, str | None, str, str]:
    extension = Path(filename or "").suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        logger.info("file_skipped | run_id=%s | file=%s | reason=unsupported_extension", run_id, filename)
        return None, None, filename or "unknown", "skipped"

    if not image_bytes:
        logger.info("file_skipped | run_id=%s | file=%s | reason=empty_file", run_id, filename)
        row = {
            "file_name": filename or "unknown",
            "ocr_mode": mode,
            "processed_at": datetime.now().isoformat(timespec="seconds"),
            **{field: "" for field in requested_fields},
            "error": "Empty file",
        }
        return row, None, row["file_name"], "error"

    mime_type = "image/jpeg" if extension in {".jpg", ".jpeg"} else "image/png"
    image_data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('utf-8')}"

    try:
        if mode == "gemini":
            extracted = provider.extract(image_bytes, mime_type, requested_fields, field_labels)
        else:
            raw = provider.extract(image_bytes)
            extracted = enforce_requested_schema(raw, requested_fields)
        status = "ok"
    except Exception as exc:
        logger.exception("file_failed | run_id=%s | file=%s", run_id, filename)
        extracted = {field: "" for field in requested_fields}
        extracted["error"] = str(exc)
        status = "error"

    row: Dict[str, str] = {
        "file_name": filename or "unknown",
        "ocr_mode": mode,
        "processed_at": datetime.now().isoformat(timespec="seconds"),
    }
    row.update(extracted)
    logger.info("file_processed | run_id=%s | file=%s", run_id, row["file_name"])
    return row, image_data_url, row["file_name"], status


class LocalOCR:
    def __init__(self) -> None:
        try:
            import easyocr  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("easyocr is not installed. Install dependencies and retry.") from exc
        self.reader = easyocr.Reader(["en"], gpu=False)

    def extract(self, image_bytes: bytes) -> Dict[str, str]:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        result = self.reader.readtext(image)
        lines: List[str] = []
        for item in result:
            if len(item) >= 2:
                text = str(item[1]).strip()
                if text:
                    lines.append(text)
        raw_text = "\n".join(lines)
        fields = parse_text_to_fields(raw_text)
        fields["raw_text"] = raw_text
        return fields


class GeminiOCR:
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash") -> None:
        self.api_key = api_key
        self.model = model.strip() or "gemini-2.5-flash"

    def extract(
        self,
        image_bytes: bytes,
        mime_type: str,
        requested_fields: List[str],
        field_labels: Dict[str, str],
    ) -> Dict[str, str]:
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        prompt = build_gemini_prompt(requested_fields, field_labels)

        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": base64.b64encode(image_bytes).decode("utf-8"),
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0,
                "response_mime_type": "application/json",
                "response_schema": {
                    "type": "object",
                    "properties": {field: {"type": "string"} for field in requested_fields},
                    "required": requested_fields,
                },
            },
        }

        response = requests.post(endpoint, params={"key": self.api_key}, json=payload, timeout=90)
        if response.status_code != 200:
            raise RuntimeError(f"Gemini API error ({response.status_code}): {response.text[:300]}")

        body = response.json()
        candidates = body.get("candidates", [])
        if not candidates:
            return {field: "" for field in requested_fields}

        parts = candidates[0].get("content", {}).get("parts", [])
        text_chunks: List[str] = []
        for part in parts:
            if "text" in part:
                text_chunks.append(part["text"])
        joined = "\n".join(text_chunks)
        parsed = extract_json_from_markdown(joined)
        return enforce_requested_schema(parsed, requested_fields)


app = FastAPI(title="Image-to-Excel Extractor", version="1.1.0")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/process")
async def process_images(
    files: List[UploadFile] = File(...),
    ocr_mode: str = Form(...),
    fields_to_extract: str = Form(...),
    gemini_api_key: str = Form(default=""),
):
    if not files:
        raise HTTPException(status_code=400, detail="Please upload at least one image.")

    requested_fields, field_labels = parse_requested_fields(fields_to_extract)
    if not requested_fields:
        raise HTTPException(
            status_code=400,
            detail="Please provide fields to extract (comma-separated), e.g. name, dob, phone.",
        )

    mode = normalize_mode(ocr_mode)
    provider = get_provider(mode, gemini_api_key)

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    logger.info(
        "run_start | run_id=%s | mode=%s | uploaded_files=%s | requested_fields=%s",
        run_id,
        mode,
        len(files),
        requested_fields,
    )

    rows: List[Dict[str, str]] = []
    for file in files:
        row, _, _, status = await process_single_upload(file, mode, provider, requested_fields, field_labels, run_id)
        if row is None and status == "skipped":
            continue
        if row is not None:
            rows.append(row)

    if not rows:
        raise HTTPException(status_code=400, detail="No valid .jpg/.jpeg/.png files were processed.")

    df, ordered_columns, _ = make_output_dataframe(rows, requested_fields, field_labels)

    filename = f"extracted_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    output_path = OUTPUT_DIR / filename
    df.to_excel(output_path, index=False)
    logger.info(
        "run_complete | run_id=%s | rows=%s | output_excel=%s | log_file=%s",
        run_id,
        len(df),
        output_path,
        LOG_FILE,
    )

    preview = df.head(10).fillna("").to_dict(orient="records")
    return {
        "message": "Processing complete.",
        "rows": len(df),
        "output_file": filename,
        "download_url": f"/download/{filename}",
        "preview": preview,
        "columns": list(df.columns),
        "log_file": str(LOG_FILE),
        "log_download_url": "/logs/download",
    }


@app.post("/process-stream")
async def process_images_stream(
    files: List[UploadFile] = File(...),
    ocr_mode: str = Form(...),
    fields_to_extract: str = Form(...),
    gemini_api_key: str = Form(default=""),
):
    if not files:
        raise HTTPException(status_code=400, detail="Please upload at least one image.")

    requested_fields, field_labels = parse_requested_fields(fields_to_extract)
    if not requested_fields:
        raise HTTPException(
            status_code=400,
            detail="Please provide fields to extract (comma-separated), e.g. name, dob, phone.",
        )

    mode = normalize_mode(ocr_mode)
    provider = get_provider(mode, gemini_api_key)

    # Buffer uploads before streaming starts; request-scoped UploadFile objects can be closed
    # by the framework once the response body iteration begins.
    buffered_files: List[Tuple[str, bytes]] = []
    for file in files:
        filename = file.filename or "unknown"
        extension = Path(filename).suffix.lower()
        if extension not in SUPPORTED_EXTENSIONS:
            continue
        image_bytes = await file.read()
        if not image_bytes:
            buffered_files.append((filename, b""))
            continue
        buffered_files.append((filename, image_bytes))

    total_files = len(buffered_files)
    if total_files == 0:
        raise HTTPException(status_code=400, detail="No valid .jpg/.jpeg/.png files were uploaded.")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    logger.info(
        "run_start_stream | run_id=%s | mode=%s | uploaded_files=%s | requested_fields=%s",
        run_id,
        mode,
        len(files),
        requested_fields,
    )

    async def event_stream() -> AsyncGenerator[str, None]:
        rows: List[Dict[str, str]] = []
        processed = 0
        display_columns = ["file_name", "ocr_mode", "processed_at"] + [field_labels.get(field, field) for field in requested_fields] + ["error"]
        start_payload = {
            "type": "start",
            "total_files": total_files,
            "columns": display_columns,
        }
        yield json.dumps(start_payload) + "\n"

        for filename, image_bytes in buffered_files:
            row, image_data_url, file_name, status = process_buffered_upload(
                filename, image_bytes, mode, provider, requested_fields, field_labels, run_id
            )
            if row is None and status == "skipped":
                continue
            if row is None:
                continue

            rows.append(row)
            processed += 1

            live_row = {
                "file_name": row.get("file_name", ""),
                "ocr_mode": row.get("ocr_mode", ""),
                "processed_at": row.get("processed_at", ""),
            }
            for field in requested_fields:
                live_row[field_labels.get(field, field)] = row.get(field, "")
            live_row["error"] = row.get("error", "")

            progress_payload = {
                "type": "progress",
                "processed": processed,
                "total_files": total_files,
                "file_name": file_name,
                "status": status,
                "row": live_row,
                "image_data_url": image_data_url,
            }
            yield json.dumps(progress_payload) + "\n"

        if not rows:
            yield json.dumps({"type": "fatal", "detail": "No valid .jpg/.jpeg/.png files were processed."}) + "\n"
            return

        df, ordered_columns, _ = make_output_dataframe(rows, requested_fields, field_labels)
        filename = f"extracted_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        output_path = OUTPUT_DIR / filename
        df.to_excel(output_path, index=False)
        logger.info(
            "run_complete_stream | run_id=%s | rows=%s | output_excel=%s | log_file=%s",
            run_id,
            len(df),
            output_path,
            LOG_FILE,
        )

        done_payload = {
            "type": "complete",
            "rows": len(df),
            "output_file": filename,
            "download_url": f"/download/{filename}",
            "columns": ordered_columns,
            "log_download_url": "/logs/download",
        }
        yield json.dumps(done_payload) + "\n"

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


@app.get("/logs/download")
async def download_log_file():
    if not LOG_FILE.exists():
        raise HTTPException(status_code=404, detail="Log file not found")
    return FileResponse(
        path=str(LOG_FILE),
        filename=f"extraction_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
        media_type="text/plain",
    )


@app.get("/download/{filename}")
async def download_file(filename: str):
    path = OUTPUT_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        path=str(path),
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
