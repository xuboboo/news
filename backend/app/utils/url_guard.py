"""AI 服务地址校验：仅放行公网 http(s) 目标，防止管理面板/配置被用作内网 SSRF 跳板。

管理面板可运行时修改 llm_base_url / embedding_base_url，若不校验，攻击者
（尤其 ADMIN_NO_AUTH 模式下）可让后端向任意内网地址发 POST 并回读响应。
"""
import ipaddress
import logging
import socket
from urllib.parse import urlsplit

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class UrlNotAllowed(ValueError):
    """AI 服务地址违反公网限制。"""


def _allowlist_hosts() -> set[str]:
    """AI_URL_ALLOWLIST 解析：小写、去空项（getattr 兜底：部分测试注入精简 settings）。"""
    raw = getattr(get_settings(), "ai_url_allowlist", "") or ""
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def assert_public_http_url(url: str) -> str:
    """校验并归一化 base_url（去尾部 /）。

    - scheme 必须是 http/https 且带主机名；
    - 主机名命中 AI_URL_ALLOWLIST（精确相等，非子串/后缀）时豁免私网校验，
      供 compose 内网服务名（如 embedding）使用（2026-09-18 审计整改 M-2）；
    - 其余主机名解析到私网/环回/链路本地/保留段时拒绝；
    - ALLOW_PRIVATE_AI_URLS=true 为已弃用的全局豁免（兼容旧部署，命中时记 warning；
      production 自检已禁止）；
    - 域名解析失败不在此拦截，交由后续 HTTP 调用自然报错。
    """
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UrlNotAllowed(f"AI 服务地址必须是合法的 http(s) URL，收到：{url!r}")

    hostname = parts.hostname.lower()
    # 白名单命中放在弃用开关之前：两种开关同时存在时取最小放行面
    if hostname in _allowlist_hosts():
        logger.debug("AI URL 主机名白名单命中，豁免私网校验：%s", hostname)
        return url.rstrip("/")
    if get_settings().allow_private_ai_urls:
        logger.warning(
            "allow_private_ai_urls 已弃用（全局豁免 SSRF 校验，攻击面大），"
            "请改用 AI_URL_ALLOWLIST 精确放行：%s", url,
        )
        return url.rstrip("/")

    if hostname == "localhost" or hostname.endswith((".localhost", ".internal", ".local")):
        raise UrlNotAllowed(f"AI 服务地址不允许指向本机/内部主机名：{hostname}")

    try:
        addr_infos = socket.getaddrinfo(parts.hostname, None)
    except socket.gaierror:
        return url.rstrip("/")

    for info in addr_infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            raise UrlNotAllowed(
                f"AI 服务地址解析到非公网 IP（{ip}），已拦截；"
                "内网服务请在 AI_URL_ALLOWLIST 精确放行其主机名"
            )
    return url.rstrip("/")
