"""SSRF 豁免收窄（2026-09-18 审计整改 M-2）：AI_URL_ALLOWLIST 精确放行主机名。

覆盖：白名单命中/不命中（含后缀伪装）、私网 IP 拦截、弃用开关兼容、
production 自检拒绝全局豁免。离线测试不依赖真实 DNS：解析路径统一
monkeypatch url_guard.socket.getaddrinfo。
"""
import types

import pytest

from app.core.config import _assert_production_safe
from app.utils import url_guard
from app.utils.url_guard import UrlNotAllowed, _allowlist_hosts, assert_public_http_url


def _stub_settings(monkeypatch, allowlist: str = "", legacy: bool = False) -> None:
    monkeypatch.setattr(
        url_guard, "get_settings",
        lambda: types.SimpleNamespace(ai_url_allowlist=allowlist, allow_private_ai_urls=legacy),
    )


def _stub_dns(monkeypatch, ip: str) -> None:
    """让全部域名"解析"到指定 IP（私网拦截路径可测，不依赖外网 DNS）。"""
    monkeypatch.setattr(
        url_guard.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", (ip, 0))],
    )


def test_allowlist_hit_allows_private_host(monkeypatch):
    _stub_settings(monkeypatch, allowlist="embedding")
    assert assert_public_http_url("http://embedding:80/v1/") == "http://embedding:80/v1"


def test_allowlist_parsing_exact_lowercase(monkeypatch):
    _stub_settings(monkeypatch, allowlist=" embedding , foo ")
    assert _allowlist_hosts() == {"embedding", "foo"}


def test_allowlist_hit_case_insensitive_hostname(monkeypatch):
    _stub_settings(monkeypatch, allowlist="embedding")
    # urlsplit 会把 hostname 转小写，EMBEDDING 亦命中
    assert assert_public_http_url("http://EMBEDDING:80/v1") == "http://EMBEDDING:80/v1"


def test_suffix_spoof_not_allowed(monkeypatch):
    """evil 后缀伪装（embedding.evil.internal 类）不命中白名单 → 走私网拦截。"""
    _stub_settings(monkeypatch, allowlist="embedding")
    _stub_dns(monkeypatch, "10.0.0.8")
    with pytest.raises(UrlNotAllowed):
        assert_public_http_url("http://embedding.evil.test/v1")


def test_private_ip_rejected_without_allowlist(monkeypatch):
    _stub_settings(monkeypatch, allowlist="")
    for u in (
        "http://192.168.1.1:8080/api",
        "http://127.0.0.1:18000/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
    ):
        with pytest.raises(UrlNotAllowed):
            assert_public_http_url(u)


def test_internal_hostname_still_rejected(monkeypatch):
    _stub_settings(monkeypatch, allowlist="embedding")
    for u in ("http://localhost:8000/x", "http://foo.internal/v1", "http://bar.local/v1"):
        with pytest.raises(UrlNotAllowed):
            assert_public_http_url(u)


def test_legacy_global_bypass_still_works(monkeypatch):
    """兼容语义：ALLOW_PRIVATE_AI_URLS=true 且白名单为空 → 旧全局豁免仍放行。"""
    _stub_settings(monkeypatch, allowlist="", legacy=True)
    assert assert_public_http_url("http://192.168.1.7/x") == "http://192.168.1.7/x"


def test_production_selfcheck_rejects_legacy_bypass():
    s = types.SimpleNamespace(
        environment="production", cookie_secure=True, enable_docs=False,
        admin_no_auth=False, database_url="postgresql+psycopg://u:strongpw@db:5432/d",
        admin_token="", bootstrap_admin_password="", allow_private_ai_urls=True,
    )
    with pytest.raises(RuntimeError, match="allow_private_ai_urls"):
        _assert_production_safe(s)


def test_production_selfcheck_passes_with_allowlist():
    s = types.SimpleNamespace(
        environment="production", cookie_secure=True, enable_docs=False,
        admin_no_auth=False, database_url="postgresql+psycopg://u:strongpw@db:5432/d",
        admin_token="", bootstrap_admin_password="", allow_private_ai_urls=False,
    )
    _assert_production_safe(s)  # 不抛异常即通过
