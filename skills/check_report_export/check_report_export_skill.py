"""检查报告导出 Skill：把检查结果渲染成 Markdown / HTML / JSON 正文。

`execute` 只读 `ctx.parameters`：
- `report_data`（dict）：检查结果数据，需含 `checks`（list[dict]）与可选
  `summary` / `risk_level` / 其他字段。
- `format`（str）：`"markdown" | "html" | "json"`，缺省 `"markdown"`。
- `project_name`（str）：报告标题中的项目名，缺省 `"未命名项目"`。

纯字符串处理，不调用大模型、不读写文件、不依赖数据库。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import ClassVar

from skill_engine.base import Skill, SkillContext, SkillResult

logger = logging.getLogger(__name__)

_REPORT_SKIP_FIELDS = ("checks", "summary", "risk_level", "has_critical_issues")
_RISK_ICONS = {"high": "🔴", "medium": "🟡", "low": "🟢"}
_STATUS_MARKS = {"pass": "✅", "fail": "❌", "warning": "⚠️"}
_HTML_STYLE_RULES = (
    "body { font-family: 'Microsoft YaHei', sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; }",
    "h1 { color: #1a56db; border-bottom: 2px solid #1a56db; padding-bottom: 8px; }",
    "h2 { color: #374151; margin-top: 24px; }",
    "table { border-collapse: collapse; width: 100%; margin: 12px 0; }",
    "th, td { border: 1px solid #d1d5db; padding: 8px 12px; text-align: left; }",
    "th { background: #f3f4f6; font-weight: 600; }",
    ".risk-high { color: #dc2626; font-weight: 700; }",
    ".risk-medium { color: #d97706; font-weight: 700; }",
    ".risk-low { color: #059669; font-weight: 700; }",
    ".pass { color: #059669; } .fail { color: #dc2626; } .warning { color: #d97706; }",
    "hr { border: none; border-top: 1px solid #e5e7eb; margin: 24px 0; }",
)


class CheckReportExportSkill(Skill):
    name = "check_report_export"
    description = "检查报告导出(Markdown/PDF/HTML)"
    category = "check"
    version = "1.0.0"
    triggers: ClassVar[list[str]] = ["导出报告", "报告导出", "检查报告"]

    async def execute(self, ctx: SkillContext) -> SkillResult:
        report_input = ctx.parameters.get("report_data", {})
        export_format = ctx.parameters.get("format", "markdown")
        proj_label = ctx.parameters.get("project_name", "未命名项目")

        if not report_input:
            return SkillResult(success=False, error="无报告数据")

        export_body = self._build_body(report_input, export_format, proj_label)

        return SkillResult(
            success=True,
            data={
                "content": export_body,
                "format": export_format,
                "size": len(export_body.encode("utf-8")),
                "generated_at": datetime.now().isoformat(),
            },
        )

    def _build_body(self, data: dict, export_format: str, proj_label: str) -> str:
        """按目标格式产出报告正文；markdown 与未知格式都走 Markdown。"""
        if export_format == "html":
            return self._render_html(data, proj_label)
        if export_format == "json":
            return json.dumps(data, ensure_ascii=False, indent=2)
        return self._render_markdown(data, proj_label)

    def _render_markdown(self, data: dict, proj_label: str) -> str:
        """把报告数据渲染成 Markdown 文本。"""
        md_lines: list[str] = []
        md_lines += self._md_title_block(proj_label)

        check_rows = data.get("checks", [])
        if isinstance(check_rows, list):
            md_lines += self._md_overview(check_rows, data)
            md_lines += self._md_detail_table(check_rows)
            md_lines += self._md_failed_section(check_rows)
        elif isinstance(data, dict):
            md_lines += self._md_extra_fields(data)

        md_lines += self._md_footer()
        return "\n".join(md_lines)

    def _md_title_block(self, proj_label: str) -> list[str]:
        """报告标题与生成信息。"""
        return [
            "# 投标文件检查报告",
            "",
            f"**项目名称**: {proj_label}",
            f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]

    def _md_overview(self, check_rows: list, data: dict) -> list[str]:
        """概要段落：风险等级与计数。"""
        summary_data = data.get("summary", {})
        risk_grade = data.get("risk_level", "unknown")
        risk_icon = _RISK_ICONS.get(risk_grade, "⚪")

        out = [
            "## 检查概要",
            "",
            f"- 风险等级: {risk_icon} {risk_grade}",
        ]
        if summary_data:
            out.append(f"- 总检查项: {summary_data.get('total', len(check_rows))}")
            out.append(f"- 通过: {summary_data.get('passed', 0)}")
            out.append(f"- 不通过: {summary_data.get('failed', 0)}")
            out.append(f"- 警告: {summary_data.get('warning', 0)}")
        out.append("")
        return out

    def _md_detail_table(self, check_rows: list) -> list[str]:
        """详细检查结果表格。"""
        out = [
            "## 详细检查结果",
            "",
            "| 检查项 | 要求 | 实际 | 状态 | 说明 |",
            "|--------|------|------|------|------|",
        ]
        for check_row in check_rows:
            if not isinstance(check_row, dict):
                continue
            name = check_row.get("check_name", check_row.get("check_type", ""))
            req_text = str(check_row.get("required", ""))[:30]
            actual_text = str(check_row.get("actual", ""))[:30]
            row_status = check_row.get("status", "unknown")
            detail_text = str(check_row.get("detail", check_row.get("suggestion", "")))[:40]
            status_mark = _STATUS_MARKS.get(row_status, row_status)
            out.append(
                f"| {name} | {req_text} | {actual_text} | {status_mark} {row_status} | {detail_text} |"
            )
        out.append("")
        return out

    def _md_failed_section(self, check_rows: list) -> list[str]:
        """不通过项的明细段落。"""
        fail_rows = [
            cell for cell in check_rows
            if isinstance(cell, dict) and cell.get("status") == "fail"
        ]
        if not fail_rows:
            return []

        out = [
            "## ❌ 不通过项详情",
            "",
        ]
        for row_num, check_row in enumerate(fail_rows, 1):
            if not isinstance(check_row, dict):
                continue
            out.append(
                f"### {row_num}. {check_row.get('check_name', check_row.get('check_type', ''))}"
            )
            out.append("")
            out.append(f"- **招标要求**: {check_row.get('required', '')}")
            out.append(f"- **投标文件**: {check_row.get('actual', '')}")
            out.append(f"- **详细说明**: {check_row.get('detail', '')}")
            suggestion_text = check_row.get("suggestion", "")
            if suggestion_text:
                out.append(f"- **修改建议**: {suggestion_text}")
            out.append("")
        return out

    def _md_extra_fields(self, data: dict) -> list[str]:
        """固定字段之外的数据，JSON 结构原样内嵌。"""
        out: list[str] = []
        for field_name, field_value in data.items():
            if field_name in _REPORT_SKIP_FIELDS:
                continue
            out.append(f"## {field_name}")
            out.append("")
            if isinstance(field_value, (list, dict)):
                out.append("```json")
                out.append(json.dumps(field_value, ensure_ascii=False, indent=2)[:2000])
                out.append("```")
            else:
                out.append(f"{field_value}")
            out.append("")
        return out

    def _md_footer(self) -> list[str]:
        """报告页脚。"""
        return [
            "---",
            "*报告由 老马标书贤内助 自动生成*",
        ]

    def _render_html(self, data: dict, proj_label: str) -> str:
        """先出 Markdown，再逐行转成 HTML。"""
        md_text = self._render_markdown(data, proj_label)
        html_fragments = self._html_head(proj_label)
        html_fragments += self._html_body(md_text)
        html_fragments.append("</body></html>")
        return "\n".join(html_fragments)

    def _html_head(self, proj_label: str) -> list[str]:
        """HTML 头部与内联样式。"""
        return [
            "<!DOCTYPE html>",
            "<html><head><meta charset='utf-8'>",
            f"<title>检查报告 - {proj_label}</title>",
            "<style>",
            *_HTML_STYLE_RULES,
            "</style></head><body>",
        ]

    def _html_body(self, md_text: str) -> list[str]:
        """逐行转换 Markdown 正文。"""
        fragments: list[str] = []
        for md_line in md_text.split("\n"):
            fragment = self._html_line(md_line)
            if fragment is not None:
                fragments.append(fragment)
        return fragments

    def _html_line(self, md_line: str) -> str | None:
        """单行 Markdown 转 HTML 片段；空行不产出内容。"""
        if md_line.startswith("# "):
            return f"<h1>{md_line[2:]}</h1>"
        if md_line.startswith("## "):
            return f"<h2>{md_line[3:]}</h2>"
        if md_line.startswith("### "):
            return f"<h3>{md_line[4:]}</h3>"
        if md_line.startswith("- "):
            return f"<li>{md_line[2:]}</li>"
        if md_line.startswith("| ") and "|" in md_line[1:]:
            table_cells = [cell.strip() for cell in md_line.split("|")[1:-1]]
            html_row = "".join(f"<td>{cell}</td>" for cell in table_cells)
            return f"<tr>{html_row}</tr>"
        if md_line.startswith("---"):
            return "<hr>"
        if md_line.strip():
            return f"<p>{md_line}</p>"
        return None
