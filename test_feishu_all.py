import os
import json
import requests
from dotenv import load_dotenv

# 加载配置
load_dotenv()
APP_ID = os.getenv("FEISHU_APP_ID")
APP_SECRET = os.getenv("FEISHU_APP_SECRET")
RECEIVER_ID = os.getenv("FEISHU_NOTICE_RECEIVER")

class FeishuTester:
    def __init__(self):
        self.test_results = []
        self.token = self._get_token()
    
    def _get_token(self):
        """获取访问令牌"""
        try:
            url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
            res = requests.post(url, json={
                "app_id": APP_ID,
                "app_secret": APP_SECRET
            })
            res.raise_for_status()
            token = res.json()['tenant_access_token']
            self.test_results.append(("✅ 获取AccessToken", "成功"))
            return token
        except Exception as e:
            self.test_results.append(("❌ 获取AccessToken", f"失败: {str(e)}"))
            return None
    
    def test_send_message(self):
        """测试发送消息功能"""
        if not self.token:
            self.test_results.append(("❌ 发送消息测试", "跳过：无AccessToken"))
            return
        
        try:
            url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
            res = requests.post(url, 
                headers={"Authorization": f"Bearer {self.token}"},
                json={
                    "receive_id": RECEIVER_ID,
                    "msg_type": "text",
                    "content": json.dumps({"text": "🧪 全功能测试：发送消息功能正常！"})
                }
            )
            res.raise_for_status()
            if res.json()['code'] == 0:
                self.test_results.append(("✅ 发送消息测试", "成功，请到飞书查看消息"))
            else:
                self.test_results.append(("❌ 发送消息测试", f"失败: {res.json()['msg']}"))
        except Exception as e:
            self.test_results.append(("❌ 发送消息测试", f"失败: {str(e)}"))
    
    def test_list_chats(self):
        """测试拉取会话列表功能"""
        if not self.token:
            self.test_results.append(("❌ 拉取会话列表测试", "跳过：无AccessToken"))
            return
        
        try:
            url = "https://open.feishu.cn/open-apis/im/v1/chats?page_size=10"
            res = requests.get(url, headers={"Authorization": f"Bearer {self.token}"})
            res.raise_for_status()
            if res.json()['code'] == 0:
                chats = res.json()['data']['items']
                self.test_results.append(("✅ 拉取会话列表测试", f"成功，共获取到{len(chats)}个会话"))
            else:
                self.test_results.append(("❌ 拉取会话列表测试", f"失败: {res.json()['msg']}"))
        except Exception as e:
            self.test_results.append(("❌ 拉取会话列表测试", f"失败: {str(e)}"))
    
    def test_list_messages(self):
        """测试拉取消息列表功能"""
        if not self.token:
            self.test_results.append(("❌ 拉取消息列表测试", "跳过：无AccessToken"))
            return
        
        try:
            # 先获取和测试用户的会话ID
            url = f"https://open.feishu.cn/open-apis/im/v1/chats?user_id_type=open_id&page_size=1"
            res = requests.get(url, headers={"Authorization": f"Bearer {self.token}"})
            chat_id = res.json()['data']['items'][0]['chat_id']
            
            # 拉取最新消息
            url = f"https://open.feishu.cn/open-apis/im/v1/messages?container_id_type=chat&container_id={chat_id}&page_size=5"
            res = requests.get(url, headers={"Authorization": f"Bearer {self.token}"})
            res.raise_for_status()
            if res.json()['code'] == 0:
                msgs = res.json()['data']['items']
                self.test_results.append(("✅ 拉取消息列表测试", f"成功，共获取到{len(msgs)}条最新消息"))
            else:
                self.test_results.append(("❌ 拉取消息列表测试", f"失败: {res.json()['msg']}"))
        except Exception as e:
            self.test_results.append(("❌ 拉取消息列表测试", f"失败: {str(e)}"))
    
    def print_report(self):
        """打印测试报告"""
        print("\n" + "="*60)
        print("📋 飞书API全功能测试报告")
        print("="*60)
        for test_name, result in self.test_results:
            print(f"{test_name:30} | {result}")
        print("="*60)
        
        success_count = sum(1 for name, res in self.test_results if res.startswith("成功"))
        total_count = len(self.test_results)
        print(f"测试结果：{success_count}/{total_count} 项测试通过")
        if success_count == total_count:
            print("🎉 所有功能测试正常！飞书配置完全没问题，MCP可以正常使用")
        else:
            print("⚠️  部分测试失败，请根据错误提示排查权限或配置")

if __name__ == "__main__":
    tester = FeishuTester()
    tester.test_send_message()
    tester.test_list_chats()
    tester.test_list_messages()
    tester.print_report()
