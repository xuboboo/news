"""密码哈希：bcrypt（cost 12）。

直接用 bcrypt 库而非 passlib（后者已停止维护，与 bcrypt 4.x 存在兼容告警）。
bcrypt 输入上限 72 字节，超长截断以保持行为可预期。
"""
import bcrypt

_COST = 12
_MAX_PASSWORD_BYTES = 72


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
