"""P1 spike: 验证 sqlite-vec 在 Windows Python 3.11 的加载兼容性。"""
import sqlite3
import sys

import sqlite_vec

db = sqlite3.connect(":memory:")
db.enable_load_extension(True)
try:
    sqlite_vec.load(db)
    print("OK: sqlite-vec 扩展加载成功")
except Exception as exc:
    print(f"FAIL: 扩展加载失败 -> {type(exc).__name__}: {exc}")
    sys.exit(1)

try:
    db.execute("CREATE VIRTUAL TABLE v USING vec0(embedding float[3])")
    db.execute("INSERT INTO v(rowid, embedding) VALUES (1, '[1.0,0.0,0.0]')")
    db.execute("INSERT INTO v(rowid, embedding) VALUES (2, '[0.0,1.0,0.0]')")
    rows = db.execute(
        "SELECT rowid, distance FROM v WHERE embedding MATCH '[0.9,0.1,0.0]' ORDER BY distance LIMIT 3"
    ).fetchall()
    print("OK: vec0 表创建 + KNN 查询 ->", rows)
except Exception as exc:
    print(f"FAIL: vec0 使用失败 -> {type(exc).__name__}: {exc}")
    sys.exit(1)

print("SPIKE-PASS")
