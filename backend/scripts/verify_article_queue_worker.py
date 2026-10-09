import verify_article_queue_guard  # fail closed before importing app/Redis
import json, os, time
import redis
from celery import current_task
from app.ai import llm_client
from app.tasks.celery_app import celery_app
from app.tasks.tasks import article_gen_task

r = redis.Redis.from_url(os.environ["REDIS_URL"])
def fake(base, key, model, protocol, prompt, **kwargs):
    task_id = getattr(current_task.request, "id", "unknown")
    r.incr("draft-check:mock-calls")
    if "MOCK_LLM_BLOCK" in prompt:
        r.set("draft-check:llm-entered", f"{os.getpid()}:{task_id}")
        while not r.get("draft-check:llm-release"):
            time.sleep(.2)
    if "MOCK_LLM_FAIL" in prompt:
        raise llm_client.LLMAPIError("simulated offline model failure")
    payload = {"title":"隔离验证标题", "alt_titles":["备选一","备选二","搜索标题"], "digest":"隔离验证摘要", "content_md":"真实 Celery 任务 mock 正文"}
    return json.dumps(payload, ensure_ascii=False), {"prompt_tokens":123,"completion_tokens":45,"total_tokens":168}
llm_client.chat = fake
if os.environ.get("DRAFT_QUEUE_VISIBILITY"):
    vis=int(os.environ["DRAFT_QUEUE_VISIBILITY"])
    celery_app.conf.broker_transport_options={**celery_app.conf.broker_transport_options,"visibility_timeout":vis}
    celery_app.conf.result_backend_transport_options={**celery_app.conf.result_backend_transport_options,"visibility_timeout":vis}
    celery_app.conf.visibility_timeout=vis
if os.environ.get("DRAFT_QUEUE_MAX_RETRIES"):
    article_gen_task.max_retries=int(os.environ["DRAFT_QUEUE_MAX_RETRIES"])
pool=os.environ.get("DRAFT_QUEUE_POOL","prefork")
concurrency=os.environ.get("DRAFT_QUEUE_CONCURRENCY","2")
celery_app.worker_main(["worker", "--queues=ai", "--concurrency="+concurrency, "--pool="+pool, "--loglevel=INFO", "--without-gossip", "--without-mingle", "--without-heartbeat"])
