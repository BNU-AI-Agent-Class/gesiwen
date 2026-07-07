from dotenv import load_dotenv
from openai import OpenAI
import os

load_dotenv()  # 读取 .env 文件

api_key = os.getenv("DEEPSEEK_API_KEY")
model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

messages = [
    {"role": "system", "content": "你是一个友善、乐于助人的 AI 助手。"}
]

print("[系统] 你好！我是多轮对话助手，输入 exit / quit / 退出 即可结束对话。\n")

while True:
    user_input = input("user：").strip()
    if user_input.lower() in {"exit", "quit", "退出"}:
        print("\n[系统] 再见！")
        break
    if not user_input:
        continue

    messages.append({"role": "user", "content": user_input})

    response = client.chat.completions.create(
        model=model,
        messages=messages
    )

    reply = response.choices[0].message.content
    messages.append({"role": "assistant", "content": reply})
    print(f"assistant：{reply}\n")
