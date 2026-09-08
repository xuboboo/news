"""密码哈希：bcrypt（cost 12）。

直接用 bcrypt 库而非 passlib（后者已停止维护，与 bcrypt 4.x 存在兼容告警）。
bcrypt 输入上限 72 字节，超长截断以保持行为可预期。
"""
import bcrypt

_COST = 12
_MAX_PASSWORD_BYTES = 72

# 常见弱口令封锁表（NIST SP 800-63B：新密码须对照 blocklist 校验）。
# 离线静态表覆盖公开泄露库的头部高频值；不做联网 HIBP 查询（内网部署约束）。
_WEAK_PASSWORDS = frozenset({
    "12345678", "123456789", "1234567890", "12345678900",
    "password", "password1", "password123", "password!1",
    "qwertyuiop", "qwerty123", "qwe12345", "qwer1234", "1q2w3e4r",
    "11111111", "00000000", "88888888", "66666666", "12341234",
    "12344321", "1234qwer", "1234abcd", "1234.com",
    "abcd1234", "abc12345", "a1234567", "a12345678", "aa123456",
    "aaaaaaaa", "abcdefgh", "12345678a", "123456789a",
    "iloveyou", "iloveyou1", "sunshine1", "princess1",
    "1qaz2wsx", "2wsx3edc", "zxcvbnm1", "asdfghjkl",
    "woaini1314", "woaini520", "qq123456", "test1234", "admin12345",
})


def check_strength(plain: str, *, email: str = "") -> None:
    """新密码弱特征校验，不合规抛 ValueError（消息面向最终用户）。

    只做 NIST 建议的封锁清单与「含账号本体」两条，不做组合规则
    （强制大小写/符号已被 NIST 明确反对）；长度校验由调用方负责。
    """
    p = (plain or "").lower()
    if p in _WEAK_PASSWORDS:
        raise ValueError("密码过于常见或过于简单，请更换")
    local = (email or "").split("@", 1)[0].strip().lower()
    # 本体 ≥3 字符才参与，避免单字母用户名误伤
    if len(local) >= 3 and local in p:
        raise ValueError("密码不能包含邮箱账号")


def hash_password(plain: str) -> str:
    data = (plain or "").encode("utf-8")[:_MAX_PASSWORD_BYTES]
    return bcrypt.hashpw(data, bcrypt.gensalt(rounds=_COST)).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:_MAX_PASSWORD_BYTES], hashed.encode("ascii"))
    except ValueError:
        return False
