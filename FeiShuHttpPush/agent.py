import os
import json
import requests
import subprocess
from volcenginesdkarkruntime import Ark


class SimpleDevAgent:
    def __init__(self, project_root, feishu_webhook, github_token=None):
        self.project_root = project_root
        self.feishu_webhook = feishu_webhook
        self.github_token = github_token

        self.ark_api_key = "ark-4e5ab73a-07b7-4436-b1d9-9f3a7bc7058d-412cc"
        self.ark_endpoint_id = "ep-20260423222752-9tcpw"
        self.ark_base_url = "https://ark.cn-beijing.volces.com/api/v3"

        self.llm_client = Ark(
            base_url=self.ark_base_url,
            api_key=self.ark_api_key
        )

        self.git_user_name = "Auto-Fix-Agent"
        self.git_user_email = "agent@example.com"

    def _run_git_command_with_env(self, cmd, env_vars=None):
        env = os.environ.copy()
        if env_vars:
            env.update(env_vars)
        
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, 
                                  cwd=self.project_root, encoding='utf-8', errors='ignore',
                                  env=env)
            return result.returncode == 0, result.stdout.strip()
        except Exception as e:
            return False, str(e)

    def _run_git_command(self, cmd):
        return self._run_git_command_with_env(cmd)

    def _get_current_branch(self):
        success, output = self._run_git_command("git branch --show-current")
        if success and output:
            return output.strip()
        return "main"

    def commit_changes(self, commit_msg):

        add_success, add_msg = self._run_git_command("git add .")
        if not add_success:
            return False, f"git add failed: {add_msg}"

        git_env = {
            "GIT_COMMITTER_NAME": self.git_user_name,
            "GIT_COMMITTER_EMAIL": self.git_user_email,
            "GIT_AUTHOR_NAME": self.git_user_name,
            "GIT_AUTHOR_EMAIL": self.git_user_email,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null"
        }
        
        commit_cmd = f'git commit -m "{commit_msg}"'
        commit_success, commit_msg_out = self._run_git_command_with_env(commit_cmd, git_env)
        if not commit_success:
            return False, f"git commit failed: {commit_msg_out}"

        current_branch = self._get_current_branch()
        
        if self.github_token:
            get_remote_cmd = "git remote get-url origin"
            success, remote_url = self._run_git_command(get_remote_cmd)
            if success and remote_url.startswith("https://"):
                token_url = remote_url.replace("https://", f"https://{self.github_token}@")
                push_cmd = f'git push {token_url} {current_branch}'
                push_success, push_msg = self._run_git_command_with_env(push_cmd, {
                    "GIT_TERMINAL_PROMPT": "0"
                })
            else:
                push_success = False
                push_msg = f"无法获取远程仓库URL: {remote_url}"
        else:
            push_success, push_msg = self._run_git_command(f"git push origin {current_branch}")
        
        return push_success, push_msg

    # 卡片发送
    def send_feishu_card(self, title, summary, commit_hash, fix_log):

        import requests
        import json

        card = {
            "schema": "2.0",
            "config": {
                "update_multi": True,
                "style": {
                    "text_size": {
                        "normal_v2": {
                            "default": "normal",
                            "pc": "normal",
                            "mobile": "heading"
                        }
                    }
                }
            },
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "plain_text",
                            "content": title,
                            "text_size": "normal_v2",
                            "text_align": "left",
                            "text_color": "default"
                        },
                        "margin": "0px 0px 0px 0px"
                    },
                    {
                        "tag": "div",
                        "text": {
                            "tag": "plain_text",
                            "content": summary,
                            "text_size": "normal_v2",
                            "text_align": "left",
                            "text_color": "default"
                        },
                        "margin": "8px 0px 0px 0px"
                    },
                    {
                        "tag": "column_set",
                        "horizontal_align": "left",
                        "columns": [
                            {
                                "tag": "column",
                                "width": "weighted",
                                "elements": [
                                    {
                                        "tag": "markdown",
                                        "content": f"**Commit:** {commit_hash}\n**Log:** {fix_log}",
                                        "text_align": "left",
                                        "text_size": "normal_v2"
                                    }
                                ],
                                "vertical_spacing": "8px",
                                "horizontal_align": "left",
                                "vertical_align": "top",
                                "weight": 1
                            },
                            {
                                "tag": "column",
                                "width": "auto",
                                "elements": [
                                    {
                                        "tag": "button",
                                        "text": {
                                            "tag": "plain_text",
                                            "content": "查看详情"
                                        },
                                        "type": "danger",
                                        "width": "default",
                                        "size": "small",
                                        "behaviors": [
                                            {
                                                "type": "open_url",
                                                "default_url": "https://open.feishu.cn",
                                                "pc_url": "https://open.feishu.cn",
                                                "ios_url": "https://open.feishu.cn",
                                                "android_url": "https://open.feishu.cn"
                                            }
                                        ]
                                    }
                                ],
                                "vertical_spacing": "8px",
                                "horizontal_align": "left",
                                "vertical_align": "top"
                            }
                        ]
                    }
                ]
            }
        }

        headers = {'Content-Type': 'application/json'}
        payload = {"msg_type": "interactive", "card": card}

        try:
            response = requests.post(self.feishu_webhook, headers=headers, data=json.dumps(payload))
            return response.status_code == 200
        except Exception as e:
            print(f"发送飞书通知失败: {e}")
            return False

    def analyze_and_fix_with_llm(self, file_path, error_message, stack_trace):

        try:
            with open(os.path.join(self.project_root, file_path), 'r', encoding='utf-8') as f:
                file_content = f.read()
        except Exception as e:
            return False, f"无法读取文件 {file_path}: {e}"

        system_prompt = """你是一个资深的 Java/Spring Boot 开发专家。你的任务是根据运行时异常，分析代码漏洞，并直接输出修复后的完整代码。
要求：
1. 只修复导致异常的具体问题，不要改动无关逻辑。
2. 输出格式必须是：```java <修复后的完整代码> ```
3. 如果原文件有多个类，必须保持所有类的完整性。
4. 修复逻辑必须精准，例如空指针异常要添加 null 检查，不要引入新 Bug。"""

        user_prompt = f"""
## 错误信息
{error_message}

## 堆栈跟踪
{stack_trace}

## 需要修复的Java文件内容（文件路径：{file_path}）
java
{file_content}
请分析上述错误，并直接给出修复后的完整 Java 代码（只输出代码块，不要额外解释）。"""

        try:
            response = self.llm_client.chat.completions.create(
                model=self.ark_endpoint_id,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.1
            )
            llm_output = response.choices[0].message.content
        except Exception as e:
            return False, f"调用豆包大模型失败: {e}"

        if "```java" in llm_output:
            fixed_code = llm_output.split("```java")[1].split("```")[0].strip()
        else:
            fixed_code = llm_output.strip()

        if not fixed_code:
            return False, "大模型未返回有效的修复代码。"

        try:
            with open(os.path.join(self.project_root, file_path), 'w', encoding='utf-8') as f:
                f.write(fixed_code)
        except Exception as e:
            return False, f"写入修复代码失败: {e}"

        return True, f"已通过豆包 2.0-Pro 分析并修复。模型反馈：{llm_output[:100]}..."


    def process_exception(self, file_path, error_message, stack_trace):

        print(f"[ERROR] 接收到异常: {error_message}")

        try:

            print("[INFO] 正在调用豆包 2.0-Pro 分析代码...")
            success, fix_desc = self.analyze_and_fix_with_llm(file_path, error_message, stack_trace)

            if not success:
                print(f"[FAIL] 修复失败: {fix_desc}")
                self.send_feishu_card(
                    title=f"[FAIL] 自动修复失败: {os.path.basename(file_path)}",
                    summary=fix_desc,
                    commit_hash="N/A",
                    fix_log="大模型修复环节出错"
                )
                return


            print("[INFO] 正在提交代码...")
            error_type = error_message.split(":")[0] if ":" in error_message else "RuntimeException"
            commit_msg = f"[AI-Fix] 修复 {error_type} in {file_path}"
            push_success, push_msg = self.commit_changes(commit_msg)


            latest_commit = self._run_git_command("git rev-parse HEAD")[1]
            if push_success:
                self.send_feishu_card(
                    title=f"[SUCCESS] 自动修复完成: {os.path.basename(file_path)}",
                    summary=f"豆包分析：{fix_desc}",
                    commit_hash=latest_commit[:7],
                    fix_log=push_msg
                )
            else:
                self.send_feishu_card(
                    title=f"[WARNING] 代码已修复但提交失败",
                    summary=fix_desc,
                    commit_hash="N/A",
                    fix_log=push_msg
                )

        except Exception as e:
            print(f" Agent 处理流程异常: {e}")