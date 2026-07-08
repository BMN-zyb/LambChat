"""在 E2B 中创建自定义模板，预装 pip 包和系统依赖

用法:
    python scripts/create_e2b_template.py

该脚本会:
1. 基于 code-interpreter-v1 模板
2. 安装系统依赖 (apt-get)
3. 安装额外的 pip 包
4. 构建名为 "lambchat" 的自定义模板

构建完成后，在 .env 中设置 E2B_TEMPLATE=lambchat 即可使用。
"""

# 标准库：os 用于拼接路径，sys 用于在校验失败时以非零退出码结束进程
import os
import sys

# 导入 settings
# 本脚本以 `python scripts/xxx.py` 方式直接执行，项目根目录默认不在 sys.path 中，
# 需要手动把上一级目录（项目根）插入 sys.path，才能正常 import src.kernel.config
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.kernel.config import settings

# ============== 配置区域 ==============
# 自定义模板名称
TEMPLATE_ALIAS = "lambchat-prod"

# ============== pip 包 ==============
# 与 create_daytona_snapshot.py 中的清单类似，用于让 Agent 在 Daytona / E2B
# 两种可切换的沙箱后端中获得基本一致的代码执行环境
EXTRA_PIP_PACKAGES = [
    # ========== 数据处理 ==========
    "pandas",
    "openpyxl",
    "xlrd",
    "xlsxwriter",
    "python-docx",
    "python-pptx",
    # ========== 文档格式 ==========
    "markdown",
    "mistune",
    "markdown2",
    "pypdf",
    "PyPDF2",
    "reportlab",
    "fpdf",
    # ========== 其他常用 ==========
    "Pillow",
    "Pygments",
    "jinja2",
    "pyyaml",
    "toml",
    "json5",
    # ========== 网络请求 ==========
    "httpx",
    "aiohttp",
    "requests",
    "urllib3",
    "python-multipart",
    # ========== 数据可视化 ==========
    "matplotlib",
    "seaborn",
    "plotly",
    # ========== 加密/安全 ==========
    "cryptography",
    "pycryptodome",
    "python-jose",
    "passlib",
    "bcrypt",
    # ========== SVG 转换 ==========
    "cairosvg",
    "svglib",
    # ========== 办公文档高级 ==========
    "docx2txt",
    "xhtml2pdf",
    "pdfminer.six",
    "pdfplumber",
    # ========== 日期时间 ==========
    "python-dateutil",
    "pytz",
    "arrow",
    # ========== 压缩/归档 ==========
    "rarfile",
    "py7zr",
    # ========== 数据验证 ==========
    "pydantic",
    "email-validator",
    # ========== Office 协作 ==========
    "python-calamine",
    # ========== 异步编程 ==========
    "aiofiles",
    "asyncpg",
    "motor",
    # ========== CLI/命令行 ==========
    "click",
    "typer",
    "rich",
    "colorama",
    # ========== 文本处理/NLP ==========
    "beautifulsoup4",
    "lxml",
    "jieba",
    "snownlp",
    # ========== 调试/日志 ==========
    "loguru",
    # ========== 浏览器自动化 ==========
    "playwright",
    "selenium",
    # ========== 实用工具 ==========
    "python-dotenv",
    "orjson",
    # ========== 视频配音 ==========
    "moviepy",
    "pydub",
]

# ============== 系统依赖 ==============
# 部分 pip 包（如 matplotlib）以及 Playwright/Chromium 依赖系统级动态库或字体，
# pip 无法安装，需要在模板构建阶段用 apt-get 装好
SYSTEM_PACKAGES = [
    # 常用工具
    "git",
    "curl",
    "unzip",
    "p7zip-full",
    # 中文字体
    "fonts-noto-cjk",
    "fonts-wqy-zenhei",
    "fonts-wqy-microhei",
    # 视频处理
    "ffmpeg",
    # PDF 相关
    "poppler-utils",
    "pandoc",
    # Python 编译依赖
    "pkg-config",
    "libcairo2-dev",
    "libjpeg-dev",
    "libpng-dev",
    "libfreetype6-dev",
    "libffi-dev",
    "libssl-dev",
    # Playwright / Chromium 系统依赖
    "libnss3",
    "libnspr4",
    "libatk1.0-0",
    "libatk-bridge2.0-0",
    "libcups2",
    "libdrm2",
    "libxkbcommon0",
    "libxcomposite1",
    "libxdamage1",
    "libxfixes3",
    "libxrandr2",
    "libgbm1",
    "libpango-1.0-0",
    "libcairo2",
    "libasound2",
    "libatspi2.0-0",
    "libwayland-client0",
]

# ============== 资源配额 ==============
# Hobby 免费计划限制: 8 vCPU, 8GB RAM, 10GB disk (https://e2b.dev/docs/billing)
# 下面两个值会传给 Template.build()，用于指定该模板对应沙箱的机器规格
CPU_COUNT = 2
MEMORY_MB = 4096
# ======================================


def main():
    """读取配置并调用 E2B API 构建自定义代码执行模板。

    副作用：会通过网络请求 E2B 云服务，实际执行 apt/pip 安装命令并生成、注册一个
    新的沙箱模板（按 TEMPLATE_ALIAS 命名），可能耗时数分钟，并产生 E2B 侧的
    构建资源与计费。构建完成后需要在 .env 中设置 E2B_TEMPLATE=<alias> 才会生效。
    """
    # 延迟导入 e2b 相关 SDK：只有真正执行构建时才需要这两个依赖
    from e2b import default_build_logger
    from e2b_code_interpreter import Template

    e2b_api_key = settings.E2B_API_KEY
    # 尽早校验必需的 API Key，未配置直接退出，避免走到后面发起网络请求才失败
    if not e2b_api_key:
        print("Error: E2B_API_KEY is not set in settings.")
        sys.exit(1)

    # 打印本次运行的关键配置，方便确认无误；真正的网络请求/副作用发生在函数后半段
    print(f"Creating E2B template: {TEMPLATE_ALIAS}")
    print(f"Pip packages: {len(EXTRA_PIP_PACKAGES)}")
    print(f"System packages: {len(SYSTEM_PACKAGES)}")
    print(f"Resources: {CPU_COUNT} vCPU, {MEMORY_MB}MB RAM")

    # 定义模板
    # 以 E2B 官方预置的 code-interpreter-v1（内置 Jupyter 内核等）为基础镜像继续扩展
    template = Template().from_template("code-interpreter-v1")

    # 安装系统依赖
    if SYSTEM_PACKAGES:
        apt_cmd = (
            f"sudo apt-get update && "
            f"sudo apt-get install -y {' '.join(SYSTEM_PACKAGES)} && "
            f"sudo rm -rf /var/lib/apt/lists/*"
        )
        template = template.run_cmd(apt_cmd)

    # 安装 pip 包
    if EXTRA_PIP_PACKAGES:
        template = template.pip_install(EXTRA_PIP_PACKAGES)

    # 安装 Playwright Chromium 浏览器
    template = template.run_cmd("playwright install chromium --with-deps")

    # 安装 mcporter（用于沙箱内 MCP 服务器管理）+ opencli（网站转 CLI 工具）
    # 基础模板 code-interpreter-v1 已自带 Node.js 20 + npm，无需额外安装
    template = template.run_cmd("sudo npm install -g mcporter @jackwener/opencli")
    template = template.run_cmd("mkdir -p ~/.mcporter")

    print("\nBuilding template (this may take a few minutes)...\n")

    # 以下为本脚本真正产生副作用的地方：调用 E2B 云端 API 构建并注册模板，
    # 可能耗时数分钟，并占用 E2B 账户侧的构建资源与计费额度
    try:
        Template.build(
            template,
            alias=TEMPLATE_ALIAS,
            cpu_count=CPU_COUNT,
            memory_mb=MEMORY_MB,
            api_key=e2b_api_key,
            on_build_logs=default_build_logger(),
        )
        print(f"\nTemplate '{TEMPLATE_ALIAS}' built successfully!")
        print(f"Set E2B_TEMPLATE={TEMPLATE_ALIAS} in your .env to use it.")
    except KeyboardInterrupt:
        print("\n\nBuild cancelled by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\n\nError building template: {e}")
        sys.exit(1)


# 支持直接执行：`python scripts/create_e2b_template.py`
if __name__ == "__main__":
    main()
