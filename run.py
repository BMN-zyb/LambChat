"""Application entry point."""

# 真正的启动逻辑在 main.py 的 run() 函数中；本文件只是另一层可直接执行的薄封装，
# 效果等价于 `python main.py`（生产环境 Dockerfile 的 CMD 与 Makefile 实际使用的是 main.py）
from main import run

# 支持 `python run.py` 直接启动服务
if __name__ == "__main__":
    run()
