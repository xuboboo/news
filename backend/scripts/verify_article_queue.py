"""Isolated Redis/Celery article-draft queue verifier; never uses business settings."""
import verify_article_queue_guard  # stdlib fail-closed validation before app/SQLAlchemy imports
import json, os, sys, time, uuid
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

URL = os.environ["DATABASE_URL"]
REDIS = os.environ["REDIS_URL"]
engine = create_engine(URL, pool_pre_ping=True)
from app.models.article import Article
from app.models.summary import ArticleSummary
from app.models.article_draft import ArticleDraft
from app.models.setting import SystemSetting
from app.models.usage import AiUsageLog, ModelPrice
with engine.begin() as c:
    c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
for model in (Article, ArticleSummary, ArticleDraft, SystemSetting, AiUsageLog, ModelPrice):
    model.__table__.create(engine, checkfirst=True)
Factory = sessionmaker(engine, expire_on_commit=False)

from app.tasks.tasks import article_gen_task

def mock_llm():
    from app.ai import llm_client
    import redis
    r = redis.Redis.from_url(REDIS)
    def chat(base, key, model, protocol, prompt, **kwargs):
        n = r.incr("draft-check:mock-calls")
        if "MOCK_LLM_FAIL" in prompt:
            raise llm_client.LLMAPIError("simulated offline model failure")
        answer = {"title":"隔离验证标题", "alt_titles":["备选一","备选二","搜索标题"], "digest":"隔离验证摘要", "content_md":"正文由隔离 mock 产生，mock token trace 可核验。"}
        return json.dumps(answer, ensure_ascii=False), {"prompt_tokens":123,"completion_tokens":45,"total_tokens":168}
    llm_client.chat = chat

def seed():
    s=Factory()
    for key,value in (("article_gen_enabled","true"),("article_gen_model",""),("article_gen_max_sources","6"),("article_gen_max_source_chars","4000")):
        row=s.get(SystemSetting,key)
        if row: row.value=value
        else: s.add(SystemSetting(key=key,value=value))
    marker=uuid.uuid4().hex[:8]
    a=Article(title="Queue fixture "+marker,url="https://example.invalid/"+marker,channel="test",source="Test",content=("MOCK_LLM_FAIL" if sys.argv[-1]=="seed-fail" else "MOCK_LLM_BLOCK" if sys.argv[-1]=="seed-block" else "Fixture facts only."),publish_time=__import__('datetime').datetime.now(__import__('datetime').timezone.utc))
    s.add(a); s.commit()
    d=ArticleDraft(source_type="article",source_id=a.id,source_title=a.title,tier="brief",status="generating",gen_count=1)
    s.add(d);s.commit(); print(json.dumps({"article_id":a.id,"draft_id":d.id,"generation":1,"marker":marker}));s.close()

def status(did):
    from app.models.article_draft import ArticleDraft
    s=Factory(); d=s.get(ArticleDraft,did)
    from celery.result import AsyncResult
    print(json.dumps({"draft_id":did,"status":d.status,"gen_count":d.gen_count,"title":d.title,"content_md":d.content_md,"error":d.error,"mock_calls":__import__('redis').Redis.from_url(REDIS).get("draft-check:mock-calls").decode() if __import__('redis').Redis.from_url(REDIS).get("draft-check:mock-calls") else "0"}));s.close()

def publish(did,g=1):
    result=article_gen_task.delay(did,trigger="manual",generation=g)
    print(json.dumps({"task_id":result.id,"state":result.state,"queue":"ai","draft_id":did,"generation":g}))

def poll(tid,timeout=35):
    from celery.result import AsyncResult
    r=AsyncResult(tid,app=article_gen_task.app); end=time.time()+timeout
    while time.time()<end:
        r=AsyncResult(tid,app=article_gen_task.app)
        if r.state in ("SUCCESS","FAILURE","RETRY","REVOKED"):
            print(json.dumps({"task_id":tid,"state":r.state,"result":str(r.result)}));return
        time.sleep(.25)
    print(json.dumps({"task_id":tid,"state":r.state,"timeout":True}))

if __name__=="__main__":
    mock_llm()
    cmd=sys.argv[1]
    if cmd in ("seed", "seed-fail", "seed-block"):seed()
    elif cmd=="status":status(int(sys.argv[2]))
    elif cmd=="publish-default":
        result=article_gen_task.app.send_task("auth_cleanup_task")
        print(json.dumps({"task_id":result.id,"state":result.state,"queue":"default"}))
    elif cmd=="publish":publish(int(sys.argv[2]),int(sys.argv[3]) if len(sys.argv)>3 else 1)
    elif cmd=="poll":poll(sys.argv[2],int(sys.argv[3]) if len(sys.argv)>3 else 35)
    elif cmd=="confirm":
        from app.tasks.tasks import article_gen_failure_confirm_task
        result=article_gen_failure_confirm_task.delay(int(sys.argv[2]),int(sys.argv[3]),"isolated failure confirmation")
        print(json.dumps({"task_id":result.id,"draft_id":int(sys.argv[2]),"generation":int(sys.argv[3])}))
    elif cmd=="recover":
        did=int(sys.argv[2]); s=Factory(); d=s.get(ArticleDraft,did); d.status="generating"; d.gen_count+=1; d.error=""; s.get(Article,d.source_id).content="Fixture facts only."; s.commit(); g=d.gen_count; s.close(); publish(did,g)
    elif cmd=="stale":
        from datetime import datetime, timedelta, timezone
        did=int(sys.argv[2]); s=Factory(); d=s.get(ArticleDraft,did); d.updated_at=datetime.now(timezone.utc)-timedelta(minutes=16); s.commit(); print(json.dumps({"draft_id":did,"updated_at":d.updated_at.isoformat()})); s.close()
    elif cmd=="hold":
        seconds=int(sys.argv[3]) if len(sys.argv)>3 else 40
        with engine.connect() as c:
            tx=c.begin(); c.execute(text("SELECT pg_advisory_xact_lock(hashtext('article_draft:' || :i))"),{"i":int(sys.argv[2])})
            print(json.dumps({"lock":"held","draft_id":int(sys.argv[2]),"seconds":seconds}),flush=True)
            time.sleep(seconds); tx.commit()
    elif cmd=="summary":
        from app.models.usage import AiUsageLog
        from app.models.article_draft import ArticleDraft
        s=Factory()
        print(json.dumps({"drafts":[{"id":d.id,"status":d.status,"gen_count":d.gen_count,"title":d.title,"error":d.error} for d in s.query(ArticleDraft).order_by(ArticleDraft.id).all()],"usage":[{"status":u.status,"task_type":u.task_type,"prompt_tokens":u.prompt_tokens,"completion_tokens":u.completion_tokens,"cost":float(u.cost or 0)} for u in s.query(AiUsageLog).order_by(AiUsageLog.id).all()]}));s.close()
    elif cmd=="queue":
        import redis
        r=redis.Redis.from_url(REDIS)
        print(json.dumps({"ai_len":r.llen("ai"),"default_len":r.llen("default"),"calls":(r.get("draft-check:mock-calls") or b"0").decode()}))
    engine.dispose()
