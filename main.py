"""LambChat 后端服务启动入口。

这是项目的主要可执行入口：Dockerfile 的 CMD 与 Makefile 的 `make dev`
都是直接执行 `python main.py`；run.py 则是另一层薄封装，效果等价。
职责很单一——读取配置并用 uvicorn 启动 src/api/main.py 中定义的
FastAPI 应用（"src.api.main:app"），本文件不包含任何业务逻辑。
"""


def run() -> None:
    """Start the application server."""
    # 延迟导入：只有真正调用 run() 启动服务时才加载 uvicorn，避免模块被其它地方 import 时产生额外开销
    import uvicorn

    # 延迟导入配置对象，读取 PORT/DEBUG 等运行期参数（来自 .env / 环境变量，详见 src/kernel/config）
    from src.kernel.config import settings

    # 启动 ASGI 服务器：加载并运行 src/api/main.py 中构建的 FastAPI app 实例
    uvicorn.run(
        "src.api.main:app",
        # 监听所有网络接口，Docker/K8s 容器场景下才能从容器外部访问
        host="0.0.0.0",
        port=settings.PORT,
        # 仅在 DEBUG=True 时开启代码热重载；生产环境应保持关闭
        reload=settings.DEBUG,
        log_level="info",
        # 收到停止信号后最多等待 30 秒让正在处理的请求完成再强制关闭，便于滚动发布/优雅停机
        timeout_graceful_shutdown=30,
        # 即使 reload=True 在生产环境也不影响，DEBUG 控制
    )


# 支持 `python main.py` 直接启动；Dockerfile 的 CMD 与 Makefile 的 `make dev` 都使用这种方式
if __name__ == "__main__":
    run()
