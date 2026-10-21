# 钓鱼服务平台 · 后端

> 面向钓鱼爱好者的垂直领域电商平台后端 —— 商品、钓场预约、社区互动、订单交易一体化。

在线体验：**http://114.132.247.207**

前端仓库：[fishinglab_frontend](https://github.com/Karvin-k/fishinglab_frontend)

---

## 技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| Web 框架 | Flask 3.1 | 蓝图划分路由，`/api` 前缀 |
| ORM | Flask-SQLAlchemy 3.1 | 9 个核心模型，29 张表 |
| 数据库 | MySQL 8.0 | utf8mb4，独立业务账号（非 root） |
| 迁移 | Flask-Migrate | 表结构变更走 Alembic |
| 鉴权 | JWT（PyJWT） | Access / Refresh 双 Token，三套密钥互相隔离 |
| 跨域 | flask-cors | 按路径白名单配置 |
| WSGI | Gunicorn 21.2 | 生产环境，systemd 托管 |
| 反向代理 | Nginx 1.18 | 静态文件 + API 分流 |

---

## 功能模块

```
用户    注册 / 登录 / JWT 刷新 / 头像上传 / 余额与优惠券
商品    分类浏览 / 规格属性（绝对价·加价两种模式）/ 图集 / 评价
钓场    钓场列表 / 座位 / 服务预约（按人数、按日期场次）/ 比赛
商城    购物车 / 下单 / 支付（模拟）/ 订单状态流转
社区    发帖 / 图片 / 点赞 / 评论 / 关注 / 排行榜
商家    入驻申请 / 商品管理 / 钓场管理 / 收入统计
后台    管理员登录 / 用户管理 / 商家审核 / 数据看板
```

---

## 目录结构

```
fishing/
├── app/
│   ├── __init__.py        # 应用工厂 + 统一的图片地址处理钩子
│   ├── models.py          # 数据模型
│   ├── routes.py          # 全部接口（蓝图 /api）
│   ├── utils.py           # 序列化、规格价格计算
│   └── static/
│       ├── images/        # 界面图（占位图、优惠券背景、金牌等）
│       ├── shop/          # 早期商品图
│       └── uploads/       # 用户上传（不进仓库）
├── config.py              # 配置类，从环境变量读
├── run.py                 # 本地开发入口 + 定时任务
├── requirements.txt
├── schema.sql             # 建表脚本（不含数据）
└── .env.example           # 环境变量模板
```

---

## 本地运行

**1. 准备数据库**

```bash
mysql -u root -p -e "CREATE DATABASE fishing_shop_db CHARACTER SET utf8mb4;"
mysql -u root -p fishing_shop_db < schema.sql
```

**2. 配置环境变量**

```bash
cp .env.example .env
# 编辑 .env，填入数据库密码和各密钥
```

**3. 安装依赖并启动**

```bash
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
python run.py
```

服务跑在 `http://localhost:5000`，接口前缀 `/api`。

---

## 部署要点

线上用的是 **Gunicorn + systemd + Nginx**：

```ini
# /etc/systemd/system/fishing.service
[Service]
WorkingDirectory=/home/ubuntu/fishing
ExecStart=/home/ubuntu/venv/bin/gunicorn -w 1 -b 127.0.0.1:5000 run:app
Restart=always
```

**为什么是 `-w 1`**：`run.py` 里挂了 APScheduler 定时任务（每 5 分钟取消过期订单）。多 worker 会让每个进程各跑一份，同一批订单被重复回滚库存，所以必须单 worker。

Nginx 承担两件事：

```nginx
location /api/ { proxy_pass http://127.0.0.1:5000; }

location /static/ {
    try_files $uri @flask_static;   # 先找前端 dist，找不到再转后端
}
```

`/static/` 用 `try_files` 兜底，是因为这个项目的图片有两套来源：
- 界面图（tabbar 图标、奖牌）在前端 `dist/static/` 里
- 用户上传图在后端 `app/static/uploads/`

---

## 几个值得说的实现细节

### 商品规格价格的两种模式

`product_attributes` 表里每个属性有个 `is_absolute_price` 标记：

- `true` —— 该值就是最终价（如「小号 99.99 / 中号 123.12」）
- `false` —— 该值是加价，累加到基础价上

前端选规格、后端加购物车、下单结算，**三处都用同一套计算逻辑**，避免"详情页显示 A 价、购物车显示 B 价"。

后端的 `calc_attr_price()` 是权威计算方，**不信任前端传的价格**。

### 图片地址的统一处理

数据库里存的是相对路径（`/static/uploads/xxx.png`），直接返回给前端会 404 —— 因为 H5 端会把相对路径解析到自己的静态目录去。

解决方式是在 `after_request` 里统一处理：

```python
@app.after_request
def _absolutize_static_urls(response):
    # 遍历 JSON 响应，把 /static/ 开头且后端确实存在的路径拼上 STATIC_BASE_URL
```

**为什么要判断文件是否存在**：有些图（金银铜牌）只存在于前端，无脑拼后端地址反而 404。

---

## 已知限制

- 支付为**模拟流程**，不涉及真实资金往来
- 商品图片未包含在仓库中（体积原因），首次运行会显示占位图
- 定时任务与 Gunicorn 单 worker 绑定，横向扩展需要把定时任务拆成独立进程
