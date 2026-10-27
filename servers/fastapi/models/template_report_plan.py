"""Content-only contract. Office templates and final rendering remain in Rail."""
from pydantic import BaseModel, ConfigDict, Field, model_validator


class TemplateImageSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=80)
    caption: str = Field(default="", max_length=300)
    name: str = Field(default="", max_length=255)


class TemplateSectionSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(ge=0, le=7)
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(default="", max_length=10_000)
    images: list[TemplateImageSource] = Field(default_factory=list, max_length=20)


class TemplateReportSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(default="", max_length=2000)
    project_name: str = Field(min_length=1, max_length=200)
    sections: list[TemplateSectionSource] = Field(min_length=1, max_length=8)
    template_rules: str = Field(default="", max_length=12_000)
    preview_ids: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def unique_sections(self):
        if len({section.id for section in self.sections}) != len(self.sections):
            raise ValueError("Section IDs must be unique")
        known = {image.id for section in self.sections for image in section.images}
        if len(set(self.preview_ids)) != len(self.preview_ids) or not set(self.preview_ids) <= known:
            raise ValueError("Preview IDs must be unique source image IDs")
        return self


class TemplateSectionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(ge=0, le=7)
    body: str = Field(max_length=10_000)
    image_ids: list[str] = Field(default_factory=list, max_length=20)


class TemplateReportPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sections: list[TemplateSectionPlan] = Field(min_length=1, max_length=8)
