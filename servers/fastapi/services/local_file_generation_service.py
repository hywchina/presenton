from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Inches, Pt
from fastapi import HTTPException, UploadFile
from llmai import get_client
from llmai.shared import (
    ImageContentPart,
    JSONSchemaResponse,
    Message,
    SystemMessage,
    UserMessage,
)
from PIL import Image, ImageOps, UnidentifiedImageError
from pathvalidate import sanitize_filename
from pydantic import BaseModel, ValidationError

from models.generated_file_plan import (
    GeneratedDocumentPlan,
    GeneratedDocumentSectionPlan,
    GeneratedFileType,
    GeneratedPresentationPlan,
    GeneratedTablePlan,
)
from utils.asset_directory_utils import (
    filesystem_image_path_to_app_data_url,
    get_exports_directory,
    get_images_directory,
)
from utils.filename_utils import safe_export_basename
from utils.llm_config import get_llm_config
from utils.llm_provider import get_model
from utils.llm_utils import generate_structured_with_schema_retries


LOGGER = logging.getLogger(__name__)
MAX_IMAGE_COUNT = 8
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_EDGE = 2048
UPLOAD_READ_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class PreparedInputImage:
    index: int
    original_name: str
    data: bytes
    mime_type: str
    path: str
    asset_url: str


@dataclass(frozen=True)
class GeneratedLocalFile:
    path: str
    title: str
    media_type: str
    extension: str
    presentation_id: uuid.UUID | None = None


class LocalFileGenerationService:
    async def prepare_images(
        self, uploads: Sequence[UploadFile] | None
    ) -> list[PreparedInputImage]:
        files = list(uploads or [])
        if len(files) > MAX_IMAGE_COUNT:
            raise HTTPException(
                status_code=400,
                detail=f"At most {MAX_IMAGE_COUNT} images are allowed",
            )

        raw_images: list[tuple[str, bytes]] = []
        total_bytes = 0
        for upload in files:
            chunks: list[bytes] = []
            image_bytes = 0
            while chunk := await upload.read(UPLOAD_READ_CHUNK_BYTES):
                image_bytes += len(chunk)
                total_bytes += len(chunk)
                if image_bytes > MAX_IMAGE_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Image {upload.filename or ''} exceeds 20 MB",
                    )
                if total_bytes > MAX_TOTAL_IMAGE_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="Total uploaded image size exceeds 64 MB",
                    )
                chunks.append(chunk)
            data = b"".join(chunks)
            if not data:
                raise HTTPException(status_code=400, detail="Uploaded image is empty")
            raw_images.append((upload.filename or "image", data))

        if not raw_images:
            return []

        normalized_images: list[PreparedInputImage] = []
        for index, (name, data) in enumerate(raw_images):
            # Keep memory bounded by normalizing one image at a time while moving
            # Pillow's CPU-bound codec work off the async request loop.
            normalized_images.append(
                await asyncio.to_thread(
                    self._normalize_and_store_image,
                    index,
                    name,
                    data,
                )
            )
        return normalized_images

    @staticmethod
    def _normalize_and_store_image(
        index: int, original_name: str, source_data: bytes
    ) -> PreparedInputImage:
        try:
            with Image.open(io.BytesIO(source_data)) as source:
                source.seek(0)
                if source.width * source.height > MAX_IMAGE_PIXELS:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Image {original_name} has too many pixels",
                    )
                normalized = ImageOps.exif_transpose(source)
                normalized.thumbnail(
                    (MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS
                )
                if normalized.mode not in {"RGB", "RGBA"}:
                    normalized = normalized.convert(
                        "RGBA" if "A" in normalized.mode else "RGB"
                    )
                output = io.BytesIO()
                normalized.save(output, format="PNG", optimize=True)
                data = output.getvalue()
        except HTTPException:
            raise
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported or invalid image: {original_name}",
            ) from exc

        image_path = os.path.join(get_images_directory(), f"{uuid.uuid4()}.png")
        with open(image_path, "wb") as output_file:
            output_file.write(data)

        return PreparedInputImage(
            index=index,
            original_name=Path(original_name).name,
            data=data,
            mime_type="image/png",
            path=image_path,
            asset_url=filesystem_image_path_to_app_data_url(image_path),
        )

    async def generate_document_plan(
        self,
        *,
        text: str,
        images: Sequence[PreparedInputImage],
        language: str,
        output_type: GeneratedFileType,
    ) -> GeneratedDocumentPlan:
        prompt = (
            "Create a self-contained document plan from the user's text and all supplied "
            "images. Analyze visual meaning, charts, labels, objects and relationships; do "
            "not reduce images to OCR alone. Use only information provided by the user and "
            "the images. Do not browse, cite external URLs, or claim access to the internet.\n"
            f"Output language: {language}. Target file type: {output_type.value}.\n"
            "image_indices are zero-based positions in the supplied image list. Place each "
            "useful image in the most relevant section and provide one caption per supplied "
            "image in image_captions. Keep paragraphs polished and ready for final delivery.\n"
            "User text:\n"
            f"{text.strip() or '(No text supplied; infer the requested document from the images.)'}"
        )
        return await self._generate_plan(
            GeneratedDocumentPlan,
            prompt=prompt,
            images=images,
            response_name="LocalDocumentPlan",
        )

    async def generate_presentation_plan(
        self,
        *,
        text: str,
        images: Sequence[PreparedInputImage],
        language: str,
        n_slides: int,
    ) -> GeneratedPresentationPlan:
        prompt = (
            "Create a presentation content plan from the user's text and all supplied images. "
            "Analyze visual meaning, charts, labels, objects and relationships; do not reduce "
            "images to OCR alone. Use only supplied information. Do not browse or cite external "
            "URLs. Each slide must be audience-facing content, not layout instructions.\n"
            f"Output language: {language}. Generate exactly {n_slides} slides.\n"
            "The first slide is a concise title slide. image_indices are zero-based positions "
            "in the supplied image list. Assign every useful image to a relevant slide and "
            "provide one caption per supplied image in image_captions. content_markdown should "
            "contain concise bullets, facts, or a small Markdown table.\n"
            "User text:\n"
            f"{text.strip() or '(No text supplied; infer the presentation from the images.)'}"
        )
        schema = GeneratedPresentationPlan.model_json_schema()
        schema["properties"]["slides"]["minItems"] = n_slides
        schema["properties"]["slides"]["maxItems"] = n_slides
        return await self._generate_plan(
            GeneratedPresentationPlan,
            prompt=prompt,
            images=images,
            response_name="LocalPresentationPlan",
            schema=schema,
        )

    async def _generate_plan(
        self,
        plan_model: type[BaseModel],
        *,
        prompt: str,
        images: Sequence[PreparedInputImage],
        response_name: str,
        schema: dict[str, Any] | None = None,
    ) -> Any:
        schema = schema or plan_model.model_json_schema()
        content: list[Any] = [prompt]
        content.extend(
            ImageContentPart(data=image.data, mime_type=image.mime_type)
            for image in images
        )
        messages: list[Message] = [
            SystemMessage(
                content=(
                    "You are the only model in a fully offline document-generation system. "
                    "Return valid structured content matching the supplied JSON schema. Never "
                    "request network access and never invent facts not grounded in the input."
                )
            ),
            UserMessage(content=content),
        ]
        response_format = JSONSchemaResponse(
            name=response_name,
            json_schema=schema,
            strict=False,
        )

        try:
            generated = await generate_structured_with_schema_retries(
                get_client(config=get_llm_config()),
                get_model(),
                messages=messages,
                response_format=response_format,
                json_schema=schema,
                validate_schema=True,
                validate_schema_max_loop_count=3,
                max_tokens=2048,
            )
            return plan_model.model_validate(generated)
        except ValidationError as exc:
            raise HTTPException(
                status_code=502,
                detail="Qwen3-VL returned an invalid file plan",
            ) from exc
        except HTTPException:
            raise
        except Exception as exc:
            LOGGER.exception("Local Qwen3-VL file planning failed")
            raise HTTPException(
                status_code=502,
                detail="Local Qwen3-VL file planning failed",
            ) from exc

    def render_markdown(
        self,
        plan: GeneratedDocumentPlan,
        images: Sequence[PreparedInputImage],
        filename: str | None,
    ) -> GeneratedLocalFile:
        lines = [f"# {plan.title}", ""]
        if plan.summary.strip():
            lines.extend([plan.summary.strip(), ""])

        used_images: set[int] = set()
        for section in plan.sections:
            lines.extend(self._markdown_section(section, plan, images, used_images))

        self._append_unplaced_markdown_images(lines, plan, images, used_images)
        path = self._output_path(filename or plan.title, ".md")
        with open(path, "w", encoding="utf-8", newline="\n") as output:
            output.write("\n".join(lines).rstrip() + "\n")
        return GeneratedLocalFile(
            path=path,
            title=plan.title,
            media_type="text/markdown; charset=utf-8",
            extension=".md",
        )

    def render_word(
        self,
        plan: GeneratedDocumentPlan,
        images: Sequence[PreparedInputImage],
        filename: str | None,
    ) -> GeneratedLocalFile:
        document = Document()
        normal_style = document.styles["Normal"]
        normal_style.font.name = "Noto Sans CJK SC"
        normal_style.font.size = Pt(10.5)
        normal_style._element.rPr.rFonts.set(qn("w:eastAsia"), "Noto Sans CJK SC")

        title = document.add_heading(plan.title, level=0)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if plan.summary.strip():
            summary = document.add_paragraph(plan.summary.strip())
            summary.style = document.styles["Quote"]

        used_images: set[int] = set()
        for section in plan.sections:
            document.add_heading(section.heading, level=1)
            for paragraph in section.paragraphs:
                if paragraph.strip():
                    document.add_paragraph(paragraph.strip())
            for bullet in section.bullets:
                if bullet.strip():
                    document.add_paragraph(bullet.strip(), style="List Bullet")
            for table_plan in section.tables:
                self._add_word_table(document, table_plan)
            for image_index in self._valid_image_indices(section.image_indices, images):
                self._add_word_image(document, plan, images[image_index])
                used_images.add(image_index)

        for image in images:
            if image.index not in used_images:
                self._add_word_image(document, plan, image)

        path = self._output_path(filename or plan.title, ".docx")
        document.save(path)
        return GeneratedLocalFile(
            path=path,
            title=plan.title,
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            extension=".docx",
        )

    def presentation_markdown(
        self,
        plan: GeneratedPresentationPlan,
        images: Sequence[PreparedInputImage],
    ) -> list[str]:
        assignments = [
            self._valid_image_indices(slide.image_indices, images)
            for slide in plan.slides
        ]
        assigned = {index for indexes in assignments for index in indexes}
        for image in images:
            if image.index not in assigned:
                target = min(image.index, len(assignments) - 1)
                assignments[target].append(image.index)

        slides: list[str] = []
        for slide, image_indices in zip(plan.slides, assignments):
            lines = [f"## {slide.title}"]
            for image_index in image_indices:
                image = images[image_index]
                caption = self._caption(plan.image_captions, image)
                lines.append(f"![{self._escape_markdown_alt(caption)}]({image.asset_url})")
            lines.append(slide.content_markdown.strip())
            slides.append("\n\n".join(part for part in lines if part))
        return slides

    def _markdown_section(
        self,
        section: GeneratedDocumentSectionPlan,
        plan: GeneratedDocumentPlan,
        images: Sequence[PreparedInputImage],
        used_images: set[int],
    ) -> list[str]:
        lines = [f"## {section.heading}", ""]
        for paragraph in section.paragraphs:
            if paragraph.strip():
                lines.extend([paragraph.strip(), ""])
        for bullet in section.bullets:
            if bullet.strip():
                lines.append(f"- {bullet.strip()}")
        if section.bullets:
            lines.append("")
        for table in section.tables:
            lines.extend(self._markdown_table(table))
        for image_index in self._valid_image_indices(section.image_indices, images):
            image = images[image_index]
            lines.extend([self._markdown_image(plan, image), ""])
            used_images.add(image_index)
        return lines

    def _append_unplaced_markdown_images(
        self,
        lines: list[str],
        plan: GeneratedDocumentPlan,
        images: Sequence[PreparedInputImage],
        used_images: set[int],
    ) -> None:
        remaining = [image for image in images if image.index not in used_images]
        if not remaining:
            return
        lines.extend(["## Images", ""])
        for image in remaining:
            lines.extend([self._markdown_image(plan, image), ""])

    def _markdown_image(
        self, plan: GeneratedDocumentPlan, image: PreparedInputImage
    ) -> str:
        caption = self._caption(plan.image_captions, image)
        encoded = base64.b64encode(image.data).decode("ascii")
        return (
            f"![{self._escape_markdown_alt(caption)}]"
            f"(data:{image.mime_type};base64,{encoded})"
        )

    @staticmethod
    def _markdown_table(table: GeneratedTablePlan) -> list[str]:
        if not table.headers:
            return []
        width = len(table.headers)

        def cells(values: Sequence[str]) -> str:
            normalized = list(values[:width]) + [""] * max(0, width - len(values))
            escaped = [
                value.replace("|", "\\|").replace("\n", " ")
                for value in normalized
            ]
            return "| " + " | ".join(escaped) + " |"

        lines: list[str] = []
        if table.title.strip():
            lines.extend([f"**{table.title.strip()}**", ""])
        lines.extend([cells(table.headers), cells(["---"] * width)])
        lines.extend(cells(row) for row in table.rows)
        lines.append("")
        return lines

    @staticmethod
    def _add_word_table(document: Document, table_plan: GeneratedTablePlan) -> None:
        if not table_plan.headers:
            return
        if table_plan.title.strip():
            title = document.add_paragraph()
            title.add_run(table_plan.title.strip()).bold = True
        table = document.add_table(rows=1, cols=len(table_plan.headers))
        table.style = "Table Grid"
        for index, header in enumerate(table_plan.headers):
            table.rows[0].cells[index].text = header
        for values in table_plan.rows:
            cells = table.add_row().cells
            for index in range(len(table_plan.headers)):
                cells[index].text = values[index] if index < len(values) else ""

    def _add_word_image(
        self,
        document: Document,
        plan: GeneratedDocumentPlan,
        image: PreparedInputImage,
    ) -> None:
        paragraph = document.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = paragraph.add_run()
        run.add_picture(image.path, width=Inches(6.2))
        caption = document.add_paragraph(self._caption(plan.image_captions, image))
        caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
        caption.style = document.styles["Caption"]

    @staticmethod
    def _valid_image_indices(
        indices: Sequence[int], images: Sequence[PreparedInputImage]
    ) -> list[int]:
        valid: list[int] = []
        for index in indices:
            if 0 <= index < len(images) and index not in valid:
                valid.append(index)
        return valid

    @staticmethod
    def _caption(captions: Sequence[str], image: PreparedInputImage) -> str:
        if image.index < len(captions) and captions[image.index].strip():
            return captions[image.index].strip()
        return image.original_name

    @staticmethod
    def _escape_markdown_alt(value: str) -> str:
        return value.replace("[", "\\[").replace("]", "\\]").replace("\n", " ")

    @staticmethod
    def _output_path(name: str, extension: str) -> str:
        base = sanitize_filename(safe_export_basename(name)).strip()
        if base.lower().endswith(extension.lower()):
            base = base[: -len(extension)]
        base = base or "generated-file"
        unique_name = f"{base}-{uuid.uuid4().hex[:8]}{extension}"
        return os.path.join(get_exports_directory(), unique_name)


LOCAL_FILE_GENERATION_SERVICE = LocalFileGenerationService()
