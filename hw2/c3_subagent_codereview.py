# 对照真 Claude Code：这一步 = Task / 子 agent（派子任务去探索，只拿回摘要，保持主上下文干净）
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

def read_file(path):                                               # 4. 工具：读文件
    if not os.path.exists(path):
        return f"文件不存在：{path}"
    if os.path.isdir(path):
        files = os.listdir(path)
        return f"'{path}' 是目录，不是文件。请指定其中一个文件，例如：{files[:10]}"
    try:
        return open(path, encoding="utf-8").read()
    except Exception as e:
        return f"读取失败：{e}"

def bash(cmd):                                                     # 5. 工具：执行命令
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30).stdout
    except Exception as e:
        return f"命令执行失败：{e}"

def subagent(task):                                                # 6. 工具：派一个"代码审查员"子智能体去查 bug
    """子 agent 专门读 demo_project 代码，找出可能的 bug 并返回审查报告。"""
    sub_tools = {"read_file": read_file, "bash": bash}             #    子 agent 也能读文件、执行命令
    SUB_SYSTEM = """你是代码审查员。你的唯一任务是读取 demo_project 目录下的代码，找出可能的 bug、边界情况、异常处理缺失、逻辑错误等问题，并给出简洁的审查报告。
可用工具（每次只回一个 JSON）：
- 读文件：{"tool": "read_file", "args": {"path": "demo_project/..."}}
- 执行命令：{"tool": "bash", "args": {"cmd": "..."}}
- 完成报告：{"done": "审查结论：..."}
注意：
1. 先用 bash 的 ls/find 浏览目录结构，再针对性读取关键文件。
2. 重点关注 stats、find、delete、add 等命令的实现。
3. 报告里列出你找到的问题、所在的文件/行号（如果有），以及修复建议。
4. 完成审查后直接返回 {"done": "..."}，不要多余聊天。"""
    sub = [
        {"role": "system", "content": SUB_SYSTEM},                 #    关键：子 agent 有专属角色提示
        {"role": "user", "content": task}
    ]
    while True:                                                     # 7. 子 agent 自己的内层循环
        r = chat(sub)
        sub.append({"role": "assistant", "content": r})
        try:
            a = parse(r)                                           # 子 agent 也加固：坏格式就重发
        except Exception:
            sub.append({"role": "user", "content": "请只回合法 JSON"}); continue
        if "done" in a: return a["done"]                           # 8. 子 agent 只把最终审查报告返回给主 agent（摘要）
        # 校验工具调用，防止 tool/args 缺失导致崩溃
        if "tool" not in a or "args" not in a:
            sub.append({"role": "user", "content": "JSON 格式不对，需要包含 tool 和 args，或 done。请重发。"}); continue
        name, args = a["tool"], a["args"]
        if name not in sub_tools:
            sub.append({"role": "user", "content": f"没有 {name} 这个工具，可用：read_file、bash。请重新选择。"}); continue
        print(f"  [子 agent 调用] {name}({args})")
        out = sub_tools[name](**args)
        sub.append({"role": "user", "content": f"工具返回：\n{out}"})

TOOLS = {"read_file": read_file, "bash": bash, "subagent": subagent}  # 9. 工具箱

def parse(s):                                                      # 容错解析：剥掉模型偶尔加的 ```json 包裹
    s = s.strip().strip("`").removeprefix("json").strip(); return json.loads(s[s.find("{"): s.rfind("}") + 1])

SYSTEM = """你是主编程助手。每次只回复一个 JSON，不要别的文字，不要 markdown；字符串值里别用英文双引号，要引用就用「」：
- 读文件：{"tool": "read_file", "args": {"path": "..."}}
- 执行命令：{"tool": "bash", "args": {"cmd": "..."}}
- 派代码审查员：{"tool": "subagent", "args": {"task": "一句话描述要审查的问题或目标代码"}}
- 完成：{"done": "总结"}
遇到用户报告 bug、抛出异常、或者需要审查代码质量时，交给 subagent（代码审查员）去读 demo_project 的代码，只拿回审查报告，保持自己上下文干净。"""  # 10. 何时用子 agent

messages = [{"role": "system", "content": SYSTEM}]                 # 11. 主 agent 的历史
while True:                                                         # 12. 外层循环
    messages.append({"role": "user", "content": input("\n你：")})
    while True:                                                     # 13. 内层循环
        reply = chat(messages)
        messages.append({"role": "assistant", "content": reply})
        try:                                 # 14. 解析
            action = parse(reply)
        except Exception:                                  # 坏格式不崩：请模型重发（加固）
            messages.append({"role": "user", "content": "上一条不是合法 JSON，请只回一个 JSON，别的都不要"}); continue
        if "done" in action:
            print(f"[完成] {action['done']}"); break
        name, args = action["tool"], action["args"]
        print(f"[调用] {name}({args})")
        result = TOOLS[name](**args)                               # 15. 派发（subagent 也是一个工具，只是它内部又跑了一个循环）
        print(f"[结果] {result}")
        messages.append({"role": "user", "content": f"工具返回：\n{result}"})
# MIT License | 郑先隽，北师大心理学部教授，人本AI设计与创新
