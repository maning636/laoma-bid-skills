from __future__ import annotations

"""
暗标清洗检查 Skill
=================

针对"暗标（匿名评标）"场景，扫描投标文件（.docx）中残留的身份信息，
避免因为作者名、公司名、页眉页脚 Logo 或文档属性等残留信息导致直接废标。

四类检查项：
1. 文档属性元数据 (core_properties)
2. 页眉/页脚 Logo（图片）
3. 正文公司名残留（需要传入 company_names 列表）
4. docProps XML（app.xml / core.xml 原始字段）

依赖：python-docx
"""

import asyncio
import logging
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import ClassVar

from docx import Document
from docx.oxml.ns import qn

from skill_engine.base import Skill, SkillContext, SkillResult

logger = logging.getLogger(__name__)

SKILL_NAME = "anon_bid_clean"
CHECK_TYPE = "disqualification"

# 常见"占位符 / 空值" —— 非空但不代表真实身份信息
_META_PLACEHOLDERS = {
    "", "unknown", "n/a", "na", "none", "null",
    "user", "admin", "administrator", "microsoft office user",
    "作者", "佚名", "未命名", "无",
}


def _is_identity_like(value: str | None) -> bool:
    """判断字符串是否看起来像真实身份信息（非空且非占位符）"""
    if value is None:
        return False
    v = str(value).strip()
    if not v:
        return False
    return v.lower() not in _META_PLACEHOLDERS


def _count_images_in_hf(hf) -> int:
    """
    统计 header/footer 中的图片数量。
    hf 是 python-docx 的 _Header 或 _Footer 对象。
    双重保险：XML 找 w:drawing/w:pict + part.related_parts 里 image/* 计数。
    """
    if hf is None:
        return 0

    count = 0

    # 1) XML 层：直接搜 drawing / pict
    try:
        root = hf.element  # lxml CT_HdrFtr
        count += len(root.findall(".//" + qn("w:drawing")))
        count += len(root.findall(".//" + qn("w:pict")))
    except Exception as e:  # noqa: BLE001
        # XML 解析层解析失败时降级到关系层统计，仅记录便于排查
        logger.debug(f"header/footer XML 层解析失败: {e}")

    # 2) 关系层：related_parts 里 content_type 以 image/ 开头的
    try:
        part = hf.part
        for p in getattr(part, "related_parts", {}).values():
            ct = getattr(p, "content_type", "") or ""
            if ct.startswith("image/"):
                count += 1
    except Exception as e:  # noqa: BLE001
        # related_parts 遍历失败时保持现有计数，不影响整体流程
        logger.debug(f"header/footer 关系层解析失败: {e}")

    return count


class AnonBidCleanSkill(Skill):
    """暗标清洗检查"""

    name = SKILL_NAME
    description = "暗标清洗检查：扫描 docx 中残留的公司名、Logo、作者/元数据等身份信息"
    category = "check"
    version = "1.0.0"
    triggers: ClassVar[list[str]] = ["暗标", "匿名评标", "匿名投标", "公司名残留", "Logo残留", "元数据清洗", "身份信息"]

    async def execute(self, ctx: SkillContext) -> SkillResult:
        params = ctx.parameters or {}

        file_path = params.get("file_path") or params.get("bid_file_path")
        company_names = params.get("company_names") or []
        bid_text = params.get("bid_text") or ""

        # 两种模式：
        # A. 文件模式（推荐）：传入 file_path（.docx），做完整四类检查
        # B. 文本模式（降级）：无 file_path 但有 bid_text 时，仅做正文公司名扫描
        is_file_mode = bool(file_path) and Path(file_path).exists()

        if not is_file_mode and not bid_text:
            return SkillResult(
                success=False,
                error="需要 file_path（.docx 文件路径），或者传入 bid_text 做降级扫描",
            )

        issues: list[dict] = []
        mode_note = ""

        if is_file_mode:
            if not str(file_path).lower().endswith(".docx"):
                return SkillResult(success=False, error="仅支持 .docx 文件")
            try:
                def _run_sync():
                    # to_thread：Document() 打开 docx（zip+lxml）+ 后续 XML/zipfile
                    # 扫描都是同步 IO/CPU，长文档可达秒级，不能在事件循环里跑。
                    doc = Document(file_path)
                    issues.extend(self._check_core_properties(doc))
                    issues.extend(self._check_headers_footers(doc))
                    if company_names:
                        issues.extend(self._check_body_company_names(doc, list(company_names)))
                    else:
                        issues.append({
                            "severity": "warning",
                            "category": "company_name",
                            "message": "未提供 company_names 列表，跳过正文公司名扫描",
                            "location": "parameters",
                            "suggestion": "传入 company_names=[\"公司全称\",\"公司简称\"] 以启用正文残留检测",
                        })
                    issues.extend(self._check_docprops_xml(file_path))

                await asyncio.to_thread(_run_sync)
            except Exception as e:
                logger.exception("打开 docx 失败")
                return SkillResult(success=False, error=f"无法打开 docx: {e}")
            mode_note = "（完整文件扫描模式）"
        else:
            # 文本降级模式：仅能扫描公司名，无法检查元数据/Logo/docProps
            mode_note = "（文本降级模式：仅正文公司名扫描；元数据/Logo/docProps 检查不可用）"
            names = [str(n).strip() for n in company_names if str(n).strip()]
            for idx, line in enumerate(bid_text.splitlines(), 1):
                for name in names:
                    if name and name in line:
                        issues.append({
                            "severity": "critical",
                            "category": "company_name",
                            "message": f"正文包含公司名 \"{name}\"",
                            "location": f"行 #{idx}: {line.strip()[:80]}",
                            "suggestion": "确认此段是否属于落款/封面/资质页；否则删除公司名以符合暗标要求",
                        })
                        break

        critical_count = sum(1 for i in issues if i.get("severity") == "critical")
        passed = critical_count == 0
        summary = (
            f"暗标清洗检查{mode_note}：发现 {len(issues)} 处身份信息残留（其中 {critical_count} 处高危）"
            if issues
            else f"暗标清洗检查{mode_note}：未检测到身份信息残留"
        )

        data = {
            "skill_name": SKILL_NAME,
            "check_type": CHECK_TYPE,
            "mode": "file" if is_file_mode else "text",
            "passed": passed,
            "issues": issues,
            "summary": summary,
        }
        return SkillResult(
            success=True,
            data=data,
            warnings=[f"发现{critical_count}项高危暗标违规项"] if critical_count else [],
        )

    # ---------- 1. core_properties 元数据 ----------
    def _check_core_properties(self, doc) -> list[dict]:
        issues: list[dict] = []
        try:
            cp = doc.core_properties
        except Exception as e:  # noqa: BLE001
            # doc.core_properties 读取失败时跳过该检查项，记录便于排查
            logger.warning(f"读取 core_properties 失败，跳过元数据检查: {e}")
            return issues

        # 字段名 -> (展示标签, 严重度, 修复提示)
        field_specs = [
            ("author", "作者 (Author)", "critical",
             "Word → 文件 → 信息 → 属性，清空作者后另存"),
            ("last_modified_by", "最后修改人 (LastModifiedBy)", "critical",
             "Word → 文件 → 信息 → 属性，清空最后修改人；建议用另存为去除痕迹"),
            ("company", "公司 (Company)", "critical",
             "Word → 文件 → 选项 → 高级 → 个人复选标记，清空公司名"),
            ("title", "标题 (Title)", "warning",
             "Word → 文件 → 信息 → 属性，确认标题不含身份线索"),
        ]

        for field, label, severity, suggestion in field_specs:
            try:
                value = getattr(cp, field, "")
            except Exception as e:  # noqa: BLE001
                # 单个字段读取失败不影响其他字段检查
                logger.debug(f"读取元数据字段 {field} 失败: {e}")
                continue
            if _is_identity_like(value):
                issues.append({
                    "severity": severity,
                    "category": "metadata",
                    "message": f"文档属性 {label} 包含疑似身份信息: \"{str(value).strip()}\"",
                    "location": f"docProps/core.xml · {field}",
                    "suggestion": suggestion,
                })
        return issues

    # ---------- 2. 页眉页脚 Logo ----------
    def _check_headers_footers(self, doc) -> list[dict]:
        issues: list[dict] = []
        sections = list(doc.sections) or []
        for idx, section in enumerate(sections, 1):
            for part_name in ("header", "footer"):
                hf = getattr(section, part_name, None)
                if hf is None:
                    continue
                try:
                    linked = getattr(hf, "is_linked_to_previous", False)
                except Exception as e:  # noqa: BLE001
                    # 保守起见按未链接处理，宁可重复扫描也不漏检
                    logger.debug(f"读取 header/footer is_linked_to_previous 失败: {e}")
                    linked = False
                if linked:
                    # 链接到上一节的 header/footer 不重复扫描
                    continue
                image_count = _count_images_in_hf(hf)
                if image_count > 0:
                    issues.append({
                        "severity": "critical",
                        "category": "logo",
                        "message": f"第 {idx} 节 {part_name} 中发现 {image_count} 张图片（疑似 Logo）",
                        "location": f"section {idx} / {part_name}",
                        "suggestion": "删除页眉页脚中的 Logo 图片；暗标要求页眉页脚不出现 Logo 或公司标识",
                    })
        return issues

    # ---------- 3. 正文公司名 ----------
    def _check_body_company_names(self, doc, company_names: list[str]) -> list[dict]:
        names = [str(n).strip() for n in company_names if str(n).strip()]
        if not names:
            return []
        issues: list[dict] = []
        for idx, para in enumerate(doc.paragraphs, 1):
            try:
                text = para.text or ""
            except Exception as e:  # noqa: BLE001
                # 单个段落读取失败不影响其他段落检查
                logger.debug(f"段落 #{idx} 读取失败: {e}")
                continue
            if not text.strip():
                continue
            for name in names:
                if name in text:
                    snippet = text.strip()
                    if len(snippet) > 80:
                        snippet = snippet[:80] + "..."
                    issues.append({
                        "severity": "critical",
                        "category": "company_name",
                        "message": f"正文段落包含公司名 \"{name}\"",
                        "location": f"段落 #{idx}: {snippet}",
                        "suggestion": "确认此段是否属于落款/封面/资质页；否则删除公司名以符合暗标要求",
                    })
                    break  # 单段只报一次，避免重复
        return issues

    # ---------- 4. docProps XML ----------
    def _check_docprops_xml(self, file_path: str) -> list[dict]:
        issues: list[dict] = []
        try:
            with zipfile.ZipFile(file_path) as zf:
                arcnames = set(zf.namelist())
        except Exception as e:  # noqa: BLE001
            logger.warning(f"打开 docx zip 失败: {e}")
            return issues

        try:
            with zipfile.ZipFile(file_path) as zf:
                # core.xml：dc:creator / cp:lastModifiedBy / cp:company / dc:title
                if "docProps/core.xml" in arcnames:
                    content = zf.read("docProps/core.xml")
                    root = ET.fromstring(content)
                    ns = {
                        "dc": "http://purl.org/dc/elements/1.1/",
                        "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
                    }
                    core_specs = [
                        ("dc:creator", "作者 (dc:creator)", "critical",
                         "在 Word 中打开文档 → 文件 → 信息 → 属性，清空作者后另存"),
                        ("cp:lastModifiedBy", "最后修改人 (cp:lastModifiedBy)", "critical",
                         "清空最后修改人；建议用另存为去除痕迹"),
                        ("cp:company", "公司 (cp:company)", "critical",
                         "清空文档属性中的公司名"),
                        ("dc:title", "标题 (dc:title)", "warning",
                         "确认标题不含身份线索"),
                    ]
                    for xp, label, severity, suggestion in core_specs:
                        el = root.find(xp, ns)
                        if el is None:
                            continue
                        text = (el.text or "").strip()
                        if _is_identity_like(text):
                            issues.append({
                                "severity": severity,
                                "category": "metadata",
                                "message": f"{label} 包含: \"{text}\"",
                                "location": "docProps/core.xml",
                                "suggestion": suggestion,
                            })

                # app.xml：Company（默认命名空间）
                if "docProps/app.xml" in arcnames:
                    content = zf.read("docProps/app.xml")
                    root = ET.fromstring(content)
                    app_specs = [
                        ("Company", "Company", "critical",
                         "在 Word 中打开文档 → 文件 → 选项 → 高级 → 个人复选标记，清空公司名"),
                    ]
                    for tag, label, severity, suggestion in app_specs:
                        el = root.find(tag)
                        if el is None:
                            continue
                        text = (el.text or "").strip()
                        if _is_identity_like(text):
                            issues.append({
                                "severity": severity,
                                "category": "metadata",
                                "message": f"{label} 标签包含: \"{text}\"",
                                "location": "docProps/app.xml",
                                "suggestion": suggestion,
                            })
        except Exception as e:  # noqa: BLE001
            logger.warning(f"解析 docProps 失败: {e}")

        return issues


# =====================================================================
# 自测：python -m services.check.skills.anon_bid_clean_skill
# =====================================================================
if __name__ == "__main__":
    import asyncio
    import os
    import struct
    import tempfile
    import zlib

    from skill_engine.base import SkillContext

    class _DummyLLM:
        """Skill 本体不需要 LLM，自测里放一个占位即可"""

    def _make_min_png(path: str) -> None:
        """生成一个最小合法 PNG（1x1 透明像素），用于测试页眉 Logo 检测"""
        # 结构：signature + IHDR + IDAT + IEND
        sig = b"\x89PNG\r\n\x1a\n"

        def chunk(tag: bytes, data: bytes) -> bytes:
            crc = zlib.crc32(tag + data) & 0xFFFFFFFF
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

        # IHDR: width=1 height=1 bit_depth=8 color_type=6(rgba)
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
        # IDAT: 1 行 1 像素 RGBA 数据，前置 filter byte 0
        raw = b"\x00\x00\x00\x00\x00"
        idat = zlib.compress(raw)
        with open(path, "wb") as f:
            f.write(sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b""))

    async def _run_self_test() -> None:
        tmp_dir = tempfile.mkdtemp(prefix="anon_bid_test_")
        docx_path = os.path.join(tmp_dir, "test_bid.docx")
        png_path = os.path.join(tmp_dir, "logo.png")
        _make_min_png(png_path)

        # 构造测试文档
        doc = Document()
        # 1) 元数据：作者 + 最后修改人
        doc.core_properties.author = "老马"
        doc.core_properties.last_modified_by = "老马助手"
        doc.core_properties.title = "XX公司 投标文件"

        # 2) 正文含公司名
        doc.add_paragraph("我们的项目经验如下：老马科技股份有限公司长期致力于政府信息化。")
        doc.add_paragraph("常规内容，无身份信息。")
        doc.add_paragraph("投标方：老马科技股份有限公司（此处属于落款，需人工判定）")

        # 3) 页眉加图片（Logo）
        try:
            section = doc.sections[0]
            header_p = section.header.paragraphs[0]
            header_p.add_run().add_picture(png_path, width=None)
        except Exception as e:  # noqa: BLE001
            print(f"[警告] 页眉插图失败（不影响其他测试）: {e}")

        doc.save(docx_path)

        # 调用 Skill
        skill = AnonBidCleanSkill()
        ctx = SkillContext(
            project_id="self-test",
            db=None,
            llm=_DummyLLM(),
            parameters={
                "file_path": docx_path,
                "company_names": ["老马科技股份有限公司", "老马科技"],
            },
        )
        result = await skill.safe_execute(ctx)

        print("=" * 60)
        print("暗标清洗检查 Skill 自测结果")
        print("=" * 60)
        print(f"success: {result.success}")
        if result.error:
            print(f"error: {result.error}")
        else:
            data = result.data
            print(f"passed: {data['passed']}")
            print(f"summary: {data['summary']}")
            for i, issue in enumerate(data["issues"], 1):
                print(f"  [{i}] {issue['severity']} · {issue['category']} · {issue['message']}")
                print(f"      location: {issue['location']}")
                print(f"      suggestion: {issue['suggestion']}")

        # 断言：至少能检出以下类别
        categories = {issue["category"] for issue in data.get("issues", [])}
        assert "metadata" in categories, "未检出 metadata 类问题"
        assert "logo" in categories, "未检出 logo 类问题"
        assert "company_name" in categories, "未检出 company_name 类问题"

        # docProps XML 应该至少报出 1 条（作者 / 最后修改人 / 标题）
        docprops_locations = [i.get("location", "") for i in data.get("issues", [])
                              if i.get("location", "").startswith("docProps/")]
        assert len(docprops_locations) >= 1, "未检出 docProps XML 中的残留"

        print("\n[OK] 自测通过：metadata / logo / company_name / docProps 检查均生效")

        # ---------- 文本降级模式测试 ----------
        text_ctx = SkillContext(
            project_id="self-test",
            db=None,
            llm=_DummyLLM(),
            parameters={
                "bid_text": "公司资质说明：老马科技股份有限公司成立于2010年。\n常规无身份信息段落。",
                "company_names": ["老马科技股份有限公司"],
            },
        )
        r2 = await skill.safe_execute(text_ctx)
        assert r2.success and r2.data.get("mode") == "text", "文本模式未生效"
        assert r2.data.get("issues"), "文本模式应能检出 company_name 残留"
        print("\n[OK] 文本降级模式测试通过：", r2.data.get("summary"))

        # ---------- 空输入负测试 ----------
        r3 = await skill.safe_execute(SkillContext(
            project_id="self-test", db=None, llm=_DummyLLM(), parameters={}
        ))
        assert not r3.success and r3.error, "空参数应报错"
        print(f"[OK] 空参数负测试通过：{r3.error}")

        # ---------- 无 company_names 文件模式 ----------
        r4 = await skill.safe_execute(SkillContext(
            project_id="self-test", db=None, llm=_DummyLLM(),
            parameters={"file_path": docx_path}
        ))
        assert r4.success, "无 company_names 时文件模式仍应返回 success"
        has_skip_warning = any(
            i.get("category") == "company_name" and "跳过" in i.get("message", "")
            for i in r4.data.get("issues", [])
        )
        assert has_skip_warning, "无 company_names 应产生一条 warning"
        print("[OK] 无 company_names 场景测试通过")

        # 清理临时文件（测试用例，失败不影响自检结论）
        try:
            os.remove(docx_path)
            os.remove(png_path)
            os.rmdir(tmp_dir)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 自检临时文件清理失败: {e}")


if __name__ == "__main__":
    asyncio.run(_run_self_test())
