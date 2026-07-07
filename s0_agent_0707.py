from dotenv import load_dotenv
from openai import OpenAI
import os

load_dotenv()  # 读取 .env 文件

api_key = os.getenv("DEEPSEEK_API_KEY")
model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

response = client.chat.completions.create(
    model=model,
    messages=[
        {"role": "system", "content": "你是一个友善、乐于助人的 AI 助手。"},
        {"role": "user", "content": "你好，请介绍一下你自己。"}
    ]
)

print(response.choices[0].message.content)
