"""pytest 根锚点：使 backend/ 进入 sys.path，测试可用 `import app.*`。运行方式（backend/ 下）：
python -m pytest tests/ -q

离线测试基建：SQLite 内存库 + FastAPI 依赖覆写，不依赖运行中的 postgres/redis/
compose 栈。仅注册用户/租户域所需表；涉及 pgvector/JSONB 专有能力的模块不建表
（audit_log.detail 已用 JSON variant 兼容；审计哈希链的 pg advisory lock 在
非 PG 方言下自动跳过，见 audit_service.record）。
"""
import types

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import passwords, throttle
from app.core.db import Base, get_db
from app.main import app

# 用户/租户域 + 运维监控域测试所需的最小表集（article/article_summary 已验证
# 可在 SQLite 建，摘要自愈等测试需要它们）
# ai_usage_log / model_price：成本看板域（usage API、连通性测试落账）均为 SQLite 兼容类型
_TEST_TABLES = (
    "tenant", "app_user", "user_session", "audit_log", "system_setting", "task_run",
    "ai_usage_log", "model_price", "article", "article_summary",
)

# 统一测试密码（弱口令表会拒绝 12345678 等，此串不在其中）
PW = "Str0ng-Test-Pw!"


@pytest.fixture()
def db():
    """独立 SQLite 内存库：返回 factory（造会话）与 override（依赖注入用）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [Base.metadata.tables[t] for t in _TEST_TABLES]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    container = types.SimpleNamespace(factory=factory, override=override)
    yield container
    engine.dispose()


@pytest.fixture()
def client(db, monkeypatch):
    """TestClient：覆写 get_db、隔离限速存储（走进程内回退而非 Redis）。"""
    from app import main as app_main

    monkeypatch.setattr(throttle, "_redis_client", lambda: None)
    monkeypatch.setattr(passwords, "_COST", 4)  # 降 bcrypt cost，建号/校验提速
    # main.py 的 403 审计处理器直接开 SessionLocal（真实 postgres）：指到测试库，
    # 其 JSON astext 去重查询在 SQLite 不兼容时由处理器的 try/except 兜底
    monkeypatch.setattr(app_main, "SessionLocal", db.factory)
    throttle._INPROC.clear()
    app.dependency_overrides[get_db] = db.override
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)
    throttle._INPROC.clear()


@pytest.fixture()
def seeded(db):
    """账号矩阵：租户A(super/ta/member) + 租户B(member_b)。全部 must_change_password=False。"""
    from app.models.tenant import Tenant
    from app.models.user import User

    s = db.factory()
    ta, tb = Tenant(name="租户A"), Tenant(name="租户B")
    s.add_all([ta, tb])
    s.flush()

    def mk(email, role, tenant, name):
        return User(
            email=email,
            password_hash=passwords.hash_password(PW),
            display_name=name,
            tenant_id=tenant.id,
            role=role,
            must_change_password=False,
            created_by="seed",
        )

    users = {
        "super": mk("super@test.local", "super_admin", ta, "超管"),
        "ta": mk("ta@test.local", "tenant_admin", ta, "租管A"),
        "member": mk("member@test.local", "member", ta, "成员A"),
        "member_b": mk("member-b@test.local", "member", tb, "成员B"),
    }
    s.add_all(users.values())
    s.commit()
    out = types.SimpleNamespace(
        ids={k: u.id for k, u in users.items()},
        tids={"a": ta.id, "b": tb.id},
        emails={k: u.email for k, u in users.items()},
    )
    s.close()
    return out


def login(client: TestClient, email: str, password: str = PW):
    return client.post(
        "/api/v2/auth/login",
        json={"email": email, "password": password},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )


def set_config(db, key: str, value: str) -> None:
    """写入 system_setting 覆盖（config_service 热调路径）。"""
    from app.models.setting import SystemSetting

    s = db.factory()
    row = s.get(SystemSetting, key)
    if row is None:
        s.add(SystemSetting(key=key, value=value))
    else:
        row.value = value
    s.commit()
    s.close()
