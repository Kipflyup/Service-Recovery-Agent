import os
from dotenv import load_dotenv
import requests
import json

# 加载.env环境配置
load_dotenv()

# 先获取tenant_access_token
def get_tenant_token():
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    payload = json.dumps({
        "app_id": os.getenv("FEISHU_APP_ID"),
        "app_secret": os.getenv("FEISHU_APP_SECRET")
    })
    headers = {
        'Content-Type': 'application/json'
    }
    response = requests.request("POST", url, headers=headers, data=payload)
    return response.json()['tenant_access_token']

# 发送消息
def send_message(token, receive_id):
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
    payload = json.dumps({
        "receive_id": receive_id,
        "msg_type": "text",
        "content": json.dumps({"text": "✅ 飞书机器人连通性测试成功！你的Service-Recovery-Agent飞书配置完全正常~"})
    })
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {token}'
    }
    response = requests.request("POST", url, headers=headers, data=payload)
    return response.json()

if __name__ == '__main__':
    try:
        token = get_tenant_token()
        receiver_id = os.getenv("FEISHU_NOTICE_RECEIVER")
        res = send_message(token, receiver_id)
        
        if res.get('code') == 0:
            print("🎉 消息发送成功！请到飞书客户端查看测试消息")
        else:
            print(f"❌ 发送失败")
            print(f"错误码：{res.get('code')}")
            print(f"错误信息：{res.get('msg')}")
            print(f"排查建议：")
            print("1. 检查.env里的FEISHU_APP_ID和FEISHU_APP_SECRET是否正确")
            print("2. 确认应用已经发布生效，「获取与发送单聊消息」权限已开通")
            print("3. 确认填写的FEISHU_NOTICE_RECEIVER是正确的open_id")
    except Exception as e:
        print(f"❌ 运行出错：{str(e)}")
