"""
System Prompt Builder - 系统提示词构建器

实现模块化的系统提示词构建，支持工具、技能、记忆等多个子系统
"""

from __future__ import annotations
import os
from typing import List, Dict, Optional, Any
from dataclasses import dataclass

from agent.prompt.shared_assets import (
    SharedAssetScope,
    build_shared_asset_guidance,
    resolve_shared_asset_scope,
)
from common.log import logger
from config import conf


@dataclass
class ContextFile:
    """A context file (path + content)."""
    path: str
    content: str


class PromptBuilder:
    """System prompt builder."""
    
    def __init__(self, workspace_dir: str, language: str = "zh"):
        """
        初始化提示词构建器
        
        Args:
            workspace_dir: 工作空间目录
            language: 语言 ("zh" 或 "en")
        """
        self.workspace_dir = workspace_dir
        self.language = language
    
    def build(
        self,
        base_persona: Optional[str] = None,
        user_identity: Optional[Dict[str, str]] = None,
        tools: Optional[List[Any]] = None,
        context_files: Optional[List[ContextFile]] = None,
        skill_manager: Any = None,
        memory_manager: Any = None,
        runtime_info: Optional[Dict[str, Any]] = None,
        project_dir: Optional[str] = None,
        workspace_scope: Optional[str] = None,
        permission_mode: Optional[str] = None,
        **kwargs
    ) -> str:
        """
        构建完整的系统提示词
        
        Args:
            base_persona: 基础人格描述（会被context_files中的AGENT.md覆盖）
            user_identity: 用户身份信息
            tools: 工具列表
            context_files: 上下文文件列表（AGENT.md, USER.md, RULE.md, BOOTSTRAP.md等）
            skill_manager: 技能管理器
            memory_manager: 记忆管理器
            runtime_info: 运行时信息
            **kwargs: 其他参数
            
        Returns:
            完整的系统提示词
        """
        return build_agent_system_prompt(
            workspace_dir=self.workspace_dir,
            language=self.language,
            base_persona=base_persona,
            user_identity=user_identity,
            tools=tools,
            context_files=context_files,
            skill_manager=skill_manager,
            memory_manager=memory_manager,
            runtime_info=runtime_info,
            project_dir=project_dir,
            workspace_scope=workspace_scope,
            permission_mode=permission_mode,
            **kwargs
        )


def build_agent_system_prompt(
    workspace_dir: str,
    language: str = "zh",
    base_persona: Optional[str] = None,
    user_identity: Optional[Dict[str, str]] = None,
    tools: Optional[List[Any]] = None,
    context_files: Optional[List[ContextFile]] = None,
    skill_manager: Any = None,
    memory_manager: Any = None,
    runtime_info: Optional[Dict[str, Any]] = None,
    project_dir: Optional[str] = None,
    workspace_scope: Optional[str] = None,
    permission_mode: Optional[str] = None,
    **kwargs
) -> str:
    """
    Build the agent system prompt.

    Section order (by importance and logical flow):
    1. Tooling - core capabilities, introduced first
    2. Skills - right after tools, since skills are read via the read tool
    3. Memory - memory recall and writing guidance
    3.5 Knowledge - structured knowledge base (injects knowledge/index.md)
    4. Workspace - working environment description
    4.5 Permissions - what this session may change (omitted for full access)
    5. User identity - user info (optional)
    6. Project context - AGENT.md, USER.md, RULE.md, MEMORY.md, BOOTSTRAP.md
    7. Runtime info - meta info (time, model, etc.)

    Args:
        workspace_dir: workspace directory
        language: language ("zh" or "en")
        base_persona: base persona description (deprecated, defined by AGENT.md)
        user_identity: user identity info
        tools: tool list
        context_files: context file list
        skill_manager: skill manager
        memory_manager: memory manager
        runtime_info: runtime info
        **kwargs: extra args

    Returns:
        The full system prompt.
    """
    sections = []

    # Resolve the shared-asset maintenance scope once for this turn: the
    # knowledge section's auto-write wording and the final constraint block
    # must agree, and both read the same per-turn facts (change
    # ``guard-shared-knowledge-skill-writes``). A resolution failure already
    # yields the conservative scope inside the resolver.
    asset_scope = resolve_shared_asset_scope(workspace_dir)

    # 1. Tooling (most important, goes first)
    if tools:
        sections.extend(_build_tooling_section(tools, language))

    # 2. Skills (right after tools, since they need the read tool)
    if skill_manager:
        sections.extend(_build_skills_section(skill_manager, tools, language))

    # 3. Memory (standalone memory capability)
    if memory_manager:
        sections.extend(
            _build_memory_section(memory_manager, tools, language, workspace_dir, project_dir)
        )

    # 3.5 Knowledge (structured knowledge base)
    if conf().get("knowledge", True):
        sections.extend(
            _build_knowledge_section(workspace_dir, language, project_dir, asset_scope)
        )

    # 4. Workspace (working environment description). Two of those blocks only
    # hold when the context files were actually loaded, which sub agents skip.
    sections.extend(
        _build_workspace_section(
            workspace_dir, language, bool(context_files), project_dir=project_dir,
            workspace_scope=workspace_scope,
            client_platform=(
                str(kwargs.get("client_platform") or "").strip()
                or _client_platform(tools)
            ),
        )
    )

    # 4.5 Permissions. Right after the workspace, because what the model may
    # change only means something once it knows where it is working. Emits
    # nothing in full-access mode, leaving the prompt as it has always been.
    if permission_mode:
        try:
            from agent.permission import describe_mode

            sections.extend(
                describe_mode(
                    permission_mode,
                    language,
                    cwd=project_dir or workspace_dir,
                )
            )
        except Exception as e:
            logger.debug(f"Permission prompt section skipped: {e}")

    # 5. User identity (if present)
    if user_identity:
        sections.extend(_build_user_identity_section(user_identity, language))

    # 6. Project context files (AGENT.md, USER.md, RULE.md - define the persona)
    if context_files:
        sections.extend(_build_context_files_section(context_files, language))

    # 7. Runtime info (meta info, goes last)
    if runtime_info:
        sections.extend(_build_runtime_section(runtime_info, language))
        sections.extend(_build_team_section(runtime_info, language))

    # A selected desktop input is not the server workspace described above.
    # Emit the verified per-turn fact explicitly; tool schemas alone do not
    # tell the model that the user has actually selected a directory.
    sections.extend(build_desktop_directory_guidance(
        kwargs.get("desktop_context"), tools, language, workspace_scope,
    ))

    # 8. Response language (always appended, independent of the skeleton language)
    sections.extend(_build_response_language_section(language))

    # 9. Shared-content maintenance scope. Last on purpose: it constrains the
    # auto-write instructions that appear earlier (knowledge section, RULE.md
    # template, skill bodies), so it must be the final word the model reads. It
    # is emitted regardless of the knowledge switch or an empty index.
    sections.extend(
        build_shared_asset_guidance(workspace_dir, language, scope=asset_scope)
    )

    return "\n".join(sections)


def build_desktop_directory_guidance(
    reference: Optional[Dict[str, Any]], tools: Optional[List[Any]],
    language: str, workspace_scope: Optional[str] = None,
) -> List[str]:
    """Describe a verified input selection without exposing ids or client paths.

    Local execution projects already route their ordinary tools to the client
    and have their own guidance. Read-only inputs use client_files instead.
    Callers supply the same available tools that are offered to the model.
    """
    if not reference or not reference.get("binding_id") or workspace_scope == "local":
        return []
    available = any(getattr(tool, "name", "") == "client_files" for tool in tools or [])
    if language == "en":
        lines = [
            "## Selected desktop input", "",
            "The user has selected a desktop directory for this turn. Requests about "
            "the current project's files or data refer to that directory.",
            "The server workspace described above is not this selected directory. "
            "An empty result from server bash/read/ls cannot establish that the "
            "selected directory has no files; do not search other server directories as a substitute.",
        ]
        if available:
            lines += [
                'First list the selected input with client_files({"op":"list"}); '
                'omit relative_path for the root (never use "" or "."), '
                "then use search/stat/read_text to inspect relevant files.",
                "For Excel, PDF or other binary files, use client_files with op=materialize "
                "and a relative_path, then analyze the returned server path with existing tools. "
                "Do not pass client paths or URIs to server tools.",
                "Selection does not grant tool permissions. If reading fails, report the actual "
                "permission/device/network error; do not call the directory empty or claim to have read it.",
            ]
        else:
            lines.append("The client_files tool is currently unavailable. Explain that the selected "
                         "input cannot currently be read; do not claim that it contains no data.")
    else:
        lines = [
            "## 本轮桌面输入来源", "",
            "本轮已选择客户端本地目录。用户所说的当前项目文件或数据，指该目录。",
            "上述服务器工作区不是所选客户端目录。服务器 bash/read/ls 即使返回空，也不能据此判断所选目录没有文件；"
            "不要继续扫描其他服务器目录来替代本地读取。",
        ]
        if available:
            lines += [
                '先调用 client_files({"op":"list"}) 列出所选目录；根目录须省略 relative_path，不能传空字符串或 "."。'
                "再用 search/stat/read_text 查看相关文件。",
                "Excel、PDF 等二进制文件先用 client_files 的 materialize 和 relative_path 导入，"
                "再用现有工具分析返回的服务器文件路径；不要把客户端路径或 URI 当成服务器路径。",
                "选择目录不等于授予工具权限。读取失败时如实说明权限、设备或网络错误，"
                "不能声称目录为空或已经读取文件。",
            ]
        else:
            lines.append("client_files 当前不可用，请明确说明暂时无法读取所选目录，不能声称其中没有数据。")
    return lines + [""]


def _build_response_language_section(language: str) -> List[str]:
    """Response-language rule, appended regardless of the prompt skeleton language.

    Keeps the agent's reply language aligned with the user's input by default,
    so a Chinese-built prompt still answers an English user in English.
    """
    if language == "en":
        return [
            "## 🌐 Response language",
            "",
            "By default, reply in the same language as the user's input, "
            "unless the user explicitly asks for another language.",
            "",
        ]
    return [
        "## 🌐 回复语言",
        "",
        "默认使用与用户输入相同的语言回复，除非用户明确要求使用其他语言。",
        "",
    ]


def _build_identity_section(base_persona: Optional[str], language: str) -> List[str]:
    """Base identity section - no longer needed, identity is defined by AGENT.md."""
    # Identity is fully defined by AGENT.md, so emit nothing here.
    return []


def _build_tooling_section(tools: List[Any], language: str) -> List[str]:
    """Build tooling section with concise tool list and call style guide."""
    is_en = language == "en"
    # One-line summaries for known tools (details are in the tool schema)
    if is_en:
        core_summaries = {
            "read": "read file content",
            "write": "create or overwrite a file",
            "edit": "make precise edits to a file",
            "ls": "list directory contents",
            "search_files": "search inside files by regex, or find files by name",
            "bash": "run shell commands",
            "client_files": "read the selected desktop input directory; materialize files for server analysis",
            "terminal": "manage background processes",
            "web_search": "web search",
            "web_fetch": "fetch URL content",
            "browser": "control the browser (screenshot key results or send to the user when help is needed)",
            "memory_search": "search memory",
            "memory_get": "read memory content",
            "env_config": "manage API keys and skill config",
            "scheduler": "manage scheduled tasks and reminders",
            "send": "send a local file to the user (local files only; put URLs directly in the reply text)",
            "vision": "analyze images (recognition, description, OCR, etc.)",
            "subagent": "hand a self-contained task to a sub agent and get back only its conclusion",
        }
    else:
        core_summaries = {
            "read": "读取文件内容",
            "write": "创建或覆盖文件",
            "edit": "精确编辑文件",
            "ls": "列出目录内容",
            "search_files": "按正则搜索文件内容，或按文件名查找文件",
            "bash": "执行shell命令",
            "client_files": "读取所选客户端目录，或导入文件供服务器分析",
            "terminal": "管理后台进程",
            "web_search": "网络搜索",
            "web_fetch": "获取URL内容",
            "browser": "控制浏览器（关键结果或需要协助可截图发送给用户）",
            "memory_search": "搜索记忆",
            "memory_get": "读取记忆内容",
            "env_config": "管理API密钥和技能配置",
            "scheduler": "管理定时任务和提醒",
            "send": "发送本地文件给用户（仅限本地文件，URL直接放在回复文本中）",
            "vision": "分析图片内容（识别、描述、OCR文字提取等）",
            "subagent": "把一件自成一体的任务交给子 Agent，只拿回它的结论",
        }

    # Preferred display order
    tool_order = [
        "read", "write", "edit", "ls", "search_files",
        "bash", "terminal",
        "web_search", "web_fetch", "browser",
        "memory_search", "memory_get",
        "env_config", "scheduler", "send", "vision", "subagent",
    ]

    # Build name -> summary mapping for available tools
    available = {}
    for tool in tools:
        name = tool.name if hasattr(tool, 'name') else str(tool)
        available[name] = core_summaries.get(name, "")

    # Generate tool lines: ordered tools first, then extras
    tool_lines = []
    for name in tool_order:
        if name in available:
            summary = available.pop(name)
            tool_lines.append(f"- {name}: {summary}" if summary else f"- {name}")
    for name in sorted(available):
        summary = available[name]
        tool_lines.append(f"- {name}: {summary}" if summary else f"- {name}")

    # The delegation rule earns its place in the prompt only when the tool is
    # there. Left to the tool description alone it competes with ~30 others and
    # loses: the model reaches for search directly and fills its own context.
    has_subagent = "subagent" in {
        tool.name if hasattr(tool, "name") else str(tool) for tool in tools
    }

    if is_en:
        lines = [
            "## 🔧 Tooling",
            "",
            "Available tools (names are case-sensitive, call exactly as listed):",
            "\n".join(tool_lines),
            "",
            "Tool-calling style:",
            "",
            "- For multi-step tasks, complex decisions or sensitive operations, briefly explain what you are doing and why, so the user follows key progress",
            "- Never just announce intent and stop: if you say you will do something, actually call the tool in the same turn. A text-only reply ends the turn, so only reply with plain text once the work is genuinely finished",
            "- Keep going until the task is done, then report the result to the user",
            "- Always redact secrets, tokens and other sensitive info in replies",
            "- Put URLs directly in the reply text; the system handles and renders them. Don't download and re-send them via the send tool",
            "",
        ]
        if has_subagent:
            # One line, and only the part the tool description cannot do for
            # itself: get the model to consider delegating at all. When and how
            # belong in the description, which it reads once it looks.
            lines.insert(
                -1,
                "- Hand a self-contained task that needs research, search or information gathering to `subagent`: one task, or several at once via tasks running in parallel; it brings back the conclusion",
            )
    else:
        lines = [
            "## 🔧 工具系统",
            "",
            "可用工具（名称大小写敏感，严格按列表调用）:",
            "\n".join(tool_lines),
            "",
            "工具调用风格：",
            "",
            "- 多步骤任务、复杂决策、敏感操作时，应简要说明当前在做什么、为什么这样做，让用户了解关键进展",
            "- 不要只宣告意图就停下：一旦你说要做某事，就必须在同一轮真正发起工具调用。纯文本回复会立即结束本轮，因此只有在任务真正完成后才用纯文本收尾",
            "- 持续推进直到任务完成，完成后向用户报告结果",
            "- 回复中涉及密钥、令牌等敏感信息必须脱敏",
            "- URL链接直接放在回复文本中即可，系统会自动处理和渲染。无需下载后使用send工具发送",
            "",
        ]
        if has_subagent:
            lines.insert(
                -1,
                "- 需要深入调研、搜索或信息采集的独立任务，交给 `subagent`：可以单个任务，也可以用 tasks 同时执行多个任务，`subagent` 负责把结论带回来",
            )

    return lines


def _build_skills_section(skill_manager: Any, tools: Optional[List[Any]], language: str) -> List[str]:
    """Build the skills section."""
    if not skill_manager:
        return []
    
    # Resolve the read tool name
    read_tool_name = "read"
    if tools:
        for tool in tools:
            tool_name = tool.name if hasattr(tool, 'name') else str(tool)
            if tool_name.lower() == "read":
                read_tool_name = tool_name
                break
    
    if language == "en":
        lines = [
            "## 🧩 Skills (mandatory)",
            "",
            "Before replying: scan the <description> of every skill in <available_skills> below.",
            "",
            f"- If a skill's description matches the user's need: use the `{read_tool_name}` tool to read the SKILL.md at its <location> path, then strictly follow the instructions in the file. "
            "Prefer using a skill when one matches.",
            "- If multiple skills apply, pick the best-matching one, then read and follow it.",
            "- If no skill clearly applies: do not read any SKILL.md, just use the general tools.",
            "",
            f"**Important**: skills are not tools and cannot be called directly. The only way to use a skill is to read its SKILL.md with `{read_tool_name}`, then act on the file's content. "
            "Never read multiple skills at once — only read one after selecting it.",
            "",
            "Available skills:"
        ]
    else:
        lines = [
            "## 🧩 技能系统（mandatory）",
            "",
            "在回复之前：扫描下方 <available_skills> 中每个技能的 <description>。",
            "",
            f"- 如果有技能的描述与用户需求匹配：使用 `{read_tool_name}` 工具读取其 <location> 路径的 SKILL.md 文件，然后严格遵循文件中的指令。"
            "当有匹配的技能时，应优先使用技能",
            "- 如果多个技能都适用则选择最匹配的一个，然后读取并遵循。",
            "- 如果没有技能明确适用：不要读取任何 SKILL.md，直接使用通用工具。",
            "",
            f"**重要**: 技能不是工具，不能直接调用。使用技能的唯一方式是用 `{read_tool_name}` 读取 SKILL.md 文件，然后按文件内容操作。"
            "永远不要一次性读取多个技能，只在选择后再读取。",
            "",
            "以下是可用技能："
        ]
    
    # Append the skills list (built by skill_manager)
    try:
        skills_prompt = skill_manager.build_skills_prompt()
        logger.debug(f"[PromptBuilder] Skills prompt length: {len(skills_prompt) if skills_prompt else 0}")
        if skills_prompt:
            lines.append(skills_prompt.strip())
            lines.append("")
        else:
            logger.warning("[PromptBuilder] No skills prompt generated - skills_prompt is empty")
    except Exception as e:
        logger.warning(f"Failed to build skills prompt: {e}")
        import traceback
        logger.debug(f"Skills prompt error traceback: {traceback.format_exc()}")
    
    return lines


def _state_path_prefix(workspace_dir: str, project_dir: Optional[str]) -> str:
    """Absolute prefix for state files (memory/knowledge) under ``workspace_dir``.

    In project mode the cwd is the project, so a bare ``MEMORY.md`` would resolve
    into the project. Memory and knowledge must stay in ``workspace_dir``, so we
    prefix them with its absolute path. Default mode returns "" (paths unchanged).
    """
    if not project_dir:
        return ""
    import os as _os
    if _os.path.realpath(_os.path.expanduser(project_dir)) == _os.path.realpath(
        _os.path.expanduser(workspace_dir)
    ):
        return ""
    return workspace_dir.rstrip("/") + "/"


def _build_memory_section(
    memory_manager: Any,
    tools: Optional[List[Any]],
    language: str,
    workspace_dir: str = "",
    project_dir: Optional[str] = None,
) -> List[str]:
    """Build the memory section.

    ``workspace_dir``/``project_dir`` let project-mode sessions keep memory paths
    anchored to ``workspace_dir`` (absolute) instead of the project cwd.
    """
    if not memory_manager:
        return []

    # In project mode, memory files must be addressed absolutely under ~/cow.
    p = _state_path_prefix(workspace_dir, project_dir)
    mem_md = f"{p}MEMORY.md"
    mem_dir = f"{p}memory"
    kb_dir = f"{p}knowledge"

    has_memory_tools = False
    if tools:
        tool_names = [tool.name if hasattr(tool, 'name') else str(tool) for tool in tools]
        has_memory_tools = any(name in ['memory_search', 'memory_get'] for name in tool_names)

    if not has_memory_tools:
        return []

    from datetime import datetime
    today_file = datetime.now().strftime("%Y-%m-%d") + ".md"

    if language == "en":
        lines = [
            "## 🧠 Memory",
            "",
            "### Memory Recall (mandatory)",
            "",
            "When the user asks about past events, references an earlier decision, mentions relationships, preferences or to-dos, or when you are unsure about something, **you must search memory before answering**.",
            "No need to re-search if the info is already in MEMORY.md. Full content and daily memory must be retrieved via tools.",
            "",
            "1. Location unknown → `memory_search` (keyword / semantic search)",
            "2. Location known → `memory_get` to read the exact lines",
            "3. Search returns nothing → `memory_get` to read the last two days of memory",
            "",
        "**Memory file structure**:",
        f"- `{mem_md}`: long-term memory index (already auto-loaded into context: core info, preferences, decisions, etc.)",
        f"- `{mem_dir}/YYYY-MM-DD.md`: daily memory; today is `{mem_dir}/{today_file}`",
        f"- `{kb_dir}/`: structured knowledge base (see the knowledge system below)",
            "",
            "### Writing memory",
            "",
            "In the following cases, **proactively** write info to memory files (no need to tell the user):",
            "",
            "- The user asks you to remember something, or uses words like \"remember\", \"from now on\", \"always\", \"never\", \"prefer\"",
            "- The user shares important personal preferences, habits or decisions",
            "- The conversation produces an important conclusion, plan or agreement",
            "- A complex task is completed and the key steps and results are worth recording",
            "",
        "**Storage rules**:",
        f"- Long-term core info → `{mem_md}`",
        f"- Today's events/progress → `{mem_dir}/{today_file}`",
        f"- Structured knowledge → `{kb_dir}/` (see the knowledge system)",
            "- Append → `edit` tool with empty oldText",
            "- Modify → `edit` tool with oldText set to the text to replace",
            "- **Never write sensitive info** (API keys, tokens, etc.)",
            "",
            "**Principle**: use memory naturally, as if you simply knew it; don't bring it up unless asked.",
            "",
        ]
    else:
        lines = [
            "## 🧠 记忆系统",
            "",
            "### Memory Recall（mandatory）",
            "",
            "当用户询问过往事件、引用之前的决定、提到人物关系、偏好、待办、或你对某事不确定时，**必须先检索记忆再回答**。",
            "如果 MEMORY.md 中已有相关信息则无需重复检索。完整内容和每日记忆需要通过工具检索。",
            "",
            "1. 不确定位置 → `memory_search` 关键词/语义检索",
            "2. 已知位置 → `memory_get` 直接读取对应行",
            "3. search 无结果 → `memory_get` 读最近两天记忆",
            "",
            "**记忆文件结构**:",
            f"- `{mem_md}`: 长期记忆索引（已自动加载到上下文，核心信息、偏好、决策等）",
            f"- `{mem_dir}/YYYY-MM-DD.md`: 每日记忆，今天是 `{mem_dir}/{today_file}`",
            f"- `{kb_dir}/`: 结构化知识库（见下方知识系统）",
            "",
            "### 写入记忆",
            "",
            "遇到以下情况时，**主动**将信息写入记忆文件（无需告知用户）：",
            "",
            "- 用户要求记住某些信息，或使用了「记住」「以后」「总是」「不要」「偏好」等表达",
            "- 用户分享了重要的个人偏好、习惯、决策",
            "- 对话中产生了重要的结论、方案、约定",
            "- 完成了复杂任务，值得记录关键步骤和结果",
            "",
            "**存储规则**:",
            f"- 长期核心信息 → `{mem_md}`",
            f"- 当天事件/进展 → `{mem_dir}/{today_file}`",
            f"- 结构化知识 → `{kb_dir}/`（见知识系统）",
            "- 追加 → `edit` 工具，oldText 留空",
            "- 修改 → `edit` 工具，oldText 填写要替换的文本",
            "- **禁止写入敏感信息**（API密钥、令牌等）",
            "",
            "**使用原则**: 自然使用记忆，就像你本来就知道；不用刻意提起，除非用户问起。",
            "",
        ]

    return lines


def _build_knowledge_section(
    workspace_dir: str,
    language: str,
    project_dir: Optional[str] = None,
    scope: Optional[SharedAssetScope] = None,
) -> List[str]:
    """Build knowledge wiki section. Injects knowledge/index.md when present.

    In project mode ``project_dir`` anchors knowledge paths to ``workspace_dir``
    (absolute) so writes don't leak into the project cwd.

    ``scope`` is this turn's shared-asset maintenance scope. When the knowledge
    base is not maintainable, the unconditional "must auto-write" rules are
    replaced by a conditional form, so a read-only base never simultaneously
    receives a mandatory write instruction (change
    ``guard-shared-knowledge-skill-writes``).
    """
    maintainable = bool(scope and scope.resolved and scope.knowledge_maintainable)
    # Resolve through state_dir so an Agent on the shared base (no knowledge/ of
    # its own) still gets the shared index injected, not an empty local one.
    from common import state_dir
    knowledge_root = str(state_dir.knowledge_dir(base=workspace_dir))
    index_path = os.path.join(knowledge_root, "index.md")
    if not os.path.exists(index_path):
        return []

    try:
        with open(index_path, 'r', encoding='utf-8') as f:
            index_content = f.read().strip()
    except Exception:
        return []

    # Anchor knowledge paths to ~/cow when a project cwd is active.
    kb = f"{_state_path_prefix(workspace_dir, project_dir)}knowledge"

    if language == "en":
        if maintainable:
            heading = "### Auto-write rules (mandatory)"
            intro = "In the following cases you **must** write to the knowledge base alongside your reply, **directly, without asking the user**:"
            outro = "⚠️ Don't ask \"should I save this to the knowledge base?\" — if a case above matches, just write it. This is instinctive."
            index_rule = f"After writing any knowledge page, you **must update** `{kb}/index.md` with a new index line in sync."
        else:
            heading = "### Write rules (bounded by your maintenance scope)"
            intro = "This knowledge base is not inside your maintenance scope this turn, so it is read-only: do not create, modify, delete or rename its files (see \"Shared content maintenance scope\"). When you need to keep something, save it to this turn's personal output directory instead. Only if your maintenance scope does allow writing do the following apply:"
            outro = "⚠️ Never write to a read-only knowledge base; the maintenance scope stated in this prompt decides."
            index_rule = f"When writing is allowed, after writing any knowledge page you **must update** `{kb}/index.md` with a new index line in sync."
        lines = [
            "## 📚 Knowledge",
            "",
            f"You have a continuously growing personal knowledge base `{kb}/` — your long-term structured knowledge store.",
            "",
            heading,
            "",
            intro,
            "",
            f"1. **User shares an article / link / document** → after reading and understanding, write the key points to `{kb}/sources/<slug>.md` in the same turn",
            f"2. **An in-depth discussion produces a conclusion / plan** → organize it into `{kb}/analysis/<slug>.md`",
            f"3. **The conversation involves an important entity** (person / company / project) → create or update `{kb}/entities/<name>.md`",
            f"4. **A technical concept / methodology is discussed** → organize it into `{kb}/concepts/<topic>.md`",
            "",
            index_rule,
            "For detailed page format and conventions, read the SKILL.md of the `knowledge-wiki` skill.",
            "",
            outro,
            "",
        ]
    else:
        if maintainable:
            heading = "### 自动写入规则（mandatory）"
            intro = "以下场景**必须**在回复的同时写入知识库，**直接写入，不要询问用户是否需要**："
            outro = "⚠️ 不要问「要不要存到知识库」——符合上述场景就直接写入，这是你的本能行为。"
            index_rule = f"每次写入知识页面后，**必须同步更新** `{kb}/index.md` 添加一行索引。"
        else:
            heading = "### 写入规则（受维护范围约束）"
            intro = "本轮该知识库不在你的维护范围内，因此为只读：不要新增、修改、删除或重命名其中的文件（见「共享内容维护范围」）。需要沉淀内容时，写入本轮的个人输出目录。只有在你的维护范围允许写入时，才适用下列规则："
            outro = "⚠️ 不要写入只读知识库；以本提示中的维护范围为准。"
            index_rule = f"允许写入时，每次写入知识页面后**必须同步更新** `{kb}/index.md` 添加一行索引。"
        lines = [
            "## 📚 知识系统",
            "",
            f"你拥有一个持续积累的个人知识库 `{kb}/`，这是你的长期结构化知识存储。",
            "",
            heading,
            "",
            intro,
            "",
            f"1. **用户分享了文章/链接/文档** → 阅读理解后，在同一轮回复中将要点写入 `{kb}/sources/<slug>.md`",
            f"2. **深度讨论产生了结论/方案** → 整理为 `{kb}/analysis/<slug>.md`",
            f"3. **对话涉及重要实体**（人物/公司/项目）→ 创建或更新 `{kb}/entities/<name>.md`",
            f"4. **讨论了技术概念/方法论** → 整理为 `{kb}/concepts/<topic>.md`",
            "",
            index_rule,
            "详细的页面格式和操作规范，请读取技能 `knowledge-wiki` 的 SKILL.md。",
            "",
            outro,
            "",
        ]

    if index_content:
        lines.extend([
            ("### Current knowledge index" if language == "en" else "### 当前知识索引"),
            "",
            index_content,
            "",
        ])

    lines.extend([
        ("**How to query**: use `read` to open a knowledge page, or `memory_search` (knowledge is in the vector index)."
         if language == "en" else
         "**查询方式**：用 `read` 读取知识页面，或用 `memory_search` 检索（知识已纳入向量索引）。"),
        "",
    ])

    return lines


def _build_user_identity_section(user_identity: Dict[str, str], language: str) -> List[str]:
    """Build the user identity section."""
    if not user_identity:
        return []
    
    is_en = language == "en"
    lines = [
        ("## 👤 User identity" if is_en else "## 👤 用户身份"),
        "",
    ]

    if user_identity.get("name"):
        lines.append(f"**{'Name' if is_en else '用户姓名'}**: {user_identity['name']}")
    if user_identity.get("nickname"):
        lines.append(f"**{'Preferred name' if is_en else '称呼'}**: {user_identity['nickname']}")
    if user_identity.get("timezone"):
        lines.append(f"**{'Timezone' if is_en else '时区'}**: {user_identity['timezone']}")
    if user_identity.get("notes"):
        lines.append(f"**{'Notes' if is_en else '备注'}**: {user_identity['notes']}")

    lines.append("")

    return lines


def _build_docs_section(workspace_dir: str, language: str) -> List[str]:
    """Docs-path section - removed, no longer needed."""
    # No docs section is generated anymore.
    return []


def _client_platform(tools: Optional[List[Any]]) -> str:
    """The client's platform for this run, or ``""`` when there is no client.

    Read from the run's script tool, which asks the *launcher* -- the only thing
    that can actually know, since the command runs on the user's machine. A tool
    that offers no hint is not an error: a server-side run has no client, and an
    unknown platform must stay unknown (see ``_local_execution_notes``).

    The first non-empty answer wins. Two script tools in one view are the same
    platform by construction -- ``tool_view_for_run`` builds them together -- so
    there is no conflict to resolve.
    """
    for tool in tools or []:
        hint = getattr(tool, "platform_hint", None)
        if not callable(hint):
            continue
        try:
            value = str(hint() or "").strip()
        except Exception as e:  # noqa: BLE001 - a failed hint is simply no hint
            logger.debug(f"Client platform hint skipped: {e}")
            continue
        if value:
            return value
    return ""


def _local_execution_notes(language: str, client_platform: str = "") -> List[str]:
    """What the model must not get wrong about a local project (task 8.7).

    Three corrections, each of them a natural misreading:

    * ``cwd`` names a directory on the *user's* machine, not a server directory;
    * the project grant covers the project, and does **not** extend to skill or
      memory maintenance -- those keep their original authorization and service
      ownership (``desktop-project-execution``: memory, knowledge, remote APIs
      and MCP keep their existing service ownership, and must not be handed
      client paths they cannot resolve);
    * the platform is the *client's*. The server's own platform is not evidence
      about a command that runs on the user's machine, so the real platform and
      the real missing pieces are read from this run's script tool.

    ``client_platform`` is the launcher-reported platform, or ``""`` when it
    cannot be determined. An unknown platform is said to be unknown rather than
    quietly filled in with the server's -- that substitution is the exact
    mistake these notes exist to prevent.
    """
    if language == "en":
        lines = [
            "",
            "**This project is on the user's own computer.**",
            "",
            "- The working directory above is a directory on the user's machine,",
            "  reached through their desktop client; it is not on the server.",
            "- Work in it with the ordinary file and shell tools. Do not ask the",
            "  user to copy files out, and do not assume `~` means this directory.",
            "- Deliver what the user asked for *into this project*: write outputs,",
            "  reports and generated files under the project directory (a relative",
            "  path already means that). Do not leave a deliverable in a scratch or",
            "  temporary directory, and do not send the user hunting for it.",
            "- This project's permission is a permission on **this directory**. It",
            "  does not extend to maintaining skills or memory: those keep their",
            "  original authority and live in the system directory above. Never",
            "  write them through the project, and never pass a client path to a",
            "  tool that resolves its paths on the server (memory, knowledge,",
            "  MCP, remote APIs) -- tell the user the tool cannot take that path",
            "  instead of inventing one.",
            "- Whether this session may *change* the project (not just read it) is",
            "  decided per tool call, not by this section. If a tool is refused,",
            "  report the refusal instead of retrying with another path.",
        ]
        if client_platform:
            lines.append(
                f"- Commands here run on the client's platform (**{client_platform}**),"
                " not on the server's. Use the shell semantics of this run's script"
                " tool, which also names any missing runtime; do not assume the"
                " server's operating system or Unix-only commands."
            )
        else:
            lines.append(
                "- Commands here run on the client's platform, which this run could"
                " not determine; do not assume it is the server's. The run's script"
                " tool description states the real platform, the shell and any"
                " missing runtime -- read it before writing platform-specific"
                " commands, and do not fall back to Unix-only commands."
            )
        lines.append("")
        return lines
    lines = [
        "",
        "**该项目位于用户自己的电脑上。**",
        "",
        "- 上面的工作目录是用户本机的目录，通过其桌面客户端访问，不在服务器上。",
        "- 用常规的文件与命令工具在其中工作；不要让用户把文件拷出来，也不要把 `~`",
        "  当作该目录。",
        "- 用户要的成果要落进**这个项目**：输出、报告与生成的文件都写在项目目录下",
        "  （相对路径本来就是这个意思）。不要把成果留在临时目录里，也不要让用户自己去找。",
        "- 该项目授予的是**这个目录**上的权限，不延伸到技能与记忆维护：二者仍遵循原有权限，",
        "  仍在上面那个系统目录中。不要经由项目去写它们；也不要把客户端路径交给",
        "  在服务端解析路径的工具（记忆、知识、MCP、远程业务 API）——工具无法接受该路径时",
        "  应如实说明，而不是自己编一个。",
        "- 本次会话能否*修改*该项目（而非仅读取）由每次工具调用决定，本段不作判定。",
        "  工具被拒绝时应如实说明，不要换路径重试。",
    ]
    if client_platform:
        lines.append(
            f"- 这里的命令运行在**客户端**平台（`{client_platform}`）上，不是服务器平台。"
            "请按本轮脚本工具描述的 Shell 语义来写，其中也列出了缺失的运行时；不要假设"
            "服务器的操作系统，也不要用仅 Unix 可用的命令。"
        )
    else:
        lines.append(
            "- 这里的命令运行在**客户端**平台上，而本轮未能确定该平台；不要假设它就是"
            "服务器的平台。本轮脚本工具的描述里写明了真实平台、Shell 与缺失的运行时，"
            "写平台相关命令前请先读它，不要退回到仅 Unix 可用的命令。"
        )
    lines.append("")
    return lines


def _build_workspace_section(
    workspace_dir: str, language: str, context_files_loaded: bool = True,
    project_dir: Optional[str] = None, workspace_scope: Optional[str] = None,
    client_platform: str = "",
) -> List[str]:
    """Build the workspace section.

    ``context_files_loaded`` gates the two blocks that are only true for an
    Agent serving a user directly. A sub agent gets neither the context files
    nor a user to talk to, so telling it that AGENT.md is already loaded would
    both misinform it and talk it out of reading the workspace rules itself.

    ``project_dir`` switches the section to the dual-directory layout used when
    the session's working directory is not the Agent's own workspace: the
    working directory holds relative paths and artifacts while the Agent's
    workspace stays the *system* directory (memory/skills), reached with
    absolute paths.

    ``workspace_scope`` says which of the two that override is, so the wording
    does not lie: ``"project"`` is a directory the user picked, ``"personal"``
    is the caller's own directory inside a tenant-shared Agent (change
    ``use-personal-workspace-for-shared-agents``), which nobody selected and
    which no other member shares, and ``"local"`` is a directory the user
    picked on their *own machine*.

    ``client_platform`` is the platform a local run's commands execute on, as
    the client reported it (task 8.7). ``""`` means unknown, which is stated as
    unknown rather than replaced with this server's platform.
    """
    normalized_project = None
    if project_dir:
        import os as _os
        if _os.path.realpath(_os.path.expanduser(project_dir)) != _os.path.realpath(
            _os.path.expanduser(workspace_dir or "")
        ):
            normalized_project = project_dir

    if normalized_project:
        if workspace_scope == "personal":
            return _build_personal_workspace_section(
                workspace_dir, normalized_project, language, context_files_loaded
            )
        if workspace_scope == "local":
            # The working directory is on the *user's own machine*, reached
            # through the desktop client rather than the server's filesystem.
            # The layout is the project one, so reuse it; the extra lines say
            # where the directory actually is, because "the working directory"
            # would otherwise read as a server path.
            lines = _build_project_workspace_section(
                workspace_dir, normalized_project, language, context_files_loaded
            )
            lines += _local_execution_notes(language, client_platform)
            return lines
        return _build_project_workspace_section(
            workspace_dir, normalized_project, language, context_files_loaded
        )

    if language == "en":
        lines = [
            "## 📂 Workspace",
            "",
            f"Your working directory is: `{workspace_dir}`",
            "",
            "**Path rules** (very important):",
            "",
            f"1. **Base directory for relative paths**: all relative paths are relative to `{workspace_dir}`",
            "   - ✅ Correct: use relative paths for files inside the workspace, e.g. `AGENT.md`",
            f"   - ❌ Wrong: using a relative path for files in other directories (if not inside `{workspace_dir}`)",
            "",
            "2. **Accessing other directories**: to reach directories outside the workspace (project code, system files), **you must use absolute paths**",
            "   - ✅ Correct: e.g. `~/chatgpt-on-wechat`, `/usr/local/`",
            "   - ❌ Wrong: assuming a relative path points to another directory",
            "",
            "3. **Path resolution examples**:",
            f"   - relative `memory/` → actual `{workspace_dir}/memory/`",
            "   - absolute `~/chatgpt-on-wechat/docs/` → actual `~/chatgpt-on-wechat/docs/`",
            "",
            "4. **When unsure**: run `bash pwd` to confirm the current directory, or `ls .` to see where you are",
            "",
        ]
        if context_files_loaded:
            lines += [
                "**Important - files already auto-loaded**:",
                "",
                "The following files are **already auto-loaded** into the system prompt at session start, so you **don't need to read them again with the read tool**:",
                "",
                "- ✅ `AGENT.md`: loaded - your persona and soul; follow it strictly. When your name, personality or style changes, proactively `edit` this file",
                "- ✅ `USER.md`: loaded - the user's identity info. When the user changes how they're addressed, their name, etc., `edit` this file",
                "- ✅ `RULE.md`: loaded - workspace guide and rules; follow them strictly",
                "- ✅ `MEMORY.md`: loaded - long-term memory index",
                "",
                "**💬 Communication norms**:",
                "",
                "- No need to expose file names for memory operations; use natural language. Say \"I'll remember that\" rather than \"updated MEMORY.md\"",
                "- Tell the user about key decisions and steps during a task, so they know what you're doing and why",
                "- Be genuinely helpful rather than performatively polite; solve the problem as much as you can",
                "- Keep replies well-structured and focused. Use **bold**, lists and sections to make info clear at a glance",
                "- Use emoji to make expression lively 🎯, but don't overdo it",
                "",
            ]
    else:
        lines = [
            "## 📂 工作空间",
            "",
            f"你的工作目录是: `{workspace_dir}`",
            "",
            "**路径使用规则** (非常重要):",
            "",
            f"1. **相对路径的基准目录**: 所有相对路径都是相对于 `{workspace_dir}` 而言的",
            f"   - ✅ 正确: 访问工作空间内的文件用相对路径，如 `AGENT.md`",
            f"   - ❌ 错误: 用相对路径访问其他目录的文件 (如果它不在 `{workspace_dir}` 内)",
            "",
            "2. **访问其他目录**: 如果要访问工作空间之外的目录（如项目代码、系统文件），**必须使用绝对路径**",
            f"   - ✅ 正确: 例如 `~/chatgpt-on-wechat`、`/usr/local/`",
            f"   - ❌ 错误: 假设相对路径会指向其他目录",
            "",
            "3. **路径解析示例**:",
            f"   - 相对路径 `memory/` → 实际路径 `{workspace_dir}/memory/`",
            f"   - 绝对路径 `~/chatgpt-on-wechat/docs/` → 实际路径 `~/chatgpt-on-wechat/docs/`",
            "",
            "4. **不确定时**: 先用 `bash pwd` 确认当前目录，或用 `ls .` 查看当前位置",
            "",
        ]
        if context_files_loaded:
            lines += [
                "**重要说明 - 文件已自动加载**:",
                "",
                "以下文件在会话启动时**已经自动加载**到系统提示词中，你**无需再用 read 工具读取**：",
                "",
                "- ✅ `AGENT.md`: 已加载 - 你的人格和灵魂设定，请严格遵循。当你的名字、性格或交流风格发生变化时，主动用 `edit` 更新此文件",
                "- ✅ `USER.md`: 已加载 - 用户的身份信息。当用户修改称呼、姓名等身份信息时，用 `edit` 更新此文件",
                "- ✅ `RULE.md`: 已加载 - 工作空间使用指南和规则，请严格遵循",
                "- ✅ `MEMORY.md`: 已加载 - 长期记忆索引",
                "",
                "**💬 交流规范**:",
                "",
                "- 记忆相关操作无需暴露文件名，用自然语言表达即可。例如说「我已记住」而非「已更新 MEMORY.md」",
                "- 任务执行过程中的关键决策和步骤应该告知用户，让用户了解你在做什么、为什么这么做",
                "- 做真正有帮助的助手，而不是表演式的客套，尽可能帮忙解决问题",
                "- 回复应结构清晰、重点突出。善用 **加粗**、列表、分段等格式让信息一目了然",
                "- 适当使用 emoji 让表达更生动自然 🎯，但不要过度堆砌",
                "",
            ]

    # Cloud deployment: inject websites directory info and access URL
    cloud_website_lines = _build_cloud_website_section(workspace_dir)
    if cloud_website_lines:
        lines.extend(cloud_website_lines)
    
    return lines


def _build_personal_workspace_section(
    workspace_dir: str, personal_dir: str, language: str, context_files_loaded: bool
) -> List[str]:
    """Workspace section for a shared Agent's caller working in their own folder.

    Same two-directory layout as a project, different fact: nobody selected this
    directory. It is the caller's own business folder inside a tenant-shared
    Agent, which is why it is *not* called a project and why the section says
    where it comes from. Calling it a project would invite the model to treat a
    picker selection as in force, and would misdescribe a directory the user
    cannot share with the other members of the Agent.
    """
    if language == "en":
        lines = [
            "## 📂 Workspace",
            "",
            "This Agent is **shared** with other members. With no project selected,"
            " your working directory is **your own folder** inside it — everything"
            " you write with a relative path stays there and is not shared.",
            "",
            f"- **Current working directory (your own folder)**: `{personal_dir}`",
            f"- **System directory (memory & skills)**: `{workspace_dir}`",
            "",
            "**Path rules** (very important):",
            "",
            f"1. **Relative paths are based on your working directory** `{personal_dir}`."
            " Put your work products here (documents, code, generated files, etc.).",
            f"   - ✅ relative `output/report.html` → `{personal_dir}/output/report.html`",
            "",
            f"2. **Memory and skills stay in the system directory** `{workspace_dir}`."
            " Never write them into your folder. Memory tools handle this for you;"
            " if you ever touch these files directly, use **absolute paths** under"
            " the system directory.",
            f"   - ✅ absolute `{workspace_dir}/MEMORY.md`",
            f"   - ❌ relative `MEMORY.md` (that would land in your folder, which is wrong)",
            "",
            "3. **Accessing any other directory**: use absolute paths.",
            "",
            "4. **When unsure**: run `bash pwd` to confirm the current directory.",
            "",
            "If the user later selects a project, the working directory switches to"
            " it; clearing the project brings it back here.",
            "",
        ]
    else:
        lines = [
            "## 📂 工作空间",
            "",
            "该智能体由**多人共享**。未选择项目时，你的工作目录是你在其中的**个人目录**——"
            "用相对路径生成的内容都落在这里，不会与其他人共享。",
            "",
            f"- **当前工作目录（你的个人目录）**: `{personal_dir}`",
            f"- **系统目录（记忆与技能）**: `{workspace_dir}`",
            "",
            "**路径使用规则** (非常重要):",
            "",
            f"1. **相对路径基于当前工作目录** `{personal_dir}`。你的工作产物（文档、代码、生成的文件等）都放在这里。",
            f"   - ✅ 相对路径 `output/report.html` → `{personal_dir}/output/report.html`",
            "",
            f"2. **记忆和技能仍在系统目录** `{workspace_dir}`，不要写入个人目录。记忆操作由记忆工具自动完成；若确需直接访问这些文件，请使用系统目录下的**绝对路径**。",
            f"   - ✅ 绝对路径 `{workspace_dir}/MEMORY.md`",
            f"   - ❌ 相对路径 `MEMORY.md`（那会落到你的个人目录里，是错误的）",
            "",
            "3. **访问其他任意目录**：使用绝对路径。",
            "",
            "4. **不确定时**：用 `bash pwd` 确认当前目录。",
            "",
            "用户后续选择项目时会切换到该项目；清除项目后回到这个个人目录。",
            "",
        ]

    if context_files_loaded:
        if language == "en":
            lines += [
                "**Files already auto-loaded** (no need to `read` again): `AGENT.md`, `USER.md`, `RULE.md`, `MEMORY.md` (from the system directory).",
                "",
            ]
        else:
            lines += [
                "**已自动加载的文件**（无需再次 `read`）：`AGENT.md`、`USER.md`、`RULE.md`、`MEMORY.md`（来自系统目录）。",
                "",
            ]

    cloud_website_lines = _build_cloud_website_section(workspace_dir)
    if cloud_website_lines:
        lines.extend(cloud_website_lines)
    return lines


def _build_project_workspace_section(
    workspace_dir: str, project_dir: str, language: str, context_files_loaded: bool
) -> List[str]:
    """Workspace section for a session pointed at a project directory.

    Two directories are in play and the model must not confuse them:
    - Project directory (the current working dir): relative paths and work
      products (documents, code, generated files, etc.) live here.
    - System directory (``workspace_dir``, e.g. ``~/cow``): memory and skills
      live here and are reached with absolute paths, never relative ones.
    """
    if language == "en":
        lines = [
            "## 📂 Workspace",
            "",
            "The user has opened a **project directory**. You are working inside the current project directory.",
            "",
            f"- **Project directory (current working dir)**: `{project_dir}`",
            f"- **System directory (memory & skills)**: `{workspace_dir}`",
            "",
            "**Path rules** (very important):",
            "",
            f"1. **Relative paths are based on the project directory** `{project_dir}`. Put your work products here (documents, code, generated files, etc.).",
            f"   - ✅ relative `output/report.html` → `{project_dir}/output/report.html`",
            "",
            f"2. **Memory and skills stay in the system directory** `{workspace_dir}`. Never write them into the project. Memory tools handle this for you; if you ever touch these files directly, use **absolute paths** under the system directory.",
            f"   - ✅ absolute `{workspace_dir}/MEMORY.md`",
            f"   - ❌ relative `MEMORY.md` (that would land in the project, which is wrong)",
            "",
            "3. **Accessing any other directory**: use absolute paths.",
            "",
            "4. **When unsure**: run `bash pwd` to confirm you are in the project directory.",
            "",
        ]
    else:
        lines = [
            "## 📂 工作空间",
            "",
            "用户已打开一个**项目目录**，你正在当前项目目录中工作。",
            "",
            f"- **项目目录（当前工作目录）**: `{project_dir}`",
            f"- **系统目录（记忆与技能）**: `{workspace_dir}`",
            "",
            "**路径使用规则** (非常重要):",
            "",
            f"1. **相对路径基于项目目录** `{project_dir}`。你的工作产物（文档、代码、生成的文件等）都放在这里。",
            f"   - ✅ 相对路径 `output/report.html` → `{project_dir}/output/report.html`",
            "",
            f"2. **记忆和技能仍在系统目录** `{workspace_dir}`，不要写入项目目录。记忆操作由记忆工具自动完成；若确需直接访问这些文件，请使用系统目录下的**绝对路径**。",
            f"   - ✅ 绝对路径 `{workspace_dir}/MEMORY.md`",
            f"   - ❌ 相对路径 `MEMORY.md`（那会落到项目目录里，是错误的）",
            "",
            "3. **访问其他任意目录**：使用绝对路径。",
            "",
            "4. **不确定时**：用 `bash pwd` 确认当前处于项目目录。",
            "",
        ]

    if context_files_loaded:
        if language == "en":
            lines += [
                "**Files already auto-loaded** (no need to `read` again): `AGENT.md`, `USER.md`, `RULE.md`, `MEMORY.md` (from the system directory).",
                "",
            ]
        else:
            lines += [
                "**已自动加载的文件**（无需再次 `read`）：`AGENT.md`、`USER.md`、`RULE.md`、`MEMORY.md`（来自系统目录）。",
                "",
            ]

    cloud_website_lines = _build_cloud_website_section(workspace_dir)
    if cloud_website_lines:
        lines.extend(cloud_website_lines)
    return lines


def _build_cloud_website_section(workspace_dir: str) -> List[str]:
    """Build cloud website access prompt when cloud deployment is configured."""
    try:
        from common.cloud_client import build_website_prompt
        return build_website_prompt(workspace_dir)
    except Exception:
        return []


def _build_context_files_section(context_files: List[ContextFile], language: str) -> List[str]:
    """Build the project context files section."""
    if not context_files:
        return []
    
    # Check whether AGENT.md is present
    has_agent = any(
        f.path.lower().endswith('agent.md') or 'agent.md' in f.path.lower()
        for f in context_files
    )
    
    is_en = language == "en"
    if is_en:
        lines = [
            "# 📋 Project context",
            "",
            "The following project context files have been loaded:",
            "",
        ]
    else:
        lines = [
            "# 📋 项目上下文",
            "",
            "以下项目上下文文件已被加载：",
            "",
        ]

    if has_agent:
        if is_en:
            lines.append("**`AGENT.md` is your soul file** 🪞: strictly follow the persona, tone and settings it defines. Be your real self, avoid stiff, template-like replies.")
            lines.append("When the user reveals new expectations about your personality, style, responsibilities or capability boundaries, proactively `edit` AGENT.md to reflect that evolution.")
        else:
            lines.append("**`AGENT.md` 是你的灵魂文件** 🪞：严格遵循其中定义的人格、语气和设定，做真实的自己，避免僵硬、模板化的回复。")
            lines.append("当用户通过对话透露了对你性格、风格、职责、能力边界的新期望，你应该主动用 `edit` 更新 AGENT.md 以反映这些演变。")
        lines.append("")
    
    # Append the content of each file
    for file in context_files:
        lines.append(f"## {file.path}")
        lines.append("")
        lines.append(file.content)
        lines.append("")
    
    return lines


def _build_team_section(runtime_info: Dict[str, Any], language: str) -> List[str]:
    """Name the other Agents sharing this conversation, if any. Empty for one.

    Addressing someone by name is routing, not delegation: the named Agent is
    already the one reading this prompt, so handover is left for work nobody
    was asked for by name.

    States the Agent's own name, which nothing else in the prompt does. Without
    it a mention reads as a third party and the Agent declines to answer on
    that stranger's behalf.
    """
    teammates = runtime_info.get("teammates")
    getter = runtime_info.get("_get_teammates")
    if callable(getter):
        try:
            teammates = getter()
        except Exception as e:
            logger.warning(f"[PromptBuilder] Failed to resolve teammates: {e}")
    if not teammates:
        return []

    # One line each, with what they are for: a bare list of names is enough to
    # address someone but not to decide whether the work is theirs.
    roster = []
    for item in teammates:
        if not item.get("id"):
            continue
        line = f"{item.get('name') or item['id']}(@{item['id']})"
        if item.get("description"):
            line += f"：{item['description']}" if language != "en" else f" — {item['description']}"
        roster.append(line)
    if not roster:
        return []

    own_id = runtime_info.get("agent_id") or ""
    own_name = runtime_info.get("agent_name") or own_id
    whoami = f"{own_name}(@{own_id})" if own_id else own_name

    if language == "en":
        return [
            "## 👥 Team conversation",
            "",
            f"You are {whoami}. Also in this conversation:",
            "",
            *[f"- {line}" for line in roster],
            "",
            "Everyone here reads the same history. A teammate's earlier "
            "reply is shown as `Name(@id)：` before their words; an unmarked "
            "one is your own. Do not take a teammate's work, or their "
            "promises, for yours.",
            "",
            "This turn is yours to answer. Answer as yourself, and do NOT "
            "prefix your reply with your own name or id — that label is only "
            "how others' past turns are shown to you.",
            "",
            "Use agent_delegate for work that belongs to a teammate, passing "
            "their id above as agent_id (without the @), and say who you handed "
            "it to and what you asked for. Refer to teammates by name to the "
            "user, without the @id — the id is internal.",
            "",
        ]
    return [
        "## 👥 团队会话",
        "",
        f"你是 {whoami}。同在这个会话里的还有：",
        "",
        *[f"- {line}" for line in roster],
        "",
        "大家读到的是同一份记录。以「名字(@id)：」开头的是那位同事说过的话，"
        "没有前缀的才是你自己说的。不要把同事做过的事、许下的承诺当成你的。",
        "",
        "这一轮由你来回答，以你自己的身份回答即可，回答时不要在开头加自己的名字或 id "
        "前缀（前缀只是用来向你标记其他成员的历史发言）。",
        "",
        "该由某位同事做的事，用 agent_delegate 交出去：把那位同事上面的 id "
        "作为 agent_id 传入 (不含@符号)，并说明交给了谁、交办了什么。对用户提到同事时只用名字，"
        "不要带 @id，id 只用于内部。",
        "",
    ]


def _build_runtime_section(runtime_info: Dict[str, Any], language: str) -> List[str]:
    """Build the runtime info section - supports dynamic time."""
    if not runtime_info:
        return []
    
    is_en = language == "en"
    time_label = "Current time" if is_en else "当前时间"
    lines = [
        ("## ⚙️ Runtime info" if is_en else "## ⚙️ 运行时信息"),
        "",
    ]

    # Add current time if available
    # Support dynamic time via callable function
    if callable(runtime_info.get("_get_current_time")):
        try:
            time_info = runtime_info["_get_current_time"]()
            time_line = f"{time_label}: {time_info['time']} {time_info['weekday']} ({time_info['timezone']})"
            lines.append(time_line)
            lines.append("")
        except Exception as e:
            logger.warning(f"[PromptBuilder] Failed to get dynamic time: {e}")
    elif runtime_info.get("current_time"):
        # Fallback to static time for backward compatibility
        time_str = runtime_info["current_time"]
        weekday = runtime_info.get("weekday", "")
        timezone = runtime_info.get("timezone", "")

        time_line = f"{time_label}: {time_str}"
        if weekday:
            time_line += f" {weekday}"
        if timezone:
            time_line += f" ({timezone})"

        lines.append(time_line)
        lines.append("")

    # Add other runtime info
    model_label = "model" if is_en else "模型"
    workspace_label = "workspace" if is_en else "工作空间"
    channel_label = "channel" if is_en else "渠道"
    runtime_parts = []
    # Support dynamic model via callable, fallback to static value
    if callable(runtime_info.get("_get_model")):
        try:
            runtime_parts.append(f"{model_label}={runtime_info['_get_model']()}")
        except Exception:
            if runtime_info.get("model"):
                runtime_parts.append(f"{model_label}={runtime_info['model']}")
    elif runtime_info.get("model"):
        runtime_parts.append(f"{model_label}={runtime_info['model']}")
    if runtime_info.get("workspace"):
        runtime_parts.append(f"{workspace_label}={runtime_info['workspace']}")
    # Only add channel if it's not the default "web"
    if runtime_info.get("channel") and runtime_info.get("channel") != "web":
        runtime_parts.append(f"{channel_label}={runtime_info['channel']}")

    if runtime_parts:
        lines.append(("Runtime: " if is_en else "运行时: ") + " | ".join(runtime_parts))
        lines.append("")

    return lines
