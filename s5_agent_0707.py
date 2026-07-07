from dotenv import load_dotenv; load_dotenv()                      # 1. 读取 .env 文件中的 API 密钥
from openai import OpenAI                                          # 2. 导入 OpenAI 兼容客户端（DeepSeek API 为 OpenAI 兼容格式）
import os                                                          # 3. 用于读取环境变量
import sys                                                         #    用于异常时退出
import datetime                                                    #    用于记忆时间戳
import json                                                        #    用于持久化对话历史与状态

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
history_file = "s5/history.json"                                    # s5 专用对话历史快照
skills_dir = "s5"                                                   # 技能包目录（与 s5_agent_0707.py 同级）
state_file = "state.json"                                           # 运行时状态快照

# 联网搜索相关配置（均可在 .env 中覆盖）
search_max_results = int(os.getenv("SEARCH_MAX_RESULTS", "5"))
search_timeout = int(os.getenv("SEARCH_TIMEOUT", "10"))
max_tool_rounds = int(os.getenv("MAX_TOOL_ROUNDS", "3"))
default_skill = os.getenv("DEFAULT_SKILL", "").strip() or None    # 默认技能名（不含 .md）

class AppState:
    """运行时状态容器，避免在全局作用域直接重新赋值配置常量。"""
    def __init__(self):
        self.auto_search_enabled = os.getenv("ENABLE_AUTO_SEARCH", "true").lower() in ("1", "true", "yes")
        self.selected_skill = default_skill

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

def save_state():
    """保存运行时状态（当前技能、搜索开关等）。"""
    payload = {
        "selected_skill": state.selected_skill,
        "auto_search_enabled": state.auto_search_enabled,
    }
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

def load_state():
    """读取运行时状态。"""
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def show_help():
    """显示扩展命令帮助。"""
    print("""
[命令帮助]
  /help                 显示本帮助
  /reload               重新读取 agent.md 与当前技能的提示词
  /memory               查看当前 agent.md 中的记忆摘要
  /remember XXX         将 "XXX" 追加到 agent.md 记忆区
  /search QUERY         手动联网搜索并总结回答
  /search-on            开启模型自动调用 web_search
  /search-off           关闭模型自动调用 web_search
  /skill list           列出所有技能包
  /skill current        查看当前使用的技能包
  /skill show NAME      查看指定技能包内容
  /skill use NAME       切换到指定技能包（保留当前对话历史）
  /skill create NAME [--template]  创建新技能包（交互式输入或模板）
  /skill generate NAME [描述]      让 AI 生成技能包草稿
  /skill delete NAME    删除指定技能包
  /save                 手动保存当前对话历史到 history.json
  /clear                清空当前会话的上下文（不会删除 agent.md 与技能）
  exit / quit / 退出    结束对话
""")

# ------------------------------------------------------------------
# 技能包管理
# ------------------------------------------------------------------
def ensure_skills_dir():
    """确保技能包目录存在。"""
    os.makedirs(skills_dir, exist_ok=True)

def normalize_skill_name(name: str) -> str:
    """统一技能名，去掉可能的 skill_ 前缀。"""
    name = name.strip()
    if name.startswith("skill_"):
        name = name[6:]
    return name

def skill_filename(name: str) -> str:
    """根据技能名生成实际文件名（自动补 skill_ 前缀）。"""
    name = normalize_skill_name(name)
    return f"skill_{name}.md"

def skill_path(name: str) -> str:
    """返回技能文件的完整路径（带 skill_ 前缀）。"""
    return os.path.join(skills_dir, skill_filename(name))

def raw_skill_path(name: str) -> str:
    """返回不带 skill_ 前缀的技能文件路径（兼容手动创建的文件）。"""
    return os.path.join(skills_dir, f"{normalize_skill_name(name)}.md")

def is_valid_skill_name(name: str) -> bool:
    """校验技能名是否合法（防止路径穿越与特殊字符）。"""
    name = normalize_skill_name(name)
    if not name or name.startswith("."):
        return False
    if any(c in name for c in r'\/:*?"<>|'):
        return False
    if " " in name or "\t" in name or "\n" in name:
        return False
    if ".." in name:
        return False
    return True

def skill_exists(name: str) -> bool:
    """检查技能文件是否存在（支持 skill_ 前缀或直接命名）。"""
    return os.path.exists(skill_path(name)) or os.path.exists(raw_skill_path(name))

def load_skill(name: str) -> str:
    """读取指定技能文件内容。"""
    path = skill_path(name)
    if not os.path.exists(path):
        path = raw_skill_path(name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def save_skill(name: str, content: str):
    """保存技能文件（统一使用 skill_ 前缀命名）。"""
    ensure_skills_dir()
    with open(skill_path(name), "w", encoding="utf-8") as f:
        f.write(content)

def list_skills() -> list:
    """列出所有技能包名称（去掉 skill_ 前缀）。"""
    ensure_skills_dir()
    names = set()
    for f in os.listdir(skills_dir):
        if f.endswith(".md") and not f.startswith("."):
            name = os.path.splitext(f)[0]
            if name.startswith("skill_"):
                name = name[6:]
            names.add(name)
    return sorted(names)

def compose_system_prompt(agent_md: str, skill_name: str) -> str:
    """将 agent.md 与当前技能拼接成最终系统提示词。"""
    skill_name = normalize_skill_name(skill_name) if skill_name else skill_name
    if not skill_name:
        return agent_md
    try:
        skill_md = load_skill(skill_name)
    except FileNotFoundError:
        return agent_md
    skill_md = skill_md.strip()
    if not skill_md:
        return agent_md
    return f"{agent_md}\n\n---\n\n# 当前技能：{skill_name}\n\n{skill_md}"

def set_skill(name: str):
    """切换当前技能，并刷新系统提示词与状态。"""
    name = normalize_skill_name(name)
    if not is_valid_skill_name(name):
        raise ValueError(f"技能名不合法：{name}")
    if not skill_exists(name):
        raise FileNotFoundError(f"找不到技能文件：{skill_path(name)}")
    skill_md = load_skill(name).strip()
    if not skill_md:
        print(f"[警告] 技能 {name} 文件为空，切换后不会附加额外提示词。")
    state.selected_skill = name
    messages[0]["content"] = compose_system_prompt(load_agent_memory(), name)
    save_state()

def create_skill_interactive(name: str):
    """交互式创建技能包。"""
    name = normalize_skill_name(name)
    if not is_valid_skill_name(name):
        print(f"[错误] 技能名不合法：{name}\n")
        return
    if skill_exists(name):
        confirm = input(f"[提示] 技能 {name} 已存在，是否覆盖？(y/n): ").strip().lower()
        if confirm not in ("y", "yes", "是"):
            print("[系统] 已取消。\n")
            return
    print(f"[系统] 正在创建技能 {name}。请逐行输入 Markdown 内容，输入 /done 结束，/cancel 取消。")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        stripped = line.strip()
        if stripped == "/done":
            break
        if stripped == "/cancel":
            print("[系统] 已取消创建。\n")
            return
        lines.append(line)
    body = "\n".join(lines).strip()
    if not body:
        print("[提示] 内容为空，未创建。\n")
        return
    content = f"# Skill: {name}\n\n{body}\n"
    save_skill(name, content)
    print(f"[系统] 技能 {name} 已保存到 {skill_path(name)}。\n")

def create_skill_template(name: str):
    """基于模板创建技能包。"""
    name = normalize_skill_name(name)
    if not is_valid_skill_name(name):
        print(f"[错误] 技能名不合法：{name}\n")
        return
    if skill_exists(name):
        confirm = input(f"[提示] 技能 {name} 已存在，是否覆盖？(y/n): ").strip().lower()
        if confirm not in ("y", "yes", "是"):
            print("[系统] 已取消。\n")
            return
    content = f"""# Skill: {name}

## 角色
你是一名专业的 {name} 领域助手。

## 核心职责
- 请补充职责 1
- 请补充职责 2

## 回答风格
- 请补充风格

## 约束
- 请补充约束

## 示例
请补充示例
"""
    save_skill(name, content)
    print(f"[系统] 已基于模板创建 {name}，请手动编辑 {skill_path(name)} 完善内容。\n")

def generate_skill_content(name: str, description: str = "") -> str:
    """调用 AI 生成技能包内容。"""
    name = normalize_skill_name(name)
    prompt = (
        f"请为 AI 助手编写一份名为「{name}」的专业技能说明文档（Markdown），"
        f"用于拼接在系统提示词中。"
        f"{('技能描述：' + description) if description else ''}"
        "文档应包括：1) 角色定位；2) 核心职责；3) 回答风格与约束；4) 一个简短示例。"
        "只输出 Markdown 正文，不要解释。"
    )
    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}]
    )
    return (r.choices[0].message.content or "").strip()

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
ensure_skills_dir()                                                 # 确保技能包目录存在

# 恢复运行时状态
saved_state = load_state()
state.selected_skill = normalize_skill_name(saved_state.get("selected_skill", default_skill) or "")
if state.selected_skill == "":
    state.selected_skill = None
if saved_state.get("auto_search_enabled") is not None:
    state.auto_search_enabled = saved_state["auto_search_enabled"]

# 若上次选中的技能文件已失效，恢复为无技能模式
if state.selected_skill and not skill_exists(state.selected_skill):
    print(f"[警告] 上次使用的技能 {state.selected_skill} 已失效，恢复为无技能模式。\n")
    state.selected_skill = None

agent_prompt = compose_system_prompt(load_agent_memory(), state.selected_skill)  # 组合系统提示词

messages = [
    {"role": "system", "content": agent_prompt}                     # 5. 系统提示词 = agent.md + skill.md
]

# 尝试恢复上次的对话历史
past_history = load_history()
if past_history:
    # 始终使用当前组合提示词作为 system 消息
    if past_history and past_history[0].get("role") == "system":
        past_history[0] = messages[0]
    else:
        past_history.insert(0, messages[0])
    messages = past_history
    print(f"[系统] 已从 {history_file} 恢复 {len(messages) - 1} 条历史消息。\n")

skill_label = state.selected_skill or "无"
print("[系统] 你好！我是基于外置 Markdown 提示词、具备联网能力与可切换技能包的对话助手。")
print(f"[系统] 当前提示词来源：agent.md + {skill_label}")
print("[系统] 输入 /help 查看扩展命令，输入 exit / quit / 退出 结束对话。\n")

while True:                                                         # 6. 对话循环
    user_input = input("你：").strip()                              # 7. 等待用户输入

    # 退出指令
    if user_input.lower() in {"exit", "quit", "退出"}:              #    支持退出
        print("\n[系统] 再见！")
        save_history(messages)
        save_state()
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
            messages[0]["content"] = compose_system_prompt(load_agent_memory(), state.selected_skill)
            print("[系统] 已重新加载 agent.md 与当前技能的提示词。\n")
        elif op == "/memory":
            print("\n[当前记忆摘要]")
            print(load_agent_memory())
            print()
        elif op == "/remember":
            if arg:
                append_memory(arg)
                # 重新加载，使新记忆立即生效
                messages[0]["content"] = compose_system_prompt(load_agent_memory(), state.selected_skill)
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
                    {"role": "system", "content": compose_system_prompt(load_agent_memory(), state.selected_skill)},
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
            save_state()
            print("[系统] 已开启模型自动联网搜索。\n")
        elif op == "/search-off":
            state.auto_search_enabled = False
            save_state()
            print("[系统] 已关闭模型自动联网搜索。\n")
        elif op == "/skill":
            if not arg:
                print("[提示] 用法：/skill create|use|list|current|show|generate|delete ...\n")
            else:
                sub_parts = arg.split(None, 1)
                sub = sub_parts[0].lower()
                sub_arg = sub_parts[1] if len(sub_parts) > 1 else ""

                if sub == "list":
                    skills = list_skills()
                    current = state.selected_skill
                    if not skills:
                        print("[系统] 暂无技能文件，使用 /skill create <name> 创建。\n")
                    else:
                        print("[技能列表]")
                        for s in skills:
                            marker = "  <- 当前" if s == current else ""
                            print(f"  - {s}{marker}")
                        print()

                elif sub == "current":
                    if state.selected_skill:
                        print(f"[当前技能] {state.selected_skill}（{skill_path(state.selected_skill)}）")
                        try:
                            content = load_skill(state.selected_skill)
                            preview = content.strip().splitlines()[:5]
                            print("内容预览：")
                            for line in preview:
                                print(line)
                        except Exception:
                            pass
                    else:
                        print("[当前技能] 未选择，仅使用 agent.md")
                    print()

                elif sub == "use":
                    name = normalize_skill_name(sub_arg.strip())
                    if not name:
                        print("[提示] 用法：/skill use <name>\n")
                    else:
                        try:
                            set_skill(name)
                            print(f"[系统] 已切换至技能：{name}。当前会话历史已保留。\n")
                        except (FileNotFoundError, ValueError) as e:
                            print(f"[错误] {e}\n")

                elif sub == "create":
                    name = sub_arg.strip()
                    if not name:
                        print("[提示] 用法：/skill create <name> [--template]\n")
                    else:
                        parts = name.split()
                        skill_name = parts[0]
                        use_template = "--template" in parts
                        if use_template:
                            create_skill_template(skill_name)
                        else:
                            create_skill_interactive(skill_name)

                elif sub == "generate":
                    name_desc = sub_arg.strip()
                    if not name_desc:
                        print("[提示] 用法：/skill generate <name> [描述]\n")
                    else:
                        name = normalize_skill_name(name_desc.split(None, 1)[0])
                        description = name_desc[len(name_desc.split(None, 1)[0]):].strip()
                        if not is_valid_skill_name(name):
                            print(f"[错误] 技能名不合法：{name}\n")
                        else:
                            print(f"[系统] 正在为 {name} 生成技能内容，请稍候...")
                            try:
                                content = generate_skill_content(name, description)
                            except Exception as e:
                                print(f"[错误] 生成失败：{e}\n")
                            else:
                                print("--- 生成内容预览 ---")
                                print(content[:800] + ("..." if len(content) > 800 else ""))
                                print("---------------------")
                                confirm = input("是否保存？(y/n): ").strip().lower()
                                if confirm in ("y", "yes", "是"):
                                    save_skill(name, content)
                                    print(f"[系统] 技能 {name} 已保存。\n")
                                else:
                                    print("[系统] 已取消保存。\n")

                elif sub == "show":
                    name = normalize_skill_name(sub_arg.strip())
                    if not name or not skill_exists(name):
                        print(f"[错误] 找不到技能：{name}\n")
                    else:
                        print(f"[技能 {name}]")
                        print(load_skill(name))
                        print()

                elif sub == "delete":
                    name = normalize_skill_name(sub_arg.strip())
                    if not name or not skill_exists(name):
                        print(f"[错误] 找不到技能：{name}\n")
                    else:
                        confirm = input(f"确认删除技能 {name}？(y/n): ").strip().lower()
                        if confirm not in ("y", "yes", "是"):
                            print("[系统] 已取消删除。\n")
                        else:
                            if state.selected_skill == name:
                                state.selected_skill = None
                                messages[0]["content"] = compose_system_prompt(load_agent_memory(), None)
                                save_state()
                            os.remove(skill_path(name))
                            print(f"[系统] 技能 {name} 已删除。\n")

                else:
                    print(f"[提示] 未知子命令 /skill {sub}，输入 /help 查看帮助。\n")
        elif op == "/save":
            save_history(messages)
            save_state()
            print("[系统] 对话历史与状态已保存。\n")
        elif op == "/clear":
            messages = [{"role": "system", "content": compose_system_prompt(load_agent_memory(), state.selected_skill)}]
            print("[系统] 当前会话上下文已清空，agent.md 记忆与技能选择仍保留。\n")
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
