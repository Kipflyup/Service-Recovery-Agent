import os
import threading

from flask import Flask, request, jsonify

from agent import SimpleDevAgent

app = Flask(__name__)


PROJECT_ROOT = os.getenv("PROJECT_ROOT", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "demo-master")))
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/66be4361-349b-43de-b13f-05ba4675eb50")


agent = SimpleDevAgent(PROJECT_ROOT, FEISHU_WEBHOOK)


@app.route('/api/error', methods=['POST'])
def report_error():
    data = request.json

    file_path = data.get('filePath')
    error_msg = data.get('errorMessage')
    stack_trace = data.get('stackTrace')

    if not file_path or not error_msg:
        return jsonify({"status": "error", "msg": "缺少必要参数"}), 400


    thread = threading.Thread(target=agent.process_exception, args=(file_path, error_msg, stack_trace))
    thread.start()

    return jsonify({"status": "success", "msg": "Agent已接收，正在处理..."})


if __name__ == '__main__':

    if not os.path.exists(PROJECT_ROOT):
        print(f"错误：项目目录不存在: {PROJECT_ROOT}")
    else:
        print(f"Agent服务启动，监听地址: http://127.0.0.1:8000/api/error")
        app.run(host='0.0.0.0', port=8000, debug=True)