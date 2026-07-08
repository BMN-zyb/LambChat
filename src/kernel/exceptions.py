"""
异常定义

定义系统中使用的所有自定义异常。
"""


# Agent（智能体）相关错误的基类；注意：尽管排在文件最前面、类名和下面的
# docstring 看起来像是要作为其它异常的公共基类，但实际上后面的
# ConfigurationError/ValidationError/NotFoundError/AuthenticationError/
# AuthorizationError/StorageError/LLMError/ToolError/SkillError/SessionError
# 全部是直接继承 Exception，与 AgentError 是平级关系，并不是它的子类，
# 代码中并没有真正形成继承层次，阅读时注意不要被名称和排列顺序误导。
class AgentError(Exception):
    """Agent 相关错误基类"""

    pass


# 配置错误：配置加载、解析或校验失败时抛出，例如缺少必需的配置项、
# 配置文件格式错误、环境变量取值不合法等场景。
class ConfigurationError(Exception):
    """配置错误"""

    pass


# 验证错误：输入参数或数据校验不通过时抛出，用于业务逻辑层面的合法性检查失败场景。
class ValidationError(Exception):
    """验证错误"""

    pass


# 资源未找到错误：按 ID 或条件查询资源（如用户、会话、文档等）但查询不到时抛出。
class NotFoundError(Exception):
    """资源未找到错误"""

    pass


# 认证错误：身份认证失败时抛出，例如登录凭证错误、Token 缺失/无效/已过期等场景。
class AuthenticationError(Exception):
    """认证错误"""

    pass


# 授权错误：权限校验不通过时抛出，例如用户尝试访问或操作超出自己权限范围的资源。
class AuthorizationError(Exception):
    """授权错误"""

    pass


# 存储错误：底层存储（数据库、对象存储、缓存等）读写操作失败时抛出。
class StorageError(Exception):
    """存储错误"""

    pass


# LLM 调用错误：调用大语言模型接口失败时抛出，例如请求超时、返回内容异常、额度或频率超限等。
class LLMError(Exception):
    """LLM 调用错误"""

    pass


# 工具执行错误：Agent 调用外部工具（Tool/Function Call）执行失败时抛出。
class ToolError(Exception):
    """工具执行错误"""

    pass


# 技能相关错误：Agent 的技能（Skill）在加载或执行过程中出错时抛出。
class SkillError(Exception):
    """技能相关错误"""

    pass


# 会话相关错误：会话（Session）创建、读取或状态维护过程中出错时抛出。
class SessionError(Exception):
    """会话相关错误"""

    pass


# 邮箱未验证错误：在 src/infra/user/manager.py 的登录认证逻辑中，当
# settings.REQUIRE_EMAIL_VERIFICATION 为开启状态且用户邮箱尚未验证
# （user.email_verified 为 False）时抛出，例如：
# raise EmailNotVerifiedError("请先验证邮箱后再登录", user.email)
# 与前面的异常不同，这里没有直接 pass 继承默认行为，而是自定义了
# __init__，在保留异常消息的同时额外携带 email 字段，把"是哪个邮箱
# 触发了未验证"这一信息一并传递给调用方。
# 调用方 src/api/routes/auth/core.py 目前并不是用
# except EmailNotVerifiedError: 这种标准的类型捕获方式，而是通过
# type(e).__name__ 类名字符串匹配、以及异常消息文本中是否包含
# "请先验证邮箱" 来识别这个异常，再转换成对应的 403 HTTP 响应返回给前端。
class EmailNotVerifiedError(Exception):
    """邮箱未验证错误"""

    # 自定义构造函数：在保留标准异常消息的同时，额外接收并保存 email 参数
    def __init__(self, message: str, email: str):
        # 调用父类 Exception 的构造函数，设置标准异常消息文本
        super().__init__(message)
        # 把触发异常的用户邮箱挂在实例上，方便调用方捕获后取用
        self.email = email


# 账户未激活错误：在 src/infra/user/manager.py 的登录认证逻辑中，当
# 用户账户被禁用（user.is_active 为 False）时抛出，例如：
# raise AccountNotActiveError("账户未激活，请验证邮箱后登录", user.email)
# 同样自定义了 __init__ 携带 email 字段，设计意图与
# EmailNotVerifiedError 一致：把触发异常的用户邮箱一并传递下去。
# 调用方 src/api/routes/auth/core.py 同样是通过 type(e).__name__
# 类名字符串匹配（以及异常消息文本中是否包含"账户未激活"）来识别，
# 而不是标准的 except AccountNotActiveError: 类型捕获，识别后转换为 403 HTTP 响应。
class AccountNotActiveError(Exception):
    """账户未激活错误"""

    # 自定义构造函数：用法与 EmailNotVerifiedError 相同，额外携带 email 参数
    def __init__(self, message: str, email: str):
        # 调用父类 Exception 的构造函数，设置标准异常消息文本
        super().__init__(message)
        # 把触发异常的用户邮箱挂在实例上，方便调用方捕获后取用
        self.email = email
