import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import (
    admin_tasks,
    audit_api,
    auth,
    availability,
    bookings,
    branches,
    contacts,
    conversations,
    integrations,
    knowledge,
    leads,
    payments,
    reports,
    services_catalog,
    staff,
    tenants,
    webhooks_telegram,
    webhooks_voice,
    webhooks_whatsapp,
)
from app.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
log = logging.getLogger("api")

settings = get_settings()
app = FastAPI(
    title="AI CRM API",
    version="0.1.0",
    description="WhatsApp AI agent · booking engine · CRM · Telegram escalation · analytics",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled error rid=%s path=%s", rid, request.url.path)
        return JSONResponse(
            {"detail": {"code": "INTERNAL", "message": "Internal error", "request_id": rid}}, 500
        )
    response.headers["x-request-id"] = rid
    log.info(
        "rid=%s %s %s %s %.0fms",
        rid,
        request.method,
        request.url.path,
        response.status_code,
        (time.perf_counter() - start) * 1000,
    )
    return response


@app.get("/health")
def health():
    return {"status": "ok"}


V1 = "/v1"
app.include_router(auth.router, prefix=V1 + "/auth", tags=["auth"])
app.include_router(tenants.router, prefix=V1, tags=["tenant"])
app.include_router(branches.router, prefix=V1 + "/branches", tags=["branches"])
app.include_router(services_catalog.router, prefix=V1 + "/services", tags=["services"])
app.include_router(staff.router, prefix=V1 + "/staff", tags=["staff"])
app.include_router(availability.router, prefix=V1 + "/availability", tags=["booking"])
app.include_router(bookings.router, prefix=V1 + "/bookings", tags=["booking"])
app.include_router(contacts.router, prefix=V1 + "/contacts", tags=["crm"])
app.include_router(leads.router, prefix=V1 + "/leads", tags=["crm"])
app.include_router(conversations.router, prefix=V1 + "/conversations", tags=["inbox"])
app.include_router(knowledge.router, prefix=V1 + "/knowledge", tags=["ai"])
app.include_router(admin_tasks.router, prefix=V1 + "/admin-tasks", tags=["escalation"])
app.include_router(payments.router, prefix=V1 + "/payments", tags=["finance"])
app.include_router(reports.router, prefix=V1 + "/reports", tags=["reports"])
app.include_router(integrations.router, prefix=V1 + "/integrations", tags=["integrations"])
app.include_router(audit_api.router, prefix=V1 + "/audit", tags=["audit"])
app.include_router(webhooks_whatsapp.router, prefix=V1 + "/webhooks/whatsapp", tags=["webhooks"])
app.include_router(webhooks_telegram.router, prefix=V1 + "/webhooks/telegram", tags=["webhooks"])
app.include_router(webhooks_voice.router, prefix=V1 + "/webhooks/voice", tags=["webhooks"])
