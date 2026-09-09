"""采集部分失败可观测（2026-09-09 审计修复）配套测试：

- has_source_error：per_source 分源异常识别；
- record_task_run：任务体置 degraded 后退出不被覆盖为 success；
- alert_service：连续 3 轮 degraded 触发告警、偶发降级不打扰。

离线运行：python -m pytest tests/test_taskrun_degraded.py -q
"""
from sqlalchemy import text

from app.crawler.multi import format_detail, has_source_error
from app.services.taskrun_service import record_task_run


# ———— multi.has_source_error ————

def test_has_source_error_detects_per_source_error():
    ok = {"per_source": {"IT之家": {"new_ids": [1]}, "快科技": {"new_ids": []}}}
    assert not has_source_error(ok)
    bad = {"per_source": {"IT之家": {"new_ids": [1]}, "快科技": {"error": "采集失败"}}}
    assert has_source_error(bad)
    assert not has_source_error({})
    assert not has_source_error({"per_source": None})


def test_format_detail_marks_error_source():
    detail = format_detail(
        {"per_source": {"快科技": {"error": "采集失败（详见 crawler 日志）"}}, "per_channel": {}}
    )
    assert "快科技" in detail and "采集失败" in detail


# ———— record_task_run degraded 语义 ————

def test_record_task_run_success_default(db, monkeypatch):
    monkeypatch.setattr("app.services.taskrun_service.new_session", db.factory)
    with record_task_run("crawler") as run:
        run.detail = "正常完成"
    s = db.factory()
    row = s.execute(
        text("SELECT status FROM task_run ORDER BY id DESC LIMIT 1")
    ).one()
    assert row[0] == "success"


def test_record_task_run_degraded_survives_exit(db, monkeypatch):
    """任务体显式置 degraded（单来源失败）：退出时保留，不覆盖为 success。"""
    monkeypatch.setattr("app.services.taskrun_service.new_session", db.factory)
    with record_task_run("crawler") as run:
        run.detail = "IT之家 新增 3 篇 ｜ 快科技 采集失败"
        run.status = "degraded"
    s = db.factory()
    row = s.execute(
        text("SELECT status, detail FROM task_run ORDER BY id DESC LIMIT 1")
    ).one()
    assert row[0] == "degraded"
    assert "快科技" in row[1]


def test_record_task_run_failure_overrides_degraded(db, monkeypatch):
    """任务体先置 degraded 后又抛异常：仍如实记 failed（异常优先）。"""
    monkeypatch.setattr("app.services.taskrun_service.new_session", db.factory)
    try:
        with record_task_run("crawler") as run:
            run.status = "degraded"
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    s = db.factory()
    row = s.execute(
        text("SELECT status FROM task_run ORDER BY id DESC LIMIT 1")
    ).one()
    assert row[0] == "failed"


# ———— alert_service 连续降级规则 ————

def _add_run(db, name: str, status: str, detail: str = "") -> None:
    from app.models.task_run import TaskRun

    s = db.factory()
    s.add(TaskRun(name=name, trigger="auto", status=status, detail=detail))
    s.commit()
    s.close()


def test_alert_flags_three_consecutive_degraded(db, monkeypatch):
    from app.services import alert_service

    monkeypatch.setattr(alert_service, "redis_ping", lambda: (True, ""))
    monkeypatch.setattr(alert_service, "celery_workers_ping", lambda: (["worker1"], None))
    for i in range(3):
        _add_run(db, "crawler", "degraded", f"快科技 采集失败 第{i}轮")
    problems = alert_service._collect_problems(db.factory())
    assert any("连续降级" in p and "crawler" in p for p in problems)


def test_alert_tolerates_isolated_degraded(db, monkeypatch):
    """最近 3 轮为 degraded/degraded/success：偶发降级不打扰。"""
    from app.services import alert_service

    monkeypatch.setattr(alert_service, "redis_ping", lambda: (True, ""))
    monkeypatch.setattr(alert_service, "celery_workers_ping", lambda: (["worker1"], None))
    _add_run(db, "crawler", "success")
    _add_run(db, "crawler", "degraded", "快科技 采集失败")
    _add_run(db, "crawler", "degraded", "快科技 采集失败")
    problems = alert_service._collect_problems(db.factory())
    assert not any("连续降级" in p for p in problems)
