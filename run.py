# run.py
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
from app import create_app
from flask_cors import CORS
from app.models import Product, Order, db

app = create_app()

# 配置详细的 CORS 设置
CORS(app, resources={
    r"/api/*": {
        "origins": "*",
        "methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],  # 添加 PATCH
        "allow_headers": ["Content-Type", "Authorization", "x-access-token", "User-ID"],
        "supports_credentials": True,
        "max_age": 3600
    }
})

def cancel_expired_orders():
    with app.app_context():
        expired_orders = Order.query.filter(Order.order_status == 0, Order.expire_time < datetime.now()).all()
        for order in expired_orders:
            # 回滚库存
            for item in order.items:
                product = Product.query.get(item.product_id)
                if product:
                    product.stock += item.quantity
            order.order_status = 3
        db.session.commit()

scheduler = BackgroundScheduler()
scheduler.add_job(func=cancel_expired_orders, trigger="interval", minutes=5)
scheduler.start()

if __name__ == '__main__':
    # 本地开发入口。上线不用它，用 gunicorn（见部署文档）。
    # debug 从环境变量读：默认开（本地方便），部署时 .env 里设 FLASK_DEBUG=0
    import os
    debug = os.getenv('FLASK_DEBUG', '1') == '1'
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')),
            debug=debug, use_reloader=debug)