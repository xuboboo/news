"""Internal-only API fixture routes for exercising the real drafts router."""
import verify_article_queue_guard
import os
from datetime import datetime, timezone
from fastapi import Request
from sqlalchemy import select
from app.main import app
from app.core.db import SessionLocal
from app.models.tenant import Tenant
from app.models.user import User, ROLE_MEMBER
from app.models.article import Article
from app.auth.sessions import create_session, SESSION_COOKIE
from app.tasks.celery_app import celery_app

celery_app.conf.update(task_publish_retry=True, task_publish_retry_policy={"max_retries":1,"interval_start":0.0,"interval_step":0.1,"interval_max":0.1}, broker_transport_options={**celery_app.conf.broker_transport_options,"socket_connect_timeout":0.5,"socket_timeout":0.5})

@app.get("/__queue_check/prime")
def prime():
    # Establish the actual app's publisher connection before broker disruption.
    conn=celery_app.connection_for_write(); conn.connect(); conn.release()
    return {"primed":True}

@app.get("/__queue_check/session")
def session(request: Request):
    with SessionLocal() as db:
        tenant=db.scalar(select(Tenant).where(Tenant.name=="draft-queue-check tenant"))
        if tenant is None:
            tenant=Tenant(name="draft-queue-check tenant",remark="isolated test"); db.add(tenant); db.flush()
        user=db.scalar(select(User).where(User.email=="draft-queue-check-member@example.invalid"))
        if user is None:
            user=User(email="draft-queue-check-member@example.invalid",password_hash="unused",display_name="Queue Test Member",tenant_id=tenant.id,role=ROLE_MEMBER,status="active",must_change_password=False,created_by="isolated-test"); db.add(user); db.flush()
        article=Article(title="HTTP queue fixture",url="https://example.invalid/http-queue-fixture/"+os.urandom(8).hex(),channel="test",source="Test",content="HTTP fixture facts",publish_time=datetime.now(timezone.utc)); db.add(article); db.commit()
        _,token=create_session(db,user.id,ttl_hours=1)
        aid=article.id
    response={"article_id":aid,"member":"draft-queue-check-member@example.invalid"}
    from fastapi.responses import JSONResponse
    out=JSONResponse(response); out.set_cookie(SESSION_COOKIE,token,httponly=True,samesite="lax")
    return out

if __name__=="__main__":
    import uvicorn
    uvicorn.run(app,host="0.0.0.0",port=8089,log_level="info")
