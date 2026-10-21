from flask import Flask
from config import Config
from app.models import db
from flask_migrate import Migrate
from .routes import api_bp
import os
from flask import json as flask_json
from flask_cors import CORS


def _is_backend_static_file(app, rel_url):
    """判断 /static/xxx 这个文件是否真存在于后端的 static 目录。

    存在的才拼后端地址；不存在（比如 first.png 只在前端 static 里）就保持相对路径，
    让前端 H5 dist 自己去提供。这样两套 static 各管各的，不会互相抢。
    """
    filename = rel_url[len('/static/'):]
    root = os.path.join(app.root_path, 'static')
    return os.path.isfile(os.path.join(root, filename))


def _absolutize(value, app, base):
    """递归遍历 JSON，把能对上的 /static/xxx 换成 完整URL。"""
    if isinstance(value, dict):
        return {k: _absolutize(v, app, base) for k, v in value.items()}
    if isinstance(value, list):
        return [_absolutize(v, app, base) for v in value]
    if isinstance(value, str) and value.startswith('/static/'):
        if _is_backend_static_file(app, value):
            return base + value
        return value
    return value


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    # 确保上传目录存在
    upload_dir = os.path.join(app.root_path, 'static', 'uploads')
    os.makedirs(upload_dir, exist_ok=True)

    # 初始化数据库
    db.init_app(app)

    # 初始化迁移
    migrate = Migrate(app, db)

    # 注册蓝图
    app.register_blueprint(api_bp, url_prefix='/api')

    CORS(app, resources={r"/api/*": {
        "origins": "*",
        "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"]
    }})

    # ============================================================
    # 统一出口：把 JSON 响应里的 /static/xxx 图片路径拼成完整 URL
    # ------------------------------------------------------------
    # 为什么这样做：
    #   数据库里存的是 /static/uploads/xxx.png 这种相对路径。
    #   H5 端拿到相对路径会去「前端 dist 的 static 目录」找，找不到就是 404，
    #   而且 <image> 加载失败不报错，只是白块 —— 极难排查。
    #   在响应出口统一拼上后端地址，就不用去改 routes.py 里那 20 多处返回。
    #
    # 为什么不是所有 /static/ 都拼：
    #   有些图（first.png / second.png / third.png 奖牌）只存在于前端 static，
    #   拼了后端地址反而 404。所以先检查后端有没有这个文件，有才拼。
    #
    # 上线时只需要改 .env 里的 STATIC_BASE_URL，这里一个字都不用动。
    # ============================================================
    @app.after_request
    def _absolutize_static_urls(response):
        if response.mimetype != 'application/json':
            return response
        payload = response.get_json(silent=True)
        if payload is None:
            return response
        base = app.config.get('STATIC_BASE_URL', '')
        if not base:
            return response
        new_payload = _absolutize(payload, app, base)
        response.set_data(flask_json.dumps(new_payload, ensure_ascii=False))
        response.headers['Content-Type'] = 'application/json; charset=utf-8'
        return response

    return app
