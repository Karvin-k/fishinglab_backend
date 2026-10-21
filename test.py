# test_minimal.py
import sys
print(f"Python版本: {sys.version}")
print(f"Python路径: {sys.executable}")

# 清除 jwt 模块缓存
if 'jwt' in sys.modules:
    del sys.modules['jwt']

# 重新导入
import importlib
importlib.invalidate_caches()

import jwt
print(f"JWT模块路径: {jwt.__file__}")
print(f"JWT版本: {getattr(jwt, '__version__', '未知')}")
print(f"是否有 encode 方法: {hasattr(jwt, 'encode')}")
print(f"encode 方法: {jwt.encode}")

# 直接尝试调用
try:
    import datetime
    payload = {'test': 1, 'exp': datetime.datetime.now(datetime.timezone.utc)}
    token = jwt.encode(payload, 'secret', algorithm='HS256')
    print(f"✅ 编码成功: {token}")
except Exception as e:
    print(f"❌ 编码失败: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()