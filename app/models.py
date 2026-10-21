import json, hashlib, uuid
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
db = SQLAlchemy()

# 订单表
class Order(db.Model):
    __tablename__ = 'orders'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    order_number = db.Column(db.String(64), unique=True, nullable=False)
    total_amount = db.Column(db.Numeric(10, 2), nullable=False)
    payment_method = db.Column(db.Integer, default=3)  # 1-微信，3-余额，4-现金
    payment_status = db.Column(db.Integer, default=0)  # 0-待支付，1-已支付，2-已取消（已弃用）
    order_status = db.Column(db.SmallInteger, default=0)  # 0-待付款，1-待收货/待使用，2-已完成，3-已取消
    address_type = db.Column(db.String(20), default='pickup')
    receiver_name = db.Column(db.String(50))
    receiver_phone = db.Column(db.String(20))
    receiver_address = db.Column(db.String(255))
    pond_id = db.Column(db.Integer, db.ForeignKey('ponds.id'))
    seat_id = db.Column(db.Integer, db.ForeignKey('seats.id'))
    user_coupon_id = db.Column(db.Integer, db.ForeignKey('user_coupons.id'))
    remark = db.Column(db.Text)
    expire_time = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    # 支持服务订单
    item_type = db.Column(db.String(20), default='product')  # product / service
    service_date = db.Column(db.Date, nullable=True)
    service_time_slot = db.Column(db.String(50), nullable=True)
    participant_count = db.Column(db.Integer, default=1)

    # 关联关系
    user = db.relationship('User', backref='orders')
    items = db.relationship('OrderItem', backref='order', cascade='all, delete-orphan')
    pond = db.relationship('Pond')
    seat = db.relationship('Seat')
    user_coupon = db.relationship('UserCoupon')



# 订单商品表
class OrderItem(db.Model):
    __tablename__ = 'order_items'
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)
    product_name = db.Column(db.String(255), nullable=False)
    product_image = db.Column(db.String(255))
    product_attributes = db.Column(db.JSON)  # 规格快照
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    total_price = db.Column(db.Numeric(10, 2), nullable=False)

    # 支持服务订单
    item_type = db.Column(db.String(20), default='product')  # product / service
    service_id = db.Column(db.Integer, db.ForeignKey('fishing_service.id'), nullable=True)
    service_date = db.Column(db.Date)
    service_time_slot = db.Column(db.String(50))
    participant_count = db.Column(db.Integer, default=1)

    # 关联
    product = db.relationship('Product')
    service = db.relationship('FishingService')


class UserFavorite(db.Model):
    __tablename__ = 'user_favorites'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    goods_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    goods_data = db.Column(db.Text)  # 存储商品快照的JSON字符串
    created_at = db.Column(db.TIMESTAMP, server_default=db.func.current_timestamp())

    # 建立与用户和商品的关系
    user = db.relationship('User', backref=db.backref('favorites', lazy=True))
    product = db.relationship('Product', backref=db.backref('favorited_by', lazy=True))

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'goods_id': self.goods_id,
            'goods_data': json.loads(self.goods_data) if self.goods_data else None,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M:%S') if self.created_at else None
        }


class AdminLog(db.Model):
    """管理员操作日志表"""
    __tablename__ = 'admin_logs'

    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=False, comment='管理员ID')
    action = db.Column(db.String(100), nullable=False, comment='操作内容')
    module = db.Column(db.String(50), comment='操作模块')
    ip = db.Column(db.String(50), comment='操作IP')
    user_agent = db.Column(db.Text, comment='用户代理')
    create_time = db.Column(db.TIMESTAMP, server_default=db.func.current_timestamp())

    # 建立与管理员的关系
    admin = db.relationship('Admin', backref=db.backref('logs', lazy=True))

    def to_dict(self):
        """操作日志序列化"""
        return {
            'id': self.id,
            'admin_id': self.admin_id,
            'admin_name': self.admin.username if self.admin else None,
            'action': self.action,
            'module': self.module,
            'ip': self.ip,
            'user_agent': self.user_agent,
            'create_time': self.create_time.strftime('%Y-%m-%d %H:%M:%S') if self.create_time else None
        }

class Admin(db.Model):
    """管理员表"""
    __tablename__ = 'admins'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False, comment='管理员账号')
    password = db.Column(db.String(255), nullable=False, comment='密码')
    nickname = db.Column(db.String(50), nullable=True, comment='昵称')
    role = db.Column(db.Enum('super_admin', 'admin'), default='admin', comment='角色')
    status = db.Column(db.Boolean, default=True, comment='状态 1:正常 0:禁用')
    last_login_time = db.Column(db.TIMESTAMP, comment='最后登录时间')
    last_login_ip = db.Column(db.String(50), comment='最后登录IP')
    create_time = db.Column(db.TIMESTAMP, server_default=db.func.current_timestamp())
    update_time = db.Column(db.TIMESTAMP, server_default=db.func.current_timestamp(),
                            onupdate=db.func.current_timestamp())

    def to_dict(self):
        """管理员信息序列化"""
        return {
            'id': self.id,
            'username': self.username,
            'nickname': self.nickname,
            'role': self.role,
            'status': self.status,
            'last_login_time': self.last_login_time.strftime('%Y-%m-%d %H:%M:%S') if self.last_login_time else None,
            'last_login_ip': self.last_login_ip,
            'create_time': self.create_time.strftime('%Y-%m-%d %H:%M:%S') if self.create_time else None,
            'update_time': self.update_time.strftime('%Y-%m-%d %H:%M:%S') if self.update_time else None
        }

# 用户表
class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(120), nullable=False)  # 存储加密后的密码
    nickname = db.Column(db.String(80), nullable=False, default='钓友')
    mobile = db.Column(db.String(20))
    avatar = db.Column(db.String(255), default='/static/images/user-avatar.jpg')
    balance = db.Column(db.Numeric(10, 2), default=0.00)
    hy_balance = db.Column(db.Numeric(10, 2), default=0.00)
    coupon_count = db.Column(db.Integer, default=0)
    fish_count = db.Column(db.Integer, default=0)
    fishing_count = db.Column(db.Integer, default=0)
    over_hundred_count = db.Column(db.Integer, default=0)
    champion_count = db.Column(db.Integer, default=0)
    # 添加角色字段
    role = db.Column(db.String(20), default='user', comment='用户角色: user, merchant, admin')
    create_time = db.Column(db.TIMESTAMP, server_default=db.func.current_timestamp())

    def to_dict(self):
        """用户信息序列化方法"""
        return {
            'id': self.id,
            'username': self.username,
            'nickname': self.nickname,
            'mobile': self.mobile,
            'avatar': self.avatar,
            'balance': float(self.balance),
            'hy_balance': float(self.hy_balance),
            'coupon_count': self.coupon_count,
            'fish_count': self.fish_count,
            'fishing_count': self.fishing_count,
            'over_hundred_count': self.over_hundred_count,
            'champion_count': self.champion_count,
            'role': self.role,
            'create_time': self.create_time.strftime('%Y-%m-%d %H:%M:%S') if self.create_time else None
        }


class Product(db.Model):
    __tablename__ = 'products'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    category = db.Column(db.String(50), nullable=False)
    price = db.Column(db.Numeric(10, 2), nullable=False)
    pic_url = db.Column(db.String(255), nullable=False)
    hot = db.Column(db.Boolean, default=False)
    detail = db.Column(db.Text)  # 添加详情字段
    sales = db.Column(db.Integer)  # 添加销售字段
    stock = db.Column(db.Integer)  # 添加库存字段
    # 时间字段
    created_at = db.Column(db.TIMESTAMP, server_default=db.func.current_timestamp())
    updated_at = db.Column(db.TIMESTAMP, server_default=db.func.current_timestamp(),
                           onupdate=db.func.current_timestamp())
    # 新增字段
    merchant_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    status = db.Column(db.SmallInteger, default=1)   # 0下架，1上架，2草稿
    is_deleted = db.Column(db.SmallInteger, default=0)  # 0未删，1已删

    # 关联关系（可选）
    merchant = db.relationship('User', backref='products')
    # 添加关系
    attributes = db.relationship('ProductAttribute', backref='product', lazy='dynamic', cascade='all, delete-orphan')

    def to_dict(self):
        """商品信息序列化"""
        data = {
            'id': self.id,
            'name': self.name,
            'category': self.category,
            'price': float(self.price),
            'pic_url': self.pic_url,
            'hot': self.hot,
            'detail': self.detail,
            'sales': self.sales,
            'stock': self.stock,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M:%S') if self.created_at else None,
            'updated_at': self.updated_at.strftime('%Y-%m-%d %H:%M:%S') if self.updated_at else None,
            'merchant_id': self.merchant_id,
            'status': self.status,
            'is_deleted': self.is_deleted
        }
        # 如果有商家信息，可以添加 merchant_name
        if self.merchant:
            data['merchant_name'] = self.merchant.nickname
        return data

class ProductTag(db.Model):
    __tablename__ = 'product_tags'
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, nullable=False)
    tag = db.Column(db.String(50), nullable=False)
    # 添加时间字段
    created_at = db.Column(db.TIMESTAMP, server_default=db.func.current_timestamp())

# 添加新的模型类
class ProductImage(db.Model):
    __tablename__ = 'product_images'
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, nullable=False)
    image_url = db.Column(db.String(255), nullable=False)
    # 添加时间字段
    created_at = db.Column(db.TIMESTAMP, server_default=db.func.current_timestamp())

class ProductAttribute(db.Model):
    __tablename__ = 'product_attributes'
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    attribute_name = db.Column(db.String(50), nullable=False)
    attribute_values = db.Column(db.Text)      # JSON 字符串
    attribute_prices = db.Column(db.Text)      # JSON 字符串
    is_absolute_price = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.TIMESTAMP, server_default=db.func.current_timestamp())

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.attribute_name,
            'values': json.loads(self.attribute_values),
            'prices': json.loads(self.attribute_prices),
            'is_absolute_price': self.is_absolute_price
        }


class Pond(db.Model):
    __tablename__ = 'ponds'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    location = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    create_time = db.Column(db.TIMESTAMP, server_default=db.func.current_timestamp())
    update_time = db.Column(db.TIMESTAMP, server_default=db.func.current_timestamp(),
                            onupdate=db.func.current_timestamp())


class Seat(db.Model):
    __tablename__ = 'seats'
    id = db.Column(db.Integer, primary_key=True)
    pond_id = db.Column(db.Integer, db.ForeignKey('ponds.id'), nullable=False)
    name = db.Column(db.String(50), nullable=False)
    status = db.Column(db.SmallInteger, default=1, comment='0-不可用,1-可用')
    type = db.Column(db.String(20), default='普通')
    price = db.Column(db.Numeric(10, 2), default=0.00)
    description = db.Column(db.Text)
    create_time = db.Column(db.TIMESTAMP, server_default=db.func.current_timestamp())
    update_time = db.Column(db.TIMESTAMP, server_default=db.func.current_timestamp(),
                            onupdate=db.func.current_timestamp())

    # 定义与鱼塘的关系
    pond = db.relationship('Pond', backref=db.backref('seats', lazy=True))


# 钓场服务
class FishingService(db.Model):
    __tablename__ = 'fishing_service'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    details = db.Column(db.Text)
    price = db.Column(db.Numeric(10, 2), nullable=False)
    cover_image = db.Column(db.String(255), nullable=False)
    address = db.Column(db.String(255), nullable=False)
    service_type = db.Column(db.String(20), nullable=False, default='regular')  # regular/competition
    match_name = db.Column(db.String(100))
    match_detail = db.Column(db.Text)
    match_time = db.Column(db.DateTime)
    is_end = db.Column(db.Boolean, default=False)
    tags = db.Column(db.JSON, nullable=True)
    merchant_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, default=0)
    status = db.Column(db.SmallInteger, nullable=False, default=0, comment='0:待审核,1:已发布,2:已下架')
    is_deleted = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    images = db.Column(db.JSON, nullable=True)  # 存储图片数组
    max_people = db.Column(db.Integer, default=0)  # 最大可预约人数
    booked_people = db.Column(db.Integer, default=0)  # 已预约人数

    # 关联关系
    merchant = db.relationship('User', backref='fishing_services')

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'details': self.details,
            'price': float(self.price),
            'cover_image': self.cover_image,
            'address': self.address,
            'service_type': self.service_type,
            'match_name': self.match_name,
            'match_detail': self.match_detail,
            'match_time': self.match_time.strftime('%Y-%m-%d %H:%M:%S') if self.match_time else None,
            'is_end': self.is_end,
            'tags': self.tags or [],
            'merchant_id': self.merchant_id,
            'status': self.status,
            'is_deleted': self.is_deleted
        }


# Merchant 模型
class Merchant(db.Model):
    __tablename__ = 'merchants'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    shop_name = db.Column(db.String(100), nullable=False)
    contact_phone = db.Column(db.String(20))
    shop_address = db.Column(db.String(255))
    business_scope = db.Column(db.String(255))
    description = db.Column(db.Text)
    business_license = db.Column(db.String(255))
    id_card_front = db.Column(db.String(255))
    id_card_back = db.Column(db.String(255))
    merchant_status = db.Column(db.String(20), default='pending')
    apply_time = db.Column(db.TIMESTAMP, server_default=db.func.current_timestamp())
    approve_time = db.Column(db.TIMESTAMP)
    reject_reason = db.Column(db.Text)
    intro = db.Column(db.Text)  # 店铺介绍HTML
    intro_images = db.Column(db.Text)  # JSON数组
    user = db.relationship('User', backref=db.backref('merchants', lazy=True))

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'shop_name': self.shop_name,
            'contact_phone': self.contact_phone,
            'shop_address': self.shop_address,
            'business_scope': self.business_scope,
            'description': self.description,
            'business_license': self.business_license,
            'id_card_front': self.id_card_front,
            'id_card_back': self.id_card_back,
            'merchant_status': self.merchant_status,
            'apply_time': self.apply_time.strftime('%Y-%m-%d %H:%M:%S') if self.apply_time else None,
            'approve_time': self.approve_time.strftime('%Y-%m-%d %H:%M:%S') if self.approve_time else None,
            'reject_reason': self.reject_reason
        }


# 店铺信息
class ShopFollow(db.Model):
    __tablename__ = 'shop_follows'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    merchant_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    create_time = db.Column(db.TIMESTAMP, server_default=db.func.current_timestamp())

    __table_args__ = (db.UniqueConstraint('user_id', 'merchant_id', name='uk_user_merchant'),)


# 优惠券模型
class Coupon(db.Model):
    __tablename__ = 'coupons'
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(50), nullable=False)        # all, unlimited, rod, bait, accessory
    value = db.Column(db.Integer, nullable=False)          # 面额（元）
    condition = db.Column(db.String(100), nullable=False)  # 使用条件描述
    min_amount = db.Column(db.Integer, default=0)          # 最低消费金额
    expire_date = db.Column(db.Date, nullable=False)       # 有效期
    created_at = db.Column(db.TIMESTAMP, server_default=db.func.current_timestamp())

    def to_dict(self):
        return {
            'id': self.id,
            'type': self.type,
            'value': self.value,
            'condition': self.condition,
            'min_amount': self.min_amount,
            'expire': self.expire_date.strftime('%Y-%m-%d')
        }


class UserCoupon(db.Model):
    __tablename__ = 'user_coupons'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    coupon_id = db.Column(db.Integer, db.ForeignKey('coupons.id'), nullable=False)
    received_at = db.Column(db.TIMESTAMP, server_default=db.func.current_timestamp())
    used_at = db.Column(db.TIMESTAMP, nullable=True)
    order_id = db.Column(db.Integer, nullable=True)

    # 关系
    user = db.relationship('User', backref='user_coupons')
    coupon = db.relationship('Coupon', backref='user_coupons')

    __table_args__ = (db.UniqueConstraint('user_id', 'coupon_id', name='uk_user_coupon'),)


class Cart(db.Model):
    __tablename__ = 'cart'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)
    quantity = db.Column(db.Integer, default=1)
    selected_attributes = db.Column(db.Text)  # JSON string
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    item_type = db.Column(db.String(20), default='product')
    service_id = db.Column(db.Integer, db.ForeignKey('fishing_service.id'), nullable=True)
    service_date = db.Column(db.Date)
    service_time_slot = db.Column(db.String(50))
    participant_count = db.Column(db.Integer, default=1)
    merchant_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    price = db.Column(db.Numeric(10, 2), default=0)
    product_name = db.Column(db.String(255))
    product_image = db.Column(db.String(255))

class Review(db.Model):
    __tablename__ = 'reviews'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    order_item_id = db.Column(db.Integer, db.ForeignKey('order_items.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    rating = db.Column(db.SmallInteger, nullable=False)
    content = db.Column(db.Text)
    images = db.Column(db.Text)  # JSON 字符串
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    # 关联关系（可选）
    user = db.relationship('User', backref='reviews')
    order = db.relationship('Order', backref='reviews')
    order_item = db.relationship('OrderItem', backref='reviews')
    product = db.relationship('Product', backref='reviews')

class ServiceReview(db.Model):
    __tablename__ = 'service_reviews'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    service_id = db.Column(db.Integer, db.ForeignKey('fishing_service.id'), nullable=False)
    order_item_id = db.Column(db.Integer, db.ForeignKey('order_items.id'), nullable=False)  # 新增
    rating = db.Column(db.SmallInteger, nullable=False)
    content = db.Column(db.Text)
    images = db.Column(db.Text)  # JSON 字符串
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

# 动态社区相关模型
class Post(db.Model):
    __tablename__ = 'posts'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    content = db.Column(db.Text)
    location = db.Column(db.String(255))
    tags = db.Column(db.Text)  # JSON string
    like_count = db.Column(db.Integer, default=0)
    comment_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    # 关联关系
    user = db.relationship('User', backref=db.backref('posts', lazy='dynamic'))
    images = db.relationship('PostImage', backref='post', lazy='dynamic', cascade='all, delete-orphan')
    likes = db.relationship('PostLike', backref='post', lazy='dynamic', cascade='all, delete-orphan')
    comments = db.relationship('PostComment', backref='post', lazy='dynamic', cascade='all, delete-orphan')
    ref_type = db.Column(db.String(20), nullable=True)
    ref_id = db.Column(db.Integer, nullable=True)

class PostImage(db.Model):
    __tablename__ = 'post_images'
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('posts.id'), nullable=False)
    image_url = db.Column(db.String(500), nullable=False)
    sort_order = db.Column(db.Integer, default=0)

class PostLike(db.Model):
    __tablename__ = 'post_likes'
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('posts.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    __table_args__ = (db.UniqueConstraint('post_id', 'user_id', name='uk_post_user'),)

class PostComment(db.Model):
    __tablename__ = 'post_comments'
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('posts.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey('post_comments.id'), nullable=True)
    reply_to_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    content = db.Column(db.String(500), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)

    # 关联
    user = db.relationship('User', foreign_keys=[user_id], backref='comments')
    reply_to_user = db.relationship('User', foreign_keys=[reply_to_user_id])
    replies = db.relationship('PostComment', backref=db.backref('parent', remote_side=[id]), lazy='dynamic')

class UserFollow(db.Model):
    __tablename__ = 'user_follows'
    id = db.Column(db.Integer, primary_key=True)
    follower_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    followed_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    __table_args__ = (db.UniqueConstraint('follower_id', 'followed_id', name='uk_follow'),)


class Notification(db.Model):
    __tablename__ = 'notifications'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    type = db.Column(db.String(50), nullable=False)  # system/like/comment/reply/follow/order/pond
    title = db.Column(db.String(100), nullable=False)
    content = db.Column(db.String(255), nullable=False)
    related_id = db.Column(db.Integer, nullable=True)
    related_type = db.Column(db.String(50), nullable=True)  # post/order/comment/user
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.now)

    user = db.relationship('User', backref='notifications')


class UserToken(db.Model):
    """用户 refresh_token 表"""
    __tablename__ = 'user_tokens'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    refresh_token = db.Column(db.String(255), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)  # refresh_token 过期时间
    created_at = db.Column(db.DateTime, default=datetime.now)

    user = db.relationship('User', backref='tokens')

    @staticmethod
    def generate_token():
        return hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest()

    @staticmethod
    def create_for_user(user_id, expires_days=30):
        token = UserToken.generate_token()
        expires_at = datetime.now() + timedelta(days=expires_days)
        user_token = UserToken(user_id=user_id, refresh_token=token, expires_at=expires_at)
        db.session.add(user_token)
        db.session.commit()
        return token


class UserAccountLink(db.Model):
    """用户关联账号表（主账号关联其他账号）"""
    __tablename__ = 'user_account_links'
    id = db.Column(db.Integer, primary_key=True)
    master_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)  # 主账号
    linked_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)  # 被关联账号
    created_at = db.Column(db.DateTime, default=datetime.now)

    __table_args__ = (db.UniqueConstraint('master_user_id', 'linked_user_id', name='uk_master_linked'),)

    master = db.relationship('User', foreign_keys=[master_user_id])
    linked = db.relationship('User', foreign_keys=[linked_user_id])


class Banner(db.Model):
    __tablename__ = 'banners'
    id = db.Column(db.Integer, primary_key=True)
    image_url = db.Column(db.String(255), nullable=False)  # 图片相对路径
    link_url = db.Column(db.String(255), nullable=True)   # 跳转链接（可选）
    sort_order = db.Column(db.Integer, default=0)         # 排序序号
    status = db.Column(db.SmallInteger, default=1)        # 1-启用 0-禁用
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)