"""
Memory add tool

Allows agents to explicitly save facts, lessons, and context to long-term memory.
"""

from typing import Dict, Any, Optional
from agent.tools.base_tool import BaseTool


class MemoryAddTool(BaseTool):
    """Tool for adding content to agent's long-term memory"""

    name: str = "memory_add"
    description: str = (
        "Save a fact, lesson, preference, or important context to long-term memory. "
        "Use this when the user explicitly asks you to remember something, or when you "
        "learned a non-obvious lesson that should be recalled in future conversations."
    )
    params: dict = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The exact text to remember. Be concise but self-contained."
            },
            "scope": {
                "type": "string",
                "description": "Who this memory applies to: 'user' (current user only), 'shared' (everyone), or 'session' (discarded after this session). Default: 'user'.",
                "enum": ["shared", "user", "session"],
                "default": "user"
            },
            "path": {
                "type": "string",
                "description": "Optional relative path for the memory file (e.g. 'memory/lessons.md'). Auto-generated if omitted.",
                "default": ""
            }
        },
        "required": ["content"]
    }

    def __init__(self, memory_manager, user_id: Optional[str] = None):
        """
        Initialize memory add tool

        Args:
            memory_manager: MemoryManager instance
            user_id: Optional user ID for user-scoped memories
        """
        super().__init__()
        self.memory_manager = memory_manager
        self.user_id = user_id

    def execute(self, args: dict):
        """
        Execute memory add

        Args:
            args: Dictionary with content, scope, path

        Returns:
            ToolResult with status
        """
        from agent.tools.base_tool import ToolResult
        import asyncio

        content = args.get("content", "").strip()
        if not content:
            return ToolResult.fail("Error: content parameter is required")

        scope = args.get("scope", "user")
        if scope not in ("shared", "user", "session"):
            return ToolResult.fail(f"Error: scope must be 'shared', 'user', or 'session', got '{scope}'")

        path = args.get("path") or None
        user_id = self.user_id if scope == "user" else None

        try:
            result = asyncio.run(self.memory_manager.add_memory(
                content=content,
                user_id=user_id,
                scope=scope,
                source="memory",
                path=path
            ))

            scope_note = f"scope={scope}"
            if scope == "user" and user_id:
                scope_note += f", user={user_id}"

            if isinstance(result, dict) and result.get('index_state') != 'ok':
                return ToolResult.success(
                    f"内容已保存，索引待恢复 ({scope_note}, id={result.get('id')}). "
                    "Do not claim it is searchable yet; retry indexing from memory management.",
                    result)
            return ToolResult.success(
                f"Memory saved successfully ({scope_note}). "
                f"It is available via memory_search. id={result.get('id')}" if isinstance(result, dict)
                else f"Memory saved successfully ({scope_note}); searchable after indexing."
            )

        except Exception as e:
            return ToolResult.fail(f"Error saving memory: {str(e)}")
