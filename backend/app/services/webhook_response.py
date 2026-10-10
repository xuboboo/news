"""统一第三方 webhook 业务码校验。"""


def _is_numeric_zero(value: object) -> bool:
    # bool 是 int 的子类：JSON false 不应被误判成业务码 0。
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0


def validate_business_response(provider: str, body: object) -> tuple[bool, str]:
    if provider == "raw":
        return True, "ok"
    if provider not in {"wecom", "dingtalk", "feishu"}:
        return False, f"不支持的 webhook 类型：{provider}"
    if not isinstance(body, dict):
        return False, "响应不是 JSON 对象"
    if provider in {"wecom", "dingtalk"}:
        code = body.get("errcode")
        ok = _is_numeric_zero(code)
        return ok, "ok" if ok else f"errcode={code}: {body.get('errmsg', '')}"
    code = body.get("code")
    if code is None:
        code = body.get("StatusCode")
    ok = _is_numeric_zero(code)
    return ok, "ok" if ok else f"code={code}: {body.get('msg', body.get('StatusMessage', ''))}"
