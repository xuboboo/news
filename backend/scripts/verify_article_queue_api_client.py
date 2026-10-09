import verify_article_queue_guard
import json, os, sys
import httpx
base="http://"+os.environ["DRAFT_QUEUE_PREFIX"]+"-api:8089"
jar=httpx.Cookies()
if os.path.exists("/tmp/draft-cookie"):
    jar.set("hjradar_session",open("/tmp/draft-cookie").read().strip())
cmd=sys.argv[1]
with httpx.Client(base_url=base,cookies=jar,timeout=4) as c:
    if cmd=="session":
        r=c.get("/__queue_check/session"); r.raise_for_status(); token=r.cookies.get("hjradar_session"); open("/tmp/draft-cookie","w").write(token); print(json.dumps(r.json(),ensure_ascii=False))
    elif cmd=="prime":
        r=c.get("/__queue_check/prime"); print(r.status_code,r.text)
    elif cmd=="create":
        article_id=int(sys.argv[2]); r=c.post("/api/v2/drafts",headers={"X-Requested-With":"XMLHttpRequest"},json={"source_type":"article","source_id":article_id,"tier":"brief"}); print(r.status_code,r.text)
    elif cmd=="regenerate":
        did=int(sys.argv[2]); r=c.post(f"/api/v2/drafts/{did}/regenerate",headers={"X-Requested-With":"XMLHttpRequest"}); print(r.status_code,r.text)
    elif cmd=="get":
        did=int(sys.argv[2]); r=c.get(f"/api/v2/drafts/{did}"); print(r.status_code,r.text)
