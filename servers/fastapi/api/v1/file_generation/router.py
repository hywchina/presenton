from __future__ import annotations

import os
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pathvalidate import sanitize_filename
from sqlalchemy.ext.asyncio import AsyncSession

from models.generated_file_plan import GeneratedFileType
from services.database import get_async_session
from services.local_file_generation_service import (
    GeneratedLocalFile,
    GeneratedReportMetadata,
    LOCAL_FILE_GENERATION_SERVICE,
)
from utils.filename_utils import safe_export_basename


FILE_GENERATION_ROUTER = APIRouter(
    prefix="/api/v1/generate-file",
    tags=["Offline File Generation"],
)


@FILE_GENERATION_ROUTER.post("")
async def generate_file(
    request: Request,
    text: Annotated[str, Form()] = "",
    output_type: Annotated[GeneratedFileType, Form(alias="type")] = GeneratedFileType.WORD,
    images: Annotated[list[UploadFile] | None, File()] = None,
    filename: Annotated[str | None, Form()] = None,
    language: Annotated[str, Form()] = "Chinese",
    n_slides: Annotated[int, Form(ge=1, le=20)] = 6,
    template: Annotated[str, Form()] = "general",
    project_name: Annotated[str, Form()] = "",
    report_type: Annotated[str, Form()] = "",
    requested_by: Annotated[str, Form()] = "",
    generated_date: Annotated[str, Form()] = "",
    sql_session: AsyncSession = Depends(get_async_session),
):
    """Generate one DOCX, Markdown, or PPTX file using the configured local VLM."""
    if not text.strip() and not images:
        raise HTTPException(
            status_code=400,
            detail="At least one of text or images is required",
        )
    if not language.strip():
        raise HTTPException(status_code=400, detail="language cannot be empty")

    prepared_images = await LOCAL_FILE_GENERATION_SERVICE.prepare_images(images)
    metadata = GeneratedReportMetadata(
        project_name=project_name.strip(),
        report_type=report_type.strip(),
        requested_by=requested_by.strip(),
        generated_date=generated_date.strip(),
    )

    if output_type in {GeneratedFileType.WORD, GeneratedFileType.MARKDOWN}:
        plan = await LOCAL_FILE_GENERATION_SERVICE.generate_document_plan(
            text=text,
            images=prepared_images,
            language=language.strip(),
            output_type=output_type,
        )
        generated = (
            LOCAL_FILE_GENERATION_SERVICE.render_word(
                plan, prepared_images, filename, metadata
            )
            if output_type == GeneratedFileType.WORD
            else LOCAL_FILE_GENERATION_SERVICE.render_markdown(
                plan, prepared_images, filename, metadata
            )
        )
        return _generated_file_response(generated, filename)

    plan = await LOCAL_FILE_GENERATION_SERVICE.generate_presentation_plan(
        text=text,
        images=prepared_images,
        language=language.strip(),
        n_slides=n_slides,
    )
    generated = LOCAL_FILE_GENERATION_SERVICE.render_presentation(
        plan,
        prepared_images,
        filename,
        metadata,
    )
    return _generated_file_response(generated, filename)


def _generated_file_response(
    generated: GeneratedLocalFile, requested_filename: str | None
) -> FileResponse:
    if not os.path.isfile(generated.path):
        raise HTTPException(status_code=500, detail="Generated file was not found")

    requested = (requested_filename or generated.title).strip() or "generated-file"
    requested = sanitize_filename(safe_export_basename(requested)) or "generated-file"
    if not requested.lower().endswith(generated.extension):
        requested += generated.extension

    headers = {"X-Generated-File-Type": generated.extension.removeprefix(".")}
    if generated.presentation_id is not None:
        headers["X-Presentation-ID"] = str(generated.presentation_id)
    return FileResponse(
        generated.path,
        media_type=generated.media_type,
        filename=requested,
        headers=headers,
    )
