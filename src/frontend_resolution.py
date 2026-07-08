"""决定 FastAPI 后端如何向浏览器提供前端页面。

被 src/api/main.py 在应用启动时调用一次：根据当前部署环境（是否已有
构建产物、是否配置了前端开发服务器）选择"挂载静态文件目录"或
"重定向到前端开发服务器"，从而让同一份后端代码同时适配生产部署
（前端已构建）和本地开发（前端由 Vite 等单独进程提供）两种场景。
"""

from __future__ import annotations

from pathlib import Path


def resolve_frontend_target(
    project_root: Path, frontend_dev_url: str
) -> tuple[str, Path | str] | None:
    """决定后端应以哪种方式对外提供前端页面/静态资源。

    按优先级依次探测三种情形：
    1. 项目根目录下是否已有构建产物 `static/`（通常是 Docker 镜像构建时
       把前端打包结果拷贝到这里，生产环境走这条路径）；
    2. 是否存在 `frontend/dist/`（本地执行过 `npm run build` 但没有拷到
       static/ 下）；
    3. 若以上两者都不存在，且配置了前端开发服务器地址（settings.FRONTEND_DEV_URL），
       则让调用方把请求重定向过去（本地开发时前端由独立的 Vite 进程提供）。
    三者都不满足则返回 None，调用方应放弃挂载前端相关路由（纯 API 模式）。

    Args:
        project_root: 项目根目录，用于拼接 static/、frontend/dist 等候选路径
        frontend_dev_url: 前端开发服务器地址；空字符串表示未配置

    Returns:
        ("static", 静态资源目录 Path)：调用方应将该目录挂载为静态文件服务；
        ("redirect", 目标 URL 字符串)：调用方应将请求重定向到该地址；
        None：以上均不满足
    """
    # 优先级最高：生产构建产物目录（如 Docker 镜像内已拷贝好的前端产物）
    static_dir = project_root / "static"
    if static_dir.exists():
        return ("static", static_dir)

    # 其次：本地开发时在 frontend/ 下执行 `npm run build` 生成的产物
    frontend_dist = project_root / "frontend" / "dist"
    if frontend_dist.exists():
        return ("static", frontend_dist)

    # 两种静态产物都不存在时，退而使用配置的前端开发服务器地址（如 Vite dev server）
    if frontend_dev_url.strip():
        return ("redirect", frontend_dev_url.rstrip("/"))

    # 既没有构建产物，也没有配置开发服务器地址：不提供前端
    return None
