import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from models.template_report_plan import TemplateReportPlan, TemplateReportSource
from services.template_report_plan_service import generate_template_report_plan, validate_template_plan


def source():
    return TemplateReportSource.model_validate({
        "title": "设计报告", "project_name": "测试项目", "summary": "尚待验证",
        "sections": [
            {"id": 0, "title": "约束", "body": "通道暂定 800 mm，尚未验证。", "images": [{"id": "0:0", "caption": "示意"}]},
            {"id": 1, "title": "后续", "body": "待完成样件验证。", "images": []},
        ],
    })


def plan():
    return TemplateReportPlan.model_validate({"sections": [
        {"id": 0, "body": "通道暂定 800 mm，仍需验证。", "image_ids": ["0:0"]},
        {"id": 1, "body": "样件验证仍待完成。", "image_ids": []},
    ]})


def test_exact_chapter_order_and_required_images():
    assert validate_template_plan(source(), plan()) == plan()
    changed = plan()
    changed.sections.reverse()
    with pytest.raises(HTTPException):
        validate_template_plan(source(), changed)
    changed = plan()
    changed.sections[0].image_ids = []
    with pytest.raises(HTTPException):
        validate_template_plan(source(), changed)
    changed.sections[0].image_ids = ["foreign-image"]
    with pytest.raises(HTTPException):
        validate_template_plan(source(), changed)
    changed.sections[0].image_ids = ["0:0", "0:0"]
    with pytest.raises(HTTPException):
        validate_template_plan(source(), changed)


def test_missing_content_and_unknown_fields_are_rejected():
    changed = plan()
    changed.sections[0].body = ""
    with pytest.raises(HTTPException):
        validate_template_plan(source(), changed)
    with pytest.raises(ValidationError):
        TemplateReportSource.model_validate({**source().model_dump(), "execute": "anything"})
    with pytest.raises(ValidationError):
        TemplateReportSource.model_validate({**source().model_dump(), "preview_ids": ["foreign-image"]})


def test_each_chapter_gets_its_own_bounded_ai_request(monkeypatch):
    response = plan()
    generate = AsyncMock(side_effect=[
        TemplateReportPlan(sections=[response.sections[0]]),
        TemplateReportPlan(sections=[response.sections[1]]),
    ])
    monkeypatch.setattr("services.template_report_plan_service.LOCAL_FILE_GENERATION_SERVICE._generate_plan", generate)
    result = asyncio.run(generate_template_report_plan(source(), []))
    assert result == response
    assert generate.await_count == 2
    schema = generate.call_args_list[0].kwargs["schema"]["$defs"]["TemplateSectionPlan"]["properties"]
    assert schema["id"]["const"] == 0
    assert schema["image_ids"]["items"]["enum"] == ["0:0"]
    assert "uniqueItems" not in schema["image_ids"]  # Current vLLM grammar compatibility; server still rejects duplicates.
    assert "untrusted reference" in generate.call_args_list[0].kwargs["prompt"]


def test_preview_pixels_must_match_declared_source_ids():
    original = source()
    original.preview_ids = ["0:0"]
    with pytest.raises(HTTPException) as error:
        asyncio.run(generate_template_report_plan(original, []))
    assert error.value.status_code == 400


def test_numeric_substring_does_not_satisfy_original_constraint():
    changed = plan()
    changed.sections[0].body = "通道暂定为1800 mm，仍待验证。"
    with pytest.raises(HTTPException):
        validate_template_plan(source(), changed)


def test_preview_file_is_cleaned_when_planning_fails(tmp_path, monkeypatch):
    from api.v1.file_generation.router import template_report_plan
    from services.local_file_generation_service import PreparedInputImage
    path = tmp_path / "preview.png"
    path.write_bytes(b"local-test-preview")
    image = PreparedInputImage(index=0, original_name="preview.png", data=b"pixels", mime_type="image/png", path=str(path), asset_url="/app_data/images/test.png")
    monkeypatch.setattr("api.v1.file_generation.router.LOCAL_FILE_GENERATION_SERVICE.prepare_images", AsyncMock(return_value=[image]))
    monkeypatch.setattr("services.template_report_plan_service.generate_template_report_plan", AsyncMock(side_effect=HTTPException(502, "test failure")))
    with pytest.raises(HTTPException):
        asyncio.run(template_report_plan(source=source().model_dump_json(), images=[]))
    assert not path.exists()


def test_long_chapter_keeps_all_text_and_image_positions_in_bounded_requests(monkeypatch):
    import json
    original = source()
    original.sections = [original.sections[0]]
    original.sections[0].body = '通道暂定800 mm，尚待验证。' * 100
    original.sections[0].images[0].caption = '示意说明' * 70
    requests = []

    async def echo(_model, **kwargs):
        payload = json.loads(next(line for line in kwargs['prompt'].splitlines() if line.startswith('{')))
        chapter = payload['sections'][0]
        assert len(chapter['body']) <= 600
        requests.append(payload)
        return TemplateReportPlan.model_validate({'sections': [{
            'id': chapter['id'], 'body': chapter['body'],
            'image_ids': [image['id'] for image in chapter['images']],
        }]})

    monkeypatch.setattr('services.template_report_plan_service.LOCAL_FILE_GENERATION_SERVICE._generate_plan', echo)
    result = asyncio.run(generate_template_report_plan(original, []))
    assert len(requests) >= 3
    assert result.sections[0].body.replace('\n', '') == original.sections[0].body
    assert result.sections[0].image_ids == ['0:0']
    assert len(original.sections[0].images[0].caption) == 280


def test_domain_repair_retries_once_instead_of_accepting_missing_numbers(monkeypatch):
    original = source()
    original.sections = [original.sections[0]]
    invalid = plan()
    invalid.sections = [invalid.sections[0]]
    invalid.sections[0].body = '仍待验证，但漏掉原文数值。'
    valid = plan()
    valid.sections = [valid.sections[0]]
    generate = AsyncMock(side_effect=[invalid, valid])
    monkeypatch.setattr('services.template_report_plan_service.LOCAL_FILE_GENERATION_SERVICE._generate_plan', generate)
    result = asyncio.run(generate_template_report_plan(original, []))
    assert '800' in result.sections[0].body
    assert generate.await_count == 2
    assert 'failed validation' in generate.call_args_list[1].kwargs['prompt']
