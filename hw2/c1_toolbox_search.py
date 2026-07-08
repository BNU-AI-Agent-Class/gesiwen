# 对照真 Claude Code：这一步 = Read / Write / Bash / Search 等专用工具（比全用 bash 更可控、可审计）
from dotenv import load_dotenv, find_dotenv; load_dotenv(find_dotenv())  # 1. 自动查找 .env 里的 API 密钥
import os, json, subprocess                                         # 2. os / json / subprocess

# 3. 根据可用的密钥选择模型后端（OpenRouter 或 DeepSeek 二选一）
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")
DEEPSEEK_KEY   = os.getenv("DEEPSEEK_API_KEY")

if OPENROUTER_KEY:
    from openrouter import OpenRouter
    client = OpenRouter(api_key=OPENROUTER_KEY)
    MODEL = os.getenv("OPENROUTER_MODEL", "anthropic/claude-opus-4")
    def chat(messages):
        return client.chat.send(model=MODEL, messages=messages).choices[0].message.content
elif DEEPSEEK_KEY:
    from openai import OpenAI
    client = OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com")
    MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    def chat(messages):
        return client.chat.completions.create(model=MODEL, messages=messages).choices[0].message.content
else:
    raise RuntimeError(
        "找不到 API 密钥。请把 .env.example 复制为 .env，并填入 OPENROUTER_API_KEY 或 DEEPSEEK_API_KEY。"
    )

def read_file(path):                                                 # 4. 工具：读文件（真 Claude Code 的 Read）
    if not os.path.exists(path):
        return f"文件不存在：{path}"
    if os.path.isdir(path):
        files = os.listdir(path)
        return f"'{path}' 是目录，不是文件。请指定其中一个文件，例如：{files[:10]}"
    try:
        return open(path, encoding="utf-8").read()
    except Exception as e:
        return f"读取失败：{e}"

def write_file(path, text):                                        # 5. 工具：写文件（真 Claude Code 的 Write）
    try:
        open(path, "w", encoding="utf-8").write(text)
        return f"已写入 {path}"
    except Exception as e:
        return f"写入失败：{e}"

def bash(cmd):                                                     # 6. 工具：执行命令（真 Claude Code 的 Bash）
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30).stdout
    except Exception as e:
        return f"命令执行失败：{e}"

def search(keyword, root="."):                                     # 7. 新增工具：在项目里按关键词搜文件内容
    """按关键词搜索项目文件内容，返回命中文件名+行号+内容摘要。"""
    if not keyword:
        return "请提供 keyword 参数。"
    hits = []
    skipped_dirs = {".git", ".claude", "__pycache__", "node_modules", ".venv", "venv"}
    for dirpath, dirnames, filenames in os.walk(root):
        # 跳过隐藏目录和常见依赖/缓存目录
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in skipped_dirs]
        for filename in filenames:
            if filename.startswith("."):
                continue
            path = os.path.join(dirpath, filename)
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, start=1):
                        if keyword in line:
                            rel = os.path.relpath(path, root)
                            hits.append(f"{rel}:{i}: {line.strip()}")
                            if len(hits) >= 50:          # 限制返回条数，防止刷屏
                                break
            except Exception:
                continue
            if len(hits) >= 50:
                break
    if not hits:
        return f"未找到包含 '{keyword}' 的文件。"
    return "\n".join(hits)

TOOLS = {"read_file": read_file, "write_file": write_file, "bash": bash, "search": search}  # 8. 工具箱：名字 → 函数
def parse(s):                                                      # 8b. 容错解析：模型偶尔会用 ```json 包裹（这就是 c0 有时崩的原因），剥掉再解析
    s = s.strip().strip("`").removeprefix("json").strip(); return json.loads(s[s.find("{"): s.rfind("}") + 1])

SYSTEM = """你是一个编程助手。每次只回复一个 JSON，不要别的文字，不要 markdown 包裹；字符串值里别用英文双引号，要引用就用「」：
- 读文件：{"tool": "read_file", "args": {"path": "文件路径"}}
- 写文件：{"tool": "write_file", "args": {"path": "文件路径", "text": "..."}}
- 执行命令：{"tool": "bash", "args": {"cmd": "真正的 shell 命令"}}
- 搜索内容：{"tool": "search", "args": {"keyword": "关键词", "root": "项目根目录（可选，默认当前目录）"}}
- 完成时：{"done": "给用户的总结"}
重要规则：
1. read_file 只能读文件，不能读目录；如果需要浏览目录内容，用 bash 的 ls 命令。
2. bash 只用来执行真正的系统命令，不要用来 echo 聊天内容。
3. 如果你想按关键词找文件里的内容，优先用 search 工具，它比 bash + grep 更可控。
4. 如果只是问候、确认、没有具体任务，直接返回 {"done": "..."}，不要用工具。
优先用 read_file / write_file / search 处理文件，它们比 bash 更安全可控。"""  # 9. 系统提示：告诉模型有哪些工具、何时用

messages = [{"role": "system", "content": SYSTEM}]             # 10. 对话历史
while True:                                                         # 11. 外层循环：等新任务
    messages.append({"role": "user", "content": input("\n你：")})
    while True:                                                     # 12. 内层循环：自主执行
        reply = chat(messages)                                      # 13. 调用已选好的后端
        messages.append({"role": "assistant", "content": reply})
        try:                              # 14. 解析 JSON
            action = parse(reply)
        except Exception:                              # 坏格式不崩：请模型重发（这就是加固）
            messages.append({"role": "user", "content": "上一条不是合法 JSON，请只回一个 JSON，别的都不要"}); continue
        if "done" in action:                                    # 15. 完成 → 跳出
            print(f"[完成] {action['done']}"); break
        name, args = action["tool"], action["args"]            # 16. 取出工具名和参数
        print(f"[调用] {name}({args})")
        result = TOOLS[name](**args)                            # 17. 关键升级：按名字派发到对应工具函数
        print(f"[结果] {result}")
        messages.append({"role": "user", "content": f"工具返回：\n{result}"})  # 18. 结果反馈
# MIT License | 郑先隽，北师大心理学部教授，人本AI设计与创新
