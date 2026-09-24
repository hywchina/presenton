from enum import Enum

from pydantic import BaseModel, Field


class GeneratedFileType(str, Enum):
    WORD = "word"
    MARKDOWN = "md"
    POWERPOINT = "ppt"


class GeneratedTablePlan(BaseModel):
    title: str = Field(default="", max_length=200)
    headers: list[str] = Field(default_factory=list, max_length=12)
    rows: list[list[str]] = Field(default_factory=list, max_length=30)


class GeneratedDocumentSectionPlan(BaseModel):
    heading: str = Field(..., min_length=1, max_length=200)
    paragraphs: list[str] = Field(default_factory=list, max_length=12)
    bullets: list[str] = Field(default_factory=list, max_length=20)
    tables: list[GeneratedTablePlan] = Field(default_factory=list, max_length=5)
    image_indices: list[int] = Field(default_factory=list, max_length=8)


class GeneratedDocumentPlan(BaseModel):
    title: str = Field(..., min_length=1, max_length=240)
    summary: str = Field(..., min_length=1, max_length=2000)
    sections: list[GeneratedDocumentSectionPlan] = Field(
        ..., min_length=1, max_length=30
    )
    image_captions: list[str] = Field(default_factory=list, max_length=8)


class GeneratedPresentationSlidePlan(BaseModel):
    title: str = Field(..., min_length=1, max_length=180)
    content_markdown: str = Field(..., min_length=1, max_length=5000)
    image_indices: list[int] = Field(default_factory=list, max_length=8)


class GeneratedPresentationPlan(BaseModel):
    title: str = Field(..., min_length=1, max_length=240)
    slides: list[GeneratedPresentationSlidePlan] = Field(
        ..., min_length=1, max_length=20
    )
    image_captions: list[str] = Field(default_factory=list, max_length=8)
