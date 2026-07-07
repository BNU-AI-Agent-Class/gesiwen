from dotenv import load_dotenv; load_dotenv()                      # 1. 读取 .env 文件中的 API 密钥
from openai import OpenAI                                          # 2. 导入 OpenAI 兼容客户端（DeepSeek API 为 OpenAI 兼容格式）
import os                                                          # 3. 用于读取环境变量
import sys                                                         #    用于异常时退出
import datetime                                                    #    用于记忆时间戳
import json                                                        #    用于持久化对话历史

# 可选：联网搜索依赖（DuckDuckGo），未安装时给出友好提示
try:
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException, TimeoutException, RatelimitException
except ImportError:
    DDGS = None
    DDGSException = TimeoutException = RatelimitException = Exception

# ------------------------------------------------------------------
# 配置与初始化
# ------------------------------------------------------------------
api_key = os.getenv("DEEPSEEK_API_KEY")
if not api_key:
    print("[错误] 找不到 DEEPSEEK_API_KEY。请在 .env 文件中设置：")
    print("       DEEPSEEK_API_KEY=sk-xxxxxxxxxxxx")
    sys.exit(1)

model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")                # 默认使用 DeepSeek 模型，可在 .env 中覆盖
memory_file = "agent.md"                                            # 外置提示词与记忆文件
history_file = "history.json"                                       # 可选：对话历史快照

# 联网搜索相关配置（均可在 .env 中覆盖）
search_max_results = int(os.getenv("SEARCH_MAX_RESULTS", "5"))
search_timeout = int(os.getenv("SEARCH_TIMEOUT", "10"))
max_tool_rounds = int(os.getenv("MAX_TOOL_ROUNDS", "3"))

class AppState:
    """运行时状态容器，避免在全局作用域直接重新赋值配置常量。"""
    def __init__(self):
        self.auto_search_enabled = os.getenv("ENABLE_AUTO_SEARCH", "true").lower() in ("1", "true", "yes")

state = AppState()

client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")  # 4. 创建 DeepSeek 客户端

# ------------------------------------------------------------------
# 工具定义（用于模型自动调用）
# ------------------------------------------------------------------
tools = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the public web for current, factual, or external information. "
                "Use this when the user asks about recent events, specific data, or anything "
                "that is likely to change over time or that you are uncertain about."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "A concise web search query, preferably in the user's language."
                    }
                },
                "required": ["query"]
            }
        }
    }
]

# ------------------------------------------------------------------
# 外置 prompt / 记忆管理
# ------------------------------------------------------------------
def ensure_agent_md():
    """如果 agent.md 不存在，则在聊天开始时新建一份初始文档。"""
    if not os.path.exists(memory_file):
        with open(memory_file, "w", encoding="utf-8") as f:
            f.write("# Agent 角色设定\n\n")
            f.write("你是一个友善、乐于助人且具备联网能力的 AI 助手。\n")
            f.write("请用自然语言与用户对话，回答他们的问题。\n")
            f.write("当需要最新信息或你对事实不确定时，可以调用 web_search 工具联网查询。\n\n")
            f.write("# 记忆区\n\n")
            f.write("- 初始记忆：用户正在使用基于外置 Markdown 提示词的对话机器人。\n")
        print(f"[系统] 已新建外置提示词文件：{memory_file}\n")

def load_agent_memory():
    """采用 open('agent.md').read() 读取记忆内容，返回给系统消息使用。"""
    with open(memory_file, "r", encoding="utf-8") as f:
        return f.read()

def append_memory(note: str):
    """将重要信息追加到 agent.md 的记忆区。"""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(memory_file, "a", encoding="utf-8") as f:
        f.write(f"\n- [{timestamp}] {note}\n")
    print(f"[记忆] 已写入 agent.md：{note}")

def save_history(messages):
    """将当前对话历史保存为 JSON 快照，便于下次恢复。"""
    with open(history_file, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)

def load_history():
    """如果存在历史快照，则恢复对话上下文。"""
    if os.path.exists(history_file):
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def show_help():
    """显示扩展命令帮助。"""
    print("""
[命令帮助]
  /help          显示本帮助
  /reload        重新读取 agent.md 的提示词与记忆
  /memory        查看当前 agent.md 中的记忆摘要
  /remember XXX  将 "XXX" 追加到 agent.md 记忆区
  /search QUERY  手动联网搜索并总结回答
  /search-on     开启模型自动调用 web_search
  /search-off    关闭模型自动调用 web_search
  /save          手动保存当前对话历史到 history.json
  /clear         清空当前会话的上下文（不会删除 agent.md）
  exit / quit / 退出  结束对话
""")

# ------------------------------------------------------------------
# 联网搜索功能
# ------------------------------------------------------------------
def web_search(query: str, max_results: int = None):
    """执行 DuckDuckGo 搜索并返回结果列表。"""
    if DDGS is None:
        raise RuntimeError("缺少联网搜索依赖。请运行：pip install ddgs")
    if not query or not query.strip():
        raise ValueError("搜索查询不能为空")

    max_results = max_results or search_max_results
    try:
        with DDGS(timeout=search_timeout) as ddgs:
            results = list(ddgs.text(query.strip(), max_results=max_results))
        return results
    except TimeoutException:
        raise RuntimeError("搜索请求超时，请检查网络后重试。")
    except RatelimitException:
        raise RuntimeError("搜索服务触发速率限制，请稍后再试。")
    except DDGSException as e:
        raise RuntimeError(f"搜索服务错误：{e}")

def format_search_results(results, query: str) -> str:
    """将搜索结果格式化为模型可读的文本块。"""
    if not results:
        return f"未找到与「{query}」相关的网络结果。"

    lines = [f"搜索「{query}」返回 {len(results)} 条结果："]
    for i, r in enumerate(results, 1):
        title = r.get("title", "无标题")
        href = r.get("href", "")
        body = r.get("body", "").replace("\n", " ").strip()
        if len(body) > 300:
            body = body[:300] + "..."
        lines.append(f"[{i}] {title}\n链接：{href}\n摘要：{body}")
    return "\n\n".join(lines)

def execute_tool_calls(tool_calls):
    """执行模型返回的工具调用，并返回 role=tool 的消息列表。"""
    tool_outputs = []
    for tc in tool_calls:
        fn_name = tc.function.name
        try:
            args = json.loads(tc.function.arguments)
        except json.JSONDecodeError as e:
            output = f"参数解析错误：{e}"
        else:
            if fn_name == "web_search":
                query = args.get("query", "")
                try:
                    results = web_search(query)
                    output = format_search_results(results, query)
                except Exception as e:
                    output = f"搜索失败：{e}"
            else:
                output = f"未知工具：{fn_name}"

        tool_outputs.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "content": output,
        })
    return tool_outputs

# ------------------------------------------------------------------
# 主程序
# ------------------------------------------------------------------
ensure_agent_md()                                                   # 聊天时新建 agent.md（若不存在）
agent_prompt = load_agent_memory()                                  # 用 open().read() 读取记忆内容

messages = [
    {"role": "system", "content": agent_prompt}                     # 5. 系统提示词来自外置 md 文件，不写死在代码里
]

# 尝试恢复上次的对话历史
past_history = load_history()
if past_history:
    # 始终使用当前 agent.md 作为 system 消息
    if past_history and past_history[0].get("role") == "system":
        past_history[0] = messages[0]
    else:
        past_history.insert(0, messages[0])
    messages = past_history
    print(f"[系统] 已从 {history_file} 恢复 {len(messages) - 1} 条历史消息。\n")

print("[系统] 你好！我是基于外置 Markdown 提示词且具备联网能力的对话助手。")
print("[系统] 当前提示词来源：agent.md")
print("[系统] 输入 /help 查看扩展命令，输入 exit / quit / 退出 结束对话。\n")

while True:                                                         # 6. 对话循环
    user_input = input("你：").strip()                              # 7. 等待用户输入

    # 退出指令
    if user_input.lower() in {"exit", "quit", "退出"}:              #    支持退出
        print("\n[系统] 再见！")
        save_history(messages)
        break

    if not user_input:
        continue

    # 扩展命令处理
    if user_input.startswith("/"):
        cmd = user_input.split(None, 1)
        op = cmd[0].lower()
        arg = cmd[1] if len(cmd) > 1 else ""

        if op == "/help":
            show_help()
        elif op == "/reload":
            messages[0]["content"] = load_agent_memory()
            print("[系统] 已重新加载 agent.md 的提示词与记忆。\n")
        elif op == "/memory":
            print("\n[当前记忆摘要]")
            print(load_agent_memory())
            print()
        elif op == "/remember":
            if arg:
                append_memory(arg)
                # 重新加载，使新记忆立即生效
                messages[0]["content"] = load_agent_memory()
            else:
                print("[提示] 用法：/remember 需要记住的内容\n")
        elif op == "/search":
            if not arg:
                print("[提示] 用法：/search 你的搜索问题\n")
            else:
                try:
                    results = web_search(arg)
                    context = format_search_results(results, arg)
                except Exception as e:
                    print(f"[搜索失败] {e}\n")
                    continue

                search_messages = [
                    {"role": "system", "content": load_agent_memory()},
                    {
                        "role": "user",
                        "content": (
                            f"请根据下面的搜索结果回答用户问题。\n\n"
                            f"用户问题：{arg}\n\n"
                            f"{context}\n\n"
                            f"请用中文回答，并引用来源编号。"
                        )
                    }
                ]

                try:
                    r = client.chat.completions.create(model=model, messages=search_messages)
                    reply = r.choices[0].message.content
                except Exception as e:
                    print(f"[API 错误] {e}\n")
                    continue

                if not reply:
                    print("[错误] AI 返回空内容\n")
                    continue

                print(f"AI：{reply}\n")
                messages.append({"role": "user", "content": f"/search {arg}"})
                messages.append({"role": "assistant", "content": reply})
                save_history(messages)
        elif op == "/search-on":
            state.auto_search_enabled = True
            print("[系统] 已开启模型自动联网搜索。\n")
        elif op == "/search-off":
            state.auto_search_enabled = False
            print("[系统] 已关闭模型自动联网搜索。\n")
        elif op == "/save":
            save_history(messages)
            print("[系统] 对话历史已保存到 history.json\n")
        elif op == "/clear":
            messages = [{"role": "system", "content": load_agent_memory()}]
            print("[系统] 当前会话上下文已清空，agent.md 记忆仍保留。\n")
        else:
            print(f"[提示] 未知命令 {op}，输入 /help 查看可用命令。\n")
        continue

    messages.append({"role": "user", "content": user_input})        # 8. 存入对话历史

    try:
        final_reply = None

        for _ in range(max_tool_rounds):
            api_kwargs = {"model": model, "messages": messages}
            if state.auto_search_enabled:
                api_kwargs["tools"] = tools
                api_kwargs["tool_choice"] = "auto"

            response = client.chat.completions.create(**api_kwargs)
            msg = response.choices[0].message

            # 若模型请求调用工具，执行工具并继续下一轮
            if getattr(msg, "tool_calls", None):
                tool_calls_data = [
                    {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]

                messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": tool_calls_data,
                })

                for out in execute_tool_calls(msg.tool_calls):
                    messages.append(out)

                continue

            # 否则已得到最终回答
            final_reply = msg.content
            break

        if final_reply is None:
            final_reply = "[系统] 工具调用轮数达到上限，未能生成最终回答。"

    except Exception as e:
        print(f"\n[API/搜索错误] {e}")
        continue

    if not final_reply:
        print("\n[错误] AI 返回空内容")
        continue

    messages.append({"role": "assistant", "content": final_reply})  # 11. 存入历史
    print(f"AI：{final_reply}\n")                                       # 12. 打印 AI 回复

    # 每次回复后自动保存历史，避免意外丢失
    save_history(messages)

# MIT License | 郑先隽，北师大心理学部教授，人本AI设计与创新
