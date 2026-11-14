"""OpenAPI completeness (frontend/docs/QA_FRONTEND.md item 3): valid spec, typed responses, documented errors."""

from __future__ import annotations

import pytest

from app.main import app


@pytest.fixture(scope="module")
def spec():
    from fastapi.testclient import TestClient

    r = TestClient(app).get("/openapi.json")
    assert r.status_code == 200
    return r.json()


def test_spec_is_valid_openapi(spec):
    validator = pytest.importorskip("openapi_spec_validator")
    validator.validate(spec)


def _success(op: dict) -> tuple[str, dict]:
    code = next(c for c in op["responses"] if c.startswith("2"))
    return code, op["responses"][code]


def _resolve(spec: dict, schema: dict) -> dict:
    while "$ref" in schema:
        schema = spec["components"]["schemas"][schema["$ref"].split("/")[-1]]
    return schema


def test_every_json_response_is_typed(spec):
    untyped = []
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            code, resp = _success(op)
            if code == "204":
                continue
            content = resp.get("content") or {}
            if "application/json" not in content:
                assert content, (method, path)  # file downloads declare their media type
                continue
            schema = _resolve(spec, content["application/json"].get("schema") or {})
            typed = (
                schema.get("properties") or schema.get("items") or schema.get("oneOf") or schema.get("anyOf")
            )
            if not typed and schema.get("type") != "string":
                untyped.append(f"{method.upper()} {path}")
    assert not untyped, untyped


def test_exports_declare_media_types(spec):
    assert "text/csv" in spec["paths"]["/v1/reports/export.csv"]["get"]["responses"]["200"]["content"]
    xlsx = spec["paths"]["/v1/reports/export.xlsx"]["get"]["responses"]["200"]["content"]
    assert "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" in xlsx
    assert "text/csv" in spec["paths"]["/v1/contacts/export.csv"]["get"]["responses"]["200"]["content"]


def test_booking_routes_document_conflicts_and_not_found(spec):
    for path, method in [("/v1/bookings", "post"), ("/v1/bookings/{booking_id}", "patch")]:
        responses = spec["paths"][path][method]["responses"]
        assert {"404", "409"} <= set(responses), (path, method)
        assert "SLOT_UNAVAILABLE" in responses["409"]["description"]
    assert "checked_in" in spec["paths"]["/v1/bookings/{booking_id}"]["patch"]["description"]


def test_money_and_percent_semantics_documented(spec):
    schemas = spec["components"]["schemas"]
    kpi = schemas["KpiOut"]["properties"]
    money = kpi["revenue"]["anyOf"][0]
    assert money["type"] == "string" and "pattern" in money
    assert "0–100" in kpi["conversion_to_booking"]["description"]
    assert schemas["PaymentOut"]["properties"]["amount"]["type"] == "string"
    created = schemas["WebhookSubscriptionCreated"]["properties"]
    assert "secret" in created and "secret" not in schemas["WebhookSubscriptionOut"]["properties"]
