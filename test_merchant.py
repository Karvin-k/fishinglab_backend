import requests
import json
import os

# 测试配置 - 使用正确的URL
BASE_URL = "http://localhost:5000"


def test_merchant_endpoints_corrected():
    """测试修正后的商家端点"""
    print("=== 测试修正后的商家端点 ===")

    endpoints = [
        ("/api/upload", "POST"),
        ("/api/merchant/apply", "POST"),
        ("/api/merchant/status", "GET")
    ]

    for endpoint, method in endpoints:
        try:
            if method == "POST":
                response = requests.post(f"{BASE_URL}{endpoint}")
            else:
                response = requests.get(f"{BASE_URL}{endpoint}")

            print(f"{endpoint:30} [{method:6}] -> 状态码: {response.status_code}")

            if response.status_code == 404:
                print(f"   ❌ 端点不存在: {endpoint}")
            elif response.status_code == 405:
                print(f"   ⚠️  方法不允许，但端点存在: {endpoint}")
            elif response.status_code == 200:
                print(f"   ✅ 端点正常响应")
                # 打印响应内容的前100个字符
                response_text = response.text[:100] + "..." if len(response.text) > 100 else response.text
                print(f"       响应: {response_text}")
            else:
                print(f"   ℹ️  其他状态码: {response.status_code}")

        except Exception as e:
            print(f"{endpoint:30} [{method:6}] -> 错误: {e}")


def test_with_real_data():
    """使用真实数据进行测试"""
    print("\n=== 使用真实数据测试 ===")

    # 测试文件上传
    print("1. 测试文件上传...")
    try:
        # 创建一个测试文件
        test_file_path = "./images/test1.png"
        if not os.path.exists(test_file_path):
            # 创建一个简单的文本文件作为测试
            with open(test_file_path, 'w') as f:
                f.write("test image content")

        with open(test_file_path, 'rb') as f:
            files = {'file': ('test.jpg', f, 'image/jpeg')}
            response = requests.post(f"{BASE_URL}/api/upload", files=files, headers={'User-ID': '1'})

        print(f"   文件上传状态: {response.status_code}")
        if response.status_code == 200:
            result = response.json()
            if result.get('code') == 0:
                file_url = result['data']['url']
                print(f"   ✅ 文件上传成功: {file_url}")

                # 测试商家申请
                print("2. 测试商家申请...")
                apply_data = {
                    "userId": 1,
                    "shopName": "测试修复后的店铺",
                    "contactPhone": "13800138000",
                    "shopAddress": "测试地址",
                    "businessScope": "渔具销售",
                    "description": "测试描述",
                    "licenseImage": file_url,
                    "idCardFront": file_url,
                    "idCardBack": file_url
                }

                response = requests.post(
                    f"{BASE_URL}/api/merchant/apply",
                    json=apply_data,
                    headers={'Content-Type': 'application/json', 'User-ID': '1'}
                )

                print(f"   商家申请状态: {response.status_code}")
                if response.status_code == 200:
                    result = response.json()
                    print(f"   响应: {result}")
                    if result.get('code') == 0:
                        print("   ✅ 商家申请提交成功")
                    else:
                        print(f"   ⚠️  商家申请返回错误: {result.get('message')}")
                else:
                    print(f"   ❌ 商家申请HTTP错误: {response.status_code}")
            else:
                print(f"   ❌ 文件上传失败: {result.get('message')}")
        else:
            print(f"   ❌ 文件上传HTTP错误: {response.status_code}")

    except Exception as e:
        print(f"   ❌ 测试过程中出错: {e}")


def main():
    print("修正后的商家功能测试...\n")

    # 测试端点是否存在
    test_merchant_endpoints_corrected()

    # 使用真实数据测试
    test_with_real_data()

    print("\n=== 测试完成 ===")
    print("如果所有端点都返回200状态码，说明修复成功！")


if __name__ == "__main__":
    main()