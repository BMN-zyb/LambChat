"""Shared grep helpers for sandbox backends."""

from __future__ import annotations

import shlex

from deepagents.backends.protocol import ExecuteResponse, GrepMatch

# grep 默认超时秒数。
_DEFAULT_GREP_TIMEOUT = 30
# ripgrep 的排除 glob(以 ! 开头表示排除):跳过依赖/构建产物等无搜索价值的目录。
_EXCLUDED_GLOB_PATTERNS = (
    "!node_modules/**",
    "!.git/**",
    "!dist/**",
    "!build/**",
    "!.venv/**",
    "!venv/**",
    "!__pycache__/**",
)
# 传统 grep 的 --exclude-dir 目录列表(与上面 ripgrep 的排除项对应)。
_EXCLUDED_GREP_DIRECTORIES = (
    "node_modules",
    ".git",
    "dist",
    "build",
    ".venv",
    "venv",
    "__pycache__",
)
# 传统 grep 的 --exclude 文件名模式(如 TS 增量编译缓存)。
_EXCLUDED_GREP_FILES = ("*.tsbuildinfo",)


def _join_shell_args(args: list[str]) -> str:
    # 用 shlex.quote 对每个参数做 shell 转义后再空格拼接,防止路径/模式中的特殊字符被 shell 误解析。
    return " ".join(shlex.quote(arg) for arg in args)


def get_sandbox_grep_timeout(settings_obj: object) -> int:
    """Return the configured grep timeout with a stable fallback."""
    # 读取配置的 grep 超时;取不到或非法则回退默认值,并保证下限为 1 秒。
    value = getattr(settings_obj, "SANDBOX_GREP_TIMEOUT", _DEFAULT_GREP_TIMEOUT)
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_GREP_TIMEOUT
    return max(1, timeout)


def build_grep_command(pattern: str, path: str | None = None, glob: str | None = None) -> str:
    """Build a literal recursive grep command optimized for large code repositories."""
    # 生成一条既能用 ripgrep(rg)又能回退到传统 grep 的递归「字面量」搜索命令(-F/-fixed-strings)。
    # 未指定路径时默认搜当前目录。
    search_path = path or "."
    # 组装 ripgrep 参数:-n 行号 -H 文件名 --no-heading 不分组 --color=never 无颜色
    # --no-messages 屏蔽报错 -F 字面量匹配;可选 glob 叠加在排除 glob 之前。
    rg_globs = ([glob] if glob else []) + list(_EXCLUDED_GLOB_PATTERNS)
    rg_args = [
        "rg",
        "-nH",
        "--no-heading",
        "--color=never",
        "--no-messages",
        "-F",
    ]
    for rg_glob in rg_globs:
        rg_args.extend(("-g", rg_glob))
    rg_args.extend((pattern, search_path))

    # 组装传统 grep 参数:-r 递归 -H 文件名 -n 行号 -I 忽略二进制 -F 字面量;
    # --include 限定文件、--exclude-dir/--exclude 排除目录与文件。
    grep_args = ["grep", "-rHnIF"]
    if glob:
        grep_args.append(f"--include={glob}")
    grep_args.extend(f"--exclude-dir={directory}" for directory in _EXCLUDED_GREP_DIRECTORIES)
    grep_args.extend(f"--exclude={file_name}" for file_name in _EXCLUDED_GREP_FILES)
    grep_args.extend(("-e", pattern, search_path))

    rg_command = _join_shell_args(rg_args)
    # grep 分支把 stderr 重定向丢弃(2>/dev/null),避免权限等噪声输出干扰解析。
    grep_command = f"{_join_shell_args(grep_args)} 2>/dev/null"
    # 运行期探测:有 rg 就用 rg,否则用 grep;两分支都以 `|| true` 收尾,
    # 使「无匹配」(grep 退出码 1)不会被当作命令失败。
    return (
        "if command -v rg >/dev/null 2>&1; "
        f"then {rg_command} || true; "
        f"else {grep_command} || true; "
        "fi"
    )


def parse_grep_response(result: ExecuteResponse, timeout: int) -> list[GrepMatch] | str:
    """Parse grep output or surface a user-facing timeout error."""
    output = result.output.rstrip()
    # 超时约定:退出码 -1 且输出含 "timed out",则返回一句面向用户的错误提示(而非匹配列表)。
    if result.exit_code == -1 and "timed out" in output.lower():
        return f"Error: grep timed out after {timeout}s. Try a more specific pattern or a narrower path."

    if not output:
        return []

    # 逐行解析 "path:line:text" 格式(text 里可能含冒号,故 split 限定最多切 3 段);
    # 段数不足或行号非数字的行直接跳过。
    matches: list[GrepMatch] = []
    for line in output.split("\n"):
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        try:
            line_number = int(parts[1])
        except ValueError:
            continue
        matches.append({"path": parts[0], "line": line_number, "text": parts[2]})

    return matches
