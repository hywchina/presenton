import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

from starlette.requests import Request

from api.v1.file_generation.router import generate_file
from models.generated_file_plan import GeneratedFileType
from services.local_file_generation_service import GeneratedLocalFile


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/generate-file",
            "headers": [],
            "query_string": b"",
            "server": ("127.0.0.1", 80),
            "client": ("127.0.0.1", 12345),
            "scheme": "http",
        }
    )


def test_generate_file_requires_text_or_images():
    try:
        asyncio.run(
            generate_file(
                request=_request(),
                text="",
                output_type=GeneratedFileType.WORD,
                images=None,
                filename=None,
                language="Chinese",
                n_slides=6,
                template="general",
                sql_session=SimpleNamespace(),
            )
        )
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 400
    else:
        raise AssertionError("empty input should fail")


def test_generate_markdown_returns_file_response(tmp_path, monkeypatch):
    output = tmp_path / "result.md"
    output.write_text("# generated\n", encoding="utf-8")
    service = SimpleNamespace(
        prepare_images=AsyncMock(return_value=[]),
        generate_document_plan=AsyncMock(return_value=SimpleNamespace(title="Report")),
        render_markdown=lambda *_args: GeneratedLocalFile(
            path=str(output),
            title="Report",
            media_type="text/markdown; charset=utf-8",
            extension=".md",
        ),
    )
    monkeypatch.setattr(
        "api.v1.file_generation.router.LOCAL_FILE_GENERATION_SERVICE", service
    )

    response = asyncio.run(
        generate_file(
            request=_request(),
            text="Generate a report",
            output_type=GeneratedFileType.MARKDOWN,
            images=None,
            filename="offline-report",
            language="English",
            n_slides=6,
            template="general",
            sql_session=SimpleNamespace(),
        )
    )

    assert response.path == str(output)
    assert response.media_type == "text/markdown; charset=utf-8"
    assert response.headers["x-generated-file-type"] == "md"
    assert "offline-report.md" in response.headers["content-disposition"]
    service.generate_document_plan.assert_awaited_once()
