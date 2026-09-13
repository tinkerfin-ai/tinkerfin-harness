"""Public user submissions retain authorized attachments without temporary IDs."""

import pytest
from pydantic import ValidationError

from tinkerfin import AgUiUserInput
from tinkerfin_contracts.media import Attachment


def test_submission_replaces_untrusted_descriptors_without_changing_text_order():
    file = Attachment(
        id="a", name="report.pdf", mime_type="application/pdf", size_bytes=8
    )
    submission = AgUiUserInput.model_validate(
        {
            "role": "user",
            "name": "reader",
            "custom": {"label": "keep"},
            "content": [
                {"type": "text", "text": "before"},
                {
                    "type": "document",
                    "source": {"type": "url", "value": "attachment:a"},
                    "metadata": {"id": "forged"},
                    "annotation": "retain-me",
                },
                {"type": "text", "text": "after"},
            ],
        }
    )
    assert submission.text == "beforeafter"
    assert submission.attachment_ids == ("a",)
    with pytest.raises(ValidationError):
        _ = submission.attachments
    authorized = submission.with_attachments([file])
    assert authorized.attachments == (file,)
    assert authorized.text == submission.text
    assert authorized.name == "reader"
    assert authorized.model_dump()["content"][1]["annotation"] == "retain-me"
    assert authorized.model_dump()["custom"] == {"label": "keep"}
    assert "id" not in authorized.model_dump()
    with pytest.raises(ValidationError):
        _ = submission.attachments


@pytest.mark.parametrize("ids", [[], ["other"], ["a", "a"]])
def test_authorized_descriptors_must_cover_exactly_the_requested_files(ids):
    submission = AgUiUserInput.model_validate(
        {
            "content": [
                {"type": "image", "source": {"type": "url", "value": "attachment:a"}}
            ],
        }
    )
    files = [
        Attachment(id=value, name="a.png", mime_type="image/png", size_bytes=1)
        for value in ids
    ]
    with pytest.raises(ValueError, match="exactly cover"):
        submission.with_attachments(files)


def test_submission_requires_no_placeholder_and_rejects_message_identity():
    submission = AgUiUserInput(content="hello")
    assert submission.text == "hello"
    assert submission.attachment_ids == ()
    assert submission.attachments == ()
    with pytest.raises(ValidationError, match="message ID"):
        AgUiUserInput.model_validate({"id": "pending", "content": "hello"})


def test_multiple_authorized_attachments_keep_submission_order():
    files = [
        Attachment(id="image", name="chart.png", mime_type="image/png", size_bytes=4),
        Attachment(
            id="report", name="report.pdf", mime_type="application/pdf", size_bytes=8
        ),
    ]
    submission = AgUiUserInput.model_validate(
        {
            "content": [
                {
                    "type": "image"
                    if file.mime_type.startswith("image/")
                    else "document",
                    "source": {"type": "url", "value": f"attachment:{file.id}"},
                }
                for file in files
            ],
        }
    )
    authorized = submission.with_attachments(list(reversed(files)))
    assert authorized.attachment_ids == ("image", "report")
    assert authorized.attachments == tuple(files)
    assert authorized.text == ""
