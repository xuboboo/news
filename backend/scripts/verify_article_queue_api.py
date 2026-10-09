"""End-to-end HTTP exercise of /api/v2/drafts with an isolated member cookie."""
import verify_article_queue_guard
import json, sys, uuid
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.main import app
from app.core.db import SessionLocal
from app.models.tenant import Tenant
from app.models.user import User, ROLE_MEMBER
from app.models.article import Article
from app.auth.sessions import create_session, SESSION_COOKIE
from app.tasks.celery_app import celery_app

# Bound broker outage: real .delay()/Kombu publish path remains intact.
celery_app.conf.update(task_publish_retry=True, task_publish_retry_policy={"max_retries":1,"interval_start":0.0,"interval_step":0.1,"interval_max":0.1}, broker_transport_options={**celery_app.conf.broker_transport_options,"socket_connect_timeout":0.5,"socket_timeout":0.5})

with TestClient(app) as client:
    with SessionLocal() as db:
        tenant=db.scalar(select(Tenant).where(Tenant.name=="draft-queue-check tenant"))
        if tenant is None:
            tenant=Tenant(name="draft-queue-check tenant",remark="isolated test")
            db.add(tenant); db.flush()
        user=db.scalar(select(User).where(User.email=="draft-queue-check-member@example.invalid"))
        if user is None:
            user=User(email="draft-queue-check-member@example.invalid",password_hash="not-used",display_name="Queue Test Member",tenant_id=tenant.id,role=ROLE_MEMBER,status="active",must_change_password=False,created_by="isolated-test")
            db.add(user); db.flush()
        article=Article(title="HTTP queue fixture",url="https://example.invalid/http-queue-fixture/"+uuid.uuid4().hex,channel="test",source="Test",content="HTTP fixture facts",publish_time=datetime.now(timezone.utc))
        db.add(article); db.commit(); uid=user.id; aid=article.id
        _,token=create_session(db,uid,ttl_hours=1)
    client.cookies.set(SESSION_COOKIE,token)
    mode=sys.argv[1]
    headers={"X-Requested-With":"XMLHttpRequest","Content-Type":"application/json"}
    if mode=="create":
        response=client.post("/api/v2/drafts",headers=headers,json={"source_type":"article","source_id":aid,"tier":"brief"})
    elif mode=="regenerate":
        draft_id=int(sys.argv[2])
        detail=client.get(f"/api/v2/drafts/{draft_id}")
        if detail.status_code != 200: raise SystemExit("draft not found before regenerate")
        response=client.post(f"/api/v2/drafts/{draft_id}/regenerate",headers=headers,json={"revision":detail.json()["revision"]})
    elif mode=="status":
        response=client.get(f"/api/v2/drafts/{int(sys.argv[2])}")
    else: raise SystemExit("mode must be create/regenerate/status")
    print(json.dumps({"mode":mode,"http":response.status_code,"body":response.json()},ensure_ascii=False))
    if response.status_code>=500: raise SystemExit(2)
