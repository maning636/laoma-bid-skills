"""DocxFormatSkill 排版逻辑回归测试（迁移到开源仓版本）。

来源：主仓 `tests/test_format_skills_golden.py`，只迁移了**第 1 节
「DocxFormatSkill — heading_number_format 多分支回归」**（3 个函数，
其中 1 个参数化×2 = 4 个用例）。它只依赖 ``docx`` + ``skill_engine.base``
+ ``skills.docx_format.docx_format_skill``，不依赖主仓业务代码。

未迁移部分（原文件第 2~3 节，共 6 个用例）：
- ``PdfExportSkill``（三级降级链）与 ``RevisionSkill``（段落级 diff +
  修订标记）两节 —— 这两个技能属于主仓 `services/format/skills/`，
  开源仓**没有** `pdf_export_skill` / `revision_skill`，**本用例依赖主仓
  services.format.skills，未迁**。

设计原则沿用主仓：不联网、不启动 LibreOffice/Word；固定文本输入 +
tmp_path；只比对 OOXML 关键结构（元素数量、属性值、文本）。
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pytest
import pytest_asyncio
from docx import Document
from docx.oxml.ns import qn

from skill_engine.base import SkillContext
from skills.docx_format.docx_format_skill import DocxFormatSkill

# ═══════════════════════════════════════════════════════════════════════
# Fixtures / helpers
# ═══════════════════════════════════════════════════════════════════════


def _ctx(parameters: dict[str, Any]) -> SkillContext:
    """构造一个空依赖的 SkillContext —— DocxFormatSkill 只读 parameters。"""
    return SkillContext(
        project_id="test",
        db=None,
        llm=None,
        parameters=parameters,
    )


def _unzip_member(path: Path, name: str) -> str:
    """按 name 精确取 docx 内的某个 XML 成员，返回文本内容。"""
    with zipfile.ZipFile(path) as zf:
        return zf.read(name).decode("utf-8")


def _find_member(docx_path: Path, needle: str) -> str | None:
    """按子串匹配 docx 内成员名（footer1.xml/footer2.xml 位置不定）。"""
    with zipfile.ZipFile(docx_path) as zf:
        for n in zf.namelist():
            if needle in n:
                return n
    return None


def _build_docx(path: Path, paras: list[str]) -> Path:
    doc = Document()
    for text in paras:
        doc.add_paragraph(text)
    doc.save(str(path))
    return path


def _build_full_docx(path: Path) -> Path:
    """构造含标题/正文/表格/编号列表的完整输入 docx。"""
    doc = Document()
    doc.add_paragraph("一、项目概述")
    doc.add_paragraph("本项目用于演示系统建设方案。")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "指标"
    table.cell(0, 1).text = "值"
    table.cell(1, 0).text = "响应时间"
    table.cell(1, 1).text = "100ms"
    doc.add_paragraph("1.1 功能模块")
    doc.add_paragraph("支持文档解析与检索。")
    doc.add_paragraph("1. 登录模块")
    doc.add_paragraph("2. 权限管理")
    doc.add_paragraph("3. 数据导出")
    doc.add_paragraph("二、技术方案")
    doc.add_paragraph("采用微服务架构。")
    doc.save(str(path))
    return path


@pytest_asyncio.fixture
async def docx_format_skill() -> DocxFormatSkill:
    return DocxFormatSkill()


def _para_outline_lvl(root: ET.Element, text: str) -> str | None:
    """从 docx document.xml 中根据段落纯文本定位 outlineLvl 值。"""
    for p in root.iter(qn("w:p")):
        para_text = "".join(t.text or "" for t in p.iter(qn("w:t")))
        if para_text.strip() == text:
            pPr = p.find(qn("w:pPr"))
            if pPr is None:
                return None
            lvl = pPr.find(qn("w:outlineLvl"))
            return lvl.get(qn("w:val")) if lvl is not None else None
    return pytest.fail(f"段落 '{text}' 未在输出 document.xml 中找到")


def _find_para_by_text(root: ET.Element, text: str) -> ET.Element:
    for p in root.iter(qn("w:p")):
        para_text = "".join(t.text or "" for t in p.iter(qn("w:t")))
        if para_text.strip() == text:
            return p
    return pytest.fail(f"段落 '{text}' 未在 document.xml 中找到")


# ═══════════════════════════════════════════════════════════════════════
# 1. DocxFormatSkill — heading_number_format 多分支回归
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("paragraphs", "expected_levels", "first_heading_sz"),
    [
        # "decimal" 分支：H2="1.1 xxx"（outlineLvl=1），H3="1.1.1 xxx"（outlineLvl=2）。
        # h2_size=15pt → 半磅 30
        (
            ["1.1 二级标题", "1.1.1 三级标题", "正文内容。"],
            [("1.1 二级标题", "1"), ("1.1.1 三级标题", "2")],
            "30",
        ),
        # 中文层级：RE_HEADING_H1="一、"（h1）+ RE_HEADING_H2="（一）"（h2）+ RE_DECIMAL_H3
        # h1_size=16pt → 半磅 32
        (
            ["一、项目概述", "（一）二级标题", "1.1.1 三级标题", "正文内容。"],
            [
                ("一、项目概述", "0"),
                ("（一）二级标题", "1"),
                ("1.1.1 三级标题", "2"),
            ],
            "32",
        ),
    ],
)
async def test_docx_format_heading_detection_defaults(
    tmp_path: Path,
    docx_format_skill: DocxFormatSkill,
    paragraphs: list[str],
    expected_levels: list[tuple[str, str]],
    first_heading_sz: str,
) -> None:
    """默认 heading_number_format 下标题能被正确识别并设置 outlineLvl。"""
    input_path = _build_docx(tmp_path / "input.docx", paragraphs)

    result = await docx_format_skill.execute(_ctx({"file_path": str(input_path)}))
    assert result.success, f"执行失败: {result.error}"
    output_path = result.data["output_path"]
    assert output_path.endswith("_formatted.docx")
    assert Path(output_path).exists()

    root = ET.fromstring(_unzip_member(Path(output_path), "word/document.xml"))

    for text, expected_lvl in expected_levels:
        assert _para_outline_lvl(root, text) == expected_lvl, (
            f"段落 {text!r} 应被识别为 outlineLvl={expected_lvl}"
        )

    # 首段：outlineLvl 已定位 → 字体应为黑体，字号应写为对应的 h{level}_size（半磅）
    first_text, _first_lvl = expected_levels[0]
    para = _find_para_by_text(root, first_text)
    rPr = para.find(f"{qn('w:r')}/{qn('w:rPr')}")
    assert rPr is not None
    rFonts = rPr.find(qn("w:rFonts"))
    assert rFonts is not None and rFonts.get(qn("w:eastAsia")) == "黑体"
    sz = rPr.find(qn("w:sz"))
    assert sz is not None and sz.get(qn("w:val")) == first_heading_sz


@pytest.mark.asyncio
async def test_docx_format_respects_custom_heading_number_format_config(
    tmp_path: Path, docx_format_skill: DocxFormatSkill
) -> None:
    """用户传入 heading_number_format 时应被检测逻辑真正读取。"""
    input_path = _build_docx(
        tmp_path / "input.docx",
        ["1）一级标题", "1.1) 二级标题", "1.1.1) 三级标题", "正文内容。"],
    )

    result = await docx_format_skill.execute(
        _ctx(
            {
                "file_path": str(input_path),
                "config": {"heading_number_format": "decimal_paren"},
            }
        )
    )
    assert result.success

    root = ET.fromstring(
        _unzip_member(Path(result.data["output_path"]), "word/document.xml")
    )
    # 期望：三行分别被识别为 h1 / h2 / h3（outlineLvl 0/1/2）
    assert _para_outline_lvl(root, "1）一级标题") == "0"
    assert _para_outline_lvl(root, "1.1) 二级标题") == "1"
    assert _para_outline_lvl(root, "1.1.1) 三级标题") == "2"


@pytest.mark.asyncio
async def test_docx_format_full_document(
    tmp_path: Path, docx_format_skill: DocxFormatSkill
) -> None:
    """完整文档：表格边框、正文两端对齐、页脚页码域。"""
    input_path = _build_full_docx(tmp_path / "input.docx")

    result = await docx_format_skill.execute(_ctx({"file_path": str(input_path)}))
    assert result.success, result.error
    output_path = result.data["output_path"]

    with zipfile.ZipFile(output_path) as zf:
        doc_xml = zf.read("word/document.xml").decode("utf-8")

    root = ET.fromstring(doc_xml)

    # 表格存在且带 tblBorders（6 条边：top/left/bottom/right/insideH/insideV）
    tables = list(root.iter(qn("w:tbl")))
    assert tables, "输出文档缺少表格"
    tbl_pr = tables[0].find(qn("w:tblPr"))
    borders = tbl_pr.find(qn("w:tblBorders")) if tbl_pr is not None else None
    assert borders is not None, "表格缺少 w:tblBorders"
    edge_names = {child.tag.split("}")[-1] for child in borders}
    assert {"top", "left", "bottom", "right", "insideH", "insideV"} <= edge_names

    # 正文段落应被标成 JUSTIFY (w:jc val="both")
    body_para = _find_para_by_text(root, "本项目用于演示系统建设方案。")
    pPr = body_para.find(qn("w:pPr"))
    assert pPr is not None
    jc = pPr.find(qn("w:jc"))
    assert jc is not None and jc.get(qn("w:val")) == "both"

    # 页脚页码：footers 存在 PAGE 域代码，带前后"—"
    footer_name = _find_member(Path(output_path), "footer")
    assert footer_name is not None, "输出文档缺少页脚部件"
    footer_xml = _unzip_member(Path(output_path), footer_name)
    assert "PAGE" in footer_xml
    assert "w:fldChar" in footer_xml
    assert "—" in footer_xml

    # stats 计数：1 个表格 + 9 个非空段落
    stats = result.data["stats"]
    assert stats["tables"] == 1
    assert stats["paragraphs"] == 9
    assert stats["headings"] >= 2
    assert stats["body"] >= 3
