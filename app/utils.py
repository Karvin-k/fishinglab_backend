# 添加JWT工具函数
from flask import current_app
import jwt, json
from datetime import datetime, timedelta

def abs_img_url(path):
    """把数据库里的图片相对路径拼成可直接访问的完整 URL。

    规则：
      - 空值             -> 空字符串
      - 已是 http(s)     -> 原样返回
      - 其余 /static/...  -> 前面拼 STATIC_BASE_URL

    为什么：数据库存的是 /static/uploads/xxx.png，H5 端拿到相对路径会去
    「前端 dist 的 static 目录」找，找不到就是 404（且 <image> 不报错只显示白块）。
    拼上后端地址，请求才会正确打到 Flask。
    """
    if not path:
        return ''
    if path.startswith('http://') or path.startswith('https://'):
        return path
    try:
        base = current_app.config.get('STATIC_BASE_URL', '')
    except RuntimeError:
        base = ''
    return base + path


def safe_load_attrs(value):
    """把 selected_attributes 统一解析成 list。

    数据库里这个字段历史上存过三种形态，都要能吃下：
      1. 已经是 list：["500g", "果香"]
      2. 正常的 JSON 字符串：'["500g", "果香"]'
      3. 被转义两次的字符串：'"[\\"500g\\", \\"果香\\"]"'   ← 老数据里的脏值

    解析不出来就返回空 list，调用方按"没选规格"处理，不会崩。
    """
    import json
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        return []
    s = value.strip()
    for _ in range(3):          # 最多剥三层转义
        if not s:
            return []
        try:
            parsed = json.loads(s)
        except Exception:
            return []
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, str):
            s = parsed
            continue
        return []
    return []


def calc_attr_price(product, selected_attributes):
    """根据选中的规格算出实际单价。

    规则和前端 detail.vue 的 calculatePrice() 保持一致：
      · 基础价 = products.price
      · is_absolute_price = True 的属性：该值就是最终价（多个属性都给了绝对价，取最大值）
      · is_absolute_price = False 的属性：该值是加价，累加到基础价上
      · 最终价不能低于基础价

    为什么后端要自己算：前端传上来的价格不可信（可被篡改，也可能因为前端 bug 传错）。
    后端算的才是准的。
    """
    base = float(product.price or 0)
    attrs_sel = safe_load_attrs(selected_attributes)
    if not attrs_sel:
        return round(base, 2)

    from app.models import ProductAttribute

    final = base
    absolute_prices = []
    for attr in ProductAttribute.query.filter_by(product_id=product.id).all():
        try:
            values = json.loads(attr.attribute_values or '[]')
            prices = json.loads(attr.attribute_prices or '[]')
        except Exception:
            continue
        if not isinstance(values, list) or not isinstance(prices, list):
            continue
        for sel in attrs_sel:
            if sel in values:
                idx = values.index(sel)
                if idx < len(prices):
                    try:
                        p = float(prices[idx])
                    except (TypeError, ValueError):
                        continue
                    if attr.is_absolute_price:
                        absolute_prices.append(p)
                    else:
                        final += p
                break

    if absolute_prices:
        # 绝对价：直接取该规格的标价，不受基础价限制。
        # 否则"小一号比基础价便宜"（如 47 vs 56）会被抬高到 56，价格就错了。
        return round(max(absolute_prices), 2)
    if final < base:
        # 加价模式：算出来的价不能低于基础价
        final = base
    return round(final, 2)


def serialize_product(product):
    """将 Product 对象序列化为字典"""
    # 确保所有字符串字段正确处理
    return {
        'id': product.id,
        'name': product.name,  # 直接返回字符串，不需要额外处理
        'category': product.category,
        'price': f"{product.price:.2f}",
        'pic_url': abs_img_url(product.pic_url),
        'hot': 1 if product.hot else 0,
        'tags': get_product_tags(product.id)
    }


def get_product_tags(product_id):
    """获取产品的标签列表"""
    from app.models import ProductTag, db

    # 查询该产品的所有标签
    tags = ProductTag.query.filter_by(product_id=product_id).all()
    # 直接返回中文标签
    return [tag.tag for tag in tags]


def serialize_products(products):
    """序列化产品列表"""
    return [serialize_product(product) for product in products]


# 添加新的商品详情序列化函数
def serialize_product_detail(product):
    """将商品详情序列化为字典"""
    from app.models import ProductImage, ProductTag, ProductAttribute
    import json

    # 获取商品图片
    images = ProductImage.query.filter_by(product_id=product.id).all()
    image_urls = [img.image_url for img in images]

    # 获取商品标签
    tags = ProductTag.query.filter_by(product_id=product.id).all()
    tag_list = [tag.tag for tag in tags]

    # 获取商品属性
    attributes = ProductAttribute.query.filter_by(product_id=product.id).all()
    attribute_list = []
    for attr in attributes:
        try:
            # 解析JSON字符串的属性值
            values = json.loads(attr.attribute_values)

            # 新增：解析价格
            prices = []
            # 检查是否有价格数据
            if attr.attribute_prices:  # 直接检查属性对象的价格字段
                try:
                    # 尝试解析价格数组
                    prices = json.loads(attr.attribute_prices)
                except json.JSONDecodeError:
                    # 如果解析失败，尝试其他格式
                    prices = [
                        float(p.strip())
                        for p in attr.attribute_prices.strip('[]').split(',')
                    ]
            else:
                # 如果没有价格数据，创建与值数量相同的0数组
                prices = [0] * len(values)

        except json.JSONDecodeError:
            # 如果解析失败，使用原始字符串分割
            values = attr.attribute_values.strip('[]').replace('"', '').split(',')
            prices = [0] * len(values)

        attribute_list.append({
            'name': attr.attribute_name,
            'values': values,
            'prices': prices,  # 新增价格数组
            'is_absolute_price': attr.is_absolute_price  # 添加价格类型标识
        })

    # 返回完整的商品信息
    return {
        'id': product.id,
        'name': product.name,
        'category': product.category,
        'price': float(product.price),
        'main_image': abs_img_url(product.pic_url),
        'images': [abs_img_url(u) for u in image_urls],
        'tags': tag_list,
        'sales': product.sales if product.sales else 0,
        'stock': product.stock if product.stock else 0,
        'detail': product.detail if product.detail else '',
        'attributes': attribute_list if attribute_list else []  # 确保属性列表不为空
    }