"""
工具注册表

管理工具的注册和发现。
"""

from typing import Any, Callable, Optional


class ToolRegistry:
    """
    工具注册表

    管理所有可用工具的注册和发现。
    """

    def __init__(self):
        # _tools：工具名 -> 可调用对象（同步或异步函数）的映射，用于实际执行
        self._tools: dict[str, Callable] = {}
        # _tool_info：工具名 -> 元数据（名称/描述/参数 schema），用于对外展示与发现
        self._tool_info: dict[str, dict] = {}

    def register(
        self,
        name: str,
        func: Callable,
        description: str = "",
        parameters: Optional[dict] = None,
    ) -> None:
        # 注册工具：同时写入可调用对象与其元数据；同名会直接覆盖
        """注册工具"""
        self._tools[name] = func
        self._tool_info[name] = {
            "name": name,
            "description": description,
            "parameters": parameters or {},
        }

    def unregister(self, name: str) -> bool:
        # 注销工具：同时移除可调用对象与元数据；返回是否确实存在并删除成功
        """注销工具"""
        if name in self._tools:
            del self._tools[name]
            del self._tool_info[name]
            return True
        return False

    def get(self, name: str) -> Optional[Callable]:
        # 按名称取出可调用对象，不存在时返回 None
        """获取工具"""
        return self._tools.get(name)

    def get_info(self, name: str) -> Optional[dict]:
        # 按名称取出工具元数据，不存在时返回 None
        """获取工具信息"""
        return self._tool_info.get(name)

    def list_tools(self) -> list[dict]:
        # 返回全部工具的元数据列表，供 UI/发现接口枚举
        """列出所有工具"""
        return list(self._tool_info.values())

    def has_tool(self, name: str) -> bool:
        # 判断某工具是否已注册
        """检查工具是否存在"""
        return name in self._tools

    async def execute(self, name: str, **kwargs: Any) -> Any:
        # 执行指定工具：未注册则抛出 ValueError
        """执行工具"""
        tool = self.get(name)
        if not tool:
            raise ValueError(f"Tool '{name}' not found")

        result = tool(**kwargs)
        # 支持异步工具
        # 若返回值是 awaitable（协程等），则 await 后再返回，从而兼容同步/异步两种工具
        if hasattr(result, "__await__"):
            result = await result
        return result


# 全局工具注册表
# 模块级单例：整个进程共享一份注册表实例
_global_registry = ToolRegistry()


def get_global_registry() -> ToolRegistry:
    # 返回全局唯一的工具注册表单例
    """获取全局工具注册表"""
    return _global_registry


def register_tool(
    name: str,
    description: str = "",
    parameters: Optional[dict] = None,
) -> Callable:
    # 工具注册装饰器工厂：用于以声明式方式把函数注册进全局注册表
    """工具注册装饰器"""

    def decorator(func: Callable) -> Callable:
        # 装饰时把被装饰函数写入全局注册表，随后原样返回该函数（不改变其行为）
        _global_registry.register(name, func, description, parameters)
        return func

    return decorator
