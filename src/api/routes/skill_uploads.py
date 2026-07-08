"""ZIP parsing helpers for the Skills API."""

# 技能（Skill）ZIP 解析辅助模块：供 Skills API 处理上传的技能压缩包。
# 一个 ZIP 内可包含多个技能——以每个 SKILL.md 所在目录作为一个独立技能的根。
# 核心职责：
#   - 安全：拦截目录穿越/绝对路径/盘符路径（_normalize_zip_member_path），
#     并跳过 __MACOSX、.DS_Store、Thumbs.db、.git 等无关条目；
#   - 限额：限制 ZIP 体积、成员数量、解压后总大小与单个成员大小（防 zip bomb）；
#   - 解析：区分文本/二进制文件，抽取技能名与描述，产出预览或完整文件字典。
import io
import re
import zipfile

from src.api.routes.upload import get_s3_enabled
from src.infra.skill.binary import is_binary_file
from src.kernel.config import settings

# 单个 ZIP 成员的大小上限；None 表示不额外限制，退回到使用整体大小上限
_ZIP_MEMBER_MAX_BYTES: int | None = None
# 单个 ZIP 允许包含的最大成员数量（防止过多小文件耗尽资源）
_ZIP_MAX_MEMBERS = 500


# 判断某个 ZIP 成员是否应被忽略：目录项、macOS 打包垃圾（__MACOSX/.DS_Store）、
# Windows 缩略图缓存（Thumbs.db）以及 .git 目录内的内容
def _zip_member_should_skip(name: str) -> bool:
    return (
        name.endswith("/")
        or "__MACOSX" in name
        or name.endswith(".DS_Store")
        or name.endswith("Thumbs.db")
        or ".git/" in name
    )


# 若整个 ZIP 只有一个顶层目录（如 awesome-skills/...），返回该前缀 "awesome-skills/"
# 以便后续剥离；否则返回空串。用于处理“压缩时多套了一层目录”的常见情况。
def _strip_single_top_level_prefix(names: list[str]) -> str:
    top_level = set()
    for name in names:
        parts = name.split("/")
        if parts[0]:
            top_level.add(parts[0])
    # 顶层目录不唯一（0 个或多个）则不做剥离
    if len(top_level) != 1:
        return ""
    top = next(iter(top_level))
    return f"{top}/" if any(name.startswith(f"{top}/") for name in names) else ""


# 归一化并安全校验 ZIP 成员路径（防目录穿越）：
#   - 统一反斜杠为正斜杠；
#   - 拒绝绝对路径（/ 开头）、Windows 盘符路径（C:/）、含 ".." 路径段的危险路径；
#   - 若提供了 prefix，则剥离该前缀（不匹配前缀的成员返回 None 表示丢弃）；
#   - 剥离后再次做同样的安全校验，最终返回安全的相对路径。
def _normalize_zip_member_path(name: str, prefix: str) -> str | None:
    normalized_name = name.replace("\\", "/")
    # 第一次校验：原始路径不得为绝对/盘符/含 ".." 的危险路径
    if (
        normalized_name.startswith("/")
        or re.match(r"^[A-Za-z]:/", normalized_name)
        or ".." in normalized_name.split("/")
    ):
        raise ValueError(f"Unsafe ZIP member path: {name}")
    if prefix:
        # 不在指定前缀下的成员直接丢弃；否则剥离该前缀
        if not normalized_name.startswith(prefix):
            return None
        normalized_name = normalized_name[len(prefix) :]
    if not normalized_name:
        return None
    # 第二次校验：剥离前缀后仍需保证安全（防止剥离后暴露出穿越路径）
    if (
        normalized_name.startswith("/")
        or re.match(r"^[A-Za-z]:/", normalized_name)
        or ".." in normalized_name.split("/")
    ):
        raise ValueError(f"Unsafe ZIP member path: {name}")
    return normalized_name


# 归一化技能内文件路径：统一分隔符，并把文件名 skill.md（任意大小写）统一为 SKILL.md，
# 使不同大小写写法的清单文件都能被后续识别
def _normalize_skill_file_path(path: str) -> str:
    parts = path.replace("\\", "/").split("/")
    if parts and parts[-1].lower() == "skill.md":
        parts[-1] = "SKILL.md"
    return "/".join(parts)


# 计算技能 ZIP 上传的大小上限，返回 (字节数, 兆字节数)。
# 启用 S3 时用 S3_MAX_FILE_SIZE（单位字节）；否则用文档类上限（配置单位为 MB，需换算）。
def _get_skill_upload_max_size() -> tuple[int, int]:
    if get_s3_enabled():
        max_size_bytes = int(settings.S3_MAX_FILE_SIZE)
    else:
        max_size_bytes = int(settings.FILE_UPLOAD_MAX_SIZE_DOCUMENT) * 1024 * 1024
    return max_size_bytes, max_size_bytes // (1024 * 1024)


# 从 SKILL.md 内容解析技能名与描述：优先取清单里的 name/description（name 会做安全化），
# 解析失败或缺失时回退到目录名（fallback_name 的最后一段），最终兜底为 "unnamed-skill"。
def _parse_skill_name_description(skill_md_content: str, fallback_name: str) -> tuple[str, str]:
    skill_name = None
    description = ""
    if skill_md_content:
        try:
            from src.infra.skill.parser import (
                parse_skill_md,
                sanitize_skill_name,
            )

            parsed_name, parsed_desc, _ = parse_skill_md(skill_md_content)
            if parsed_name:
                skill_name = sanitize_skill_name(parsed_name)
            if parsed_desc:
                description = parsed_desc
        except Exception:
            # 解析失败不致命，后续用回退名兜底
            pass
    if not skill_name and fallback_name:
        skill_name = fallback_name.split("/")[-1]
    return skill_name or "unnamed-skill", description


# 打开并校验上传的 ZIP，返回 (ZipFile 对象, 成员信息列表, 大小上限)。
# 多重限额防护（含 zip bomb 防护）：压缩包体积、成员数量、解压后总大小三者均设上限。
def _validate_zip_upload(zip_content: bytes) -> tuple[zipfile.ZipFile, list[zipfile.ZipInfo], int]:
    max_file_size, _max_file_size_mb = _get_skill_upload_max_size()
    # 压缩包本身的字节数上限
    if len(zip_content) > max_file_size:
        raise ValueError("ZIP file too large")

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_content))
    except zipfile.BadZipFile:
        raise ValueError("Invalid ZIP file")

    try:
        infos = zf.infolist()
        # 成员数量上限
        if len(infos) > _ZIP_MAX_MEMBERS:
            raise ValueError(f"ZIP contains too many files (max {_ZIP_MAX_MEMBERS})")

        # 解压后总大小上限：防止高压缩比的 zip bomb 撑爆内存/磁盘
        total_uncompressed_size = sum(info.file_size for info in infos)
        if total_uncompressed_size > max_file_size:
            max_file_size_mb = max_file_size // (1024 * 1024)
            raise ValueError(f"ZIP uncompressed content too large (max {max_file_size_mb}MB)")
        return zf, infos, max_file_size
    except Exception:
        # 校验中途出错要关闭已打开的 ZipFile，避免资源泄漏
        zf.close()
        raise


# 在不真正落库的前提下预览 ZIP 内包含哪些技能：返回每个技能的名称、描述、
# 文件数量、文件列表与二进制文件列表，供前端在安装前展示确认。
def _parse_zip_skill_preview(zip_content: bytes) -> list[dict]:
    zf, infos, max_file_size = _validate_zip_upload(zip_content)
    try:
        names = [info.filename for info in infos]
        info_by_name = {info.filename: info for info in infos}
        # 剥离可能存在的单一顶层目录前缀
        prefix = _strip_single_top_level_prefix(names)
        member_max_size = _ZIP_MEMBER_MAX_BYTES or max_file_size
        valid_paths: list[str] = []
        binary_paths: set[str] = set()
        skill_md_by_path: dict[str, str] = {}

        # 逐个成员：跳过无关项 → 安全归一化路径 → 校验单成员大小 → 记录路径/二进制/清单
        for name in names:
            if _zip_member_should_skip(name):
                continue
            rel_path = _normalize_zip_member_path(name, prefix)
            if not rel_path:
                continue
            info = info_by_name.get(name)
            # 单个成员大小上限校验
            if info and info.file_size > member_max_size:
                raise ValueError(
                    f"ZIP member too large: {name} "
                    f"({info.file_size} bytes, max {member_max_size} bytes)"
                )
            rel_path = _normalize_skill_file_path(rel_path)
            valid_paths.append(rel_path)
            if is_binary_file(rel_path):
                binary_paths.add(rel_path)
            # 遇到 SKILL.md 时读取其文本内容（要求 UTF-8）作为技能清单
            if rel_path.split("/")[-1].lower() == "skill.md":
                try:
                    skill_md_by_path[rel_path] = zf.read(name).decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError(f"SKILL.md must be UTF-8 text: {rel_path}") from exc

        # 没有任何 SKILL.md 即视为无效技能包
        if not skill_md_by_path:
            raise ValueError("No SKILL.md found in ZIP")

        # 每个 SKILL.md 对应一个技能：以其所在目录为根，收集该根下的相对路径文件
        previews: list[dict] = []
        for skill_md_path, skill_md_content in skill_md_by_path.items():
            skill_root = skill_md_path.rsplit("/", 1)[0] if "/" in skill_md_path else ""
            skill_prefix = skill_root + "/" if skill_root else ""
            files = [
                path[len(skill_prefix) :]
                for path in valid_paths
                if path.startswith(skill_prefix) and path[len(skill_prefix) :]
            ]
            if not files:
                continue
            skill_name, description = _parse_skill_name_description(
                skill_md_content,
                skill_root,
            )
            binary_files = sorted(
                path[len(skill_prefix) :]
                for path in binary_paths
                if path.startswith(skill_prefix) and path[len(skill_prefix) :]
            )
            previews.append(
                {
                    "name": skill_name,
                    "description": description,
                    "file_count": len(files),
                    "files": sorted(files),
                    "binary_files": binary_files,
                }
            )

        if not previews:
            raise ValueError("No valid skills found in ZIP")
        return previews
    finally:
        # 预览完成后务必关闭 ZipFile
        zf.close()


def _parse_zip_skills(
    zip_content: bytes,
) -> list[tuple[str, dict[str, str], dict[str, bytes]]]:
    """
    解析 ZIP 内容，找到所有 SKILL.md 文件，每个 SKILL.md 的上级文件夹作为一个独立 skill。

    Returns:
        list of (skill_name, text_files_dict, binary_files_dict) tuples
    """
    zf, infos, max_file_size = _validate_zip_upload(zip_content)
    try:
        names = [info.filename for info in infos]
        info_by_name = {info.filename: info for info in infos}
        member_max_size = _ZIP_MEMBER_MAX_BYTES or max_file_size

        # 检测并去掉单顶层目录前缀（如 awesome-claude-skills/xxx → xxx）
        prefix = _strip_single_top_level_prefix(names)

        # 读取所有有效文件，区分文本和二进制
        text_files: dict[str, str] = {}
        binary_files: dict[str, bytes] = {}
        for name in names:
            if _zip_member_should_skip(name):
                continue
            info = info_by_name.get(name)
            # 单个成员大小上限校验（与预览逻辑保持一致）
            if info and info.file_size > member_max_size:
                raise ValueError(
                    f"ZIP member too large: {name} "
                    f"({info.file_size} bytes, max {member_max_size} bytes)"
                )
            # 读取失败的成员跳过（不因单个坏条目导致整体解析失败）
            try:
                raw = zf.read(name)
            except Exception:
                continue

            # 检测二进制文件
            if is_binary_file(name, raw):
                binary_files[name] = raw
            else:
                try:
                    text = raw.decode("utf-8")
                    text_files[_normalize_skill_file_path(name)] = text
                except UnicodeDecodeError:
                    # 即使通过了扩展名检查，UTF-8 解码失败也当二进制
                    binary_files[name] = raw

        # 去掉顶层目录前缀
        if prefix:
            text_files = {
                _normalize_skill_file_path(normalized): content
                for key, content in text_files.items()
                if (normalized := _normalize_zip_member_path(key, prefix))
            }
            binary_files = {
                normalized: data
                for key, data in binary_files.items()
                if (normalized := _normalize_zip_member_path(key, prefix))
            }

        # 找到所有 SKILL.md 的路径
        skill_md_paths = [p for p in text_files.keys() if p.split("/")[-1].lower() == "skill.md"]

        if not skill_md_paths:
            raise ValueError("No SKILL.md found in ZIP")

        skills: list[tuple[str, dict[str, str], dict[str, bytes]]] = []

        for skill_md_path in skill_md_paths:
            # SKILL.md 所在的文件夹就是 skill 的根目录
            skill_root = skill_md_path.rsplit("/", 1)[0] if "/" in skill_md_path else ""
            skill_prefix = skill_root + "/" if skill_root else ""

            # 收集该 skill 根目录下的所有文件（相对路径）
            skill_text_files: dict[str, str] = {}
            for fpath, content in text_files.items():
                if fpath.startswith(skill_prefix):
                    rel = fpath[len(skill_prefix) :]
                    if rel:
                        skill_text_files[rel] = content

            skill_binary_files: dict[str, bytes] = {}
            for fpath, data in binary_files.items():
                if fpath.startswith(skill_prefix):
                    rel = fpath[len(skill_prefix) :]
                    if rel:
                        skill_binary_files[rel] = data

            # 优先使用 SKILL.md 的 name 字段，回退到文件夹名
            skill_md_content = skill_text_files.get("SKILL.md", "")
            skill_name, _description = _parse_skill_name_description(skill_md_content, skill_root)

            # 该 skill 根目录下有任意文件才收录为一个技能
            if skill_text_files or skill_binary_files:
                skills.append((skill_name, skill_text_files, skill_binary_files))

        if not skills:
            raise ValueError("No valid skills found in ZIP")

        return skills
    finally:
        # 解析完成后务必关闭 ZipFile
        zf.close()
