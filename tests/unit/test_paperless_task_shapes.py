"""Post-ship amendment (2026-09-02): paperless 3.x task payload shape.

Live finding on paperless-ngx 3.1.1: a finished consume task reports
`status: "success"` (lowercase), `result_data: {"document_id": N}` and
`related_document_ids: [N]` — there is no `related_document` field, so
`docs_file` raised "succeeded without a document id" after a successful
upload. Properties: the client resolves the document id from any of the
three shapes (2.x `related_document` wins), and the human message from
`result` / `message` / `result_data.{message,error,result}`.
"""

from __future__ import annotations

from ember.records.paperless import _task_message, _task_related_document


def test_related_document_2x_shape() -> None:
    assert _task_related_document({"related_document": "12"}) == 12


def test_related_document_3x_result_data() -> None:
    item = {"status": "success", "result_data": {"document_id": 66}, "related_document_ids": [66]}
    assert _task_related_document(item) == 66


def test_related_document_3x_ids_list_only() -> None:
    assert _task_related_document({"related_document_ids": [7]}) == 7


def test_related_document_absent() -> None:
    assert _task_related_document({"status": "PENDING"}) is None
    assert _task_related_document({"related_document_ids": [], "result_data": {}}) is None


def test_message_precedence_and_fallbacks() -> None:
    assert _task_message({"result": "dup of #3", "message": "x"}) == "dup of #3"
    assert _task_message({"message": "waiting"}) == "waiting"
    assert _task_message({"result_data": {"message": "Not consuming: duplicate of #9"}}) == (
        "Not consuming: duplicate of #9"
    )
    assert _task_message({"result_data": {"error": "boom"}}) == "boom"
    assert _task_message({"result_data": "plain"}) == "plain"
    assert _task_message({"result": None}) == ""
