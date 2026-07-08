# 对照真 Claude Code：这一步 = /compact 自动压缩（长会话把历史折叠成摘要，重开窗口）
from dotenv import load_dotenv, find_dotenv; load_dotenv(find_dotenv())  # 1. 自动查找 .env 里的 API 密钥
import os, json, subprocess                                          # 2. os / json / subprocess

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

LIMIT = 12                                                          # 4. 阈值：历史超过这么多条就压缩（真 CC 看 token，这里简化成条数）
MAX_RETRIES = 3                                                     # 4b. 连续解析/调用失败上限，防止无限重试

def bash(cmd):                                                      # 5. 工具：执行命令
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30).stdout
    except Exception as e:
        return f"命令执行失败：{e}"

def read_file(path):                                                # 5b. 工具：读文件
    if not os.path.exists(path):
        return f"文件不存在：{path}"
    if os.path.isdir(path):
        files = os.listdir(path)
        return f"'{path}' 是目录，不是文件。请指定其中一个文件，例如：{files[:10]}"
    try:
        return open(path, encoding="utf-8").read()
    except Exception as e:
        return f"读取失败：{e}"

def write_file(path, text):                                         # 5c. 工具：写文件
    try:
        open(path, "w", encoding="utf-8").write(text)
        return f"已写入 {path}"
    except Exception as e:
        return f"写入失败：{e}"

TOOLS = {"bash": bash, "read_file": read_file, "write_file": write_file}

def compact(messages):                                             # 6. 关键升级：把长历史"压缩"成一条摘要
    system = messages[0]                                            #    保留系统提示
    body = "\n".join(f'{m["role"]}: {m["content"]}' for m in messages[1:])
    summary = chat([{"role": "user", "content": f"用要点总结这段对话的进展和关键结论，供接力：\n{body}"}])
    print("[压缩] 历史已折叠成一条摘要，窗口重开")
    return [system, {"role": "user", "content": f"【之前进展摘要】\n{summary}"}]  # 7. 新历史 = 系统提示 + 摘要

def parse(s):                                                      # 容错解析：剥掉模型偶尔加的 ```json 包裹
    s = s.strip().strip("`").removeprefix("json").strip(); return json.loads(s[s.find("{"): s.rfind("}") + 1])

SYSTEM = """你是编程助手。每次只回复一个 JSON，不要别的文字，不要 markdown；字符串值里别用英文双引号，要引用就用「」：
- 读文件：{"tool": "read_file", "args": {"path": "文件路径"}}
- 写文件：{"tool": "write_file", "args": {"path": "文件路径", "text": "..."}}
- 执行命令：{"tool": "bash", "args": {"cmd": "真正的 shell 命令"}}
- 完成时：{"done": "总结"}
重要规则：
1. 写文件必须使用 write_file 工具，禁止用 bash 的 echo/cat 等命令写文件。
2. 如果 bash 命令里必须包含字符串，优先用单引号包裹，避免双引号转义问题。"""  # 8. 系统提示

messages = [{"role": "system", "content": SYSTEM}]                 # 9. 历史
while True:                                                         # 10. 外层循环
    messages.append({"role": "user", "content": input("\n你：")})
    failures = 0                                                    # 11. 新一轮用户任务，失败计数清零
    while True:                                                     # 12. 内层循环
        if len(messages) > LIMIT:                                  # 13. 每轮先检查：太长了就压缩
            messages = compact(messages)
        reply = chat(messages)
        messages.append({"role": "assistant", "content": reply})
        try:                                                        # 14. 解析
            action = parse(reply)
        except Exception:                                           # 坏格式不崩：请模型重发（这就是加固）
            failures += 1
            if failures >= MAX_RETRIES:
                print(f"[停止] 连续 {MAX_RETRIES} 次无法解析 JSON，请重新输入。"); break
            messages.append({"role": "user", "content": "上一条不是合法 JSON，请只回一个 JSON，别的都不要"}); continue
        if "done" in action:
            print(f"[完成] {action['done']}"); break
        # 15. 校验工具名，防止 AI 调用不存在的工具（如 fly）导致 KeyError
        if "tool" not in action or action["tool"] not in TOOLS:
            failures += 1
            available = ", ".join(TOOLS.keys())
            if failures >= MAX_RETRIES:
                print(f"[停止] 连续 {MAX_RETRIES} 次调用不存在的工具，请重新输入。可用的工具：{available}"); break
            messages.append({"role": "user", "content": f"没有这个工具，可用的有：{available}。请重新选择。"}); continue
        name, args = action["tool"], action["args"]
        # 16. 按不同工具校验必要参数
        required = {
            "bash": {"cmd"},
            "read_file": {"path"},
            "write_file": {"path", "text"},
        }.get(name)
        if required is None or not required.issubset(args):
            failures += 1
            hint = {
                "bash": "bash 需要 args.cmd",
                "read_file": "read_file 需要 args.path",
                "write_file": "write_file 需要 args.path 和 args.text",
            }.get(name, "参数格式错误")
            if failures >= MAX_RETRIES:
                print(f"[停止] 连续 {MAX_RETRIES} 次参数格式错误，请重新输入。"); break
            messages.append({"role": "user", "content": f"{hint}。请重新发送合法 JSON。"}); continue
        # 17. 派发执行
        result = TOOLS[name](**args)
        print(f"[调用] {name}({args})")
        if name == "bash":
            print(f"[执行] {action['args']['cmd']}\n[结果] {result}")
        else:
            print(f"[结果] {result}")
        messages.append({"role": "user", "content": f"输出：\n{result}"})
        failures = 0                                                # 成功执行后重置失败计数
# MIT License | 郑先隽，北师大心理学部教授，人本AI设计与创新
