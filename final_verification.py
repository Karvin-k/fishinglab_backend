import requests
import json
import os

BASE_URL = "http://localhost:5000"

def final_test():
    """最终验证测试"""
    print("=== 商家入驻功能最终验证 ===\n")

    # 1. 测试文件上传
    print("1. 📁 文件上传测试...")
    test_file = "./images/test3.jpg"
    with open(test_file, 'rb') as f:
        files = {'file': (test_file, f, 'image/jpeg')}
        headers = {'User-ID': '1'}
        response = requests.post(f"{BASE_URL}/api/upload", files=files, headers=headers)

    if response.status_code == 200 and response.json().get('code') == 0:
        file_url = response.json()['data']['url']
        print("   ✅ 文件上传成功")
    else:
        print("   ❌ 文件上传失败")
        return

    # 2. 测试商家申请
    print("2. 📝 商家申请测试...")
    apply_data = {
        "userId": 6,  # 使用存在的用户ID
        "shopName": "最终验证测试店铺",
        "contactPhone": "13800138000",
        "shopAddress": "最终验证地址",
        "businessScope": "渔具销售",
        "description": "最终验证描述",
        "licenseImage": file_url,
        "idCardFront": file_url,
        "idCardBack": file_url
    }

    headers = {
        'Content-Type': 'application/json',
        'User-ID': '1'
    }

    response = requests.post(
        f"{BASE_URL}/api/merchant/apply",
        json=apply_data,
        headers=headers
    )

    print(f"   响应状态码: {response.status_code}")
    print(f"   响应内容: {response.text}")

    if response.status_code == 200 and response.json().get('code') == 0:
        apply_result = response.json()['data']
        print("   ✅ 商家申请成功")
        print(f"   申请ID: {apply_result['apply_id']}")
        print(f"   申请时间: {apply_result['apply_time']}")
    else:
        error_info = response.json()
        print(f"   ❌ 商家申请失败: {error_info.get('message', '未知错误')}")
        return

    # 3. 测试状态查询
    print("3. 🔍 状态查询测试...")
    params = {'userId': 8}  # 修正为正确的用户ID
    response = requests.get(f"{BASE_URL}/api/merchant/status", params=params)

    if response.status_code == 200:
        result = response.json()
        if result.get('code') == 0:
            status_data = result['data']
            print("   ✅ 状态查询成功")
            print(f"   店铺名称: {status_data['shop_name']}")
            print(f"   申请状态: {status_data['merchant_status']}")
            print(f"   联系电话: {status_data['contact_phone']}")
        else:
            print(f"   ❌ 状态查询失败: {result.get('message')}")
    else:
        print(f"   ❌ HTTP错误: {response.status_code}")

    print("\n🎉 === 商家入驻功能验证完成 ===")

def check_database_record():
    """检查数据库中的记录"""
    print("\n=== 数据库记录检查 ===")
    import sys
    import os
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))

    from app import create_app
    from app.models import Merchant

    app = create_app()
    with app.app_context():
        merchants = Merchant.query.all()
        print(f"商家申请记录总数: {len(merchants)}")

        for merchant in merchants:
            print(f"\n📋 申请记录 #{merchant.id}:")
            print(f"   用户ID: {merchant.user_id}")
            print(f"   店铺名称: {merchant.shop_name}")
            print(f"   状态: {merchant.merchant_status}")
            print(f"   申请时间: {merchant.apply_time}")
            print(f"   营业执照: {'✅ 已上传' if merchant.business_license else '❌ 未上传'}")

if __name__ == "__main__":
    final_test()
    check_database_record()