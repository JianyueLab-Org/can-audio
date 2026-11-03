"""在 Murmur 监听公网端口之前启用原生的外部认证强制模式。"""
from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
import sys


class UnsafeAdmission(RuntimeError):
    pass

def require_supported_version(version):
    match = re.fullmatch(r"(?:\d+:)?(\d+)\.(\d+)\.(\d+)(?:[-+~].*)?", version)
    if not match or tuple(map(int, match.groups())) < (1, 5, 735):
        raise UnsafeAdmission("Murmur >= 1.5.735 is required for the verified forceExternalAuth control")



def root_settings(text):
    """只读 QSettings 的顶层键；本部署的数据库配置使用普通字符串。"""
    settings = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            break
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        settings[key.strip()] = value.strip()
    return settings


def prepare(ini_path):
    """拒绝不能检查的数据库和关闭强制认证的持久化覆盖，不改账号或 ACL。"""
    # Murmur 相对传入 ini 的目录打开数据库；解析 ini 符号链接会检查到另一份库。
    ini_path = Path(os.path.abspath(ini_path))
    text = ini_path.read_text(encoding="utf-8")
    settings = root_settings(text)
    # QSettings 的 [General] 也是根键，不能把它误当普通节而漏查覆盖。
    if any(re.match(r"\[\s*general\s*\]", line.strip(), re.IGNORECASE) for line in text.splitlines()):
        raise UnsafeAdmission("Move [General] settings to the unsectioned top level before startup")
    if any("%" in key for key in settings):
        raise UnsafeAdmission("Percent-escaped top-level QSettings keys are not supported by admission preflight")
    if settings.get("dbDriver", "QSQLITE") != "QSQLITE":
        raise UnsafeAdmission("Admission preflight requires the deployment's SQLite database")
    database = settings.get("database", "").strip('"')
    if not database or any(char in database for char in ('\\', '"', ',', '@', ';', '#', '\x00')):
        raise UnsafeAdmission("Set an explicit plain SQLite database path in the top-level database= setting")
    prefix = settings.get("dbPrefix", "")
    if not re.fullmatch(r"[A-Za-z0-9_]*", prefix):
        raise UnsafeAdmission("Unsupported SQLite table prefix")
    database_path = Path(database)
    if not database_path.is_absolute():
        database_path = ini_path.parent / database_path
    try:
        database_size = database_path.stat().st_size
    except FileNotFoundError:
        database_size = 0
    if database_size:
        try:
            with closing(sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                overrides = connection.execute(
                    f'SELECT server_id, value FROM "{prefix}config" WHERE key = ?',
                    ("forceExternalAuth",)).fetchall()
        except sqlite3.Error as error:
            raise UnsafeAdmission("Cannot inspect Murmur's persisted admission configuration") from error
        for server_id, value in overrides:
            if str(value).lower() not in ("true", "1"):
                raise UnsafeAdmission(
                    f"Server {server_id} disables external authentication; set its "
                    "forceExternalAuth override to true through Ice before stopping the old deployment")

    # 保留节、权限和属主；不能把此键追加到 [Ice] 底下。
    lines = text.splitlines(keepends=True)
    root = True
    kept = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            root = False
        if root and stripped.split("=", 1)[0].strip() == "forceExternalAuth":
            continue
        kept.append(line)
    with ini_path.open("w", encoding="utf-8") as target:
        target.write("forceExternalAuth=true\n")
        target.write("".join(kept))


if __name__ == "__main__":
    try:
        require_supported_version(sys.argv[2])
        prepare(sys.argv[1])
    except (UnsafeAdmission, OSError) as error:
        print(f"Admission preflight failed: {error}", file=sys.stderr)
        sys.exit(1)
