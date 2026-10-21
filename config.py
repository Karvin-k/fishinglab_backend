import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', 'your-secret-key')
    SQLALCHEMY_DATABASE_URI = (
        f"mysql+pymysql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
        f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT', '3306')}/{os.getenv('DB_NAME')}"
        "?charset=utf8mb4"  # 添加字符集配置
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # 是否打印 SQL 语句：默认关闭。本地调试想开，就在 .env 里加 SQL_ECHO=true
    # 上线必须关闭：既拖慢响应，也会把表结构打到日志里
    SQLALCHEMY_ECHO = os.getenv('SQL_ECHO', 'false').lower() == 'true'
    JSON_AS_ASCII = False  # 禁用ASCII转义

    # 添加微信小程序配置
    WX_APPID = os.getenv('WX_APPID')
    WX_SECRET = os.getenv('WX_SECRET')
    JWT_SECRET_KEY = os.getenv('JWT_SECRET_KEY', 'your-jwt-secret-key')

    # 图片访问基础地址：数据库里存的是 /static/uploads/xxx 这种相对路径，
    # 返回给前端时要拼上这个前缀，否则 H5 会去前端的 static 目录找，必然 404。
    # 本地开发 = http://localhost:5000 ，上线后改成 http://公网IP 或 https://域名
    STATIC_BASE_URL = os.getenv('STATIC_BASE_URL', 'http://localhost:5000')

    # 文件上传配置
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max file size
    UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app', 'static', 'uploads')
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp'}