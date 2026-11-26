"""DocxFormatSkill — DOCX 排版技能（clean-room 重写）。

设计要点：
- 保持 ``DocxFormatSkill.execute`` 与 ``SkillResult.data`` 契约；
- 6 种 ``heading_number_format``（decimal / decimal_paren / chinese_paren /
  mixed / letter / chinese）均可在运行时切换生效；
- 修复：标题识别使用运行时 ``config`` 而不是模块级默认值；
- 输出：outlineLvl、黑体、字号、正文两端对齐、表格 6 条边框、页脚 PAGE 域
  + 破折号、``stats`` 计数。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, ClassVar

from skill_engine.base import Skill, SkillContext, SkillResult

logger = logging.getLogger(__name__)

# ───────────────────────── 标题识别：正则表 ─────────────────────────

# 点分隔十进制：按段数（8→2）从深到浅排列，命中即得层级
DOT = r"(\d+)\s*[\.．]\s*"
DECIMAL_NUMBERING_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"^" + DOT + DOT + DOT + DOT + DOT + DOT + DOT + r"(\d+)\s*[^\d\.．]"), 8),
    (re.compile(r"^" + DOT + DOT + DOT + DOT + DOT + DOT + r"(\d+)\s*[^\d\.．]"), 7),
    (re.compile(r"^" + DOT + DOT + DOT + DOT + DOT + r"(\d+)\s*[^\d\.．]"), 6),
    (re.compile(r"^" + DOT + DOT + DOT + DOT + r"(\d+)\s*[^\d\.．]"), 5),
    (re.compile(r"^" + DOT + DOT + DOT + r"(\d+)\s*[^\d\.．]"), 4),
    (re.compile(r"^" + DOT + DOT + r"(\d+)\s*[^\d\.．]"), 3),
    (re.compile(r"^" + DOT + r"(\d+)\s*[^\d\.．]"), 2),
)

CN_NUM = r"[一二三四五六七八九十百千万零]+"
_RE_H1 = re.compile(r"^" + CN_NUM + r"\s*、")
_RE_H2 = re.compile(r"^[（\(]" + CN_NUM + r"[）\)]")
_RE_H3 = re.compile(r"^\d+\s*[\.．]")
_RE_H4 = re.compile(r"^[（\(]\d+[）\)]")
RE_ATTACHMENT = re.compile(r"^附件\s*(\d+|" + CN_NUM + r")?\s*[:：]?$")

_TOC_TAB = re.compile(r"^.+\t\s*\d+\s*$")
_TOC_LEADER = re.compile(r"\.{3,}\s*\d+\s*$")
_TOC_WORDS = {"目录", "目  录", "目　录"}

# 目录 / 大标题样式名白名单（小写比较）
_STYLE_TOC_KEYS = ("toc", "目录")
_STYLE_TITLE_KEYS = ("标题", "title", "封面")

# 表格数字判定
RE_NUMERIC_TABLE_TEXT = re.compile(r"^[-+]?(?:\d+(?:\.\d+)?|\.\d+)%?$")
RE_CURRENCY_PREFIX = re.compile(r"^[¥￥$]")
RE_CURRENCY_SUFFIX = re.compile(r"(元|万元|亿元)$")

# 高保真样式 ID（beautify 分支用）
_STYLE_HIK_H4 = "15"
_STYLE_HIK_H5 = "16"
_STYLE_HIK_INDENT = "25"

_DEFAULT_INDENT_H4 = 360
_DEFAULT_INDENT_H5 = 480

DEFAULT_FORMAT_CONFIG: dict[str, Any] = {
    # 页面
    "page_number_align": "居中",
    "footer_distance": 2.5,
    "margin_top": 2.6,
    "margin_bottom": 2.2,
    "margin_left": 2.5,
    "margin_right": 2.5,
    "force_a4": True,
    # 行距
    "line_spacing": 28,
    "line_spacing_mode": "固定值(磅)",
    "title_line_spacing": 33,
    "subtitle_line_spacing": 33,
    # 字体：标题
    "title_font": "方正小标宋简体",
    "h1_font": "黑体", "h2_font": "黑体", "h3_font": "黑体",
    "h4_font": "楷体_GB2312", "h5_font": "楷体_GB2312",
    "h6_font": "宋体", "h7_font": "宋体", "h8_font": "宋体",
    # 字体：正文/表格/附件
    "body_font": "仿宋_GB2312",
    "page_number_font": "宋体",
    "table_caption_font": "宋体", "figure_caption_font": "宋体",
    "attachment_font": "黑体", "subtitle_font": "楷体_GB2312",
    "table_font": "仿宋_GB2312", "table_header_font": "仿宋_GB2312",
    "english_font": "Times New Roman",
    # 字号
    "title_size": 22,
    "h1_size": 16, "h2_size": 15, "h3_size": 15, "h4_size": 14,
    "h5_size": 12, "h6_size": 10.5, "h7_size": 10.5, "h8_size": 10.5,
    "body_size": 12,
    "page_number_size": 14,
    "table_caption_size": 10.5, "figure_caption_size": 10.5,
    "attachment_size": 16, "subtitle_size": 16, "table_size": 12,
    # 粗体
    "h1_bold": True, "h2_bold": True, "h3_bold": True,
    "h4_bold": True, "h5_bold": True, "h6_bold": True,
    "h7_bold": False, "h8_bold": False,
    # 段落间距
    "h1_space_before": 12, "h1_space_after": 12,
    "h2_space_before": 6, "h2_space_after": 6,
    "h3_space_before": 6, "h3_space_after": 6,
    "h4_space_before": 2.4, "h4_space_after": 2.4,
    "h5_space_before": 1.2, "h5_space_after": 1.2,
    # 行为开关
    "set_outline": True,
    "enable_attachment_formatting": True,
    "use_custom_english_font": True,
    "normalize_punctuation": True,
    "enable_table_formatting": True,
    "table_auto_col_width": True,
    "table_width_percent": 100,
    "table_header_bold": True,
    "table_smart_align": True,
    "table_unified_borders": True,
    "table_border_size_pt": 0.5,
    "table_row_height_cm": 0.7,
    "table_line_spacing": 22,
    # 编号格式
    "heading_number_format": "decimal",
    "bold_before_colon": False,
    "left_indent_cm": 0.0,
    "right_indent_cm": 0.0,
    "indent_h4": _DEFAULT_INDENT_H4,
    "indent_h5": _DEFAULT_INDENT_H5,
}

_ALIGN_TO_WD = {
    "left": "LEFT",
    "center": "CENTER",
    "justify": "JUSTIFY",
    "right": "RIGHT",
}
_MARGIN_KEYS = (
    ("top_margin", "margin_top", "上边距"),
    ("bottom_margin", "margin_bottom", "下边距"),
    ("left_margin", "margin_left", "左边距"),
    ("right_margin", "margin_right", "右边距"),
)


class DocxFormatSkill(Skill):
    name = "docx_format"
    description = "智能排版(集成Word-Formatter-Pro引擎)"
    category = "output"
    version = "3.0.0"
    triggers: ClassVar[list[str]] = ["排版", "格式化", "格式调整", "美化", "格式检查"]

    # ═══════════════════════ 入口 ═══════════════════════

    async def execute(self, ctx: SkillContext) -> SkillResult:
        """按 ``mode`` 分派；返回带 ``stats`` 等 data 字段的 SkillResult。"""
        path = ctx.parameters.get("file_path", "")
        mode = ctx.parameters.get("mode", "format")
        overrides = ctx.parameters.get("config") or {}

        if not path or not Path(path).exists():
            return SkillResult(success=False, error=f"文件不存在: {path}")

        config = {**DEFAULT_FORMAT_CONFIG, **overrides}
        return await {
            "check": self._mode_check,
            "diff": self._mode_diff,
            "beautify": self._mode_beautify,
            "format": self._mode_format,
        }.get(mode, self._mode_format)(path, config)

    # ═══════════════════════ mode=format ═══════════════════════

    async def _mode_format(self, path: str, cfg: dict[str, Any]) -> SkillResult:
        from docx import Document

        def _run_sync():
            # to_thread：python-docx 段落/表格遍历 + doc.save 都是同步 IO/CPU，
            # 长文档可达秒级，必须在事件循环外跑。
            doc = Document(path)
            counters: dict[str, int] = {
                "paragraphs": 0, "headings": 0, "body": 0,
                "tables": 0, "margins_fixed": 0,
            }
            self._apply_section_layout(doc, cfg, counters)

            for para in doc.paragraphs:
                text = para.text.strip()
                if not text:
                    continue
                counters["paragraphs"] += 1
                label = self._label_of(para, text, cfg)
                rules = self._rules_for(label, cfg)
                self._apply_para_style(para, rules, cfg, label)
                counters["headings" if label != "正文" else "body"] += 1

            if cfg.get("enable_table_formatting", True):
                for tbl in doc.tables:
                    self._format_table(tbl, cfg)
                    counters["tables"] += 1

            out = str(Path(path).with_suffix("")) + "_formatted.docx"
            doc.save(out)
            return counters, out

        try:
            counters, out = await asyncio.to_thread(_run_sync)
        except Exception as exc:  # noqa: BLE001
            return SkillResult(success=False, error=f"文件打开失败: {exc}")

        return SkillResult(success=True, data={"output_path": out, "stats": counters})

    # ═══════════════════════ mode=check / diff ═══════════════════════

    async def _mode_check(self, path: str, cfg: dict[str, Any]) -> SkillResult:
        from docx import Document
        from docx.oxml.ns import qn

        def _run_sync():
            # to_thread：docx 打开 + 每段落 lxml 属性遍历，长文档可达秒级
            doc = Document(path)
            total_para = checked_run = 0
            problems: list[dict[str, Any]] = []

            for para in doc.paragraphs:
                text = para.text.strip()
                if not text:
                    continue
                total_para += 1
                label = self._label_of(para, text, cfg)
                rules = self._rules_for(label, cfg)
                preview = text[:30]

                for run in para.runs:
                    checked_run += 1
                    expected_font = rules.get("font_name")
                    if expected_font is not None:
                        actual_ascii = run.font.name or ""
                        actual_ea = ""
                        rPr = run._element.rPr
                        if rPr is not None:
                            rFonts = rPr.find(qn("w:rFonts"))
                            if rFonts is not None:
                                actual_ea = rFonts.get(qn("w:eastAsia"), "")
                        if actual_ascii != expected_font and actual_ea != expected_font:
                            problems.append({
                                "type": "font_mismatch", "expected": expected_font,
                                "actual": actual_ascii or actual_ea,
                                "text_preview": preview, "label": label,
                            })

                    expected_size = rules.get("font_size")
                    if expected_size is not None:
                        actual = run.font.size
                        if actual is not None and actual.pt != expected_size:
                            problems.append({
                                "type": "size_mismatch", "expected": expected_size,
                                "actual": actual.pt, "text_preview": preview,
                                "label": label,
                            })

                    expected_bold = rules.get("bold")
                    if expected_bold is not None and bool(run.font.bold) != bool(expected_bold):
                        # bool(None) == False，避免"未显式加粗"被误判为与 expected=False 冲突
                        problems.append({
                            "type": "bold_mismatch", "expected": expected_bold,
                            "actual": run.font.bold, "text_preview": preview,
                            "label": label,
                        })

                if (label == "正文"
                        and rules.get("first_line_indent") == 2):
                    pf = para.paragraph_format
                    if pf.first_line_indent is None or pf.first_line_indent.pt == 0:
                        problems.append({
                            "type": "indent_missing", "expected": "首行缩进2字符",
                            "actual": "无缩进", "text_preview": preview,
                            "label": label,
                        })

            # 页面边距
            for section in doc.sections:
                for attr, key, desc in _MARGIN_KEYS:
                    val = getattr(section, attr)
                    if val is None:
                        continue
                    if abs(val.cm - cfg[key]) > 0.1:
                        problems.append({
                            "type": "margin_mismatch",
                            "expected": f"{cfg[key]}cm",
                            "actual": f"{val.cm:.1f}cm",
                            "detail": desc,
                        })

            return total_para, checked_run, problems

        try:
            total_para, checked_run, problems = await asyncio.to_thread(_run_sync)
        except Exception as exc:  # noqa: BLE001
            return SkillResult(success=False, error=f"文件打开失败: {exc}")

        problems = self._dedupe(problems)
        counts = {t: sum(1 for p in problems if p["type"] == t)
                  for t in ("font_mismatch", "size_mismatch",
                            "indent_missing", "margin_mismatch")}

        return SkillResult(success=True, data={
            "total_paragraphs": total_para,
            "checked_runs": checked_run,
            "total_issues": len(problems),
            "font_issues": counts["font_mismatch"],
            "size_issues": counts["size_mismatch"],
            "indent_issues": counts["indent_missing"],
            "margin_issues": counts["margin_mismatch"],
            "issues": problems[:100],
            "compliance_rate": max(
                0, round((1 - len(problems) / max(checked_run, 1)) * 100, 1)
            ),
        })

    async def _mode_diff(self, path: str, cfg: dict[str, Any]) -> SkillResult:
        check = await self._mode_check(path, cfg)
        if not check.success:
            return check
        payload = check.data
        diffs = [{
            "location": p.get("text_preview") or p.get("detail", ""),
            "property": p.get("type", ""),
            "expected": p.get("expected", ""),
            "actual": p.get("actual", ""),
            "label": p.get("label", ""),
        } for p in payload["issues"]]
        auto_fixable = sum(
            1 for d in diffs
            if d["property"] in {"font_mismatch", "size_mismatch",
                                 "bold_mismatch", "indent_missing",
                                 "margin_mismatch"}
        )
        return SkillResult(success=True, data={
            "diff_count": len(diffs),
            "diffs": diffs[:200],
            "auto_fixable": auto_fixable,
            "check_summary": {
                "total_paragraphs": payload["total_paragraphs"],
                "total_issues": payload["total_issues"],
                "compliance_rate": payload["compliance_rate"],
            },
        })

    # ═══════════════════════ mode=beautify ═══════════════════════

    async def _mode_beautify(self, path: str, cfg: dict[str, Any]) -> SkillResult:
        workdir = tempfile.mkdtemp(prefix="bidfmt_")

        def _run_sync():
            # to_thread：zipfile 解压/重打包 + os.walk + 多次 open 是同步 IO，
            # 大文档可达秒级，不能在事件循环里跑。
            try:
                with zipfile.ZipFile(path) as zf:
                    zf.extractall(workdir)

                report: dict[str, Any] = {}
                for sub, name in (("word/document.xml", "document"),
                                  ("word/numbering.xml", "numbering"),
                                  ("word/styles.xml", "styles")):
                    target = os.path.join(workdir, sub)
                    if not os.path.exists(target):
                        continue
                    if name == "document":
                        text = self._beautify_document_xml(
                            self._read_xml(target), cfg)
                    elif name == "numbering":
                        text = self._beautify_numbering_xml(
                            self._read_xml(target), cfg)
                    else:
                        text = self._beautify_styles_xml(self._read_xml(target))
                    self._write_xml(target, text)
                    report[name] = 1

                out = str(Path(path).with_suffix("")) + "_beautified.docx"
                with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
                    for root, _, files in os.walk(workdir):
                        for fname in files:
                            fpath = os.path.join(root, fname)
                            zf.write(fpath, os.path.relpath(fpath, workdir))
                return out, report
            finally:
                shutil.rmtree(workdir, ignore_errors=True)

        try:
            out, report = await asyncio.to_thread(_run_sync)
        except Exception as exc:  # noqa: BLE001
            return SkillResult(success=False, error=f"XML美化失败: {exc}")
        return SkillResult(success=True, data={
            "output_path": out, "report": report})

    # ═══════════════════════ 标题识别 ═══════════════════════

    def _label_of(self, para, text: str, cfg: dict[str, Any]) -> str:
        """识别段落语义标签：title / toc / h1..h8 / attachment / 正文。

        运行时读取 ``cfg['heading_number_format']``，6 种编号格式均可生效。
        """
        if self._style_is(para, _STYLE_TOC_KEYS) or self._looks_like_toc(text):
            return "toc"
        if self._style_is(para, _STYLE_TITLE_KEYS):
            return "title"

        ruleset = cfg.get("heading_number_format", "decimal")
        label = self._label_by_numbering_ruleset(text, ruleset)
        if label is not None:
            return label
        return self._label_by_generic_pattern(text)

    @staticmethod
    def _style_is(para, needles: tuple[str, ...]) -> bool:
        try:
            raw = (para.style.name if para.style else "") or ""
        except Exception:  # noqa: BLE001
            return False
        lowered = raw.lower()
        return any(n in lowered for n in needles)

    @staticmethod
    def _looks_like_toc(text: str) -> bool:
        if text in _TOC_WORDS:
            return True
        return bool(_TOC_TAB.match(text) or _TOC_LEADER.search(text))

    @staticmethod
    def _label_by_numbering_ruleset(text: str, ruleset: str) -> str | None:
        """按 6 种 heading_number_format 分支解析；无匹配返回 None。"""
        if ruleset == "decimal":
            return _first_match_label(text, DECIMAL_NUMBERING_PATTERNS)
        if ruleset == "decimal_paren":
            for pattern, level in (
                (r"^\d+\s*[\.．]\s*\d+\s*[\.．]\s*\d+\s*[）\)]", 3),
                (r"^\d+\s*[\.．]\s*\d+\s*[）\)]", 2),
                (r"^\d+\s*[）\)]", 1),
            ):
                if re.match(pattern, text):
                    return f"h{level}"
            return None
        if ruleset == "chinese_paren":
            if re.match(r"^[（\(]" + CN_NUM + r"\s*[）\)]", text):
                return "h2"
            return None
        if ruleset == "mixed":
            if _RE_H1.match(text):
                return "h1"
            return _first_match_label(text, DECIMAL_NUMBERING_PATTERNS)
        if ruleset == "letter":
            for pattern, level in (
                (r"^[A-Z]\s*[\.．]\s*\d+\s*[\.．]\s*\d+", 3),
                (r"^[A-Z]\s*[\.．]\s*\d+", 2),
                (r"^[A-Z]\s*[\.．、]", 1),
                (r"^[a-z]\s*[\.．、]", 2),
            ):
                if re.match(pattern, text):
                    return f"h{level}"
            return None
        if ruleset == "chinese":
            if _RE_H1.match(text):
                return "h1"
            if _RE_H2.match(text):
                return "h2"
            if _RE_H3.match(text):
                return "h3"
            if _RE_H4.match(text):
                return "h4"
        return None

    @staticmethod
    def _label_by_generic_pattern(text: str) -> str:
        """通用编号模式：中文一、/（一）/1./（1）+ 附件。"""
        if _RE_H1.match(text):
            return "h1"
        if _RE_H2.match(text):
            return "h2"
        if _RE_H3.match(text):
            return "h3"
        if _RE_H4.match(text):
            return "h4"
        if RE_ATTACHMENT.match(text):
            return "attachment"
        return "正文"

    # ═══════════════════════ 规则 → 属性 ═══════════════════════

    @staticmethod
    def _rules_for(label: str, cfg: dict[str, Any]) -> dict[str, Any]:
        if label in ("toc", "title"):
            return {}
        if label.startswith("h"):
            lvl = label[1:]
            return {
                "font_name": cfg.get(f"h{lvl}_font", cfg.get("body_font", "宋体")),
                "font_size": cfg.get(f"h{lvl}_size", 12),
                "bold": cfg.get(f"h{lvl}_bold", True),
                "space_before": cfg.get(f"h{lvl}_space_before", 0),
                "space_after": cfg.get(f"h{lvl}_space_after", 0),
                "alignment": "left",
                "first_line_indent": 0,
            }
        if label == "attachment":
            return {
                "font_name": cfg.get("attachment_font", "黑体"),
                "font_size": cfg.get("attachment_size", 16),
                "bold": True,
                "alignment": "left",
                "first_line_indent": 0,
            }
        return {
            "font_name": cfg.get("body_font", "仿宋_GB2312"),
            "font_size": cfg.get("body_size", 12),
            "bold": False,
            "alignment": "justify",
            "first_line_indent": 2,
            "line_spacing": cfg.get("line_spacing", 28),
        }

    # ═══════════════════════ 应用样式 ═══════════════════════

    @staticmethod
    def _apply_section_layout(doc, cfg: dict[str, Any], counters: dict[str, int]) -> None:
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Cm

        for section in doc.sections:
            section.top_margin = Cm(cfg["margin_top"])
            section.bottom_margin = Cm(cfg["margin_bottom"])
            section.left_margin = Cm(cfg["margin_left"])
            section.right_margin = Cm(cfg["margin_right"])
            section.footer_distance = Cm(cfg["footer_distance"])
            if cfg.get("force_a4"):
                section.page_width = Cm(21)
                section.page_height = Cm(29.7)
            counters["margins_fixed"] += 1

            if cfg.get("page_number_align") == "居中":
                # 页脚定义可能不存在（第三方生成的 docx 常见：section 继承前一节
                # 的页脚，python-docx 取到的是非 XML Part）。页脚属于排版装饰，
                # 拿不到就跳过，不能让整篇排版报「文件打开失败」。
                try:
                    footer = section.footer
                    target = (
                        footer.paragraphs[0] if footer.paragraphs
                        else footer.add_paragraph()
                    )
                    target.clear()
                    target.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    DocxFormatSkill._insert_page_number(target, cfg)
                except (AttributeError, ValueError):
                    continue

    def _apply_para_style(
        self, para, rules: dict[str, Any],
        cfg: dict[str, Any], label: str,
    ) -> None:
        if not para.runs:
            return
        en_font = (cfg.get("english_font", "Times New Roman")
                   if cfg.get("use_custom_english_font")
                   else rules.get("font_name", "宋体"))
        self._apply_run_typography(para.runs, rules, en_font)
        self._apply_para_geometry(para, rules)
        if cfg.get("set_outline") and rules.get("alignment") == "left" \
                and label.startswith("h"):
            self._set_outline_level(para, int(label[1:]) - 1)

    @staticmethod
    def _apply_run_typography(runs, rules: dict[str, Any], en_font: str) -> None:
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from docx.shared import Pt

        cn_font = rules.get("font_name")
        pt = rules.get("font_size")
        bold = rules.get("bold")
        for run in runs:
            if cn_font is not None:
                ascii_font = en_font or cn_font
                run.font.name = ascii_font
                rPr = run._element.get_or_add_rPr()
                rFonts = rPr.find(qn("w:rFonts"))
                if rFonts is None:
                    rFonts = OxmlElement("w:rFonts")
                    rPr.insert(0, rFonts)
                for attr in ("w:eastAsiaTheme", "w:asciiTheme",
                             "w:hAnsiTheme", "w:cstheme", "w:csTheme"):
                    rFonts.attrib.pop(qn(attr), None)
                rFonts.set(qn("w:eastAsia"), cn_font)
                rFonts.set(qn("w:ascii"), ascii_font)
                rFonts.set(qn("w:hAnsi"), ascii_font)
            if pt is not None:
                run.font.size = Pt(pt)
            if bold is not None:
                run.font.bold = bool(bold)

    @staticmethod
    def _apply_para_geometry(para, rules: dict[str, Any]) -> None:
        from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
        from docx.oxml.ns import qn
        from docx.shared import Pt

        align_key = rules.get("alignment")
        if align_key is not None:
            wd_key = _ALIGN_TO_WD.get(align_key, "LEFT")
            para.alignment = getattr(WD_ALIGN_PARAGRAPH, wd_key)

        indent_chars = rules.get("first_line_indent")
        if indent_chars is not None:
            if indent_chars == 0:
                para.paragraph_format.first_line_indent = None
                pPr = para._p.get_or_add_pPr()
                ind = pPr.find(qn("w:ind"))
                if ind is not None:
                    ind.set(qn("w:firstLineChars"), "0")
                    for attr in ("w:firstLine", "w:hanging", "w:hangingChars"):
                        ind.attrib.pop(qn(attr), None)
            else:
                para.paragraph_format.first_line_indent = Pt(
                    float(rules["font_size"]) * indent_chars * 2)

        space_before = rules.get("space_before")
        if space_before is not None:
            para.paragraph_format.space_before = Pt(space_before)
        space_after = rules.get("space_after")
        if space_after is not None:
            para.paragraph_format.space_after = Pt(space_after)
        line_spacing = rules.get("line_spacing")
        if line_spacing is not None:
            para.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
            para.paragraph_format.line_spacing = Pt(line_spacing)

    @staticmethod
    def _set_outline_level(para, level: int) -> None:
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn

        pPr = para._p.get_or_add_pPr()
        node = pPr.find(qn("w:outlineLvl"))
        if node is None:
            node = OxmlElement("w:outlineLvl")
            pPr.append(node)
        node.set(qn("w:val"), str(level))

    @staticmethod
    def _insert_page_number(paragraph, cfg: dict[str, Any]) -> None:
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from docx.shared import Pt, RGBColor

        font_name = cfg.get("page_number_font", "宋体")
        font_size = cfg.get("page_number_size", 14)

        prefix = paragraph.add_run("— ")
        middle = paragraph.add_run()
        suffix = paragraph.add_run(" —")
        for run in (prefix, middle, suffix):
            run.font.name = font_name
            run.font.size = Pt(font_size)
            run.font.color.rgb = RGBColor(0, 0, 0)

        begin = OxmlElement("w:fldChar")
        begin.set(qn("w:fldCharType"), "begin")
        instr = OxmlElement("w:instrText")
        instr.set(qn("xml:space"), "preserve")
        instr.text = "PAGE"
        end = OxmlElement("w:fldChar")
        end.set(qn("w:fldCharType"), "end")
        middle._r.extend([begin, instr, end])

    @staticmethod
    def _format_table(table, cfg: dict[str, Any]) -> None:
        from docx.enum.table import WD_ROW_HEIGHT_RULE
        from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from docx.shared import Cm, Pt

        tbl_pr = table._tbl.tblPr
        if tbl_pr is None:
            tbl_pr = OxmlElement("w:tblPr")
            table._tbl.insert(0, tbl_pr)
        borders = tbl_pr.find(qn("w:tblBorders"))
        if borders is None:
            borders = OxmlElement("w:tblBorders")
            tbl_pr.append(borders)
        else:
            for child in list(borders):
                borders.remove(child)
        size_eighths = max(1, int(float(cfg.get("table_border_size_pt", 0.5)) * 8))
        for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
            node = OxmlElement(f"w:{edge}")
            node.set(qn("w:val"), "single")
            node.set(qn("w:sz"), str(size_eighths))
            node.set(qn("w:space"), "0")
            node.set(qn("w:color"), "000000")
            borders.append(node)

        body_font = cfg.get("table_font", "仿宋_GB2312")
        header_font = cfg.get("table_header_font", body_font)
        pt_size = cfg.get("table_size", 12)
        header_bold = cfg.get("table_header_bold", True)
        smart = cfg.get("table_smart_align", True)
        row_cm = cfg.get("table_row_height_cm", 0.7)
        line_pt = cfg.get("table_line_spacing", 22)

        for row_idx, row in enumerate(table.rows):
            if row_cm and float(row_cm) > 0:
                row.height = Cm(float(row_cm))
                row.height_rule = WD_ROW_HEIGHT_RULE.AT_LEAST
            for cell in row.cells:
                for para in cell.paragraphs:
                    is_header = row_idx == 0
                    use_font = header_font if is_header else body_font
                    for run in para.runs:
                        run.font.name = use_font
                        rPr = run._element.get_or_add_rPr()
                        rFonts = rPr.find(qn("w:rFonts"))
                        if rFonts is None:
                            rFonts = OxmlElement("w:rFonts")
                            rPr.insert(0, rFonts)
                        rFonts.set(qn("w:eastAsia"), use_font)
                        rFonts.set(qn("w:ascii"), use_font)
                        rFonts.set(qn("w:hAnsi"), use_font)
                        run.font.size = Pt(pt_size)
                        if is_header and header_bold:
                            run.font.bold = True

                    para.paragraph_format.first_line_indent = Pt(0)
                    para.paragraph_format.space_before = Pt(0)
                    para.paragraph_format.space_after = Pt(0)
                    if line_pt and float(line_pt) > 0:
                        para.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
                        para.paragraph_format.line_spacing = Pt(float(line_pt))
                    if smart:
                        if is_header:
                            para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                        elif DocxFormatSkill._looks_numeric(para.text.strip()):
                            para.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                        else:
                            para.alignment = WD_ALIGN_PARAGRAPH.LEFT

    # ═══════════════════════ 工具 ═══════════════════════

    @staticmethod
    def _looks_numeric(text: str) -> bool:
        stripped = text.strip().replace(",", "").replace("，", "").replace("％", "%")
        stripped = RE_CURRENCY_PREFIX.sub("", stripped)
        stripped = RE_CURRENCY_SUFFIX.sub("", stripped)
        return bool(RE_NUMERIC_TABLE_TEXT.match(stripped))

    @staticmethod
    def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple] = set()
        out: list[dict[str, Any]] = []
        for item in items:
            key = (item.get("type"), item.get("expected"), item.get("actual"),
                   item.get("text_preview"), item.get("label"), item.get("detail"))
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    # ─────────── beautify 子过程 ───────────

    @staticmethod
    def _beautify_document_xml(xml: str, cfg: dict[str, Any]) -> str:
        """把 4/5 级标题映射到 HIK 样式，正文补首行缩进，并去重多余编号。"""
        pattern = re.compile(r"(<w:p[ >])(.*?)(</w:p>)", re.DOTALL)
        edits: list[tuple[int, int, str]] = []
        for match in pattern.finditer(xml):
            body = match.group(2)
            if "<w:pStyle" in body:
                continue
            text = _join_run_texts(body)
            if not text:
                continue

            m5 = re.match(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)\.(\d+)\s+(.*)$", text)
            if m5 and _is_short_heading_text(m5.group(6)):
                new_body = _add_style_before_pPr(body, _STYLE_HIK_H5)
                if new_body != body:
                    edits.append((match.start(2), match.end(2), new_body))
                continue

            m4 = re.match(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)\s+(.*)$", text)
            if m4 and _is_short_heading_text(m4.group(6)):
                new_body = _add_style_before_pPr(body, _STYLE_HIK_H4)
                if new_body != body:
                    edits.append((match.start(2), match.end(2), new_body))

        result = xml
        for start, end, repl in reversed(edits):
            result = result[:start] + repl + result[end:]
        result = _strip_duplicate_heading_prefixes(result, _STYLE_HIK_H4, _STYLE_HIK_H5)
        result = _apply_body_indent(result, _STYLE_HIK_INDENT)
        return result

    @staticmethod
    def _beautify_numbering_xml(xml: str, cfg: dict[str, Any]) -> str:
        h4 = int(cfg.get("indent_h4", _DEFAULT_INDENT_H4))
        h5 = int(cfg.get("indent_h5", _DEFAULT_INDENT_H5))
        for ilvl, target in ((3, h4), (4, h5)):
            pattern = (r'(<w:abstractNum w:abstractNumId="1"[^>]*>.*?'
                       rf'<w:lvl w:ilvl="{ilvl}"[^>]*>.*?'
                       r'<w:ind w:left=")\d+(" w:hanging=")\d+("/>)')
            xml, _ = re.subn(pattern, rf"\g<1>{target}\g<2>{target}\g<3>",
                             xml, count=1, flags=re.DOTALL)
        return xml

    @staticmethod
    def _beautify_styles_xml(xml: str) -> str:
        # H4 (styleId=15)：把 spacing 与 rFonts 收敛到统一黑体
        xml = re.sub(
            r'<w:spacing w:before="120" w:after="120" w:line="360" w:lineRule="auto"/>',
            '<w:spacing w:before="60" w:after="40" w:line="280" w:lineRule="auto"/>',
            xml)
        xml = re.sub(
            r'<w:rFonts w:ascii="黑体" w:hAnsi="宋体" w:eastAsia="黑体" w:cs="Times New Roman"/>',
            '<w:rFonts w:ascii="黑体" w:hAnsi="黑体" w:eastAsia="黑体" w:cs="黑体"/>',
            xml)
        # H5 (styleId=16)：补 spacing / rFonts / sz
        m = re.search(
            r'(<w:style w:type="paragraph"[^>]*w:styleId="16"[^>]*>.*?<w:pPr>)(.*?)(</w:pPr>)',
            xml, re.DOTALL)
        if m and "<w:spacing" not in m.group(2):
            spacing = '<w:spacing w:before="40" w:after="20" w:line="276" w:lineRule="auto"/>'
            xml = xml[:m.start(2)] + m.group(2) + spacing + xml[m.end(2):]
        xml = re.sub(
            r'<w:rFonts w:ascii="宋体" w:hAnsi="黑体" w:eastAsia="宋体" w:cs="宋体"/>',
            '<w:rFonts w:ascii="黑体" w:hAnsi="黑体" w:eastAsia="黑体" w:cs="黑体"/>',
            xml)
        m2 = re.search(
            r'(<w:style w:type="paragraph"[^>]*w:styleId="16"[^>]*>.*?<w:rPr>)(.*?)(</w:rPr>)',
            xml, re.DOTALL)
        if m2 and "<w:sz " not in m2.group(2):
            xml = xml[:m2.start(2)] + m2.group(2) + \
                  '<w:sz w:val="21"/><w:szCs w:val="21"/>' + xml[m2.end(2):]
        return xml

    @staticmethod
    def _read_xml(path: str) -> str:
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    @staticmethod
    def _write_xml(path: str, content: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)


# ───────────────────────── beautify 辅助函数（模块级） ─────────────────────────

def _add_style_before_pPr(body: str, style_id: str) -> str:
    if re.search(r"<w:pStyle\s", body):
        return body
    if "<w:pPr>" in body:
        return body.replace("<w:pPr>", f'<w:pPr><w:pStyle w:val="{style_id}"/>')
    return f'<w:pPr><w:pStyle w:val="{style_id}"/></w:pPr>{body}'


def _strip_duplicate_heading_prefixes(
    xml: str, style_h4: str, style_h5: str,
) -> str:
    pattern = re.compile(r"(<w:p[ >])(.*?)(</w:p>)", re.DOTALL)
    edits: list[tuple[int, int, str]] = []
    style_re = re.compile(rf'w:val="{re.escape(style_h4)}|{re.escape(style_h5)}"')
    for match in pattern.finditer(xml):
        body = match.group(2)
        if not style_re.search(body):
            continue
        t_match = re.search(r"(<w:t[^>]*>)([^<]*)(</w:t>)", body)
        if not t_match:
            continue
        original = t_match.group(2).strip()
        if not original:
            continue
        new_text = re.sub(r"^\d+(\.\d+){3,}\s+(?=.)", "", original, count=1)
        if new_text != original and new_text.strip():
            edits.append((
                match.start(2) + t_match.start(),
                match.start(2) + t_match.end(),
                t_match.group(1) + new_text + t_match.group(3),
            ))
    result = xml
    for start, end, repl in reversed(edits):
        result = result[:start] + repl + result[end:]
    return result


def _apply_body_indent(xml: str, style_id: str) -> str:
    pattern = re.compile(r"(<w:p[ >])(.*?)(</w:p>)", re.DOTALL)
    edits: list[tuple[int, int, str]] = []
    for match in pattern.finditer(xml):
        body = match.group(2)
        if "<w:pStyle" in body:
            continue
        text = _join_run_texts(body)
        if not text:
            continue
        if re.match(r"^[\d\s.,;:\-+=\\/()\[\]\"'<>]+$", text):
            continue
        if re.match(r"^\d+\.", text):
            continue
        new_body = _add_style_before_pPr(body, style_id)
        if new_body != body:
            edits.append((match.start(2), match.end(2), new_body))
    result = xml
    for start, end, repl in reversed(edits):
        result = result[:start] + repl + result[end:]
    return result


def _join_run_texts(body: str) -> str:
    return "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", body)).strip()


def _is_short_heading_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > 60:
        return False
    if re.search(r"[。；！？]", stripped) and len(stripped) > 15:
        return False
    return not (re.match(r"^[a-zA-Z0-9\-\+,./\\()[\]]{2,}$", stripped) and len(stripped) < 20)


def _first_match_label(
    text: str,
    table: tuple[tuple[re.Pattern[str], int], ...],
) -> str | None:
    """按 pattern→level 表从上到下匹配，首个命中返回对应 h{level}。"""
    for pattern, level in table:
        if pattern.match(text):
            return f"h{level}"
    return None
