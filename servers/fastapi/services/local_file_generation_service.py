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
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
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
from pptx import Presentation
from pptx.dml.color import RGBColor as PptxRGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches as PptxInches
from pptx.util import Pt as PptxPt

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


@dataclass(frozen=True)
class GeneratedReportMetadata:
    project_name: str = ""
    report_type: str = ""
    requested_by: str = ""
    generated_date: str = ""


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
            "Create a self-contained professional report plan from the user's text and all "
            "supplied images. Preserve the supplied title, chapter order, named concepts, "
            "qualifications and pending checks. Analyze visible image content, but treat it "
            "as design evidence rather than proof of dimensions, performance or compliance. "
            "Use only information explicitly provided by the user or directly visible in the "
            "images. Never add metrics, standards, regulatory compliance, test conclusions, "
            "dates, benefits, capabilities or project outcomes that are not supplied. Do not "
            "browse, cite external URLs, or claim access to the internet.\n"
            f"Output language: {language}. Target file type: {output_type.value}.\n"
            "image_indices are zero-based positions in the supplied image list. Place each "
            "useful image in the most relevant section and provide one caption per supplied "
            "image in image_captions. Use factual captions. Avoid repeating the same claim. "
            "Write direct, specific paragraphs. Use bullets only for actions, comparisons or "
            "checks already present in the input. Keep each section concise and ready for "
            "final delivery.\n"
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
            "Preserve the supplied title, chapter sequence, qualifications and pending checks. "
            "Analyze visible image content, but never infer dimensions, performance, compliance, "
            "standards, dates, benefits, capabilities or completed outcomes that the input does "
            "not state. Use only supplied information. Do not browse or cite external URLs. "
            "Each slide must be audience-facing content, not layout instructions.\n"
            f"Output language: {language}. Generate exactly {n_slides} slides.\n"
            "The first slide is a concise title slide. image_indices are zero-based positions "
            "in the supplied image list. Assign each useful image to exactly one relevant slide "
            "and provide one factual caption per supplied image in image_captions. Do not create "
            "generic showcase slides or repeat material to fill space. content_markdown should "
            "state the main point first, then use at most four concise bullets grounded in the "
            "input. If the requested slide count exceeds the supplied chapters, use an overview "
            "or next-step slide without inventing new content.\n"
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
        metadata: GeneratedReportMetadata | None = None,
    ) -> GeneratedLocalFile:
        metadata = metadata or GeneratedReportMetadata()
        lines = [f"# {plan.title}", ""]
        info_rows = [
            ("项目", metadata.project_name),
            ("报告类型", metadata.report_type),
            ("生成日期", metadata.generated_date),
            ("编制人", metadata.requested_by),
        ]
        info_rows = [(label, value) for label, value in info_rows if value.strip()]
        if info_rows:
            lines.extend(["## 报告信息", "", "| 字段 | 内容 |", "| --- | --- |"])
            lines.extend(
                f"| {label} | {value.replace('|', '&#124;')} |"
                for label, value in info_rows
            )
            lines.append("")
        if plan.summary.strip():
            lines.extend(["## 执行摘要", "", plan.summary.strip(), ""])

        lines.extend(["## 章节导航", ""])
        lines.extend(
            f"{index}. [{section.heading}](#{index}-{self._markdown_anchor(section.heading)})"
            for index, section in enumerate(plan.sections, start=1)
        )
        lines.append("")

        used_images: set[int] = set()
        figure_number = 1
        for section_index, section in enumerate(plan.sections, start=1):
            section_lines, figure_number = self._markdown_section(
                section,
                plan,
                images,
                used_images,
                section_index=section_index,
                figure_number=figure_number,
            )
            lines.extend(section_lines)

        self._append_unplaced_markdown_images(
            lines, plan, images, used_images, figure_number
        )
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
        metadata: GeneratedReportMetadata | None = None,
    ) -> GeneratedLocalFile:
        metadata = metadata or GeneratedReportMetadata()
        document = Document()
        section = document.sections[0]
        section.page_height = Inches(11.69)
        section.page_width = Inches(8.27)
        section.top_margin = Inches(0.78)
        section.bottom_margin = Inches(0.72)
        section.left_margin = Inches(0.82)
        section.right_margin = Inches(0.82)
        section.different_first_page_header_footer = True
        self._configure_word_header_footer(section)

        normal_style = document.styles["Normal"]
        normal_style.font.name = "Noto Sans CJK SC"
        normal_style.font.size = Pt(11)
        normal_style._element.rPr.rFonts.set(qn("w:eastAsia"), "Noto Sans CJK SC")
        normal_style.paragraph_format.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
        normal_style.paragraph_format.space_after = Pt(6)

        for style_name, size in (("Title", 30), ("Heading 1", 18), ("Heading 2", 14)):
            style = document.styles[style_name]
            style.font.name = "Noto Sans CJK SC"
            style.font.size = Pt(size)
            style.font.color.rgb = RGBColor(0x1F, 0x29, 0x37)
            style._element.rPr.rFonts.set(qn("w:eastAsia"), "Noto Sans CJK SC")
            style.paragraph_format.keep_with_next = True

        document.add_paragraph().paragraph_format.space_after = Pt(62)
        report_type = metadata.report_type.strip()
        if report_type:
            kicker = document.add_paragraph()
            kicker.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = kicker.add_run(report_type)
            run.bold = True
            run.font.color.rgb = RGBColor(0xB5, 0x12, 0x1B)
            run.font.size = Pt(12)

        title = document.add_paragraph(style="Title")
        title.add_run(plan.title)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        title.paragraph_format.space_before = Pt(18)
        title.paragraph_format.space_after = Pt(28)
        for value in (metadata.project_name, metadata.generated_date, metadata.requested_by):
            if value.strip():
                paragraph = document.add_paragraph(value.strip())
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                paragraph.runs[0].font.color.rgb = RGBColor(0x66, 0x70, 0x85)
                paragraph.runs[0].font.size = Pt(11)
        document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

        if any(
            value.strip()
            for value in (
                metadata.project_name,
                metadata.report_type,
                metadata.generated_date,
                metadata.requested_by,
            )
        ):
            document.add_heading("报告信息", level=1)
            info_table = document.add_table(rows=0, cols=2)
            info_table.style = "Table Grid"
            info_table.autofit = False
            for label, value in (
                ("项目", metadata.project_name),
                ("报告类型", metadata.report_type),
                ("生成日期", metadata.generated_date),
                ("编制人", metadata.requested_by),
            ):
                if not value.strip():
                    continue
                cells = info_table.add_row().cells
                cells[0].width = Inches(1.35)
                cells[1].width = Inches(5.3)
                cells[0].text = label
                cells[1].text = value.strip()
                cells[0].vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                cells[1].vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                self._shade_word_cell(cells[0], "F3F4F6")
                cells[0].paragraphs[0].runs[0].bold = True

        if plan.summary.strip():
            document.add_heading("执行摘要", level=1)
            summary = document.add_paragraph(plan.summary.strip())
            summary.paragraph_format.first_line_indent = Inches(0.3)

        document.add_heading("章节导航", level=1)
        for section_index, report_section in enumerate(plan.sections, start=1):
            paragraph = document.add_paragraph()
            number = paragraph.add_run(f"{section_index:02d}")
            number.bold = True
            number.font.color.rgb = RGBColor(0xB5, 0x12, 0x1B)
            paragraph.add_run(f"  {report_section.heading}")

        used_images: set[int] = set()
        figure_number = 1
        for section_index, report_section in enumerate(plan.sections, start=1):
            if report_section.image_indices:
                document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
            document.add_heading(
                f"{section_index}. {report_section.heading}", level=1
            )
            for paragraph in report_section.paragraphs:
                if paragraph.strip():
                    body = document.add_paragraph(paragraph.strip())
                    body.paragraph_format.first_line_indent = Inches(0.3)
            for bullet in report_section.bullets:
                if bullet.strip():
                    bullet_paragraph = document.add_paragraph(
                        bullet.strip(), style="List Bullet"
                    )
                    bullet_paragraph.paragraph_format.keep_together = True
            for table_plan in report_section.tables:
                self._add_word_table(document, table_plan)
            for image_index in self._valid_image_indices(
                report_section.image_indices, images
            ):
                self._add_word_image(
                    document, plan, images[image_index], figure_number
                )
                figure_number += 1
                used_images.add(image_index)

        for image in images:
            if image.index not in used_images:
                self._add_word_image(document, plan, image, figure_number)
                figure_number += 1

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
        # The first slide is always the controlled cover. Any image the model assigns
        # there is redistributed to a content slide instead of being silently lost.
        if assignments:
            assignments[0] = []
        assigned = {index for indexes in assignments for index in indexes}
        for image in images:
            if image.index not in assigned:
                target = min(image.index, len(assignments) - 1)
                assignments[target].append(image.index)

        seen: set[int] = set()
        unique_assignments: list[list[int]] = []
        for indexes in assignments:
            unique_indexes: list[int] = []
            for index in indexes:
                if index in seen:
                    continue
                seen.add(index)
                unique_indexes.append(index)
            unique_assignments.append(unique_indexes)
        assignments = unique_assignments

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

    def render_presentation(
        self,
        plan: GeneratedPresentationPlan,
        images: Sequence[PreparedInputImage],
        filename: str | None,
        metadata: GeneratedReportMetadata | None = None,
    ) -> GeneratedLocalFile:
        """Render a deterministic, editable PPTX without downstream content invention."""
        metadata = metadata or GeneratedReportMetadata()
        presentation = Presentation()
        presentation.slide_width = PptxInches(13.333)
        presentation.slide_height = PptxInches(7.5)
        blank_layout = presentation.slide_layouts[6]

        assignments = self._unique_presentation_image_assignments(plan, images)
        LOGGER.info(
            "Rendering controlled presentation with %d slides, %d images and assignments %s",
            len(plan.slides),
            len(images),
            assignments,
        )
        for slide_index, slide_plan in enumerate(plan.slides):
            slide = presentation.slides.add_slide(blank_layout)
            if slide_index == 0:
                self._add_presentation_cover(slide, plan.title, metadata)
                continue
            self._add_presentation_chrome(
                slide,
                slide_index + 1,
                metadata.report_type,
            )
            self._add_presentation_title(slide, slide_plan.title)
            image_indices = assignments[slide_index]
            body_lines = self._presentation_body_lines(slide_plan.content_markdown)
            if image_indices:
                self._add_presentation_images(
                    slide,
                    plan,
                    images,
                    image_indices,
                )
                self._add_presentation_body(
                    slide,
                    body_lines,
                    x=7.35,
                    y=1.6,
                    width=5.15,
                    height=4.95,
                )
            else:
                self._add_presentation_body(
                    slide,
                    body_lines,
                    x=0.85,
                    y=1.65,
                    width=11.65,
                    height=4.9,
                )

        path = self._output_path(filename or plan.title, ".pptx")
        presentation.save(path)
        return GeneratedLocalFile(
            path=path,
            title=plan.title,
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation"
            ),
            extension=".pptx",
        )

    def _unique_presentation_image_assignments(
        self,
        plan: GeneratedPresentationPlan,
        images: Sequence[PreparedInputImage],
    ) -> list[list[int]]:
        assignments = [
            self._valid_image_indices(slide.image_indices, images)
            for slide in plan.slides
        ]
        if assignments:
            assignments[0] = []
        assigned = {index for indexes in assignments for index in indexes}
        content_slide_count = max(1, len(assignments) - 1)
        for image in images:
            if image.index in assigned:
                continue
            target = min(
                len(assignments) - 1,
                1 + min(image.index, content_slide_count - 1),
            )
            assignments[target].append(image.index)

        seen: set[int] = set()
        for slide_index, indexes in enumerate(assignments):
            unique: list[int] = []
            for index in indexes:
                if index in seen:
                    continue
                seen.add(index)
                unique.append(index)
            assignments[slide_index] = unique[:2]
        return assignments

    @staticmethod
    def _add_presentation_cover(slide, title: str, metadata: GeneratedReportMetadata):
        background = slide.background.fill
        background.solid()
        background.fore_color.rgb = PptxRGBColor(0x1F, 0x29, 0x37)
        accent = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE,
            PptxInches(0.75),
            PptxInches(0.75),
            PptxInches(1.3),
            PptxInches(0.08),
        )
        accent.fill.solid()
        accent.fill.fore_color.rgb = PptxRGBColor(0xB5, 0x12, 0x1B)
        accent.line.fill.background()
        LocalFileGenerationService._add_presentation_text(
            slide,
            metadata.report_type or "项目设计报告",
            0.75,
            1.12,
            6.5,
            0.4,
            size=16,
            color="FCA5A5",
            bold=True,
        )
        LocalFileGenerationService._add_presentation_text(
            slide,
            title,
            0.75,
            1.78,
            11.5,
            1.55,
            size=34 if len(title) <= 22 else 28,
            color="FFFFFF",
            bold=True,
            valign=MSO_ANCHOR.MIDDLE,
        )
        LocalFileGenerationService._add_presentation_text(
            slide,
            metadata.project_name,
            0.75,
            4.18,
            8.0,
            0.4,
            size=20,
            color="D0D5DD",
        )
        footer = " · ".join(
            value
            for value in (metadata.generated_date, metadata.requested_by)
            if value
        )
        LocalFileGenerationService._add_presentation_text(
            slide,
            footer,
            0.75,
            4.78,
            8.0,
            0.3,
            size=12,
            color="98A2B3",
        )
        LocalFileGenerationService._add_presentation_text(
            slide,
            "轨道客室智能设计平台",
            0.75,
            6.75,
            4.0,
            0.25,
            size=10,
            color="98A2B3",
        )

    @staticmethod
    def _add_presentation_chrome(slide, page_number: int, report_type: str):
        line = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE,
            PptxInches(0.55),
            PptxInches(7.1),
            PptxInches(0.75),
            PptxInches(0.025),
        )
        line.fill.solid()
        line.fill.fore_color.rgb = PptxRGBColor(0xB5, 0x12, 0x1B)
        line.line.fill.background()
        LocalFileGenerationService._add_presentation_text(
            slide,
            report_type or "项目设计报告",
            0.55,
            0.42,
            5.0,
            0.25,
            size=10,
            color="B5121B",
            bold=True,
        )
        LocalFileGenerationService._add_presentation_text(
            slide,
            "轨道客室智能设计平台",
            1.45,
            6.98,
            4.0,
            0.25,
            size=9,
            color="667085",
        )
        LocalFileGenerationService._add_presentation_text(
            slide,
            str(page_number),
            12.45,
            6.98,
            0.35,
            0.25,
            size=9,
            color="667085",
            align=PP_ALIGN.RIGHT,
        )

    @staticmethod
    def _add_presentation_title(slide, title: str):
        LocalFileGenerationService._add_presentation_text(
            slide,
            title,
            0.72,
            0.72,
            11.9,
            0.65,
            size=28 if len(title) <= 28 else 24,
            color="1F2937",
            bold=True,
        )

    def _add_presentation_images(
        self,
        slide,
        plan: GeneratedPresentationPlan,
        images: Sequence[PreparedInputImage],
        image_indices: Sequence[int],
    ):
        count = len(image_indices)
        image_height = 4.3 if count == 1 else 2.0
        for position, image_index in enumerate(image_indices):
            image = images[image_index]
            top = 1.62 + position * 2.35
            self._add_contained_picture(
                slide,
                image.path,
                x=0.72,
                y=top,
                width=6.05,
                height=image_height,
            )
            self._add_presentation_text(
                slide,
                self._caption(plan.image_captions, image),
                0.72,
                top + image_height + 0.08,
                6.05,
                0.28,
                size=9,
                color="667085",
                align=PP_ALIGN.CENTER,
            )

    @staticmethod
    def _add_contained_picture(slide, path: str, *, x, y, width, height):
        with Image.open(path) as source:
            ratio = source.width / max(1, source.height)
        target_ratio = width / height
        if ratio >= target_ratio:
            picture_width = width
            picture_height = width / ratio
        else:
            picture_height = height
            picture_width = height * ratio
        slide.shapes.add_picture(
            path,
            PptxInches(x + (width - picture_width) / 2),
            PptxInches(y + (height - picture_height) / 2),
            width=PptxInches(picture_width),
            height=PptxInches(picture_height),
        )

    @staticmethod
    def _presentation_body_lines(markdown: str) -> list[str]:
        lines: list[str] = []
        for raw_line in markdown.splitlines():
            line = raw_line.strip().lstrip("#").strip()
            line = line.removeprefix("- ").removeprefix("* ").strip()
            if line and line not in lines:
                lines.append(line)
        return lines[:5] or ["本页内容以输入资料为准。"]

    @staticmethod
    def _add_presentation_body(
        slide,
        lines: Sequence[str],
        *,
        x: float,
        y: float,
        width: float,
        height: float,
    ):
        box = slide.shapes.add_textbox(
            PptxInches(x),
            PptxInches(y),
            PptxInches(width),
            PptxInches(height),
        )
        frame = box.text_frame
        frame.clear()
        frame.word_wrap = True
        frame.margin_left = PptxInches(0.08)
        frame.margin_right = PptxInches(0.04)
        frame.margin_top = PptxInches(0.04)
        frame.margin_bottom = PptxInches(0.04)
        for index, line in enumerate(lines):
            paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
            paragraph.text = line
            paragraph.level = 0
            paragraph.space_after = PptxPt(14)
            paragraph.font.name = "Noto Sans CJK SC"
            paragraph.font.size = PptxPt(17 if len(line) < 90 else 15)
            paragraph.font.color.rgb = PptxRGBColor(0x34, 0x41, 0x54)

    @staticmethod
    def _add_presentation_text(
        slide,
        text: str,
        x: float,
        y: float,
        width: float,
        height: float,
        *,
        size: int,
        color: str,
        bold: bool = False,
        align=PP_ALIGN.LEFT,
        valign=MSO_ANCHOR.TOP,
    ):
        box = slide.shapes.add_textbox(
            PptxInches(x),
            PptxInches(y),
            PptxInches(width),
            PptxInches(height),
        )
        frame = box.text_frame
        frame.clear()
        frame.word_wrap = True
        frame.vertical_anchor = valign
        paragraph = frame.paragraphs[0]
        paragraph.alignment = align
        run = paragraph.add_run()
        run.text = text
        run.font.name = "Noto Sans CJK SC"
        run.font.size = PptxPt(size)
        run.font.bold = bold
        run.font.color.rgb = PptxRGBColor.from_string(color)

    def _markdown_section(
        self,
        section: GeneratedDocumentSectionPlan,
        plan: GeneratedDocumentPlan,
        images: Sequence[PreparedInputImage],
        used_images: set[int],
        *,
        section_index: int,
        figure_number: int,
    ) -> tuple[list[str], int]:
        lines = [f"## {section_index}. {section.heading}", ""]
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
            caption = self._caption(plan.image_captions, image)
            lines.extend(
                [
                    self._markdown_image(plan, image),
                    "",
                    f"*图 {figure_number} {caption}*",
                    "",
                ]
            )
            figure_number += 1
            used_images.add(image_index)
        return lines, figure_number

    def _append_unplaced_markdown_images(
        self,
        lines: list[str],
        plan: GeneratedDocumentPlan,
        images: Sequence[PreparedInputImage],
        used_images: set[int],
        figure_number: int,
    ) -> None:
        remaining = [image for image in images if image.index not in used_images]
        if not remaining:
            return
        lines.extend(["## 附录 图片资料", ""])
        for image in remaining:
            caption = self._caption(plan.image_captions, image)
            lines.extend(
                [
                    self._markdown_image(plan, image),
                    "",
                    f"*图 {figure_number} {caption}*",
                    "",
                ]
            )
            figure_number += 1

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
            table.rows[0].cells[index].vertical_alignment = (
                WD_CELL_VERTICAL_ALIGNMENT.CENTER
            )
            LocalFileGenerationService._shade_word_cell(
                table.rows[0].cells[index], "1F2937"
            )
            for run in table.rows[0].cells[index].paragraphs[0].runs:
                run.bold = True
                run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        for values in table_plan.rows:
            cells = table.add_row().cells
            for index in range(len(table_plan.headers)):
                cells[index].text = values[index] if index < len(values) else ""
                cells[index].vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER

    def _add_word_image(
        self,
        document: Document,
        plan: GeneratedDocumentPlan,
        image: PreparedInputImage,
        figure_number: int,
    ) -> None:
        paragraph = document.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.paragraph_format.keep_with_next = True
        run = paragraph.add_run()
        with Image.open(image.path) as source:
            ratio = source.width / max(1, source.height)
        width = 6.25
        height = width / ratio
        if height > 6.6:
            height = 6.6
            width = height * ratio
        run.add_picture(image.path, width=Inches(width), height=Inches(height))
        caption = document.add_paragraph(
            f"图 {figure_number} {self._caption(plan.image_captions, image)}"
        )
        caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
        caption.style = document.styles["Caption"]
        caption.paragraph_format.keep_together = True

    @staticmethod
    def _configure_word_header_footer(section) -> None:
        header = section.header
        header.is_linked_to_previous = False
        header_paragraph = header.paragraphs[0]
        header_paragraph.text = "轨道客室智能设计平台  项目设计报告"
        header_paragraph.runs[0].font.color.rgb = RGBColor(0x66, 0x70, 0x85)
        header_paragraph.runs[0].font.size = Pt(9)

        footer = section.footer
        footer.is_linked_to_previous = False
        footer_paragraph = footer.paragraphs[0]
        footer_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = footer_paragraph.add_run("第 ")
        run.font.color.rgb = RGBColor(0x66, 0x70, 0x85)
        page = OxmlElement("w:fldSimple")
        page.set(qn("w:instr"), "PAGE")
        footer_paragraph._p.append(page)
        footer_paragraph.add_run(" 页")

        section.first_page_header.is_linked_to_previous = False
        section.first_page_header.paragraphs[0].text = ""
        section.first_page_footer.is_linked_to_previous = False
        section.first_page_footer.paragraphs[0].text = ""

    @staticmethod
    def _shade_word_cell(cell, fill: str) -> None:
        properties = cell._tc.get_or_add_tcPr()
        shading = properties.find(qn("w:shd"))
        if shading is None:
            shading = OxmlElement("w:shd")
            properties.append(shading)
        shading.set(qn("w:fill"), fill)

    @staticmethod
    def _markdown_anchor(value: str) -> str:
        return "-".join(value.lower().split())

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
