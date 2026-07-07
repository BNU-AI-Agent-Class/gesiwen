from dotenv import load_dotenv
from openai import OpenAI
import os

load_dotenv()  # 读取 .env 文件

api_key = os.getenv("DEEPSEEK_API_KEY")
model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

user_input = input("你：").strip()

response = client.chat.completions.create(
    model=model,
    messages=[
        {"role": "system", "content": "你是一个友善、乐于助人的 AI 助手。"},
        {"role": "user", "content": user_input}
    ]
)

print(f"AI：{response.choices[0].message.content}")
