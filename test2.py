import sys
import os

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# 检查蓝图导入
try:
    from app.routes import api_bp

    print("✅ 成功导入 api_bp 蓝图")
    print(f"蓝图名称: {api_bp.name}")
    print(f"蓝图URL前缀: {api_bp.url_prefix}")
except ImportError as e:
    print(f"❌ 导入蓝图失败: {e}")

# 检查应用创建
try:
    from app import create_app

    app = create_app()
    print("✅ 成功创建Flask应用")

    # 检查是否注册了蓝图
    registered_blueprints = [bp.name for bp in app.blueprints.values()]
    print(f"已注册的蓝图: {registered_blueprints}")

    if 'api' in registered_blueprints:
        print("✅ api蓝图已注册")
    else:
        print("❌ api蓝图未注册")

except Exception as e:
    print(f"❌ 创建应用失败: {e}")