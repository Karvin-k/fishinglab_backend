from flask import Blueprint, jsonify, make_response, request
from app.models import Product, ProductTag, ProductImage, ProductAttribute, UserToken, UserAccountLink
from app.utils import serialize_products, safe_load_attrs, calc_attr_price
from app.models import Pond, Seat, ShopFollow, Coupon, UserCoupon, Cart, Review, ServiceReview
from app.models import FishingService, Post, PostImage, PostLike, PostComment, UserFollow
from flask import request, jsonify, make_response
from app.models import db, User, UserFavorite, Product, Merchant, Order, OrderItem, Notification, Banner
from werkzeug.utils import secure_filename
from flask import current_app
from functools import wraps
from datetime import datetime, timedelta, date
from decimal import Decimal
from sqlalchemy.orm import joinedload
import random
import string
import os
import uuid
import json
import hashlib
import jwt
import re

api_bp = Blueprint('api', __name__)
# 文件上传配置
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

# 管理员认证配置
# 密钥一律从 .env 读取，不要写死在代码里（代码会进 Git 仓库，写死等于公开）
ADMIN_SECRET_KEY = os.getenv('ADMIN_SECRET_KEY', 'dev-only-change-me')
ADMIN_TOKEN_EXPIRE_HOURS = 24
# JWT 配置
ACCESS_SECRET_KEY = os.getenv('ACCESS_SECRET_KEY', 'dev-only-change-me')
ACCESS_TOKEN_EXPIRE_MINUTES = 30
REFRESH_TOKEN_EXPIRE_DAYS = 30
# 验证key
USER_SECRET_KEY = os.getenv('USER_SECRET_KEY', 'dev-only-change-me')

# 演示用超级管理员账号，同样从 .env 读（不配置则这个后门不生效）
DEMO_ADMIN_USER = os.getenv('DEMO_ADMIN_USER', '')
DEMO_ADMIN_PASS = os.getenv('DEMO_ADMIN_PASS', '')

# 生成token函数
def generate_access_token(user_id):
    payload = {
        'user_id': user_id,
        'exp': datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    }
    return jwt.encode(payload, ACCESS_SECRET_KEY, algorithm='HS256')

def generate_refresh_token():
    return hashlib.sha256(os.urandom(64)).hexdigest()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'code': 401, 'message': '未提供有效的认证令牌'}), 401
        token = auth_header.split(' ')[1]
        try:
            payload = jwt.decode(token, ACCESS_SECRET_KEY, algorithms=['HS256'])
            request.current_user_id = payload['user_id']
        except jwt.ExpiredSignatureError:
            return jsonify({'code': 401, 'message': '令牌已过期，请重新登录'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'code': 401, 'message': '无效的令牌'}), 401
        return f(*args, **kwargs)
    return decorated

# 生成订单号
def generate_order_number():
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    random_str = ''.join(random.choices(string.digits, k=6))
    return f'DD{timestamp}{random_str}'


# 创建订单
@api_bp.route('/orders', methods=['POST'])
@login_required
def create_order():
    """
    创建订单（支持商品、服务、混合订单）
    """
    data = request.get_json()
    user_id = request.current_user_id
    items = data.get('items', [])
    address_type = data.get('address_type', 'pickup')
    delivery_info = data.get('delivery_info', {})
    pond_id = data.get('pond_id')
    seat_id = data.get('seat_id')
    user_coupon_id = data.get('user_coupon_id')
    remark = data.get('remark', '')
    # 以下字段主要用于纯服务订单（整体预约信息），混合订单中每个服务项有自己的预约信息
    service_date = data.get('service_date')
    service_time_slot = data.get('service_time_slot')
    participant_count = data.get('participant_count', 1)

    if not user_id or not items:
        return jsonify({'code': 1, 'message': '参数不完整'})

    # 空字符串转为 None
    if service_date == '':
        service_date = None
    if service_time_slot == '':
        service_time_slot = None
    participant_count = data.get('participant_count', 1)

    try:
        user = User.query.get(user_id)
        if not user:
            return jsonify({'code': 1, 'message': '用户不存在'})

        total_amount = Decimal('0.00')
        discount = Decimal('0.00')

        # ========== 第一遍遍历：校验库存、名额、计算总金额 ==========
        for item in items:
            item_type = item.get('item_type', 'product')
            if item_type == 'product':
                product_id = item.get('product_id')
                if not product_id:
                    db.session.rollback()
                    return jsonify({'code': 1, 'message': '商品ID缺失'})
                product = Product.query.get(product_id)
                if not product:
                    db.session.rollback()
                    return jsonify({'code': 1, 'message': f'商品不存在'})
                quantity = int(item.get('quantity', 1))
                if product.stock < quantity:
                    db.session.rollback()
                    return jsonify({'code': 1, 'message': f'商品 {product.name} 库存不足'})
                # 扣减库存（暂扣，支付失败需回滚）
                product.stock -= quantity
                # 后端按规格自己算价，不信任前端传来的 unit_price
                real_unit = calc_attr_price(
                    product,
                    item.get('attributes') or item.get('selected_attributes')
                )
                item['_unit_price'] = Decimal(str(real_unit))
                total_amount += item['_unit_price'] * quantity
            else:  # service
                service_id = item.get('service_id')
                if not service_id:
                    db.session.rollback()
                    return jsonify({'code': 1, 'message': '服务ID缺失'})
                service = FishingService.query.get(service_id)
                if not service:
                    db.session.rollback()
                    return jsonify({'code': 1, 'message': f'服务不存在'})
                if service.is_end:
                    db.session.rollback()
                    return jsonify({'code': 1, 'message': f'服务已结束，无法预约'})
                people_count = int(item.get('participant_count', 1))
                if service.max_people > 0:
                    if service.booked_people + people_count > service.max_people:
                        db.session.rollback()
                        return jsonify({'code': 1, 'message': f'{service.name} 预约人数已达上限'})
                    service.booked_people += people_count
                # 服务价格以数据库为准，不用前端传的
                item['_unit_price'] = Decimal(str(service.price or 0))
                total_amount += item['_unit_price'] * people_count   # 只累加一次，后面创建订单项时不再累加

        # ========== 优惠券处理 ==========
        used_coupon = None
        if user_coupon_id:
            user_coupon = UserCoupon.query.filter_by(id=user_coupon_id, user_id=user_id, used_at=None).first()
            if user_coupon and user_coupon.coupon.expire_date >= date.today() and total_amount >= user_coupon.coupon.min_amount:
                discount = Decimal(str(user_coupon.coupon.value))
                total_amount -= discount
                if total_amount < 0:
                    total_amount = Decimal('0.00')
                used_coupon = user_coupon

        # ========== 计算订单类型 ==========
        has_product = any(item.get('item_type') == 'product' for item in items)
        has_service = any(item.get('item_type') == 'service' for item in items)
        if has_product and has_service:
            order_type = 'mixed'
        elif has_product:
            order_type = 'product'
        else:
            order_type = 'service'

        # ========== 创建订单主表 ==========
        order = Order(
            user_id=user_id,
            order_number=generate_order_number(),
            total_amount=total_amount,
            remark=remark,
            payment_method=3,          # 余额支付
            address_type=address_type,
            receiver_name=delivery_info.get('name') if address_type == 'delivery' else None,
            receiver_phone=delivery_info.get('phone') if address_type == 'delivery' else None,
            receiver_address=delivery_info.get('address') if address_type == 'delivery' else None,
            pond_id=pond_id if address_type == 'pickup' else None,
            seat_id=seat_id if address_type == 'pickup' else None,
            order_status=0,            # 待付款
            expire_time=datetime.now() + timedelta(minutes=30),
            item_type=order_type,
            service_date=service_date,          # 纯服务订单使用
            service_time_slot=service_time_slot,
            participant_count=participant_count
        )
        db.session.add(order)
        db.session.flush()   # 获取 order.id

        # ========== 创建订单商品/服务快照（order_items） ==========
        for item in items:
            item_type = item.get('item_type', 'product')
            if item_type == 'product':
                # 商品项
                order_item = OrderItem(
                    order_id=order.id,
                    product_id=item['product_id'],
                    product_name=item.get('product_name', ''),
                    product_image=item.get('product_image', ''),
                    product_attributes=item.get('attributes', []),
                    quantity=item.get('quantity', 1),
                    unit_price=item.get('_unit_price', Decimal('0.00')),
                    total_price=item.get('_unit_price', Decimal('0.00')) * item.get('quantity', 1),
                    item_type='product'
                )
            else:
                # 服务项：优先使用当前服务项中的预约信息，如果为空则使用订单主表的预约信息（针对纯服务订单）
                service_id = item['service_id']
                service = FishingService.query.get(service_id)
                # 人数：优先使用当前服务项的 participant_count，否则使用订单级 participant_count
                people_count = int(item.get('participant_count', participant_count))
                # 日期：优先使用当前服务项的 service_date，否则使用订单级 service_date
                service_date_str = item.get('service_date')
                if not service_date_str and order_type == 'service':
                    service_date_str = service_date
                # 场次：优先使用当前服务项的 service_time_slot，否则使用订单级 service_time_slot
                service_time_slot_item = item.get('service_time_slot')
                if not service_time_slot_item and order_type == 'service':
                    service_time_slot_item = service_time_slot
                # 转换日期字符串为 date 对象
                service_date_obj = None
                if service_date_str:
                    try:
                        service_date_obj = datetime.strptime(service_date_str, '%Y-%m-%d').date()
                    except Exception:
                        pass
                order_item = OrderItem(
                    order_id=order.id,
                    service_id=service_id,
                    product_name=service.name,
                    product_image=service.cover_image,
                    unit_price=item.get('_unit_price', Decimal('0.00')),
                    total_price=item.get('_unit_price', Decimal('0.00')) * people_count,
                    item_type='service',
                    quantity=1,                       # 服务数量固定为1
                    service_date=service_date_obj,
                    service_time_slot=service_time_slot_item,
                    participant_count=people_count
                )
            db.session.add(order_item)

        # 关联优惠券（支付成功后才真正标记已使用，这里只保存关系）
        if used_coupon:
            order.user_coupon_id = user_coupon_id

        db.session.commit()
        return jsonify({
            'code': 0,
            'data': {
                'order_id': order.id,
                'order_number': order.order_number,
                'total_amount': float(total_amount),
                'discount': float(discount)
            }
        })

    except Exception as e:
        db.session.rollback()
        print(f"创建订单失败: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'code': 1, 'message': f'创建订单失败: {str(e)}'})

# 支付订单
@api_bp.route('/orders/<int:order_id>/pay', methods=['POST'])
@login_required
def pay_order(order_id):
    data = request.get_json()
    user_id = request.current_user_id
    if not user_id:
        return jsonify({'code': 1, 'message': '缺少用户ID'})

    order = Order.query.get(order_id)
    if not order or order.user_id != user_id:
        return jsonify({'code': 1, 'message': '订单不存在'})

    if order.order_status != 0:
        return jsonify({'code': 1, 'message': '订单状态不允许支付'})

    if order.expire_time and order.expire_time < datetime.now():
        return jsonify({'code': 1, 'message': '订单已过期'})

    user = User.query.get(user_id)
    if user.balance < order.total_amount:
        return jsonify({'code': 1, 'message': '余额不足'})

    # 扣减余额
    user.balance -= order.total_amount

    # 更新订单状态
    order.order_status = 1
    order.payment_status = 1

    # 标记优惠券为已使用
    if order.user_coupon_id:
        user_coupon = UserCoupon.query.get(order.user_coupon_id)
        if user_coupon:
            user_coupon.used_at = datetime.now()
            user_coupon.order_id = order.id

    # 删除购物车中对应的项
    for item in order.items:
        if item.item_type == 'product':
            cart_item = Cart.query.filter_by(
                user_id=user_id,
                product_id=item.product_id
            ).first()
        else:
            cart_item = Cart.query.filter_by(
                user_id=user_id,
                item_type='service',
                service_id=item.service_id,
                service_date=item.service_date,
                service_time_slot=item.service_time_slot
            ).first()
        if cart_item:
            db.session.delete(cart_item)

    # ========== 消息通知 ==========
    # 订单支付成功后通知用户
    notification = Notification(
        user_id=order.user_id,
        type='order',
        title='订单支付成功',
        content=f'您的订单 {order.order_number} 已支付成功，请等待处理。',
        related_id=order.id,
        related_type='order'
    )
    db.session.add(notification)

    db.session.commit()
    return jsonify({'code': 0, 'message': '支付成功'})

# 确认使用
@api_bp.route('/orders/<int:order_id>/confirm_use', methods=['POST'])
@login_required
def confirm_use(order_id):
    data = request.get_json()
    user_id = request.current_user_id
    if not user_id:
        return jsonify({'code': 1, 'message': '缺少用户ID'}), 400

    order = Order.query.get(order_id)
    if not order or order.user_id != user_id:
        return jsonify({'code': 1, 'message': '订单不存在'}), 404

    if order.order_status != 1 or getattr(order, 'item_type', 'product') != 'service':
        return jsonify({'code': 1, 'message': '订单状态无法确认使用'}), 400

    order.order_status = 2  # 已完成

    # ========== 消息通知 ==========
    notification = Notification(
        user_id=order.user_id,
        type='order',
        title='服务已确认使用',
        content=f'您的服务订单 {order.order_number} 已完成，期待您的再次光临！',
        related_id=order.id,
        related_type='order'
    )
    db.session.add(notification)

    db.session.commit()
    return jsonify({'code': 0, 'message': '确认使用成功'})

# 取消订单
@api_bp.route('/orders/<int:order_id>/cancel', methods=['POST'])
@login_required
def cancel_order(order_id):
    data = request.get_json()
    user_id = request.current_user_id
    if not user_id:
        return jsonify({'code': 1, 'message': '缺少用户ID'})

    order = Order.query.get(order_id)
    if not order or order.user_id != user_id:
        return jsonify({'code': 1, 'message': '订单不存在'})

    if order.order_status != 0:
        return jsonify({'code': 1, 'message': '订单状态无法取消'})

    # 回滚库存
    for item in order.items:
        product = Product.query.get(item.product_id)
        if product:
            product.stock += item.quantity

    # 在取消订单的代码中，如果是服务订单则释放名额
    if order.item_type == 'service':
        for item in order.items:
            if item.service_id:
                service = FishingService.query.get(item.service_id)
                if service and service.booked_people >= item.quantity:
                    service.booked_people -= item.quantity


    order.order_status = 3  # 已取消
    db.session.commit()
    return jsonify({'code': 0, 'message': '订单已取消'})

# 确认收货
@api_bp.route('/orders/<int:order_id>/confirm', methods=['POST'])
@login_required
def confirm_order(order_id):
    data = request.get_json()
    user_id = request.current_user_id
    if not user_id:
        return jsonify({'code': 1, 'message': '缺少用户ID'})

    order = Order.query.get(order_id)
    if not order or order.user_id != user_id:
        return jsonify({'code': 1, 'message': '订单不存在'})

    if order.order_status != 1:
        return jsonify({'code': 1, 'message': '订单状态无法确认收货'})

    order.order_status = 2  # 已完成

    # ========== 消息通知 ==========
    notification = Notification(
        user_id=order.user_id,
        type='order',
        title='订单已确认收货',
        content=f'您的订单 {order.order_number} 已完成，感谢您的购买！',
        related_id=order.id,
        related_type='order'
    )
    db.session.add(notification)

    db.session.commit()
    return jsonify({'code': 0, 'message': '确认收货成功'})

# 获取用户订单列表（支持状态筛选）
@api_bp.route('/orders', methods=['GET'])
@login_required
def get_orders():
    user_id = request.current_user_id
    status = request.args.get('status', type=int)
    if not user_id:
        return jsonify({'code': 1, 'message': '缺少用户ID'})

    query = Order.query.filter_by(user_id=user_id).options(
        joinedload(Order.items).joinedload(OrderItem.product)
    )
    if status is not None:
        query = query.filter_by(order_status=status)

    orders = query.order_by(Order.created_at.desc()).all()
    order_list = []
    for order in orders:
        order_data = {
            'id': order.id,
            'order_number': order.order_number,
            'total_amount': float(order.total_amount),
            'order_status': order.order_status,
            'created_at': order.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'item_type': getattr(order, 'item_type', 'product'),
            'service_date': order.service_date.strftime('%Y-%m-%d') if hasattr(order, 'service_date') and order.service_date else None,
            'service_time_slot': getattr(order, 'service_time_slot', None),
            'participant_count': getattr(order, 'participant_count', 1),
            'items': [],
            'expire_time': order.expire_time.strftime('%Y-%m-%d %H:%M:%S') if order.expire_time else None,
        }
        for item in order.items:
            if item.item_type == 'service':
                # 查询服务评价
                reviewed = ServiceReview.query.filter_by(order_item_id=item.id).first() is not None
                service = FishingService.query.get(item.service_id)
                order_data['items'].append({
                    'id': item.id,
                    'item_type': 'service',
                    'service_id': item.service_id,
                    'product_name': item.product_name,
                    'product_image': item.product_image,
                    'quantity': item.quantity,
                    'unit_price': float(item.unit_price),
                    'total_price': float(item.total_price),
                    'reviewed': reviewed,
                    'merchant_id': service.merchant_id if service else 0,
                    'service_date': item.service_date.strftime('%Y-%m-%d') if item.service_date else None,
                    'service_time_slot': item.service_time_slot,
                    'participant_count': item.participant_count,
                    'is_competition': service.service_type == 'competition' if service else False,
                    'service_type': service.service_type if service else None
                })
            else:
                # 商品项
                product = item.product
                reviewed = Review.query.filter_by(order_item_id=item.id).first() is not None
                order_data['items'].append({
                    'id': item.id,
                    'product_id': item.product_id,
                    'merchant_id': product.merchant_id if product else 0,
                    'reviewed': reviewed,
                    'product_name': item.product_name,
                    'product_image': item.product_image,
                    'product_attributes': item.product_attributes or [],
                    'quantity': item.quantity,
                    'unit_price': float(item.unit_price),
                    'total_price': float(item.total_price),
                    'item_type': 'product'
                })
        order_list.append(order_data)

    return jsonify({'code': 0, 'data': order_list})

# 获取某个订单详情
@api_bp.route('/orders/<int:order_id>', methods=['GET'])
@login_required
def get_order_detail(order_id):
    user_id = request.current_user_id
    if not user_id:
        return jsonify({'code': 1, 'message': '缺少用户ID'}), 400

    order = Order.query.options(joinedload(Order.items)).get(order_id)
    if not order or order.user_id != user_id:
        return jsonify({'code': 1, 'message': '订单不存在'}), 404

    order_data = {
        'id': order.id,
        'order_number': order.order_number,
        'total_amount': float(order.total_amount),
        'order_status': order.order_status,
        'address_type': order.address_type,
        'receiver_name': order.receiver_name,
        'receiver_phone': order.receiver_phone,
        'receiver_address': order.receiver_address,
        'pond_id': order.pond_id,
        'seat_id': order.seat_id,
        'remark': order.remark,
        'created_at': order.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        'expire_time': order.expire_time.strftime('%Y-%m-%d %H:%M:%S') if order.expire_time else None,
        'item_type': getattr(order, 'item_type', 'product'),
        'service_date': order.service_date.strftime('%Y-%m-%d') if hasattr(order, 'service_date') and order.service_date else None,
        'service_time_slot': getattr(order, 'service_time_slot', None),
        'participant_count': getattr(order, 'participant_count', 1),
        'items': []
    }
    for item in order.items:
        # 根据订单项的类型区分处理
        if item.item_type == 'service':
            # 获取服务详情（用于获取地址）
            service = FishingService.query.get(item.service_id)
            service_address = service.address if service else ''
            order_data['items'].append({
                'id': item.id,
                'item_type': 'service',
                'service_id': item.service_id,
                'product_name': item.product_name,
                'product_image': item.product_image,
                'quantity': item.quantity,
                'unit_price': float(item.unit_price),
                'total_price': float(item.total_price),
                'address': service_address,          # 服务地址
                # 服务预约信息（如果订单主表有）
                'service_date': item.service_date.strftime('%Y-%m-%d') if item.service_date else None,
                'service_time_slot': item.service_time_slot,
                'participant_count': item.participant_count,
                'is_competition': service.service_type == 'competition' if service else False
            })
        else:
            # 商品项
            order_data['items'].append({
                'id': item.id,
                'item_type': 'product',
                'product_id': item.product_id,
                'product_name': item.product_name,
                'product_image': item.product_image,
                'product_attributes': item.product_attributes or [],
                'quantity': item.quantity,
                'unit_price': float(item.unit_price),
                'total_price': float(item.total_price)
            })
    return jsonify({'code': 0, 'data': order_data})

# 用户排行榜接口
@api_bp.route('/user/rank', methods=['GET'])
def get_user_rank():
    """获取用户排行榜"""
    try:
        # 获取参数
        limit = request.args.get('limit', type=int, default=10)  # 默认返回前10名
        rank_type = request.args.get('type', 'comprehensive')  # 排序类型

        # 查询所有普通用户
        users = User.query.filter(User.role == 'user').all()

        # 计算每个用户的综合评分
        user_scores = []
        for user in users:
            if rank_type == 'comprehensive':
                # 综合评分
                score = calculate_user_score(user)
            else:
                # 按单个字段排序
                score = getattr(user, rank_type, 0)

            user_scores.append({
                'user': user,
                'score': score
            })

        # 按分数排序
        user_scores.sort(key=lambda x: x['score'], reverse=True)

        # 构建响应数据
        rank_list = []
        for index, item in enumerate(user_scores[:limit]):
            user = item['user']

            # 根据排名分配标签
            if index == 0:
                tag_url = '/static/images/first.png'  # 金牌
            elif index == 1:
                tag_url = '/static/images/second.png'  # 银牌
            elif index == 2:
                tag_url = '/static/images/third.png'  # 铜牌
            else:
                tag_url = None

            # 处理头像URL：只保证有兜底图，保持相对路径。
            # 由 app/__init__.py 的 after_request 统一拼 STATIC_BASE_URL。
            # 千万不要在这里硬编码 IP —— 一换服务器/域名就全错。
            avatar_url = user.avatar or '/static/images/user-avatar.jpg'

            user_data = {
                'id': user.id,
                'avatar': avatar_url,
                'nickname': user.nickname,
                'all_weight': str(user.fish_count),  # 累计鱼获
                'tag': tag_url,
                'rank': index + 1,  # 排名
                'score': item['score'],  # 综合评分
                'fish_count': user.fish_count,
                'fishing_count': user.fishing_count,
                'over_hundred_count': user.over_hundred_count,
                'champion_count': user.champion_count
            }
            rank_list.append(user_data)

        return jsonify({
            'code': 0,
            'message': '获取成功',
            'data': rank_list
        })

    except Exception as e:
        print(f'获取排行榜错误: {str(e)}')
        return jsonify({
            'code': 500,
            'message': f'服务器错误: {str(e)}',
            'data': []
        }), 500


def calculate_user_score(user):
    """计算用户综合评分"""
    # 权重分配
    weights = {
        'fish_count': 0.4,  # 鱼获数量权重 40%
        'fishing_count': 0.2,  # 钓鱼次数权重 20%
        'over_hundred_count': 0.2,  # 过百次数权重 20%
        'champion_count': 0.2  # 坑冠次数权重 20%
    }

    # 计算加权分数
    score = (
            user.fish_count * weights['fish_count'] +
            user.fishing_count * weights['fishing_count'] +
            user.over_hundred_count * weights['over_hundred_count'] +
            user.champion_count * weights['champion_count']
    )

    return round(score, 2)

# 用户头像更新接口
@api_bp.route('/user/avatar', methods=['POST'])
@login_required
def update_user_avatar():
    """更新用户头像"""
    try:
        # 添加调试信息
        print("=== 用户头像更新接口调试信息 ===")
        print("请求方法:", request.method)
        print("请求头:", dict(request.headers))
        print("请求表单:", request.form)
        print("请求文件:", request.files)
        print("=====================")

        # 检查是否有文件
        if 'avatar' not in request.files:
            print("❌ 没有找到 'avatar' 字段")
            return jsonify({
                'code': 400,
                'message': '没有上传头像文件'
            }), 400

        file = request.files.get('avatar')
        user_id = request.current_user_id   # 从 token 获取，不再需要 form 中的 user_id

        if not user_id:
            return jsonify({
                'code': 400,
                'message': '用户ID不能为空'
            }), 400

        # 检查文件名
        if file.filename == '':
            print("❌ 文件名为空")
            return jsonify({
                'code': 400,
                'message': '没有选择文件'
            }), 400

        # 检查文件大小
        file.seek(0, 2)  # 移动到文件末尾
        file_size = file.tell()
        file.seek(0)  # 重置文件指针

        if file_size > MAX_FILE_SIZE:
            return jsonify({
                'code': 400,
                'message': f'文件大小不能超过{MAX_FILE_SIZE // 1024 // 1024}MB'
            }), 400

        # 验证用户是否存在
        user = User.query.get(user_id)
        if not user:
            return jsonify({
                'code': 404,
                'message': '用户不存在'
            }), 404

        # 保存文件
        file_path = save_uploaded_file(file)
        if not file_path:
            return jsonify({
                'code': 400,
                'message': '不支持的文件类型，仅支持图片文件'
            }), 400

        # 更新用户头像
        user.avatar = file_path
        db.session.commit()

        print(f"✅ 用户头像更新成功: {file_path}")

        return jsonify({
            'code': 0,
            'message': '头像更新成功',
            'data': {
                'avatar_url': file_path,
                'user_id': user_id
            }
        })

    except Exception as e:
        db.session.rollback()
        print(f"❌ 头像更新异常: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'code': 500,
            'message': f'头像更新失败: {str(e)}'
        }), 500

# 管理员认证装饰器
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.headers.get('Authorization')
        if not token:
            return jsonify({'code': 401, 'message': '缺少认证令牌'}), 401

        # 移除 Bearer 前缀
        if token.startswith('Bearer '):
            token = token[7:]

        try:
            # 验证令牌
            payload = jwt.decode(token, ADMIN_SECRET_KEY, algorithms=['HS256'])
            request.admin_id = payload.get('admin_id')
            request.admin_role = payload.get('role')
        except jwt.ExpiredSignatureError:
            return jsonify({'code': 401, 'message': '令牌已过期'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'code': 401, 'message': '无效令牌'}), 401

        return f(*args, **kwargs)

    return decorated_function


# 生成管理员令牌
def generate_admin_token(admin_id, username, role='admin'):
    payload = {
        'admin_id': admin_id,
        'username': username,
        'role': role,
        'exp': datetime.utcnow() + timedelta(hours=ADMIN_TOKEN_EXPIRE_HOURS)
    }
    return jwt.encode(payload, ADMIN_SECRET_KEY, algorithm='HS256')

# 审核申请api
@api_bp.route('/admin/applications/review', methods=['POST'])
@admin_required
def review_application():
    """审核商家申请"""
    content_type = request.headers.get('Content-Type', '').lower()
    if not any(x in content_type for x in ['application/json', 'text/json']):
        return jsonify({
            'code': 415,
            'message': 'Unsupported Media Type: Content-Type must be application/json.'
        }), 415

    try:
        data = request.get_json()
        application_id = data.get('application_id')
        action = data.get('action')  # 'approve' 或 'reject'
        reject_reason = data.get('reject_reason', '')

        if not application_id or not action:
            return jsonify({
                'code': 400,
                'message': '申请ID和操作类型不能为空'
            }), 400

        if action not in ['approve', 'reject']:
            return jsonify({
                'code': 400,
                'message': '操作类型必须是 approve 或 reject'
            }), 400

        # 查找申请记录
        application = Merchant.query.get(application_id)
        if not application:
            return jsonify({
                'code': 404,
                'message': '申请记录不存在'
            }), 404

        # 检查申请状态
        if application.merchant_status != 'pending':
            return jsonify({
                'code': 400,
                'message': '该申请已被处理，无法重复审核'
            }), 400

        if action == 'approve':
            application.merchant_status = 'approved'
            application.approve_time = db.func.current_timestamp()
            application.reject_reason = None

            # 更新用户角色为商家
            user = User.query.get(application.user_id)
            if user:
                user.role = 'merchant'

            message = '申请已通过'

        else:  # reject
            if not reject_reason:
                return jsonify({
                    'code': 400,
                    'message': '拒绝原因不能为空'
                }), 400

            application.merchant_status = 'rejected'
            application.reject_reason = reject_reason
            message = '申请已拒绝'

        db.session.commit()

        # 记录操作日志（这里可以添加日志记录逻辑）
        print(f"管理员 {request.admin_id} 审核了申请 {application_id}，操作: {action}")

        return jsonify({
            'code': 0,
            'message': message,
            'data': {
                'application_id': application.id,
                'merchant_status': application.merchant_status,
                'review_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
        })

    except Exception as e:
        db.session.rollback()
        print(f'审核申请错误: {str(e)}')
        return jsonify({
            'code': 500,
            'message': f'审核失败: {str(e)}'
        }), 500

# 申请列表api
@api_bp.route('/admin/applications', methods=['GET'])
@admin_required
def get_applications_list():
    """获取申请列表（支持分页和搜索）"""
    try:
        # 获取查询参数
        page = request.args.get('page', 1, type=int)
        page_size = request.args.get('page_size', 10, type=int)
        keyword = request.args.get('keyword', '')

        # 构建查询
        query = Merchant.query

        # 关键词搜索（店铺名称或联系电话）
        if keyword:
            query = query.filter(
                db.or_(
                    Merchant.shop_name.ilike(f'%{keyword}%'),
                    Merchant.contact_phone.ilike(f'%{keyword}%')
                )
            )

        # 分页
        pagination = query.order_by(Merchant.apply_time.desc()).paginate(
            page=page, per_page=page_size, error_out=False
        )

        applications = pagination.items
        total = pagination.total

        # 构建响应数据
        applications_data = []
        for app in applications:
            # 获取用户信息
            user = User.query.get(app.user_id)
            user_info = {
                'username': user.username if user else '未知用户',
                'nickname': user.nickname if user else '',
                'mobile': user.mobile if user else ''
            }

            app_data = {
                'id': app.id,
                'user_id': app.user_id,
                'user_info': user_info,
                'shop_name': app.shop_name,
                'contact_phone': app.contact_phone,
                'shop_address': app.shop_address,
                'business_scope': app.business_scope,
                'description': app.description,
                'business_license': app.business_license,
                'id_card_front': app.id_card_front,
                'id_card_back': app.id_card_back,
                'merchant_status': app.merchant_status,
                'apply_time': app.apply_time.strftime('%Y-%m-%d %H:%M:%S') if app.apply_time else '',
                'approve_time': app.approve_time.strftime('%Y-%m-%d %H:%M:%S') if app.approve_time else '',
                'reject_reason': app.reject_reason
            }
            applications_data.append(app_data)

        return jsonify({
            'code': 0,
            'message': '获取成功',
            'data': {
                'list': applications_data,
                'total': total,
                'page': page,
                'page_size': page_size,
                'total_pages': (total + page_size - 1) // page_size
            }
        })

    except Exception as e:
        print(f'获取申请列表错误: {str(e)}')
        return jsonify({
            'code': 500,
            'message': f'服务器错误: {str(e)}'
        }), 500

# 最近申请api
@api_bp.route('/admin/applications/recent', methods=['GET'])
@admin_required
def get_recent_applications():
    """获取最近申请"""
    try:
        # 获取最近5条申请，按申请时间倒序
        recent_apps = Merchant.query.order_by(Merchant.apply_time.desc()).limit(5).all()

        applications_data = []
        for app in recent_apps:
            app_data = {
                'id': app.id,
                'shop_name': app.shop_name,
                'contact_phone': app.contact_phone,
                'apply_time': app.apply_time.strftime('%Y-%m-%d %H:%M:%S') if app.apply_time else '',
                'merchant_status': app.merchant_status
            }
            applications_data.append(app_data)

        return jsonify({
            'code': 0,
            'message': '获取成功',
            'data': applications_data
        })

    except Exception as e:
        print(f'获取最近申请错误: {str(e)}')
        return jsonify({
            'code': 500,
            'message': f'服务器错误: {str(e)}'
        }), 500

# 申请统计api
@api_bp.route('/admin/applications/stats', methods=['GET'])
@admin_required
def get_applications_stats():
    """获取申请统计"""
    try:
        # 统计各种状态的申请数量
        pending_count = Merchant.query.filter_by(merchant_status='pending').count()
        approved_count = Merchant.query.filter_by(merchant_status='approved').count()
        rejected_count = Merchant.query.filter_by(merchant_status='rejected').count()
        total_count = Merchant.query.count()

        return jsonify({
            'code': 0,
            'message': '获取成功',
            'data': {
                'pendingApplications': pending_count,
                'approvedApplications': approved_count,
                'rejectedApplications': rejected_count,
                'totalApplications': total_count
            }
        })

    except Exception as e:
        print(f'获取申请统计错误: {str(e)}')
        return jsonify({
            'code': 500,
            'message': f'服务器错误: {str(e)}'
        }), 500

# 管理员登录api
@api_bp.route('/admin/login', methods=['POST'])
def admin_login():
    """管理员登录"""
    content_type = request.headers.get('Content-Type', '').lower()
    if not any(x in content_type for x in ['application/json', 'text/json']):
        return jsonify({
            'code': 415,
            'message': 'Unsupported Media Type: Content-Type must be application/json.'
        }), 415

    try:
        data = request.get_json()
        username = data.get('username')
        password = data.get('password')

        if not username or not password:
            return jsonify({'code': 400, 'message': '用户名和密码不能为空'}), 400

        # 演示用超级管理员账号：账号密码从 .env 读，
        # 代码里不留明文（否则推到公开仓库等于公布后台密码）。
        # 没在 .env 配 DEMO_ADMIN_USER / DEMO_ADMIN_PASS 时，这个分支不生效。
        if DEMO_ADMIN_USER and username == DEMO_ADMIN_USER and password == DEMO_ADMIN_PASS:
            # 生成令牌
            token = generate_admin_token(1, 'superadmin', 'super_admin')

            # 返回管理员信息和令牌
            admin_info = {
                'id': 1,
                'username': 'superadmin',
                'nickname': '超级管理员',
                'role': 'super_admin'
            }

            return jsonify({
                'code': 0,
                'message': '登录成功',
                'data': {
                    'token': token,
                    'admin_info': admin_info
                }
            })
        else:
            return jsonify({'code': 401, 'message': '用户名或密码错误'}), 401

    except Exception as e:
        print(f'管理员登录错误: {str(e)}')
        return jsonify({'code': 500, 'message': f'服务器错误: {str(e)}'}), 500

# 商家申请状态api
@api_bp.route('/merchant/application_status', methods=['GET'])
def get_merchant_application_status():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify(code=1, message='用户ID不能为空')

    application = Merchant.query.filter_by(user_id=user_id).first()
    if not application:
        return jsonify(code=0, data=None)

    return jsonify(code=0, data={
        'shop_name': application.shop_name,
        'contact_phone': application.contact_phone,
        'apply_time': application.apply_time.strftime('%Y-%m-%d %H:%M:%S') if application.apply_time else '',
        'merchant_status': application.merchant_status,
        'reject_reason': application.reject_reason
    })

def allowed_file(filename):
    return '.' in filename and \
        filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def save_uploaded_file(file):
    if file and allowed_file(file.filename):
        file_ext = file.filename.rsplit('.', 1)[1].lower()
        filename = f"{uuid.uuid4().hex}.{file_ext}"
        upload_dir = os.path.join(current_app.root_path, 'static', 'uploads')
        os.makedirs(upload_dir, exist_ok=True)
        file_path = os.path.join(upload_dir, filename)
        try:
            file.save(file_path)
            print(f"文件已保存: {file_path}")
            return f"/static/uploads/{filename}"
        except Exception as e:
            print(f"保存文件异常: {str(e)}")
            return None
    else:
        print(f"文件不允许或无效: {file.filename if file else 'None'}")
        return None


# 文件上传接口
@api_bp.route('/upload', methods=['POST'])
def upload_file():
    # 打印请求基本信息
    print("=== 上传请求调试 ===")
    print("请求方法:", request.method)
    print("请求头 Content-Type:", request.headers.get('Content-Type'))
    print("请求表单 keys:", request.form.keys())
    print("请求文件 keys:", request.files.keys())

    # 检查是否有文件
    if 'file' not in request.files:
        print("错误：没有 'file' 字段")
        return jsonify({'code': 400, 'message': '没有上传文件'}), 400

    file = request.files['file']
    print("文件名:", file.filename)
    print("文件大小:", len(file.read()) if file else 0)
    file.seek(0)  # 重置指针，因为上面 read() 消耗了内容

    # 检查文件名
    if file.filename == '':
        print("错误：文件名为空")
        return jsonify({'code': 400, 'message': '没有选择文件'}), 400

    # 检查文件大小（需要先获取大小）
    file.seek(0, 2)
    file_size = file.tell()
    file.seek(0)
    print("实际文件大小:", file_size)

    if file_size > MAX_FILE_SIZE:
        print(f"文件过大: {file_size} bytes, 限制: {MAX_FILE_SIZE} bytes")
        return jsonify({'code': 400,
                        'message': f'文件大小不能超过{MAX_FILE_SIZE // 1024 // 1024}MB，当前文件{file_size // 1024}KB'}), 400

    # 检查扩展名
    if not allowed_file(file.filename):
        print(f"错误：不允许的文件扩展名 {file.filename}")
        return jsonify({'code': 400, 'message': '不支持的文件类型，仅支持图片文件'}), 400

    # 保存文件
    file_path = save_uploaded_file(file)
    if not file_path:
        print("错误：保存文件失败")
        return jsonify({'code': 400, 'message': '文件保存失败'}), 400

    print(f"成功保存到: {file_path}")
    return jsonify({'code': 0, 'message': '上传成功', 'data': {'url': file_path}})

# 商家入驻申请接口
@api_bp.route('/merchant/apply', methods=['POST'])
def merchant_apply():
    """商家入驻申请"""
    # 检查内容类型
    content_type = request.headers.get('Content-Type', '').lower()
    if not any(x in content_type for x in ['application/json', 'text/json']):
        return jsonify({
            'code': 415,
            'message': 'Unsupported Media Type: Content-Type must be application/json.'
        }), 415

    try:
        data = request.get_json()
        print(f"收到商家申请数据: {data}")  # 添加日志

        # 数据验证
        required_fields = ['userId', 'shopName', 'contactPhone', 'shopAddress', 'businessScope']
        missing_fields = [field for field in required_fields if not data.get(field)]
        if missing_fields:
            return jsonify({
                'code': 400,
                'message': f'以下字段不能为空: {", ".join(missing_fields)}'
            }), 400

        # 验证图片字段
        image_fields = ['licenseImage', 'idCardFront', 'idCardBack']
        missing_images = [field for field in image_fields if not data.get(field)]
        if missing_images:
            return jsonify({
                'code': 400,
                'message': f'请上传以下图片: {", ".join(missing_images)}'
            }), 400

        user_id = data.get('userId')
        shop_name = data.get('shopName')
        contact_phone = data.get('contactPhone')
        shop_address = data.get('shopAddress')
        business_scope = data.get('businessScope')
        description = data.get('description', '')
        license_image = data.get('licenseImage')
        id_card_front = data.get('idCardFront')
        id_card_back = data.get('idCardBack')

        print(f"验证用户ID: {user_id}")  # 调试日志

        # 验证用户是否存在
        user = User.query.get(user_id)
        if not user:
            print(f"用户 {user_id} 不存在")  # 调试日志
            return jsonify({
                'code': 404,
                'message': '用户不存在'
            }), 404

        print(f"用户 {user_id} 存在，用户名: {user.username}")  # 调试日志

        # 检查用户是否已经提交过申请 - 修改这里！
        existing_application = Merchant.query.filter_by(user_id=user_id).first()
        if existing_application:
            print(f"用户 {user_id} 已有申请，状态: {existing_application.merchant_status}")  # 调试日志

            # 根据申请状态决定是否允许重新申请
            if existing_application.merchant_status == 'pending':
                return jsonify({
                    'code': 409,
                    'message': '您的申请正在审核中，请耐心等待',
                    'data': {
                        'current_status': existing_application.merchant_status
                    }
                }), 409
            elif existing_application.merchant_status == 'approved':
                return jsonify({
                    'code': 409,
                    'message': '您已经是商家，无需再次申请',
                    'data': {
                        'current_status': existing_application.merchant_status
                    }
                }), 409
            elif existing_application.merchant_status == 'rejected':
                # 被拒绝的申请允许重新提交 - 更新现有记录
                print(f"用户 {user_id} 的申请被拒绝，允许重新提交")

                # 更新申请记录
                existing_application.shop_name = shop_name
                existing_application.contact_phone = contact_phone
                existing_application.shop_address = shop_address
                existing_application.business_scope = business_scope
                existing_application.description = description
                existing_application.business_license = license_image
                existing_application.id_card_front = id_card_front
                existing_application.id_card_back = id_card_back
                existing_application.merchant_status = 'pending'  # 重置为待审核
                existing_application.apply_time = db.func.current_timestamp()  # 更新申请时间
                existing_application.approve_time = None  # 清空审核时间
                existing_application.reject_reason = None  # 清空拒绝原因

                db.session.commit()

                print(f"商家申请更新成功，ID: {existing_application.id}")  # 调试日志

                return jsonify({
                    'code': 0,
                    'message': '商家入驻申请重新提交成功',
                    'data': {
                        'apply_id': existing_application.id,
                        'apply_time': existing_application.apply_time.strftime(
                            '%Y-%m-%d %H:%M:%S') if existing_application.apply_time else None
                    }
                })

        # 创建新的商家申请记录（用户第一次申请）
        new_merchant = Merchant(
            user_id=user_id,
            shop_name=shop_name,
            contact_phone=contact_phone,
            shop_address=shop_address,
            business_scope=business_scope,
            description=description,
            business_license=license_image,
            id_card_front=id_card_front,
            id_card_back=id_card_back,
            merchant_status='pending'
        )

        db.session.add(new_merchant)
        db.session.commit()

        print(f"商家申请创建成功，ID: {new_merchant.id}")  # 调试日志

        return jsonify({
            'code': 0,
            'message': '商家入驻申请提交成功',
            'data': {
                'apply_id': new_merchant.id,
                'apply_time': new_merchant.apply_time.strftime('%Y-%m-%d %H:%M:%S') if new_merchant.apply_time else None
            }
        })

    except Exception as e:
        db.session.rollback()
        print(f"商家申请错误: {str(e)}")  # 添加错误日志
        import traceback
        traceback.print_exc()  # 打印完整堆栈跟踪
        return jsonify({
            'code': 500,
            'message': f'提交失败: {str(e)}'
        }), 500

# 商家申请状态查询接口
@api_bp.route('/merchant/status', methods=['GET'])
def get_merchant_status():
    """获取商家申请状态"""
    try:
        user_id = request.args.get('userId', type=int)

        if not user_id:
            return jsonify({
                'code': 400,
                'message': '用户ID不能为空'
            }), 400

        # 查询用户的商家申请记录
        merchant = Merchant.query.filter_by(user_id=user_id).first()

        if not merchant:
            return jsonify({
                'code': 404,
                'message': '未找到商家申请记录',
                'data': None
            }), 404

        return jsonify({
            'code': 0,
            'message': '获取成功',
            'data': merchant.to_dict()
        })

    except Exception as e:
        return jsonify({
            'code': 500,
            'message': f'查询失败: {str(e)}'
        }), 500


# 获取所有商家申请（管理员用）
@api_bp.route('/merchant/applications', methods=['GET'])
def get_merchant_applications():
    """获取所有商家申请（管理员界面使用）"""
    try:
        status = request.args.get('status')  # 可选参数：pending/approved/rejected

        query = Merchant.query
        if status:
            query = query.filter_by(merchant_status=status)

        applications = query.order_by(Merchant.apply_time.desc()).all()

        applications_data = []
        for app in applications:
            app_data = app.to_dict()
            # 添加用户信息
            user = User.query.get(app.user_id)
            if user:
                app_data['user_info'] = {
                    'username': user.username,
                    'nickname': user.nickname,
                    'mobile': user.mobile
                }
            applications_data.append(app_data)

        return jsonify({
            'code': 0,
            'message': '获取成功',
            'data': applications_data
        })

    except Exception as e:
        return jsonify({
            'code': 500,
            'message': f'查询失败: {str(e)}'
        }), 500


# 审核商家申请（管理员用）
@api_bp.route('/merchant/review', methods=['POST'])
def review_merchant_application():
    """审核商家申请"""
    content_type = request.headers.get('Content-Type', '').lower()
    if not any(x in content_type for x in ['application/json', 'text/json']):
        return jsonify({
            'code': 415,
            'message': 'Unsupported Media Type: Content-Type must be application/json.'
        }), 415

    try:
        data = request.get_json()
        application_id = data.get('applicationId')
        action = data.get('action')
        reject_reason = data.get('rejectReason', '')

        if not application_id or not action:
            return jsonify({'code': 400, 'message': '申请ID和操作类型不能为空'}), 400
        if action not in ['approve', 'reject']:
            return jsonify({'code': 400, 'message': '操作类型必须是 approve 或 reject'}), 400

        application = Merchant.query.get(application_id)
        if not application:
            return jsonify({'code': 404, 'message': '申请记录不存在'}), 404

        if action == 'approve':
            application.merchant_status = 'approved'
            application.approve_time = db.func.current_timestamp()
            application.reject_reason = None
            user = User.query.get(application.user_id)
            if user:
                user.role = 'merchant'
            notification_title = '商家入驻申请已通过'
            notification_content = f'您的店铺“{application.shop_name}”已通过审核，现在可以发布商品和服务了。'
            message = '申请已通过'
        else:  # reject
            if not reject_reason:
                return jsonify({'code': 400, 'message': '拒绝原因不能为空'}), 400
            application.merchant_status = 'rejected'
            application.reject_reason = reject_reason
            notification_title = '商家入驻申请被拒绝'
            notification_content = f'您的店铺“{application.shop_name}”申请被拒绝，原因：{reject_reason}'
            message = '申请已拒绝'

        db.session.commit()  # 先提交商家审核状态

        # 创建通知（需在 commit 之后，避免回滚）
        notification = Notification(
            user_id=application.user_id,
            type='system',
            title=notification_title,
            content=notification_content,
            related_id=application.id,
            related_type='merchant_application'
        )
        db.session.add(notification)
        db.session.commit()
        print("=======================================")
        print(f"通知已发送给用户 {application.user_id}: {notification_title}")  # 调试日志

        return jsonify({'code': 0, 'message': message, 'data': application.to_dict()})

    except Exception as e:
        db.session.rollback()
        print(f"审核失败: {str(e)}")  # 打印错误
        return jsonify({'code': 500, 'message': f'审核失败: {str(e)}'}), 500


# 添加收藏
@api_bp.route('/user/favorite', methods=['POST'])
@login_required
def add_favorite():
    """添加收藏"""
    data = request.get_json()
    user_id = request.current_user_id
    goods_id = data.get('goods_id')

    if not goods_id:
        return jsonify({'code': 400, 'message': '商品ID不能为空'}), 400

    # 检查商品是否存在
    product = Product.query.get(goods_id)
    if not product:
        return jsonify({'code': 404, 'message': '商品不存在'}), 404

    # 检查是否已经收藏
    existing_favorite = UserFavorite.query.filter_by(user_id=user_id, goods_id=goods_id).first()
    if existing_favorite:
        return jsonify({'code': 409, 'message': '您已收藏过该商品'}), 409

    # 获取商品快照数据
    from app.utils import serialize_product_detail
    goods_data = serialize_product_detail(product)
    goods_data_str = json.dumps(goods_data, ensure_ascii=False)

    new_favorite = UserFavorite(
        user_id=user_id,
        goods_id=goods_id,
        goods_data=goods_data_str
    )

    db.session.add(new_favorite)
    db.session.commit()

    return jsonify({
        'code': 0,
        'message': '收藏成功',
        'data': {'favorite_id': new_favorite.id}
    })

# 删除收藏
@api_bp.route('/user/favorite/<int:favorite_id>', methods=['DELETE'])
@login_required
def delete_favorite(favorite_id):
    """删除收藏"""
    user_id = request.current_user_id
    favorite = UserFavorite.query.get(favorite_id)

    if not favorite:
        return jsonify({'code': 404, 'message': '收藏记录不存在'}), 404

    if favorite.user_id != user_id:
        return jsonify({'code': 403, 'message': '无权删除'}), 403

    db.session.delete(favorite)
    db.session.commit()

    return jsonify({'code': 0, 'message': '取消收藏成功'})

# 检查收藏状态
@api_bp.route('/user/favorite/check', methods=['GET'])
@login_required
def check_favorite():
    """检查是否已收藏"""
    user_id = request.current_user_id
    goods_id = request.args.get('goodsId', type=int)

    if not goods_id:
        return jsonify({'code': 400, 'message': '商品ID不能为空'}), 400

    favorite = UserFavorite.query.filter_by(user_id=user_id, goods_id=goods_id).first()

    return jsonify({
        'code': 0,
        'data': {
            'exists': favorite is not None,
            'favorite_id': favorite.id if favorite else None
        }
    })

# 获取用户收藏列表
@api_bp.route('/user/favorite', methods=['GET'])
@login_required
def get_user_favorites():
    """获取用户收藏列表"""
    user_id = request.current_user_id

    favorites = UserFavorite.query.filter_by(user_id=user_id).order_by(UserFavorite.created_at.desc()).all()

    favorite_list = []
    for fav in favorites:
        goods_data = json.loads(fav.goods_data) if fav.goods_data else None
        favorite_list.append({
            'id': fav.id,
            'goods_id': fav.goods_id,
            'created_at': fav.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'goods_data': goods_data
        })

    return jsonify({'code': 0, 'data': favorite_list})

# 用户登录
@api_bp.route('/user/login', methods=['POST'])
def user_login():
    content_type = request.headers.get('Content-Type', '').lower()
    if not any(x in content_type for x in ['application/json', 'text/json']):
        return jsonify({'code': 415, 'message': 'Content-Type must be application/json.'}), 415
    try:
        data = request.get_json()
        username = data.get('username')
        password = data.get('password')
        if not username or not password:
            return jsonify({'code': 400, 'message': '用户名和密码不能为空'}), 400
        user = User.query.filter_by(username=username).first()
        if not user:
            return jsonify({'code': 404, 'message': '用户不存在'}), 404
        hashed = hashlib.sha256(password.encode('utf-8')).hexdigest()
        if user.password != hashed:
            return jsonify({'code': 401, 'message': '密码错误'}), 401

        # 生成 access_token（有效期30分钟，与装饰器一致）
        access_token = jwt.encode(
            {'user_id': user.id, 'exp': datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)},
            ACCESS_SECRET_KEY,
            algorithm='HS256'
        )
        # 生成 refresh_token 并存储到数据库
        refresh_token = UserToken.create_for_user(user.id, expires_days=REFRESH_TOKEN_EXPIRE_DAYS)
        user_data = user.to_dict()
        return jsonify({
            'code': 0,
            'data': {
                'user': user_data,
                'access_token': access_token,
                'refresh_token': refresh_token
            }
        })
    except Exception as e:
        return jsonify({'code': 500, 'message': f'服务器错误: {str(e)}'}), 500

# 刷新token接口
@api_bp.route('/user/refresh', methods=['POST'])
def refresh_token():
    data = request.get_json()
    refresh_token = data.get('refresh_token')
    if not refresh_token:
        return jsonify({'code': 400, 'message': '缺少 refresh_token'}), 400
    token_record = UserToken.query.filter_by(refresh_token=refresh_token).first()
    if not token_record or token_record.expires_at < datetime.now():
        return jsonify({'code': 401, 'message': 'refresh_token 无效或已过期'}), 401
    # 生成新的 access_token
    new_access_token = jwt.encode(
        {'user_id': token_record.user_id, 'exp': datetime.utcnow() + timedelta(hours=2)},
        ACCESS_SECRET_KEY,
        algorithm='HS256'
    )
    return jsonify({'code': 0, 'data': {'access_token': new_access_token}})


# 获取当前用户的关联账号列表
@api_bp.route('/user/associations', methods=['GET'])
@login_required
def get_associations():
    user_id = request.current_user_id
    links = UserAccountLink.query.filter_by(master_user_id=user_id).all()
    result = []
    for link in links:
        linked_user = link.linked
        result.append({
            'userId': linked_user.id,
            'username': linked_user.username,
            'nickname': linked_user.nickname,
            'avatar': linked_user.avatar,
            'mobile': linked_user.mobile,
            'balance': float(linked_user.balance)
        })
    return jsonify({'code': 0, 'data': result})


# 用户注册
@api_bp.route('/user/register', methods=['POST'])
def user_register():
    # 添加详细日志
    print("=" * 50)
    print("请求头:", request.headers)
    print("Content-Type:", request.headers.get('Content-Type'))
    print("请求方法:", request.method)
    print("请求数据:", request.data)
    print("=" * 50)
    """用户注册"""
    content_type = request.headers.get('Content-Type', '').lower()
    if not any(x in content_type for x in ['application/json', 'text/json']):
        return jsonify({
            'code': 415,
            'message': f'Unsupported Media Type: Content-Type must be application/json. Received: {content_type}'
        }), 415

    try:
        data = request.get_json()
        username = data.get('username')
        password = data.get('password')
        nickname = data.get('nickname', username)  # 默认使用用户名作为昵称

        if not username or not password:
            return jsonify({'code': 400, 'message': '用户名和密码不能为空'}), 400

        # 检查用户名是否已存在
        if User.query.filter_by(username=username).first():
            return jsonify({'code': 409, 'message': '用户名已存在'}), 409

        # 加密密码
        hashed_password = hashlib.sha256(password.encode('utf-8')).hexdigest()

        # 创建新用户
        new_user = User(
            username=username,
            password=hashed_password,
            nickname=nickname
        )

        db.session.add(new_user)
        db.session.commit()

        return jsonify({'code': 0, 'message': '注册成功', 'data': {'id': new_user.id}})

    except Exception as e:
        db.session.rollback()
        return jsonify({'code': 500, 'message': f'服务器错误: {str(e)}'}), 500

@api_bp.route('/fishing', methods=['GET'])
def get_products():
    """获取所有上架且未删除的产品信息"""
    try:
        # 只返回上架且未删除的商品
        products = Product.query.filter_by(status=1, is_deleted=0).all()

        # 序列化
        serialized_data = serialize_products(products)

        response_data = {
            "code": 200,
            "message": "success",
            "data": serialized_data
        }

        response = make_response(
            json.dumps(response_data, ensure_ascii=False, indent=2)
        )
        response.mimetype = "application/json"
        return response

    except Exception as e:
        import traceback
        traceback.print_exc()

        error_response = {
            "code": 500,
            "message": f"服务器错误: {str(e)}",
            "data": []
        }
        response = make_response(
            json.dumps(error_response, ensure_ascii=False)
        )
        response.mimetype = "application/json"
        return response, 500

# 添加商品详情API
@api_bp.route('/goods/detail/<int:goods_id>', methods=['GET'])
def get_goods_detail(goods_id):
    """获取单个商品详细信息"""
    try:
        # 1. 获取商品基本信息
        product = Product.query.filter_by(id=goods_id, status=1, is_deleted=0).first()
        if not product:
            return jsonify({'code': 404, 'message': '商品不存在或已下架', 'data': None}), 404

        # 2. 获取商品图片
        images = ProductImage.query.filter_by(product_id=goods_id).all()
        image_urls = [img.image_url for img in images]

        # 3. 获取商品标签
        tags = ProductTag.query.filter_by(product_id=goods_id).all()
        tag_list = [tag.tag for tag in tags]

        # 4. 获取商品属性
        attributes = ProductAttribute.query.filter_by(product_id=goods_id).all()
        attribute_list = []
        for attr in attributes:
            try:
                # 解析JSON字符串的属性值
                values = json.loads(attr.attribute_values)

                # 解析价格
                prices = []
                if attr.attribute_prices:  # 直接检查属性对象的价格字段
                    try:
                        prices = json.loads(attr.attribute_prices)
                    except json.JSONDecodeError:
                        # 处理价格解析错误
                        prices = [
                            float(p.strip())
                            for p in attr.attribute_prices.strip('[]').split(',')
                        ]
                else:
                    prices = [0] * len(values)

            except json.JSONDecodeError:
                # 如果解析失败，使用原始字符串分割
                values = attr.attribute_values.strip('[]').replace('"', '').split(',')
                prices = [0] * len(values)

            attribute_list.append({
                'name': attr.attribute_name,
                'values': values,
                'prices': prices,  # 添加价格字段
                'is_absolute_price': attr.is_absolute_price  # 添加价格类型标识
            })

        
        # 构建返回数据
        data = {
            'id': product.id,
            'name': product.name,
            'price': float(product.price),  # 转换为浮点数
            'category': product.category,
            'main_image': product.pic_url,
            'images': image_urls,
            'tags': tag_list,
            'sales': product.sales if product.sales else 0,
            'stock': product.stock if product.stock else 0,
            'detail': product.detail if product.detail else '',
            'attributes': attribute_list,
            'merchant_id': product.merchant_id,
            'merchant_name': product.merchant.nickname if product.merchant else '平台自营'
        }

        return jsonify({'code': 0, 'message': 'success', 'data': data})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'code': 500, 'message': f'服务器错误: {str(e)}', 'data': None}), 500


# 鱼塘、钓位
@api_bp.route('/pond/list', methods=['GET'])
def get_pond_list():
    """获取所有鱼塘列表"""
    try:
        ponds = Pond.query.all()

        pond_list = []
        for pond in ponds:
            pond_list.append({
                'id': pond.id,
                'name': pond.name,
                'location': pond.location,
                'description': pond.description
            })

        return jsonify({
            'code': 0,
            'msg': 'success',
            'data': pond_list
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            'code': 500,
            'msg': f'服务器错误: {str(e)}',
            'data': []
        }), 500


@api_bp.route('/pond/seat', methods=['GET'])
def get_seat_by_pond():
    """根据鱼塘ID获取钓位列表"""
    try:
        pond_id = request.args.get('id', type=int)
        if not pond_id:
            return jsonify({
                'code': 400,
                'msg': '缺少鱼塘ID参数',
                'data': []
            }), 400

        # 获取鱼塘信息
        pond = Pond.query.get(pond_id)
        if not pond:
            return jsonify({
                'code': 404,
                'msg': '鱼塘不存在',
                'data': []
            }), 404

        # 获取可用的钓位 (status=1)
        seats = Seat.query.filter_by(pond_id=pond_id, status=1).all()

        seat_list = []
        for seat in seats:
            seat_list.append({
                'seat_id': seat.id,
                'name': seat.name,
                'type': seat.type,
                'price': float(seat.price),
                'description': seat.description
            })

        return jsonify({
            'code': 0,
            'msg': 'success',
            'data': {
                'pond_id': pond.id,
                'pond_name': pond.name,
                'seats': seat_list
            }
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            'code': 500,
            'msg': f'服务器错误: {str(e)}',
            'data': []
        }), 500


# 首页相关的钓场服务
@api_bp.route('/home/services', methods=['GET'])
def get_home_services():
    """获取首页开钓付费和比赛预约数据（添加时间判断）"""
    try:
        # 获取开钓付费数据 (普通服务)
        pond_services = FishingService.query.filter_by(
            service_type='regular'
        ).order_by(FishingService.id.desc()).limit(2).all()

        # 获取比赛预约数据 (比赛服务) - 这里只获取未结束的比赛
        match_services = FishingService.query.filter_by(
            service_type='competition'
        ).order_by(FishingService.match_time.asc()).all()

        # 过滤出未结束的比赛
        active_matches = []
        for service in match_services:
            is_ended = False
            if service.match_time:
                try:
                    match_time = service.match_time
                    if isinstance(match_time, str):
                        match_time = datetime.strptime(match_time, '%Y-%m-%d %H:%M:%S')
                    is_ended = match_time < datetime.now()
                except Exception:
                    is_ended = service.is_end

            if not is_ended:
                active_matches.append(service)
                if len(active_matches) >= 2:  # 限制数量
                    break

        # 构建响应数据
        data = {
            'ponds': [service.to_dict() for service in pond_services],
            'matches': [service.to_dict() for service in active_matches]
        }

        return jsonify({
            'code': 0,
            'msg': 'success',
            'data': data
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            'code': 500,
            'msg': f'服务器错误: {str(e)}',
            'data': {}
        }), 500


# 完整的钓场服务数据
@api_bp.route('/fishing/services', methods=['GET'])
def get_all_services():
    try:
        services = FishingService.query.filter_by(status=1, is_deleted=0).order_by(FishingService.created_at.desc()).all()
        service_list = []
        for service in services:
            merchant = User.query.get(service.merchant_id) if service.merchant_id else None
            merchant_name = merchant.nickname if merchant else '平台自营'
            merchant_avatar = merchant.avatar if merchant else '/static/images/user-avatar.jpg'

            is_ended = service.is_end
            if service.service_type == 'competition' and service.match_time and service.match_time < datetime.now():
                is_ended = True

            service_data = {
                'id': service.id,
                'name': service.name,
                'details': service.details,
                'price': float(service.price),
                'cover_image': service.cover_image,
                'address': service.address,
                'service_type': service.service_type,
                'match_name': service.match_name,
                'match_detail': service.match_detail,
                'match_time': service.match_time.strftime('%Y-%m-%d %H:%M:%S') if service.match_time else None,
                'is_end': is_ended,
                'tags': service.tags if service.tags else [],
                'merchant_id': service.merchant_id,
                'merchant_name': merchant_name,
                'merchant_avatar': merchant_avatar,
                'created_at': service.created_at.strftime('%Y-%m-%d %H:%M:%S') if service.created_at else ''
            }
            service_list.append(service_data)
        return jsonify({'code': 0, 'msg': 'success', 'data': service_list})
    except Exception as e:
        print(f"获取钓场服务失败: {str(e)}")
        return jsonify({'code': 500, 'msg': f'服务器错误: {str(e)}', 'data': []}), 500

# 获取任意商家的店铺
@api_bp.route('/fishing/services/by_merchant', methods=['GET'])
def get_fishing_services_by_merchant():
    """公开接口：获取指定商家的钓场服务列表（上架且未删除）"""
    merchant_id = request.args.get('merchant_id', type=int)
    if not merchant_id:
        return jsonify({'code': 1, 'message': '缺少商家ID'}), 400
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 10, type=int)
    query = FishingService.query.filter_by(merchant_id=merchant_id, status=1, is_deleted=0)
    pagination = query.order_by(FishingService.created_at.desc()).paginate(page=page, per_page=page_size, error_out=False)
    services = pagination.items
    data = []
    for s in services:
        data.append({
            'id': s.id,
            'name': s.name,
            'price': float(s.price),
            'cover_image': s.cover_image,
            'service_type': s.service_type,
            'is_end': s.is_end,
            'status': s.status,
            'created_at': s.created_at.strftime('%Y-%m-%d %H:%M:%S') if s.created_at else ''
        })
    return jsonify({'code': 0, 'data': {'list': data, 'total': pagination.total, 'page': page, 'page_size': page_size}})

# 商家获取自己的钓场服务列表接口
@api_bp.route('/merchant/fishing_services', methods=['GET'])
@login_required
def get_merchant_fishing_services():
    user_id = request.current_user_id
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 10, type=int)
    if not user_id:
        return jsonify({'code': 1, 'message': '缺少商家ID'}), 400
    query = FishingService.query.filter_by(merchant_id=user_id, is_deleted=0)
    pagination = query.order_by(FishingService.created_at.desc()).paginate(page=page, per_page=page_size, error_out=False)
    services = pagination.items
    total = pagination.total
    data = []
    for s in services:
        data.append({
            'id': s.id,
            'name': s.name,
            'price': float(s.price),
            'cover_image': s.cover_image,
            'service_type': s.service_type,
            'is_end': s.is_end,
            'status': s.status,
            'created_at': s.created_at.strftime('%Y-%m-%d %H:%M:%S') if s.created_at else ''
        })
    return jsonify({'code': 0, 'data': {'list': data, 'total': total, 'page': page, 'page_size': page_size}})

# 发布钓场服务接口
@api_bp.route('/merchant/fishing_services', methods=['POST'])
@login_required
def create_fishing_service():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'code': 1, 'message': '无效的请求数据'}), 400

        user_id = request.current_user_id

        cover_images = data.get('cover_images', [])
        if not cover_images:
            return jsonify({'code': 1, 'message': '封面图不能为空'}), 400
        cover_image = cover_images[0]  # 第一张作为封面

        # 验证必要字段
        if not data.get('name'):
            return jsonify({'code': 1, 'message': '服务名称不能为空'}), 400
        if not data.get('price'):
            return jsonify({'code': 1, 'message': '价格不能为空'}), 400

        # 处理比赛时间
        match_time = None
        if data.get('match_time'):
            try:
                match_time = datetime.strptime(data['match_time'], '%Y-%m-%d %H:%M:%S')
            except Exception as e:
                return jsonify({'code': 1, 'message': f'比赛时间格式错误: {str(e)}'}), 400

        # 处理标签（前端传来的 tags 是列表）
        tags = data.get('tags', [])

        service = FishingService(
            merchant_id=user_id,
            name=data['name'],
            details=data.get('details', ''),
            price=float(data['price']),
            cover_image=cover_image,
            images=cover_images,          # SQLAlchemy 自动序列化为 JSON
            address=data.get('address', ''),
            service_type=data.get('service_type', 'regular'),
            match_name=data.get('match_name', ''),
            match_detail=data.get('match_detail', ''),
            match_time=match_time,
            is_end=data.get('is_end', False),
            tags=tags,                    # 直接赋值列表
            status=1
        )
        db.session.add(service)
        db.session.commit()
        return jsonify({'code': 0, 'message': '发布成功', 'data': {'id': service.id}})
    except Exception as e:
        db.session.rollback()
        print(f"创建钓场服务失败: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'code': 1, 'message': f'服务器错误: {str(e)}'}), 500

# 编辑、删除、上下架接口
@api_bp.route('/merchant/fishing_services/<int:service_id>', methods=['PUT'])
@login_required
def update_fishing_service(service_id):
    try:
        data = request.get_json()
        user_id = request.current_user_id
        service = FishingService.query.get(service_id)
        if not service or service.merchant_id != user_id:
            return jsonify({'code': 1, 'message': '无权限或服务不存在'}), 403

        # 更新多图
        if 'cover_images' in data:
            cover_images = data['cover_images']
            if cover_images:
                service.images = cover_images          # 直接赋值列表
                service.cover_image = cover_images[0]
            else:
                # 允许清空图片
                service.images = []
                service.cover_image = ''

        # 更新其他字段（只更新传入的字段）
        if 'name' in data:
            service.name = data['name']
        if 'details' in data:
            service.details = data['details']
        if 'price' in data:
            service.price = float(data['price'])
        if 'address' in data:
            service.address = data['address']
        if 'service_type' in data:
            service.service_type = data['service_type']
        if 'match_name' in data:
            service.match_name = data['match_name']
        if 'match_detail' in data:
            service.match_detail = data['match_detail']
        if 'match_time' in data:
            match_time_str = data['match_time']
            if match_time_str:
                try:
                    service.match_time = datetime.strptime(match_time_str, '%Y-%m-%d %H:%M:%S')
                except:
                    service.match_time = None
            else:
                service.match_time = None
        if 'is_end' in data:
            service.is_end = data['is_end']
        if 'tags' in data:
            service.tags = data['tags']   # 直接赋值列表

        db.session.commit()
        return jsonify({'code': 0, 'message': '更新成功'})
    except Exception as e:
        db.session.rollback()
        print(f"更新钓场服务失败: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'code': 1, 'message': f'服务器错误: {str(e)}'}), 500


@api_bp.route('/merchant/fishing_services/<int:service_id>', methods=['DELETE'])
@login_required
def delete_fishing_service(service_id):
    user_id = request.current_user_id
    service = FishingService.query.get(service_id)
    if not service or service.merchant_id != user_id:
        return jsonify({'code': 1, 'message': '无权限或服务不存在'}), 403
    service.is_deleted = True
    db.session.commit()
    return jsonify({'code': 0, 'message': '删除成功'})

@api_bp.route('/merchant/fishing_services/<int:service_id>/status', methods=['PATCH'])
@login_required
def update_fishing_service_status(service_id):
    data = request.get_json()
    user_id = request.current_user_id
    new_status = data.get('status')
    service = FishingService.query.get(service_id)
    if not service or service.merchant_id != user_id:
        return jsonify({'code': 1, 'message': '无权限或服务不存在'}), 403
    service.status = new_status
    db.session.commit()
    return jsonify({'code': 0, 'message': '状态更新成功'})


# 单个钓场服务详情接口（供详情页使用）
@api_bp.route('/fishing/service/<int:service_id>', methods=['GET'])
def get_fishing_service_detail(service_id):
    try:
        service = FishingService.query.get(service_id)
        if not service or service.is_deleted or service.status != 1:
            return jsonify({'code': 1, 'message': '服务不存在或未发布'}), 404

        merchant = User.query.get(service.merchant_id)

        # 处理 images 字段（兼容字符串和列表）
        images = []
        if service.images is not None:
            if isinstance(service.images, str):
                try:
                    images = json.loads(service.images)
                except:
                    images = []
            else:
                images = service.images
        # 兼容旧数据：如果没有 images 但有 cover_image，则用 cover_image 作为单图数组
        if not images and service.cover_image:
            images = [service.cover_image]

        # 处理 tags 字段
        tags = []
        if service.tags is not None:
            if isinstance(service.tags, str):
                try:
                    tags = json.loads(service.tags)
                except:
                    tags = []
            else:
                tags = service.tags

        # 动态计算比赛是否结束
        is_ended = service.is_end
        if service.service_type == 'competition' and service.match_time:
            if service.match_time < datetime.now():
                is_ended = True

        data = {
            'id': service.id,
            'name': service.name,
            'details': service.details,
            'price': float(service.price),
            'cover_image': service.cover_image,
            'images': images,
            'address': service.address,
            'service_type': service.service_type,
            'match_name': service.match_name,
            'match_detail': service.match_detail,
            'match_time': service.match_time.strftime('%Y-%m-%d %H:%M:%S') if service.match_time else None,
            'is_end': is_ended,
            'tags': tags,
            'merchant_id': service.merchant_id,
            'merchant_name': merchant.nickname if merchant else '平台',
            'merchant_avatar': merchant.avatar if merchant else '/static/images/user-avatar.jpg'
        }
        return jsonify({'code': 0, 'data': data})
    except Exception as e:
        print(f"获取钓场服务详情失败: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'code': 1, 'message': f'服务器错误: {str(e)}'}), 500

# 获取服务剩余名额
@api_bp.route('/fishing/service/<int:service_id>/quota', methods=['GET'])
def get_service_quota(service_id):
    service = FishingService.query.get(service_id)
    if not service:
        return jsonify({'code': 1, 'message': '服务不存在'}), 404
    remaining = max(0, service.max_people - service.booked_people) if service.max_people > 0 else -1
    return jsonify({'code': 0, 'data': {'remaining': remaining, 'max': service.max_people, 'booked': service.booked_people}})




# 修改手机号接口
@api_bp.route('/user/update_mobile', methods=['POST'])
@login_required
def update_mobile():
    """修改用户手机号"""
    content_type = request.headers.get('Content-Type', '').lower()
    if not any(x in content_type for x in ['application/json', 'text/json']):
        return jsonify({
            'code': 415,
            'message': 'Unsupported Media Type: Content-Type must be application/json.'
        }), 415

    try:
        data = request.get_json()
        user_id = request.current_user_id
        mobile = data.get('mobile')

        if not user_id or not mobile:
            return jsonify({
                'code': 400,
                'message': '用户ID和手机号不能为空'
            }), 400

        # 验证手机号格式
        if not re.match(r'^1[3-9]\d{9}$', mobile):
            return jsonify({
                'code': 400,
                'message': '请输入正确的手机号格式'
            }), 400

        # 查找用户
        user = User.query.get(user_id)
        if not user:
            return jsonify({
                'code': 404,
                'message': '用户不存在'
            }), 404

        # 检查手机号是否已被其他用户使用
        existing_user = User.query.filter_by(mobile=mobile).first()
        if existing_user and existing_user.id != user_id:
            return jsonify({
                'code': 409,
                'message': '手机号已被其他用户使用'
            }), 409

        # 更新手机号
        user.mobile = mobile
        db.session.commit()

        return jsonify({
            'code': 0,
            'message': '手机号修改成功',
            'data': {
                'user_id': user.id,
                'mobile': user.mobile
            }
        })

    except Exception as e:
        db.session.rollback()
        print(f'修改手机号失败: {str(e)}')
        return jsonify({
            'code': 500,
            'message': f'修改失败: {str(e)}'
        }), 500

# 修改密码接口
@api_bp.route('/user/update_password', methods=['POST'])
@login_required
def update_password():
    """修改用户密码"""
    content_type = request.headers.get('Content-Type', '').lower()
    if not any(x in content_type for x in ['application/json', 'text/json']):
        return jsonify({
            'code': 415,
            'message': 'Unsupported Media Type: Content-Type must be application/json.'
        }), 415

    try:
        data = request.get_json()
        user_id = request.current_user_id
        old_password = data.get('old_password')
        new_password = data.get('new_password')

        if not user_id or not old_password or not new_password:
            return jsonify({
                'code': 400,
                'message': '用户ID、原密码和新密码不能为空'
            }), 400

        # 查找用户
        user = User.query.get(user_id)
        if not user:
            return jsonify({
                'code': 404,
                'message': '用户不存在'
            }), 404

        # 验证原密码
        hashed_old_password = hashlib.sha256(old_password.encode('utf-8')).hexdigest()
        if user.password != hashed_old_password:
            return jsonify({
                'code': 401,
                'message': '原密码错误'
            }), 200

        # 验证新密码格式
        if len(new_password) < 6 or len(new_password) > 20:
            return jsonify({
                'code': 400,
                'message': '密码长度需在6-20位之间'
            }), 400

        if not re.match(r'^(?=.*[a-zA-Z])(?=.*\d)', new_password):
            return jsonify({
                'code': 400,
                'message': '密码需包含字母和数字'
            }), 400

        # 加密新密码
        hashed_new_password = hashlib.sha256(new_password.encode('utf-8')).hexdigest()

        # 更新密码
        user.password = hashed_new_password
        db.session.commit()

        return jsonify({
            'code': 0,
            'message': '密码修改成功'
        })

    except Exception as e:
        db.session.rollback()
        print(f'修改密码失败: {str(e)}')
        return jsonify({
            'code': 500,
            'message': f'修改失败: {str(e)}'
        }), 500

# 获取商家商品列表
@api_bp.route('/merchant/products', methods=['GET'])
@login_required
def get_merchant_products():
    """获取当前商家的商品列表，支持分页和状态筛选"""

    user_id = request.current_user_id
    # 验证用户是否存在且为商家
    user = User.query.get(user_id)
    if not user:
        return jsonify({'code': 404, 'message': '用户不存在'}), 404
    if user.role != 'merchant':
        return jsonify({'code': 403, 'message': '无权限，仅限商家操作'}), 403

    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('pageSize', 10, type=int)
    status = request.args.get('status', type=int)  # 可选

    query = Product.query.filter_by(merchant_id=user_id, is_deleted=0)
    if status is not None:
        query = query.filter_by(status=status)

    pagination = query.order_by(Product.created_at.desc()).paginate(page=page, per_page=page_size, error_out=False)

    products = [p.to_dict() for p in pagination.items]

    return jsonify({
        'code': 0,
        'data': {
            'list': products,
            'total': pagination.total,
            'page': page,
            'pageSize': page_size
        }
    })

# 发布新商品
@api_bp.route('/merchant/products', methods=['POST'])
@login_required
def create_product():
    """商家发布新商品，支持多规格"""
    data = request.get_json()
    detail_images = data.get('detail_images', [])  # 从请求中获取
    user_id = request.current_user_id

    user = User.query.get(user_id)
    if not user:
        return jsonify({'code': 404, 'message': '用户不存在'}), 404
    if user.role != 'merchant':
        return jsonify({'code': 403, 'message': '无权限，仅限商家操作'}), 403

    # 必填字段
    name = data.get('name')
    category = data.get('category')
    price = data.get('price')
    stock = data.get('stock')
    detail = data.get('detail', '')
    pic_url = data.get('pic_url', '')
    hot = data.get('hot', False)
    attributes = data.get('attributes', [])  # 格式见下文

    if not all([name, category, price, stock]):
        return jsonify({'code': 400, 'message': '缺少必要字段'}), 400

    # 创建商品
    product = Product(
        name=name,
        category=category,
        price=price,
        stock=stock,
        detail=detail,
        pic_url=pic_url,
        hot=hot,
        merchant_id=user_id,
        status=1  # 默认上架
    )
    db.session.add(product)
    db.session.flush()  # 获取 product.id

    # 处理详情图片（先删除原有图片，再添加新图片）
    # 在创建商品后，或编辑商品时
    ProductImage.query.filter_by(product_id=product.id).delete()
    for img_url in detail_images:
        pi = ProductImage(product_id=product.id, image_url=img_url)
        db.session.add(pi)


    # 处理规格
    for attr in attributes:
        pa = ProductAttribute(
            product_id=product.id,
            attribute_name=attr['name'],
            attribute_values=json.dumps(attr['values']),
            attribute_prices=json.dumps(attr['prices']),
            is_absolute_price=attr.get('is_absolute_price', 0)
        )
        db.session.add(pa)

    db.session.commit()
    return jsonify({'code': 0, 'data': {'id': product.id}, 'message': '发布成功'}), 201


# 编辑商品
@api_bp.route('/merchant/products/<int:product_id>', methods=['PUT'])
@login_required
def update_product(product_id):
    """编辑商品，更新基础信息和规格"""
    data = request.get_json()
    user_id = request.current_user_id
    detail_images = data.get('detail_images', [])  # 从请求中获取
    product = Product.query.filter_by(id=product_id, merchant_id=user_id, is_deleted=0).first()
    if not product:
        return jsonify({'code': 404, 'message': '商品不存在或无权限'}), 404

    # 处理详情图片（先删除原有图片，再添加新图片）
    # 在创建商品后，或编辑商品时
    ProductImage.query.filter_by(product_id=product.id).delete()
    for img_url in detail_images:
        pi = ProductImage(product_id=product.id, image_url=img_url)
        db.session.add(pi)

    # 更新基础字段
    if 'name' in data:
        product.name = data['name']
    if 'category' in data:
        product.category = data['category']
    if 'price' in data:
        product.price = data['price']
    if 'stock' in data:
        product.stock = data['stock']
    if 'detail' in data:
        product.detail = data['detail']
    if 'pic_url' in data:
        product.pic_url = data['pic_url']
    if 'hot' in data:
        product.hot = data['hot']

    # 更新规格（先删除原有规格，再添加新规格）
    if 'attributes' in data:
        # 删除原有规格
        ProductAttribute.query.filter_by(product_id=product.id).delete()
        for attr in data['attributes']:
            pa = ProductAttribute(
                product_id=product.id,
                attribute_name=attr['name'],
                attribute_values=json.dumps(attr['values']),
                attribute_prices=json.dumps(attr['prices']),
                is_absolute_price=attr.get('is_absolute_price', 0)
            )
            db.session.add(pa)

    db.session.commit()
    return jsonify({'code': 0, 'message': '更新成功'})

# 上下架商品
@api_bp.route('/merchant/products/<int:product_id>/status', methods=['PATCH'])
@login_required
def update_product_status(product_id):
    """修改商品状态（上下架）"""
    data = request.get_json()
    user_id = request.current_user_id
    product = Product.query.filter_by(id=product_id, merchant_id=user_id, is_deleted=0).first()
    if not product:
        return jsonify({'code': 404, 'message': '商品不存在或无权限'}), 404

    status = data.get('status')
    if status not in [0, 1, 2]:
        return jsonify({'code': 400, 'message': '状态值无效'}), 400

    product.status = status
    db.session.commit()
    return jsonify({'code': 0, 'message': '状态更新成功'})


# 软删除商品
@api_bp.route('/merchant/products/<int:product_id>', methods=['DELETE'])
@login_required
def delete_product(product_id):
    """软删除商品"""
    user_id = request.current_user_id
    product = Product.query.filter_by(id=product_id, merchant_id=user_id, is_deleted=0).first()
    if not product:
        return jsonify({'code': 404, 'message': '商品不存在或无权限'}), 404

    product.is_deleted = 1
    db.session.commit()
    return jsonify({'code': 0, 'message': '删除成功'})

# 检查是否已经关注
@api_bp.route('/shop/follow/check', methods=['GET'])
@login_required
def check_follow():
    user_id = request.current_user_id
    merchant_id = request.args.get('merchantId', type=int)
    if not merchant_id:
        return jsonify({'code': 400, 'message': '缺少商家ID'}), 400

    followed = ShopFollow.query.filter_by(user_id=user_id, merchant_id=merchant_id).first() is not None
    return jsonify({'code': 0, 'data': {'followed': followed}})

# 关注/取消关注
@api_bp.route('/shop/follow', methods=['POST'])
@login_required
def follow_shop():
    data = request.get_json()
    user_id = request.current_user_id
    merchant_id = data.get('merchantId')
    action = data.get('action')  # 'follow' or 'unfollow'

    if not merchant_id:
        return jsonify({'code': 400, 'message': '缺少商家ID'}), 400

    user = User.query.get(user_id)
    if not user:
        return jsonify({'code': 404, 'message': '用户不存在'}), 404

    if action == 'follow':
        exists = ShopFollow.query.filter_by(user_id=user_id, merchant_id=merchant_id).first()
        if not exists:
            follow = ShopFollow(user_id=user_id, merchant_id=merchant_id)
            db.session.add(follow)
            db.session.commit()
        return jsonify({'code': 0, 'message': '关注成功'})
    elif action == 'unfollow':
        ShopFollow.query.filter_by(user_id=user_id, merchant_id=merchant_id).delete()
        db.session.commit()
        return jsonify({'code': 0, 'message': '取消关注成功'})
    else:
        return jsonify({'code': 400, 'message': '无效的action'}), 400

# 更新店铺信息
@api_bp.route('/shop/info', methods=['PUT'])
def update_shop_info():
    data = request.get_json()
    merchant_id = data.get('merchantId')
    if not merchant_id:
        return jsonify({'code': 400, 'message': '缺少商家ID'}), 400

    # 验证权限（当前登录用户必须是该商家）
    # 获取当前用户ID（从请求参数或header中获取）
    user_id = data.get('merchantId')  # 商家编辑时传递的merchantId就是自己的用户ID
    if not user_id:
        return jsonify({'code': 400, 'message': '缺少用户ID'}), 400

    user = User.query.get(user_id)
    if not user or user.role != 'merchant':
        return jsonify({'code': 403, 'message': '无权限'}), 403

    merchant = Merchant.query.filter_by(user_id=merchant_id).first()
    if not merchant:
        return jsonify({'code': 404, 'message': '商家记录不存在'}), 404

    # 更新字段
    if 'shopName' in data:
        merchant.shop_name = data['shopName']
    if 'description' in data:
        merchant.description = data['description']
    if 'intro' in data:
        merchant.intro = data['intro']
    if 'introImages' in data:
        merchant.intro_images = json.dumps(data['introImages'])

    # 更新用户头像
    if 'avatar' in data:
        user.avatar = data['avatar']

    db.session.commit()
    return jsonify({'code': 0, 'message': '更新成功'})

# 获取店铺信息
@api_bp.route('/shop/info', methods=['GET'])
def get_shop_info():
    merchant_id = request.args.get('merchantId', type=int)
    if not merchant_id:
        return jsonify({'code': 400, 'message': '缺少商家ID'}), 400

    merchant = Merchant.query.filter_by(user_id=merchant_id).first()
    user = User.query.get(merchant_id)
    if not merchant or not user:
        return jsonify({'code': 404, 'message': '商家不存在'}), 404

    # 统计粉丝数
    follow_count = ShopFollow.query.filter_by(merchant_id=merchant_id).count()
    # 统计商品数（上架且未删除）
    product_count = Product.query.filter_by(merchant_id=merchant_id, status=1, is_deleted=0).count()

    # 计算商家的平均评分（基于商品评价和服务评价）
    total_rating_sum = 0
    total_rating_count = 0

    # 1. 商品评价：通过商品关联商家
    product_ids = db.session.query(Product.id).filter(Product.merchant_id == merchant_id,
                                                      Product.is_deleted == 0).subquery()
    product_reviews = db.session.query(Review.rating).filter(Review.product_id.in_(product_ids)).all()
    for r in product_reviews:
        total_rating_sum += r.rating
        total_rating_count += 1

    # 2. 服务评价：通过钓场服务关联商家
    service_ids = db.session.query(FishingService.id).filter(FishingService.merchant_id == merchant_id,
                                                             FishingService.is_deleted == False).subquery()
    service_reviews = db.session.query(ServiceReview.rating).filter(ServiceReview.service_id.in_(service_ids)).all()
    for r in service_reviews:
        total_rating_sum += r.rating
        total_rating_count += 1

    avg_rating = round(total_rating_sum / total_rating_count, 1) if total_rating_count > 0 else 4.8

    data = {
        'shopName': merchant.shop_name or user.nickname,
        'avatar': user.avatar or '/static/images/user-avatar.jpg',
        'description': merchant.description or '',
        'intro': merchant.intro or '',
        'introImages': json.loads(merchant.intro_images) if merchant.intro_images else [],
        'followCount': follow_count,
        'productCount': product_count,
        'rating': avg_rating
    }
    return jsonify({'code': 0, 'data': data})

# 获取店铺商品列表（支持分类、排序、分页）
@api_bp.route('/shop/products', methods=['GET'])
def get_shop_products():
    merchant_id = request.args.get('merchantId', type=int)
    category = request.args.get('category', '')
    sort = request.args.get('sort', 'comprehensive')
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('pageSize', 10, type=int)

    if not merchant_id:
        return jsonify({'code': 400, 'message': '缺少商家ID'}), 400

    query = Product.query.filter_by(merchant_id=merchant_id, status=1, is_deleted=0)
    if category:
        query = query.filter_by(category=category)

    # 排序
    if sort == 'sales':
        query = query.order_by(Product.sales.desc())
    elif sort == 'price_asc':
        query = query.order_by(Product.price.asc())
    elif sort == 'price_desc':
        query = query.order_by(Product.price.desc())
    elif sort == 'new':
        query = query.order_by(Product.created_at.desc())
    else:  # 综合排序（按创建时间倒序，销量次之）
        query = query.order_by(Product.created_at.desc(), Product.sales.desc())

    pagination = query.paginate(page=page, per_page=page_size, error_out=False)
    products = [p.to_dict() for p in pagination.items]

    return jsonify({
        'code': 0,
        'data': {
            'list': products,
            'total': pagination.total,
            'page': page,
            'pageSize': page_size
        }
    })


# 商品搜索接口（支持关键词、店铺限定、排序、分页）
@api_bp.route('/products/search', methods=['GET'])
def search_products():
    keyword = request.args.get('keyword', '')
    merchant_id = request.args.get('merchantId', type=int)  # 可选，限定店铺
    sort = request.args.get('sort', 'comprehensive')  # comprehensive, sales, price_asc, price_desc, new
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('pageSize', 10, type=int)

    # 基础查询：只查上架且未删除的商品
    query = Product.query.filter_by(status=1, is_deleted=0)

    # 店铺限定
    if merchant_id:
        query = query.filter_by(merchant_id=merchant_id)

    # 关键词搜索（商品名称、标签、详情等，这里简单按名称搜索）
    if keyword:
        query = query.filter(Product.name.ilike(f'%{keyword}%'))

    # 排序
    if sort == 'sales':
        query = query.order_by(Product.sales.desc())
    elif sort == 'price_asc':
        query = query.order_by(Product.price.asc())
    elif sort == 'price_desc':
        query = query.order_by(Product.price.desc())
    elif sort == 'new':
        query = query.order_by(Product.created_at.desc())
    else:  # comprehensive 综合排序（按创建时间倒序，销量次之）
        query = query.order_by(Product.created_at.desc(), Product.sales.desc())

    pagination = query.paginate(page=page, per_page=page_size, error_out=False)
    products = [p.to_dict() for p in pagination.items]

    return jsonify({
        'code': 0,
        'data': {
            'list': products,
            'total': pagination.total,
            'page': page,
            'pageSize': page_size
        }
    })

@api_bp.route('/fishing/services/search', methods=['GET'])
def search_fishing_services():
    """搜索钓场服务（支持关键词、分页）"""
    keyword = request.args.get('keyword', '')
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('pageSize', 10, type=int)

    # 基础查询：未删除且已发布（status=1）
    query = FishingService.query.filter_by(is_deleted=False, status=1)
    if keyword:
        # 按名称或标签搜索（tags是JSON数组，需要处理）
        # 简单模糊匹配名称和详情，也可匹配标签（需要JSON查询，不同数据库语法不同）
        # 按名称模糊匹配
        query = query.filter(
            db.or_(
                FishingService.name.ilike(f'%{keyword}%'),
                FishingService.details.ilike(f'%{keyword}%')
            )
        )
        # 按标签匹配，用以下方式（MySQL JSON函数）
        query = query.filter(FishingService.tags.cast(db.String).ilike(f'%{keyword}%'))

    # 分页
    pagination = query.order_by(FishingService.created_at.desc()).paginate(page=page, per_page=page_size, error_out=False)
    services = pagination.items
    total = pagination.total

    # 序列化
    data = []
    for s in services:
        data.append({
            'id': s.id,
            'name': s.name,
            'price': float(s.price),
            'pic_url': s.cover_image,
            'desc': s.details,
            'service_type': s.service_type,
            'address': s.address,
            'tags': s.tags if s.tags else [],
            'is_end': s.is_end,
            'created_at': s.created_at.strftime('%Y-%m-%d %H:%M:%S') if s.created_at else ''
        })
    return jsonify({'code': 0, 'message': 'success', 'data': {'list': data, 'total': total, 'page': page, 'pageSize': page_size}})


# 热门推荐商品（搜索页面）
@api_bp.route('/products/hot', methods=['GET'])
def get_hot_products():
    """获取热门推荐商品（按销量降序，支持店铺限定）"""
    limit = request.args.get('limit', 8, type=int)
    merchant_id = request.args.get('merchantId', type=int)  # 可选
    query = Product.query.filter_by(status=1, is_deleted=0)
    if merchant_id:
        query = query.filter_by(merchant_id=merchant_id)
    products = query.order_by(Product.sales.desc()).limit(limit).all()
    data = [p.to_dict() for p in products]
    return jsonify({'code': 0, 'data': data})

# 获取用户信息
@api_bp.route('/user/info', methods=['GET'])
@login_required
def get_user_info():
    # 优先从查询参数获取 userId
    target_user_id = request.args.get('userId', type=int)
    if not target_user_id:
        # 如果没有传，则使用当前登录用户
        target_user_id = request.current_user_id
    user = User.query.get(target_user_id)
    if not user:
        return jsonify({'code': 404, 'message': '用户不存在'}), 404
    user_data = {
        'id': user.id,
        'username': user.username,
        'nickname': user.nickname,
        'mobile': user.mobile,
        'avatar': user.avatar,
        'balance': float(user.balance),
        'hy_balance': float(user.hy_balance),
        'coupon_count': user.coupon_count,
        'fish_count': user.fish_count,
        'fishing_count': user.fishing_count,
        'over_hundred_count': user.over_hundred_count,
        'champion_count': user.champion_count,
        'role': user.role,
        'create_time': user.create_time.strftime('%Y-%m-%d %H:%M:%S') if user.create_time else None
    }
    return jsonify({'code': 0, 'data': user_data})

# 修改昵称
@api_bp.route('/user/nickname', methods=['PUT'])
@login_required
def update_nickname():
    data = request.get_json()
    user_id = request.current_user_id
    new_nickname = data.get('nickname')
    if not new_nickname:
        return jsonify({'code': 400, 'message': '昵称不能为空'}), 400
    if len(new_nickname) < 2 or len(new_nickname) > 10:
        return jsonify({'code': 400, 'message': '昵称长度需在2-10个字符'}), 400
    user = User.query.get(user_id)
    if not user:
        return jsonify({'code': 404, 'message': '用户不存在'}), 404
    user.nickname = new_nickname
    db.session.commit()
    return jsonify({'code': 0, 'message': '昵称修改成功'})

# 用户充值
@api_bp.route('/user/recharge', methods=['POST'])
@login_required
def recharge():
    """模拟充值（更新用户余额）"""
    data = request.get_json()
    user_id = request.current_user_id   # 从 token 获取用户 ID
    amount = data.get('amount')

    if not amount:
        return jsonify({'code': 400, 'message': '缺少金额参数'}), 400

    # 验证金额为正整数
    try:
        amount = int(amount)
        if amount <= 0:
            raise ValueError
    except:
        return jsonify({'code': 400, 'message': '金额必须为正整数'}), 400

    user = User.query.get(user_id)
    if not user:
        return jsonify({'code': 404, 'message': '用户不存在'}), 404

    # 更新余额
    user.balance = float(user.balance) + amount
    db.session.commit()

    return jsonify({'code': 0, 'message': '充值成功', 'data': {'new_balance': float(user.balance)}})

# 领取优惠券
@api_bp.route('/coupons/receive', methods=['POST'])
@login_required
def receive_coupon():
    """领取优惠券"""
    data = request.get_json()
    user_id = request.current_user_id
    coupon_id = data.get('coupon_id')
    if not coupon_id:
        return jsonify({'code': 400, 'message': '参数不完整'}), 400

    # 检查用户是否存在
    user = User.query.get(user_id)
    if not user:
        return jsonify({'code': 404, 'message': '用户不存在'}), 404

    # 检查优惠券是否存在
    coupon = Coupon.query.get(coupon_id)
    if not coupon:
        return jsonify({'code': 404, 'message': '优惠券不存在'}), 404

    # 检查是否已领取
    existing = UserCoupon.query.filter_by(user_id=user_id, coupon_id=coupon_id).first()
    if existing:
        return jsonify({'code': 409, 'message': '您已领取过该优惠券'}), 409

    # 创建领取记录
    user_coupon = UserCoupon(user_id=user_id, coupon_id=coupon_id)
    db.session.add(user_coupon)
    db.session.commit()

    return jsonify({'code': 0, 'message': '领取成功'})

# 获取优惠券列表（支持分类筛选）
@api_bp.route('/coupons/list', methods=['GET'])
@login_required
def get_coupon_list():
    user_id = request.current_user_id
    category = request.args.get('category', 'all')

    query = Coupon.query
    if category != 'all':
        query = query.filter_by(type=category)

    today = date.today()
    coupons = query.filter(Coupon.expire_date >= today).all()

    # 获取该用户所有相关的 UserCoupon 记录
    user_coupons = UserCoupon.query.filter_by(user_id=user_id).all()
    received_ids = set()   # 已领取未使用的
    used_ids = set()       # 已使用的（无论是否过期）
    for uc in user_coupons:
        if uc.used_at is None:
            received_ids.add(uc.coupon_id)
        else:
            used_ids.add(uc.coupon_id)

    data = []
    for c in coupons:
        if c.id in used_ids:
            continue  # 已使用的优惠券不显示
        data.append({
            'id': c.id,
            'type': c.type,
            'value': c.value,
            'condition': c.condition,
            'min_amount': c.min_amount,
            'expire': c.expire_date.strftime('%Y-%m-%d'),
            'received': c.id in received_ids
        })
    return jsonify({'code': 0, 'data': data})

# 获取用户已领取的优惠券（用于个人中心显示）
@api_bp.route('/user/coupons', methods=['GET'])
@login_required
def get_user_coupons():
    """获取用户已领取且未使用的优惠券列表"""
    user_id = request.current_user_id   # 从 token 获取，无需前端传参

    # 只查询未使用的优惠券（used_at IS NULL）
    user_coupons = UserCoupon.query.filter_by(user_id=user_id, used_at=None).all()

    data = []
    for uc in user_coupons:
        coupon = uc.coupon
        # 可选：过滤已过期的优惠券
        # if coupon.expire_date < date.today():
        #     continue
        data.append({
            'id': uc.id,
            'coupon_id': coupon.id,
            'value': coupon.value,
            'condition': coupon.condition,
            'min_amount': coupon.min_amount,
            'expire': coupon.expire_date.strftime('%Y-%m-%d'),
            'received_at': uc.received_at.strftime('%Y-%m-%d %H:%M:%S') if uc.received_at else None
        })

    return jsonify({'code': 0, 'data': data})

# 获取用户可用优惠券列表（针对当前订单）
@api_bp.route('/user/available_coupons', methods=['GET'])
@login_required
def get_available_coupons():
    """获取用户可用优惠券（针对当前订单金额和商品类别）"""
    user_id = request.current_user_id
    total_amount = request.args.get('total_amount', type=float)
    category = request.args.get('category', '')
    if not total_amount:
        return jsonify({'code': 400, 'message': '缺少订单金额'}), 400

    user_coupons = UserCoupon.query.filter_by(user_id=user_id, used_at=None).all()
    available = []
    for uc in user_coupons:
        coupon = uc.coupon
        if coupon.expire_date < date.today():
            continue
        if total_amount < coupon.min_amount:
            continue
        if coupon.type not in ['all', 'unlimited'] and coupon.type != category:
            continue
        available.append({
            'id': uc.id,
            'coupon_id': coupon.id,
            'value': coupon.value,
            'condition': coupon.condition,
            'min_amount': coupon.min_amount,
            'expire': coupon.expire_date.strftime('%Y-%m-%d'),
            'type': coupon.type
        })
    return jsonify({'code': 0, 'data': available})

# 获取用户所有优惠券
@api_bp.route('/user/all_coupons', methods=['GET'])
@login_required
def get_all_user_coupons():
    user_id = request.current_user_id
    if not user_id:
        return jsonify({'code': 400, 'message': '缺少用户ID'}), 400
    user_coupons = UserCoupon.query.filter_by(user_id=user_id, used_at=None).all()
    data = []
    for uc in user_coupons:
        coupon = uc.coupon
        data.append({
            'id': uc.id,
            'value': coupon.value,
            'condition': coupon.condition,
            'min_amount': coupon.min_amount,
            'expire': coupon.expire_date.strftime('%Y-%m-%d'),
            'type': coupon.type
        })
    return jsonify({'code': 0, 'data': data})


# 添加商品到购物车
@api_bp.route('/cart/add', methods=['POST'])
@login_required
def add_to_cart():
    data = request.get_json()
    user_id = request.current_user_id
    product_id = data.get('product_id')
    quantity = data.get('quantity', 1)
    # 统一解析成 list，兼容前端传数组 / 传被转义字符串两种情况
    selected_attributes = safe_load_attrs(data.get('selected_attributes', []))

    if not product_id:
        return jsonify({'code': 400, 'message': '商品ID缺失'}), 400

    # 检查商品是否存在
    product = Product.query.get(product_id)
    if not product:
        return jsonify({'code': 404, 'message': '商品不存在'}), 404

    # 检查库存
    if product.stock < quantity:
        return jsonify({'code': 400, 'message': '库存不足'}), 400

    # 关键：按选中的规格算出真实单价。
    # 以前这里既不算价也不存价，购物车只能拿商品基础价，选规格等于白选。
    unit_price = calc_attr_price(product, selected_attributes)

    # 查找是否已存在相同规格的购物车项（用同样的 dumps 格式做精确匹配）
    attrs_key = json.dumps(selected_attributes)
    cart_item = Cart.query.filter_by(
        user_id=user_id,
        product_id=product_id,
        selected_attributes=attrs_key
    ).first()

    if cart_item:
        # 更新数量，同时同步价格和快照（商品可能改过价/改名）
        cart_item.quantity += quantity
        cart_item.price = unit_price
        cart_item.product_name = product.name
        cart_item.product_image = product.pic_url
        cart_item.merchant_id = product.merchant_id
    else:
        cart_item = Cart(
            user_id=user_id,
            product_id=product_id,
            quantity=quantity,
            selected_attributes=attrs_key,
            price=unit_price,
            product_name=product.name,
            product_image=product.pic_url,
            merchant_id=product.merchant_id,
            item_type='product'
        )
        db.session.add(cart_item)

    db.session.commit()
    return jsonify({'code': 0, 'message': '添加成功',
                    'data': {'unit_price': unit_price,
                             'selected_attributes': selected_attributes}})

# 获取购物车列表
@api_bp.route('/cart/list', methods=['GET'])
@login_required
def get_cart_list():
    user_id = request.current_user_id
    cart_items = Cart.query.filter_by(user_id=user_id).all()
    data = []
    for item in cart_items:
        if item.item_type == 'product':
            product = Product.query.get(item.product_id)
            if not product:
                continue
            # 优先用「加购时按规格算好的价格」。
            # 老数据 price 是 0（以前压根没存），那就按 selected_attributes 现算一次兜底。
            stored_price = float(item.price or 0)
            if stored_price > 0:
                current_price = stored_price
            else:
                current_price = calc_attr_price(product, item.selected_attributes)
            attrs_list = safe_load_attrs(item.selected_attributes)
            data.append({
                'id': item.id,
                'item_type': 'product',
                'product_id': product.id,
                'product_name': product.name,
                'product_image': product.pic_url,
                'price': current_price,
                'unit_price': current_price,
                'base_price': float(product.price),
                'quantity': item.quantity,
                'selected_attributes': attrs_list,
                'selected_attributes_text': ' / '.join([str(a) for a in attrs_list]),
                'stock': product.stock,
                'merchant_id': product.merchant_id,
                'merchant_name': product.merchant.nickname if product.merchant else '平台自营'
            })
        else:  # service
            service = FishingService.query.get(item.service_id)
            if not service:
                continue
            unit_price = float(service.price)
            total_price = unit_price * (item.participant_count or 1)
            data.append({
                'id': item.id,
                'item_type': 'service',
                'service_id': service.id,
                'service_name': service.name,
                'service_image': service.cover_image,
                'price': total_price,
                'unit_price': unit_price,
                'quantity': 1,
                'service_date': item.service_date.strftime('%Y-%m-%d') if item.service_date else None,
                'service_time_slot': item.service_time_slot,
                'participant_count': item.participant_count or 1,
                'merchant_id': service.merchant_id,
                'merchant_name': service.merchant.nickname if service.merchant else '平台自营',
                'address': service.address,  # 服务地址
                'is_competition': service.service_type == 'competition'
            })
    return jsonify({'code': 0, 'data': data})

# 服务项人数更新
@api_bp.route('/cart/update_service', methods=['POST'])
@login_required
def update_cart_service():
    data = request.get_json()
    user_id = request.current_user_id
    cart_id = data.get('cart_id')
    participant_count = data.get('participant_count')
    if not user_id or not cart_id:
        return jsonify({'code': 1, 'message': '参数不完整'}), 400
    cart_item = Cart.query.filter_by(id=cart_id, user_id=user_id, item_type='service').first()
    if not cart_item:
        return jsonify({'code': 1, 'message': '购物车项不存在'}), 404
    cart_item.participant_count = participant_count
    # 重新计算价格
    service = FishingService.query.get(cart_item.service_id)
    if service:
        cart_item.price = float(service.price) * participant_count
    db.session.commit()
    return jsonify({'code': 0, 'message': '更新成功'})

# 修改购物车商品数量
@api_bp.route('/cart/update', methods=['POST'])
@login_required
def update_cart_quantity():
    data = request.get_json()
    user_id = request.current_user_id
    cart_id = data.get('cart_id')
    quantity = data.get('quantity')

    # 兼容前端可能传递对象的情况
    if isinstance(quantity, dict):
        quantity = quantity.get('value', 1)
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        return jsonify({'code': 400, 'message': '数量格式错误'}), 400

    if not user_id or not cart_id:
        return jsonify({'code': 400, 'message': '参数不完整'}), 400

    cart_item = Cart.query.filter_by(id=cart_id, user_id=user_id).first()
    if not cart_item:
        return jsonify({'code': 404, 'message': '购物车项不存在'}), 404

    # 检查库存
    product = Product.query.get(cart_item.product_id)
    if product.stock < quantity:
        return jsonify({'code': 400, 'message': '库存不足'}), 400

    cart_item.quantity = quantity
    db.session.commit()
    return jsonify({'code': 0, 'message': '更新成功'})

# 添加服务到购物车
@api_bp.route('/cart/add_service', methods=['POST'])
@login_required
def add_service_to_cart():
    data = request.get_json()
    user_id = request.current_user_id
    service_id = data.get('service_id')
    service_date = data.get('service_date')
    service_time_slot = data.get('service_time_slot')
    participant_count = data.get('participant_count', 1)

    if not user_id or not service_id:
        return jsonify({'code': 1, 'message': '参数不完整'}), 400

    service = FishingService.query.get(service_id)
    if not service or service.is_deleted or service.status != 1:
        return jsonify({'code': 1, 'message': '服务不存在'}), 400

    # 检查是否已有相同服务+日期的购物车项
    existing = Cart.query.filter_by(
        user_id=user_id,
        item_type='service',
        service_id=service_id,
        service_date=service_date,
        service_time_slot=service_time_slot
    ).first()
    if existing:
        existing.participant_count = participant_count
        existing.quantity = 1
    else:
        cart_item = Cart(
            user_id=user_id,
            item_type='service',
            service_id=service_id,
            product_id=None,            # 显式设置 product_id 为 None
            product_name=service.name,
            product_image=service.cover_image,
            price=float(service.price) * participant_count,  # 存储总价
            quantity=1,
            selected_attributes=None,
            merchant_id=service.merchant_id,
            service_date=service_date,
            service_time_slot=service_time_slot,
            participant_count=participant_count
        )
        db.session.add(cart_item)
    db.session.commit()
    return jsonify({'code': 0, 'message': '已加入购物车'})


# 删除购物车商品
@api_bp.route('/cart/delete', methods=['POST'])
@login_required
def delete_cart_item():
    data = request.get_json()
    user_id = request.current_user_id
    cart_id = data.get('cart_id')

    if not user_id or not cart_id:
        return jsonify({'code': 400, 'message': '参数不完整'}), 400

    cart_item = Cart.query.filter_by(id=cart_id, user_id=user_id).first()
    if not cart_item:
        return jsonify({'code': 404, 'message': '购物车项不存在'}), 404

    db.session.delete(cart_item)
    db.session.commit()
    return jsonify({'code': 0, 'message': '删除成功'})


# 删除订单（仅限已取消状态的订单，物理删除）
@api_bp.route('/orders/<int:order_id>/delete', methods=['DELETE'])
@login_required
def delete_order(order_id):
    try:
        data = request.get_json() or {}
        user_id = request.current_user_id
        if not user_id:
            return jsonify({'code': 1, 'message': '缺少用户ID'}), 400

        order = Order.query.get(order_id)
        if not order or order.user_id != user_id:
            return jsonify({'code': 1, 'message': '订单不存在'}), 404

        if order.order_status != 3:
            return jsonify({'code': 1, 'message': '只能删除已取消的订单'}), 400

        # 先删除订单商品项
        for item in order.items:
            db.session.delete(item)
        # 再删除订单
        db.session.delete(order)
        db.session.commit()

        return jsonify({'code': 0, 'message': '删除成功'})
    except Exception as e:
        db.session.rollback()
        print(f"删除订单失败: {str(e)}")
        return jsonify({'code': 1, 'message': f'删除失败: {str(e)}'}), 500

# 创建评价
@api_bp.route('/reviews', methods=['POST'])
@login_required
def create_review():
    data = request.get_json()
    user_id = request.current_user_id
    order_id = data.get('order_id')
    order_item_id = data.get('order_item_id')
    product_id = data.get('product_id')
    rating = data.get('rating')
    content = data.get('content', '')
    images = data.get('images', [])

    if not all([user_id, order_id, order_item_id, product_id, rating]):
        return jsonify({'code': 1, 'message': '参数不完整'}), 400

    # 验证订单属于该用户且已完成
    order = Order.query.filter_by(id=order_id, user_id=user_id, order_status=2).first()
    if not order:
        return jsonify({'code': 1, 'message': '订单不存在或未完成'}), 400

    # 重复评价检查
    existing = Review.query.filter_by(order_item_id=order_item_id).first()
    if existing:
        return jsonify({'code': 1, 'message': '该商品已评价过'}), 400

    # 自评限制（可选）
    product = Product.query.get(product_id)
    if product and product.merchant_id == user_id:
        return jsonify({'code': 1, 'message': '不能评价自己店铺的商品'}), 400

    # 创建评价
    review = Review(
        user_id=user_id,
        order_id=order_id,
        order_item_id=order_item_id,
        product_id=product_id,
        rating=rating,
        content=content,
        images=json.dumps(images)
    )
    db.session.add(review)
    db.session.commit()

    return jsonify({'code': 0, 'message': '评价成功', 'data': {'id': review.id}})

# 获取某个订单的评价列表（用于订单详情页或评价页面）
@api_bp.route('/orders/<int:order_id>/reviews', methods=['GET'])
def get_order_reviews(order_id):
    user_id = request.args.get('user_id', type=int)
    if not user_id:
        return jsonify({'code': 1, 'message': '缺少用户ID'}), 400
    reviews = Review.query.filter_by(order_id=order_id, user_id=user_id).all()
    data = []
    for r in reviews:
        product = Product.query.get(r.product_id)
        data.append({
            'id': r.id,
            'product_id': r.product_id,
            'product_name': product.name if product else '',
            'product_image': product.pic_url if product else '',
            'rating': r.rating,
            'content': r.content,
            'images': json.loads(r.images) if r.images else [],
            'created_at': r.created_at.strftime('%Y-%m-%d %H:%M:%S')
        })
    return jsonify({'code': 0, 'data': data})


# 获取用户对于“商品”的所有评价（用于“评价”分类栏，支持分页）
@api_bp.route('/user/reviews', methods=['GET'])
@login_required
def get_user_reviews():
    user_id = request.current_user_id
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 10, type=int)

    pagination = Review.query.filter_by(user_id=user_id).order_by(Review.created_at.desc()).paginate(page=page,
                                                                                                     per_page=page_size,
                                                                                                     error_out=False)
    reviews = pagination.items
    total = pagination.total

    data = []
    for r in reviews:
        product = Product.query.get(r.product_id)
        order = Order.query.get(r.order_id)
        data.append({
            'id': r.id,
            'order_id': r.order_id,
            'product_id': r.product_id,
            'product_name': product.name if product else '',
            'product_image': product.pic_url if product else '',
            'rating': r.rating,
            'content': r.content,
            'images': json.loads(r.images) if r.images else [],
            'created_at': r.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'order_number': order.order_number if order else ''
        })
    return jsonify({
        'code': 0,
        'data': {
            'list': data,
            'total': total,
            'page': page,
            'page_size': page_size
        }
    })



# 获取用户对于“钓场”的所有评价
@api_bp.route('/user/service_reviews', methods=['GET'])
@login_required
def get_user_service_reviews():
    user_id = request.current_user_id
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 10, type=int)

    # 联查订单信息以获取订单号
    query = db.session.query(ServiceReview, OrderItem, Order). \
        join(OrderItem, ServiceReview.order_item_id == OrderItem.id). \
        join(Order, OrderItem.order_id == Order.id). \
        filter(ServiceReview.user_id == user_id). \
        order_by(ServiceReview.created_at.desc())

    pagination = query.paginate(page=page, per_page=page_size, error_out=False)
    items = pagination.items

    data = []
    for review, order_item, order in items:
        service = FishingService.query.get(review.service_id)
        data.append({
            'id': review.id,
            'service_id': review.service_id,
            'service_name': service.name if service else '',
            'service_image': service.cover_image if service else '',
            'service_type': service.service_type if service else '',
            'rating': review.rating,
            'content': review.content,
            'images': json.loads(review.images) if review.images else [],
            'created_at': review.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'order_number': order.order_number,
            'service_date': order_item.service_date.strftime('%Y-%m-%d') if order_item.service_date else None,
            'service_time_slot': order_item.service_time_slot,
            'participant_count': order_item.participant_count,
            'item_type': 'service'
        })
    return jsonify({
        'code': 0,
        'data': {
            'list': data,
            'total': pagination.total,
            'page': page,
            'page_size': page_size
        }
    })

# 获取用户评价统计-“商品”（总评价数、平均评分、各星级数量）
@api_bp.route('/user/reviews/stats', methods=['GET'])
@login_required
def get_user_review_stats():
    user_id = request.current_user_id

    all_reviews = Review.query.filter_by(user_id=user_id).all()
    total = len(all_reviews)
    if total == 0:
        return jsonify({'code': 0, 'data': {'total': 0, 'avg_rating': 0, 'star_counts': {}}}), 200

    total_rating = sum(r.rating for r in all_reviews)
    avg_rating = round(total_rating / total, 1)

    star_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for r in all_reviews:
        star_counts[r.rating] += 1

    return jsonify({
        'code': 0,
        'data': {
            'total': total,
            'avg_rating': avg_rating,
            'star_counts': star_counts
        }
    })
# 获取用户评价统计-“钓场”
@api_bp.route('/user/service_reviews/stats', methods=['GET'])
@login_required
def get_user_service_review_stats():
    user_id = request.current_user_id
    reviews = ServiceReview.query.filter_by(user_id=user_id).all()
    total = len(reviews)
    if total == 0:
        return jsonify({'code': 0, 'data': {'total': 0, 'avg_rating': 0, 'star_counts': {}}})
    total_rating = sum(r.rating for r in reviews)
    avg_rating = round(total_rating / total, 1)
    star_counts = {1:0, 2:0, 3:0, 4:0, 5:0}
    for r in reviews:
        star_counts[r.rating] += 1
    return jsonify({'code': 0, 'data': {'total': total, 'avg_rating': avg_rating, 'star_counts': star_counts}})

# 获取商品评价
@api_bp.route('/products/<int:product_id>/reviews', methods=['GET'])
def get_product_reviews(product_id):
    """获取商品评价，支持排序和评分筛选"""
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 10, type=int)
    sort = request.args.get('sort', 'latest')  # latest, rating_high, rating_low

    query = Review.query.filter_by(product_id=product_id)

    rating_filter = request.args.get('rating', 'all')
    if rating_filter == 'high':
        query = query.filter(Review.rating.in_([4, 5]))
    elif rating_filter == 'below3':
        query = query.filter(Review.rating <= 3)
    elif rating_filter == 'all':
        pass
    else:
        # 兼容旧版数字
        try:
            rating_int = int(rating_filter)
            query = query.filter_by(rating=rating_int)
        except:
            pass

    # 排序
    if sort == 'rating_high':
        query = query.order_by(Review.rating.desc(), Review.created_at.desc())
    elif sort == 'rating_low':
        query = query.order_by(Review.rating.asc(), Review.created_at.desc())
    else:  # latest
        query = query.order_by(Review.created_at.desc())

    pagination = query.paginate(page=page, per_page=page_size, error_out=False)
    reviews = pagination.items
    total = pagination.total

    data = []
    for r in reviews:
        user = User.query.get(r.user_id)
        data.append({
            'id': r.id,
            'user_name': user.nickname if user else '匿名用户',
            'user_avatar': user.avatar if user else '/static/images/user-avatar.jpg',
            'rating': r.rating,
            'content': r.content,
            'images': json.loads(r.images) if r.images else [],
            'created_at': r.created_at.strftime('%Y-%m-%d %H:%M:%S')
        })

    return jsonify({
        'code': 0,
        'data': {
            'list': data,
            'total': total,
            'page': page,
            'page_size': page_size,
            'total_pages': pagination.pages
        }
    })

# 商品评价统计
@api_bp.route('/products/<int:product_id>/reviews/stats', methods=['GET'])
def get_product_review_stats(product_id):
    """获取商品评价统计数据：总数、平均分、各星级数量"""
    reviews = Review.query.filter_by(product_id=product_id).all()
    total = len(reviews)
    if total == 0:
        return jsonify({
            'code': 0,
            'data': {
                'total': 0,
                'avg_rating': 0,
                'star_counts': {1:0, 2:0, 3:0, 4:0, 5:0}
            }
        })
    total_rating = sum(r.rating for r in reviews)
    avg_rating = round(total_rating / total, 1)
    star_counts = {1:0, 2:0, 3:0, 4:0, 5:0}
    for r in reviews:
        star_counts[r.rating] += 1
    return jsonify({
        'code': 0,
        'data': {
            'total': total,
            'avg_rating': avg_rating,
            'star_counts': star_counts
        }
    })

# （钓场服务）提交评价
@api_bp.route('/service_reviews', methods=['POST'])
@login_required
def create_service_review():
    data = request.get_json()
    user_id = request.current_user_id
    service_id = data.get('service_id')
    order_item_id = data.get('order_item_id')  # 新增
    rating = data.get('rating')
    content = data.get('content', '')
    images = data.get('images', [])

    if not user_id or not service_id or not order_item_id or not rating:
        return jsonify({'code': 1, 'message': '参数不完整'}), 400

    # 检查是否已评价
    existing = ServiceReview.query.filter_by(order_item_id=order_item_id).first()
    if existing:
        return jsonify({'code': 1, 'message': '您已评价过该服务'}), 400

    review = ServiceReview(
        user_id=user_id,
        service_id=service_id,
        order_item_id=order_item_id,
        rating=rating,
        content=content,
        images=json.dumps(images)
    )
    db.session.add(review)
    db.session.commit()
    return jsonify({'code': 0, 'message': '评价成功', 'data': {'id': review.id}})

# 获取某服务的评价列表（分页，支持最新/评分排序）
@api_bp.route('/service_reviews', methods=['GET'])
def get_service_reviews():
    service_id = request.args.get('service_id', type=int)
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 10, type=int)
    sort = request.args.get('sort', 'latest')  # latest / rating_high / rating_low

    if not service_id:
        return jsonify({'code': 1, 'message': '缺少服务ID'}), 400

    query = ServiceReview.query.filter_by(service_id=service_id)
    if sort == 'rating_high':
        query = query.order_by(ServiceReview.rating.desc(), ServiceReview.created_at.desc())
    elif sort == 'rating_low':
        query = query.order_by(ServiceReview.rating.asc(), ServiceReview.created_at.desc())
    else:
        query = query.order_by(ServiceReview.created_at.desc())

    pagination = query.paginate(page=page, per_page=page_size, error_out=False)
    reviews = pagination.items
    total = pagination.total

    data = []
    for r in reviews:
        user = User.query.get(r.user_id)
        data.append({
            'id': r.id,
            'user_name': user.nickname if user else '匿名',
            'user_avatar': user.avatar if user else '/static/images/user-avatar.jpg',
            'rating': r.rating,
            'content': r.content,
            'images': json.loads(r.images) if r.images else [],
            'created_at': r.created_at.strftime('%Y-%m-%d %H:%M:%S')
        })
    return jsonify({'code': 0, 'data': {'list': data, 'total': total, 'page': page, 'page_size': page_size}})

# 获取服务评价统计（平均分、总数、各星级数量）
@api_bp.route('/service_reviews/stats', methods=['GET'])
def get_service_review_stats():
    service_id = request.args.get('service_id', type=int)
    if not service_id:
        return jsonify({'code': 1, 'message': '缺少服务ID'}), 400

    reviews = ServiceReview.query.filter_by(service_id=service_id).all()
    total = len(reviews)
    if total == 0:
        return jsonify({'code': 0, 'data': {'total': 0, 'avg_rating': 0, 'star_counts': {}}})

    total_rating = sum(r.rating for r in reviews)
    avg_rating = round(total_rating / total, 1)

    star_counts = {1:0, 2:0, 3:0, 4:0, 5:0}
    for r in reviews:
        star_counts[r.rating] += 1

    return jsonify({'code': 0, 'data': {'total': total, 'avg_rating': avg_rating, 'star_counts': star_counts}})

# 获取引用对象的 JSON 表示
def get_ref_info(ref_type, ref_id):
    if not ref_type or not ref_id:
        return None
    if ref_type == 'product':
        product = Product.query.get(ref_id)
        if product and product.status == 1 and not product.is_deleted:
            return {
                'type': 'product',
                'id': product.id,
                'name': product.name,
                'image': product.pic_url,
                'price': float(product.price),
                'unit': '元'
            }
    elif ref_type == 'service':
        service = FishingService.query.get(ref_id)
        if service and service.status == 1 and not service.is_deleted:
            unit = '/天' if service.service_type == 'regular' else '/人'
            return {
                'type': 'service',
                'id': service.id,
                'name': service.name,
                'image': service.cover_image,
                'price': float(service.price),
                'unit': unit
            }
    return None

# 动态发布与获取
@api_bp.route('/posts', methods=['POST'])
@login_required
def create_post():
    """发布动态"""
    data = request.get_json()
    user_id = request.current_user_id
    content = data.get('content', '')
    location = data.get('location', '')
    tags = data.get('tags', [])  # 话题标签数组
    images = data.get('images', [])  # 图片URL数组（已上传）
    ref_type = data.get('ref_type')  # 'product' 或 'service'
    ref_id = data.get('ref_id')


    if not content and not images:
        return jsonify({'code': 1, 'message': '内容或图片不能都为空'}), 400

    # 如果传了引用，验证有效性
    if ref_type and ref_id:
        if ref_type == 'product':
            product = Product.query.get(ref_id)
            if not product or product.status != 1 or product.is_deleted:
                return jsonify({'code': 1, 'message': '商品不存在或已下架'}), 400
        elif ref_type == 'service':
            service = FishingService.query.get(ref_id)
            if not service or service.status != 1 or service.is_deleted:
                return jsonify({'code': 1, 'message': '服务不存在或已下架'}), 400
        else:
            return jsonify({'code': 1, 'message': '无效的引用类型'}), 400

    post = Post(
        user_id=user_id,
        content=content,
        location=location,
        tags=json.dumps(tags),
        ref_type = ref_type,
        ref_id = ref_id
    )

    db.session.add(post)
    db.session.flush()  # 获取post.id

    # 添加图片
    for idx, img_url in enumerate(images):
        pi = PostImage(post_id=post.id, image_url=img_url, sort_order=idx)
        db.session.add(pi)

    db.session.commit()
    return jsonify({'code': 0, 'message': '发布成功', 'data': {'post_id': post.id}})


@api_bp.route('/posts', methods=['GET'])
def get_posts():
    """获取动态列表（支持分页、筛选关注/推荐）"""
    # 尝试从 token 获取 user_id，如果没有则设为 None
    user_id = None
    auth_header = request.headers.get('Authorization')
    if auth_header and auth_header.startswith('Bearer '):
        try:
            token = auth_header.split(' ')[1]
            payload = jwt.decode(token, ACCESS_SECRET_KEY, algorithms=['HS256'])
            user_id = payload.get('user_id')
        except:
            pass

    feed_type = request.args.get('type', 'latest')
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 10, type=int)

    query = Post.query

    if feed_type == 'follow' and user_id:
        followed_ids = db.session.query(UserFollow.followed_id).filter_by(follower_id=user_id).subquery()
        query = query.filter(Post.user_id.in_(followed_ids))
    elif feed_type == 'mine' and user_id:
        query = query.filter(Post.user_id == user_id)

    # 排序
    if feed_type == 'hot':
        query = query.order_by((Post.like_count + Post.comment_count).desc(), Post.created_at.desc())
    else:
        query = query.order_by(Post.created_at.desc())

    pagination = query.paginate(page=page, per_page=page_size, error_out=False)
    posts = pagination.items
    total = pagination.total

    data = []
    for post in posts:
        images = [img.image_url for img in post.images.order_by(PostImage.sort_order).all()]
        user = User.query.get(post.user_id)
        liked = False
        is_following = False
        if user_id:
            liked = PostLike.query.filter_by(post_id=post.id, user_id=user_id).first() is not None
            if feed_type != 'mine' and post.user_id != user_id:
                is_following = UserFollow.query.filter_by(follower_id=user_id, followed_id=post.user_id).first() is not None
        is_merchant = (user.role == 'merchant') if user else False
        ref_info = get_ref_info(post.ref_type, post.ref_id)
        tags = json.loads(post.tags) if post.tags else []
        data.append({
            'id': post.id,
            'user_id': post.user_id,
            'user_name': user.nickname if user else '钓友',
            'user_avatar': user.avatar if user else '/static/images/user-avatar.jpg',
            'content': post.content,
            'location': post.location,
            'tags': tags,
            'images': images,
            'like_count': post.like_count,
            'comment_count': post.comment_count,
            'liked': liked,
            'created_at': post.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'is_following': is_following,
            'user_is_merchant': is_merchant,
            'ref_info': ref_info
        })

    return jsonify({
        'code': 0,
        'data': {
            'list': data,
            'total': total,
            'page': page,
            'page_size': page_size,
            'total_pages': pagination.pages
        }
    })

@api_bp.route('/posts/<int:post_id>', methods=['GET'])
@login_required
def get_post_detail(post_id):
    """获取单条动态详情"""
    user_id = request.current_user_id
    post = Post.query.get(post_id)
    if not post:
        return jsonify({'code': 1, 'message': '动态不存在'}), 404

    # 获取图片列表
    images = [img.image_url for img in post.images.order_by(PostImage.sort_order).all()]
    user = User.query.get(post.user_id)
    liked = False
    if user_id:
        liked = PostLike.query.filter_by(post_id=post.id, user_id=user_id).first() is not None
    tags = json.loads(post.tags) if post.tags else []
    ref_info = get_ref_info(post.ref_type, post.ref_id)

    data = {
        'id': post.id,
        'user_id': post.user_id,
        'user_name': user.nickname if user else '钓友',
        'user_avatar': user.avatar if user else '/static/images/user-avatar.jpg',
        'content': post.content,
        'location': post.location,
        'tags': tags,
        'images': images,
        'like_count': post.like_count,
        'comment_count': post.comment_count,
        'liked': liked,
        'created_at': post.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        'ref_info': ref_info
    }
    return jsonify({'code': 0, 'data': data})


@api_bp.route('/posts/user/<int:target_user_id>', methods=['GET'])
def get_user_posts(target_user_id):
    """获取指定用户的动态列表（支持分页）"""
    current_user_id = request.args.get('user_id', type=int)  # 当前登录用户ID（用于判断点赞状态）
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 10, type=int)

    # 直接按 user_id 查询，移除 is_deleted 条件
    query = Post.query.filter_by(user_id=target_user_id)
    pagination = query.order_by(Post.created_at.desc()).paginate(page=page, per_page=page_size, error_out=False)
    posts = pagination.items
    total = pagination.total

    data = []
    for post in posts:
        # 获取图片列表（假设 PostImage 有 sort_order 字段，如果没有可以去掉排序）
        images = [img.image_url for img in post.images.order_by(PostImage.sort_order).all()]
        user = User.query.get(post.user_id)
        liked = False
        if current_user_id:
            liked = PostLike.query.filter_by(post_id=post.id, user_id=current_user_id).first() is not None
        tags = json.loads(post.tags) if post.tags else []

        is_merchant = (user.role == 'merchant') if user else False
        ref_info = get_ref_info(post.ref_type, post.ref_id)

        data.append({
            'id': post.id,
            'user_id': post.user_id,
            'user_name': user.nickname if user else '钓友',
            'user_avatar': user.avatar if user else '/static/images/user-avatar.jpg',
            'content': post.content,
            'location': post.location,
            'tags': tags,
            'images': images,
            'like_count': post.like_count,
            'comment_count': post.comment_count,
            'liked': liked,
            'created_at': post.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'user_is_merchant': is_merchant,
            'ref_info': ref_info
        })
    return jsonify({
        'code': 0,
        'data': {
            'list': data,
            'total': total,
            'page': page,
            'page_size': page_size,
            'total_pages': pagination.pages
        }
    })

@api_bp.route('/posts/<int:post_id>', methods=['DELETE'])
@login_required
def delete_post(post_id):
    """删除动态（仅限作者本人）"""
    data = request.get_json() or {}
    user_id = request.current_user_id

    post = Post.query.get(post_id)
    if not post:
        return jsonify({'code': 1, 'message': '动态不存在'}), 404
    if post.user_id != user_id:
        return jsonify({'code': 1, 'message': '无权删除'}), 403
    db.session.delete(post)
    db.session.commit()
    return jsonify({'code': 0, 'message': '删除成功'})


# 点赞动态
@api_bp.route('/posts/<int:post_id>/like', methods=['POST'])
@login_required
def like_post(post_id):
    data = request.get_json()
    user_id = request.current_user_id

    # 检查是否已点赞
    existing = PostLike.query.filter_by(post_id=post_id, user_id=user_id).first()
    if existing:
        return jsonify({'code': 1, 'message': '已经点赞过了'}), 400

    # 创建点赞记录
    like = PostLike(post_id=post_id, user_id=user_id)
    db.session.add(like)

    # 更新动态点赞数
    post = Post.query.get(post_id)
    if post:
        post.like_count = PostLike.query.filter_by(post_id=post_id).count()

    # ========== 消息通知 ==========
    # 如果点赞者不是动态作者，发送通知
    if post and post.user_id != user_id:
        # 获取点赞者昵称（用于通知内容）
        liker = User.query.get(user_id)
        liker_name = liker.nickname if liker else '钓友'
        notification = Notification(
            user_id=post.user_id,          # 接收人为动态作者
            type='like',
            title='有人赞了你的动态',
            content=f'{liker_name} 赞了你的动态',
            related_id=post_id,
            related_type='post'
        )
        db.session.add(notification)

    db.session.commit()

    return jsonify({
        'code': 0,
        'message': '点赞成功',
        'data': {'like_count': post.like_count if post else 0}
    })

# 取消点赞
@api_bp.route('/posts/<int:post_id>/unlike', methods=['POST'])
@login_required
def unlike_post(post_id):
    data = request.get_json()
    user_id = request.current_user_id
    like = PostLike.query.filter_by(post_id=post_id, user_id=user_id).first()
    if not like:
        return jsonify({'code': 1, 'message': '尚未点赞'}), 400
    db.session.delete(like)
    post = Post.query.get(post_id)
    if post:
        post.like_count = PostLike.query.filter_by(post_id=post_id).count()
    db.session.commit()
    return jsonify({'code': 0, 'message': '取消点赞成功', 'data': {'like_count': post.like_count if post else 0}})


# 评论功能
@api_bp.route('/posts/<int:post_id>/comments', methods=['POST'])
@login_required
def create_comment(post_id):
    data = request.get_json()
    user_id = request.current_user_id
    content = data.get('content')
    parent_id = data.get('parent_id')
    reply_to_user_id = data.get('reply_to_user_id')
    if not content:
        return jsonify({'code': 1, 'message': '参数不完整'}), 400

    comment = PostComment(
        post_id=post_id,
        user_id=user_id,
        content=content,
        parent_id=parent_id,
        reply_to_user_id=reply_to_user_id
    )
    db.session.add(comment)
    db.session.flush()  # 获取comment.id

    # 更新动态评论数
    post = Post.query.get(post_id)
    if post:
        post.comment_count = PostComment.query.filter_by(post_id=post_id).count()

    # ========== 消息通知 ==========
    # 获取评论用户信息（用于通知内容）
    comment_user = User.query.get(user_id)
    comment_user_name = comment_user.nickname if comment_user else '钓友'
    content_preview = content[:50]  # 截取前50字符

    if parent_id is None:
        # 一级评论：通知动态作者（如果评论者不是作者本人）
        if post and post.user_id != user_id:
            notification = Notification(
                user_id=post.user_id,
                type='comment',
                title='有人评论了你的动态',
                content=f'{comment_user_name} 评论了你的动态：{content_preview}',
                related_id=post_id,
                related_type='post'
            )
            db.session.add(notification)
    else:
        # 回复：通知被回复的用户（如果被回复者不是评论者本人）
        if reply_to_user_id and reply_to_user_id != user_id:
            notification = Notification(
                user_id=reply_to_user_id,
                type='reply',
                title='有人回复了你的评论',
                content=f'{comment_user_name} 回复了你的评论：{content_preview}',
                related_id=post_id,
                related_type='post'
            )
            db.session.add(notification)

    db.session.commit()

    # 返回新评论信息（用于前端显示）
    user = User.query.get(user_id)
    return jsonify({
        'code': 0,
        'data': {
            'id': comment.id,
            'user_id': user_id,
            'user_name': user.nickname if user else '钓友',
            'user_avatar': user.avatar if user else '/static/images/user-avatar.jpg',
            'content': content,
            'created_at': comment.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'parent_id': parent_id,
            'reply_to_user_id': reply_to_user_id
        }
    })


@api_bp.route('/posts/<int:post_id>/comments', methods=['GET'])
def get_comments(post_id):
    """获取某动态的一级评论（带回复）"""
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 20, type=int)
    pagination = PostComment.query.filter_by(post_id=post_id, parent_id=None).order_by(PostComment.created_at.desc()).paginate(
        page=page, per_page=page_size, error_out=False
    )
    comments = pagination.items
    data = []
    for c in comments:
        user = User.query.get(c.user_id)
        # 获取回复列表（仅前几条）
        replies = PostComment.query.filter_by(parent_id=c.id).order_by(PostComment.created_at.asc()).limit(5).all()
        reply_list = []
        for r in replies:
            r_user = User.query.get(r.user_id)
            reply_to_user = User.query.get(r.reply_to_user_id) if r.reply_to_user_id else None
            reply_list.append({
                'id': r.id,
                'user_id': r.user_id,
                'user_name': r_user.nickname if r_user else '钓友',
                'user_avatar': r_user.avatar if r_user else '/static/images/user-avatar.jpg',
                'content': r.content,
                'reply_to_user_name': reply_to_user.nickname if reply_to_user else None,
                'created_at': r.created_at.strftime('%Y-%m-%d %H:%M:%S')
            })
        data.append({
            'id': c.id,
            'user_id': c.user_id,
            'user_name': user.nickname if user else '钓友',
            'user_avatar': user.avatar if user else '/static/images/user-avatar.jpg',
            'content': c.content,
            'created_at': c.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'replies': reply_list,
            'reply_count': PostComment.query.filter_by(parent_id=c.id).count()
        })
    return jsonify({
        'code': 0,
        'data': {
            'list': data,
            'total': pagination.total,
            'page': page,
            'page_size': page_size
        }
    })


# 关注功能
@api_bp.route('/user/follow', methods=['POST'])
@login_required
def follow_user():
    data = request.get_json()
    follower_id = request.current_user_id  # 从 token 获取
    followed_id = data.get('followed_id')
    if not followed_id:
        return jsonify({'code': 1, 'message': '参数不完整'}), 400
    if follower_id == followed_id:
        return jsonify({'code': 1, 'message': '不能关注自己'}), 400
    existing = UserFollow.query.filter_by(follower_id=follower_id, followed_id=followed_id).first()
    if existing:
        return jsonify({'code': 1, 'message': '已经关注过了'}), 400
    follow = UserFollow(follower_id=follower_id, followed_id=followed_id)
    db.session.add(follow)

    # ========== 消息通知 ==========
    # 关注成功后通知被关注者（如果被关注者不是关注者本人）
    if follower_id != followed_id:
        # 获取关注者昵称
        follower = User.query.get(follower_id)
        follower_name = follower.nickname if follower else '钓友'
        notification = Notification(
            user_id=followed_id,
            type='follow',
            title='有人关注了你',
            content=f'{follower_name} 关注了你',
            related_id=follower_id,
            related_type='user'
        )
        db.session.add(notification)

    db.session.commit()
    return jsonify({'code': 0, 'message': '关注成功'})


# 取消关注
@api_bp.route('/user/unfollow', methods=['POST'])
@login_required
def unfollow_user():
    data = request.get_json()
    follower_id = request.current_user_id   # 从 token 获取
    followed_id = data.get('followed_id')
    if not followed_id:
        return jsonify({'code': 1, 'message': '参数不完整'}), 400
    follow = UserFollow.query.filter_by(follower_id=follower_id, followed_id=followed_id).first()
    if not follow:
        return jsonify({'code': 1, 'message': '尚未关注'}), 400
    db.session.delete(follow)
    db.session.commit()
    return jsonify({'code': 0, 'message': '取消关注成功'})


@api_bp.route('/user/follow/status', methods=['GET'])
@login_required
def check_follow_status():
    follower_id = request.current_user_id   # 从 token 获取
    followed_id = request.args.get('followed_id', type=int)
    if not followed_id:
        return jsonify({'code': 1, 'message': '参数不完整'}), 400
    is_following = UserFollow.query.filter_by(follower_id=follower_id, followed_id=followed_id).first() is not None
    return jsonify({'code': 0, 'data': {'is_following': is_following}})

# 关注统计
@api_bp.route('/user/follow/stats', methods=['GET'])
@login_required
def get_follow_stats():
    user_id = request.current_user_id
    follow_count = UserFollow.query.filter_by(follower_id=user_id).count()
    fans_count = UserFollow.query.filter_by(followed_id=user_id).count()
    return jsonify({'code': 0, 'data': {'follow_count': follow_count, 'fans_count': fans_count}})

# 热门话题（简单统计）
@api_bp.route('/posts/hot/tags', methods=['GET'])
def get_hot_tags():
    """获取热门话题标签（按出现次数排序）"""
    # 简化实现：查询所有动态，提取tags并统计
    all_posts = Post.query.all()
    tag_count = {}
    for post in all_posts:
        if post.tags:
            tags = json.loads(post.tags)
            for tag in tags:
                tag_count[tag] = tag_count.get(tag, 0) + 1
    # 按次数排序取前10
    sorted_tags = sorted(tag_count.items(), key=lambda x: x[1], reverse=True)[:10]
    hot_tags = [{'name': tag, 'count': count} for tag, count in sorted_tags]
    return jsonify({'code': 0, 'data': hot_tags})


# 获取消息列表
@api_bp.route('/notifications', methods=['GET'])
@login_required
def get_notifications():
    user_id = request.current_user_id
    type_filter = request.args.get('type', 'all')  # all, interaction, order, system
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 20, type=int)
    query = Notification.query.filter_by(user_id=user_id)

    if type_filter == 'interaction':
        query = query.filter(Notification.type.in_(['like', 'comment', 'reply', 'follow']))
    elif type_filter == 'order':
        query = query.filter_by(type='order')
    elif type_filter == 'system':
        query = query.filter_by(type='system')

    pagination = query.order_by(Notification.created_at.desc()).paginate(page=page, per_page=page_size, error_out=False)
    notifications = pagination.items

    data = []
    for n in notifications:
        data.append({
            'id': n.id,
            'type': n.type,
            'title': n.title,
            'content': n.content,
            'related_id': n.related_id,
            'related_type': n.related_type,
            'is_read': n.is_read,
            'created_at': n.created_at.strftime('%Y-%m-%d %H:%M:%S')
        })

    return jsonify({
        'code': 0,
        'data': {
            'list': data,
            'total': pagination.total,
            'page': page,
            'page_size': page_size,
            'total_pages': pagination.pages
        }
    })


# 标记已读
@api_bp.route('/notifications/read', methods=['POST'])
@login_required
def mark_notifications_read():
    data = request.get_json()
    user_id = request.current_user_id
    notification_ids = data.get('ids', [])  # 可以为空，表示全部已读
    mark_all = data.get('all', False)
    query = Notification.query.filter_by(user_id=user_id, is_read=0)
    if not mark_all and notification_ids:
        query = query.filter(Notification.id.in_(notification_ids))

    query.update({'is_read': 1}, synchronize_session=False)
    db.session.commit()

    return jsonify({'code': 0, 'message': '已标记为已读'})

# 删除单个通知
@api_bp.route('/notifications/<int:notification_id>', methods=['DELETE'])
@login_required
def delete_notification(notification_id):
    user_id = request.current_user_id
    notification = Notification.query.get(notification_id)
    if not notification or notification.user_id != user_id:
        return jsonify({'code': 1, 'message': '通知不存在'}), 404
    db.session.delete(notification)
    db.session.commit()
    return jsonify({'code': 0, 'message': '删除成功'})

# 获取未读数量
@api_bp.route('/notifications/unread_count', methods=['GET'])
@login_required
def get_unread_count():
    user_id = request.current_user_id
    count = Notification.query.filter_by(user_id=user_id, is_read=0).count()
    return jsonify({'code': 0, 'data': {'count': count}})

# 获取关联账号列表
@api_bp.route('/user/associated_accounts', methods=['GET'])
@login_required
def get_associated_accounts():
    user_id = request.current_user_id
    links = UserAccountLink.query.filter_by(master_user_id=user_id).all()
    data = []
    for link in links:
        linked_user = User.query.get(link.linked_user_id)
        if linked_user:
            data.append({
                'userId': linked_user.id,
                'username': linked_user.username,
                'nickname': linked_user.nickname,
                'avatar': linked_user.avatar or '/static/images/user-avatar.jpg',
                'mobile': linked_user.mobile,
                'balance': float(linked_user.balance)
            })
    return jsonify({'code': 0, 'data': data})

# 添加关联账号
@api_bp.route('/user/associate', methods=['POST'])
@login_required
def associate_account():
    data = request.get_json()
    master_user_id = request.current_user_id
    username = data.get('username')
    password = data.get('password')
    if not username or not password:
        return jsonify({'code': 400, 'message': '用户名和密码不能为空'}), 400

    target_user = User.query.filter_by(username=username).first()
    if not target_user:
        return jsonify({'code': 404, 'message': '用户不存在'}), 404
    hashed = hashlib.sha256(password.encode('utf-8')).hexdigest()
    if target_user.password != hashed:
        return jsonify({'code': 401, 'message': '密码错误'}), 401

    if target_user.id == master_user_id:
        return jsonify({'code': 400, 'message': '不能关联自己'}), 400

    existing = UserAccountLink.query.filter_by(master_user_id=master_user_id, linked_user_id=target_user.id).first()
    if existing:
        return jsonify({'code': 409, 'message': '该账号已关联'}), 409

    count = UserAccountLink.query.filter_by(master_user_id=master_user_id).count()
    if count >= 3:
        return jsonify({'code': 400, 'message': '最多关联3个账号'}), 400

    new_link = UserAccountLink(master_user_id=master_user_id, linked_user_id=target_user.id)
    db.session.add(new_link)
    db.session.commit()
    return jsonify({'code': 0, 'message': '关联成功'})

#  删除关联账号
@api_bp.route('/user/associate', methods=['DELETE'])
@login_required
def remove_associate():
    data = request.get_json()
    master_user_id = request.current_user_id
    linked_user_id = data.get('linked_user_id')
    if not linked_user_id:
        return jsonify({'code': 400, 'message': '缺少目标用户ID'}), 400

    link = UserAccountLink.query.filter_by(master_user_id=master_user_id, linked_user_id=linked_user_id).first()
    if not link:
        return jsonify({'code': 404, 'message': '关联不存在'}), 404

    db.session.delete(link)
    db.session.commit()
    return jsonify({'code': 0, 'message': '取消关联成功'})

# 切换账号
@api_bp.route('/user/switch', methods=['POST'])
@login_required
def switch_account():
    data = request.get_json()
    current_user_id = request.current_user_id
    target_user_id = data.get('target_user_id')
    if not target_user_id:
        return jsonify({'code': 400, 'message': '缺少目标用户ID'}), 400

    # 检查关联关系（双向）
    link = UserAccountLink.query.filter(
        (UserAccountLink.master_user_id == current_user_id) & (UserAccountLink.linked_user_id == target_user_id)
    ).first()
    if not link:
        link = UserAccountLink.query.filter(
            (UserAccountLink.master_user_id == target_user_id) & (UserAccountLink.linked_user_id == current_user_id)
        ).first()
    if not link:
        return jsonify({'code': 403, 'message': '无权限切换到此账号'}), 403

    target_user = User.query.get(target_user_id)
    if not target_user:
        return jsonify({'code': 404, 'message': '目标用户不存在'}), 404

    # 生成新的 access_token
    access_token = generate_access_token(target_user.id)

    user_data = {
        'id': target_user.id,
        'username': target_user.username,
        'nickname': target_user.nickname,
        'mobile': target_user.mobile,
        'avatar': target_user.avatar,
        'balance': float(target_user.balance),
        'hy_balance': float(target_user.hy_balance),
        'coupon_count': target_user.coupon_count,
        'fish_count': target_user.fish_count,
        'fishing_count': target_user.fishing_count,
        'over_hundred_count': target_user.over_hundred_count,
        'champion_count': target_user.champion_count,
        'role': target_user.role,
        'create_time': target_user.create_time.strftime('%Y-%m-%d %H:%M:%S') if target_user.create_time else None
    }

    return jsonify({
        'code': 0,
        'data': {
            'user': user_data,
            'access_token': access_token
        }
    })

# 获取 Banner 列表（供用户端首页使用，无需认证）
@api_bp.route('/banners', methods=['GET'])
def get_banners():
    banners = Banner.query.filter_by(status=1).order_by(Banner.sort_order.asc()).all()
    data = [{'id': b.id, 'image_url': b.image_url, 'link_url': b.link_url} for b in banners]
    return jsonify({'code': 0, 'data': data})

# 管理员：获取所有 Banner（分页）
@api_bp.route('/admin/banners', methods=['GET'])
@admin_required
def get_admin_banners():
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 10, type=int)
    pagination = Banner.query.order_by(Banner.sort_order.asc()).paginate(page=page, per_page=page_size, error_out=False)
    data = [{
        'id': b.id,
        'image_url': b.image_url,
        'link_url': b.link_url,
        'sort_order': b.sort_order,
        'status': b.status,
        'created_at': b.created_at.strftime('%Y-%m-%d %H:%M:%S')
    } for b in pagination.items]
    return jsonify({'code': 0, 'data': {'list': data, 'total': pagination.total, 'page': page, 'page_size': page_size}})

# 管理员：添加 Banner
@api_bp.route('/admin/banners', methods=['POST'])
@admin_required
def add_banner():
    data = request.get_json()
    image_url = data.get('image_url')
    link_url = data.get('link_url')
    sort_order = data.get('sort_order', 0)
    status = data.get('status', 1)
    if not image_url:
        return jsonify({'code': 1, 'message': '图片地址不能为空'}), 400
    banner = Banner(image_url=image_url, link_url=link_url, sort_order=sort_order, status=status)
    db.session.add(banner)
    db.session.commit()
    return jsonify({'code': 0, 'message': '添加成功', 'data': {'id': banner.id}})

# 管理员：更新 Banner
@api_bp.route('/admin/banners/<int:banner_id>', methods=['PUT'])
@admin_required
def update_banner(banner_id):
    banner = Banner.query.get(banner_id)
    if not banner:
        return jsonify({'code': 1, 'message': 'Banner不存在'}), 404
    data = request.get_json()
    if 'image_url' in data:
        banner.image_url = data['image_url']
    if 'link_url' in data:
        banner.link_url = data['link_url']
    if 'sort_order' in data:
        banner.sort_order = data['sort_order']
    if 'status' in data:
        banner.status = data['status']
    db.session.commit()
    return jsonify({'code': 0, 'message': '更新成功'})

# 管理员：删除 Banner
@api_bp.route('/admin/banners/<int:banner_id>', methods=['DELETE'])
@admin_required
def delete_banner(banner_id):
    banner = Banner.query.get(banner_id)
    if not banner:
        return jsonify({'code': 1, 'message': 'Banner不存在'}), 404
    db.session.delete(banner)
    db.session.commit()
    return jsonify({'code': 0, 'message': '删除成功'})


# 管理员：获取所有优惠券（分页）
@api_bp.route('/admin/coupons', methods=['GET'])
@admin_required
def get_admin_coupons():
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 10, type=int)
    pagination = Coupon.query.order_by(Coupon.created_at.desc()).paginate(page=page, per_page=page_size, error_out=False)
    data = [c.to_dict() for c in pagination.items]
    return jsonify({'code': 0, 'data': {'list': data, 'total': pagination.total, 'page': page, 'page_size': page_size}})

# 管理员：添加优惠券
@api_bp.route('/admin/coupons', methods=['POST'])
@admin_required
def add_coupon():
    data = request.get_json()
    required = ['type', 'value', 'min_amount', 'expire_date']
    for f in required:
        if not data.get(f):
            return jsonify({'code': 1, 'message': f'缺少字段: {f}'}), 400
    coupon = Coupon(
        type=data['type'],
        value=data['value'],
        condition=data.get('condition', f"满{data['min_amount']}元可用"),
        min_amount=data['min_amount'],
        expire_date=datetime.strptime(data['expire_date'], '%Y-%m-%d').date()
    )
    db.session.add(coupon)
    db.session.commit()
    return jsonify({'code': 0, 'message': '添加成功', 'data': {'id': coupon.id}})

# 管理员：删除优惠券（谨慎使用，可能有用户已领取）
@api_bp.route('/admin/coupons/<int:coupon_id>', methods=['DELETE'])
@admin_required
def delete_coupon(coupon_id):
    coupon = Coupon.query.get(coupon_id)
    if not coupon:
        return jsonify({'code': 1, 'message': '优惠券不存在'}), 404
    # 检查是否已被领取
    used_count = UserCoupon.query.filter_by(coupon_id=coupon_id).count()
    if used_count > 0:
        return jsonify({'code': 1, 'message': '该优惠券已被领取，无法删除'}), 400
    db.session.delete(coupon)
    db.session.commit()
    return jsonify({'code': 0, 'message': '删除成功'})


# 数据分析统计接口
@api_bp.route('/admin/statistics/overview', methods=['GET'])
@admin_required
def admin_statistics_overview():
    """获取核心业务概览数据"""
    # 订单总览
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)

    total_orders = Order.query.count()
    total_amount = db.session.query(db.func.sum(Order.total_amount)).filter(Order.order_status == 2).scalar() or 0

    today_orders = Order.query.filter(Order.created_at >= today).count()
    week_orders = Order.query.filter(Order.created_at >= week_ago).count()
    month_orders = Order.query.filter(Order.created_at >= month_ago).count()

    # 用户总数
    total_users = User.query.filter(User.role == 'user').count()
    today_users = User.query.filter(User.create_time >= today, User.role == 'user').count()
    week_users = User.query.filter(User.create_time >= week_ago, User.role == 'user').count()
    month_users = User.query.filter(User.create_time >= month_ago, User.role == 'user').count()

    # 商品总数
    total_products = Product.query.filter(Product.is_deleted == 0).count()

    # 钓场服务总数
    total_services = FishingService.query.filter(FishingService.is_deleted == False).count()

    return jsonify({
        'code': 0,
        'data': {
            'orders': {
                'total': total_orders,
                'total_amount': float(total_amount),
                'today': today_orders,
                'week': week_orders,
                'month': month_orders
            },
            'users': {
                'total': total_users,
                'today': today_users,
                'week': week_users,
                'month': month_users
            },
            'products_total': total_products,
            'services_total': total_services
        }
    })

@api_bp.route('/admin/statistics/orders/trend', methods=['GET'])
@admin_required
def admin_orders_trend():
    """获取近30天订单趋势（数量 & 金额）"""
    days = 30
    result = []
    for i in range(days):
        date = datetime.now().date() - timedelta(days=days-1-i)
        start = datetime.combine(date, datetime.min.time())
        end = datetime.combine(date, datetime.max.time())
        count = Order.query.filter(Order.created_at >= start, Order.created_at <= end).count()
        amount = db.session.query(db.func.sum(Order.total_amount)).filter(
            Order.created_at >= start, Order.created_at <= end, Order.order_status == 2
        ).scalar() or 0
        result.append({
            'date': date.strftime('%Y-%m-%d'),
            'count': count,
            'amount': float(amount)
        })
    return jsonify({'code': 0, 'data': result})

@api_bp.route('/admin/statistics/products/top', methods=['GET'])
@admin_required
def admin_products_top():
    """热销商品TOP5（根据订单项销量统计）"""
    # 通过 OrderItem 聚合销量
    top_products = db.session.query(
        Product.id,
        Product.name,
        Product.pic_url,
        db.func.sum(OrderItem.quantity).label('total_sales')
    ).join(OrderItem, Product.id == OrderItem.product_id)\
     .filter(Product.is_deleted == 0)\
     .group_by(Product.id)\
     .order_by(db.desc('total_sales'))\
     .limit(5).all()
    data = [{
        'id': p.id,
        'name': p.name,
        'image': p.pic_url,
        'sales': int(p.total_sales)
    } for p in top_products]
    return jsonify({'code': 0, 'data': data})

@api_bp.route('/admin/statistics/services/top', methods=['GET'])
@admin_required
def admin_services_top():
    """热门服务TOP5（根据订单项服务次数统计）"""
    top_services = db.session.query(
        FishingService.id,
        FishingService.name,
        FishingService.cover_image,
        db.func.sum(OrderItem.quantity).label('total_bookings')
    ).join(OrderItem, FishingService.id == OrderItem.service_id)\
     .filter(FishingService.is_deleted == False)\
     .group_by(FishingService.id)\
     .order_by(db.desc('total_bookings'))\
     .limit(5).all()
    data = [{
        'id': s.id,
        'name': s.name,
        'image': s.cover_image,
        'bookings': int(s.total_bookings)
    } for s in top_services]
    return jsonify({'code': 0, 'data': data})

@api_bp.route('/admin/statistics/orders/status', methods=['GET'])
@admin_required
def admin_order_status_distribution():
    """订单状态分布"""
    status_map = {0: '待付款', 1: '待收货/待使用', 2: '已完成', 3: '已取消'}
    result = []
    for status, name in status_map.items():
        count = Order.query.filter_by(order_status=status).count()
        result.append({'name': name, 'value': count})
    return jsonify({'code': 0, 'data': result})

@api_bp.route('/admin/statistics/users/growth', methods=['GET'])
@admin_required
def admin_user_growth():
    """近30天用户增长趋势"""
    days = 30
    result = []
    for i in range(days):
        date = datetime.now().date() - timedelta(days=days-1-i)
        start = datetime.combine(date, datetime.min.time())
        end = datetime.combine(date, datetime.max.time())
        count = User.query.filter(User.create_time >= start, User.create_time <= end, User.role == 'user').count()
        result.append({'date': date.strftime('%Y-%m-%d'), 'count': count})
    return jsonify({'code': 0, 'data': result})