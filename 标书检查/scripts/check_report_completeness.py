#!/usr/bin/env python3
"""校验标书主报告、文件痕迹产物及结构化交接声明；只读文件。"""

import argparse
import hashlib
import json
import re
from pathlib import Path

STATUS_PATTERN = "进行中|部分完成|未完成|已完成"
BRANCH_STATUSES = {"未执行", "进行中", "部分完成", "未完成", "已完成"}
TRACE_TOPIC_RE = re.compile(r"文件痕迹|痕迹分支|file-trace-review", re.IGNORECASE)
INSTRUCTION_RE = re.compile(r"(?:不得|不能|不应|不可)(?:写|标|声明|称为|算)")


def clean_status(value):
    return value.replace("**", "").replace("`", "").strip()


def status_in_text(text, label="状态"):
    match = re.search(
        rf"^-\s*{label}[：:]\s*(?:\*\*)?({STATUS_PATTERN})",
        text,
        re.MULTILINE,
    )
    return match.group(1) if match else ""


def section(text, heading_pattern):
    match = re.search(rf"^##\s+{heading_pattern}[^\n]*$", text, re.MULTILINE)
    if not match:
        return ""
    next_heading = re.search(r"^##\s+", text[match.end():], re.MULTILINE)
    end = match.end() + next_heading.start() if next_heading else len(text)
    return text[match.end():end]


def field_value(text, label):
    match = re.search(rf"^-\s*{re.escape(label)}[：:]\s*(.*)$", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def hashes_in_value(value):
    return set(re.findall(r"(?i)(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", value))


def registered_hashes(main_text):
    inputs = section(main_text, "输入版本、范围与盲区")
    if not inputs:
        return set()
    return set(re.findall(r"(?i)SHA-256\s*[：:]?\s*`?([0-9a-f]{64})`?", inputs))


def path_reference(value):
    backtick = re.search(r"`([^`]+)`", value)
    reference = backtick.group(1).strip() if backtick else value.strip()
    return "" if reference in {"", "—", "-", "无", "不适用", "待补", "待定"} else reference


def with_trace_filename(path, reference):
    if reference.endswith(("/", "\\")) or path.is_dir() or not path.suffix:
        return path / "文件痕迹报告.md"
    return path


def resolve_trace_report(main_report, reference):
    path = Path(reference)
    if not path.is_absolute():
        path = main_report.parent / path
    return with_trace_filename(path, reference)


def resolve_handoff_report(main_report, handoff, reference):
    path = Path(reference)
    candidates = [path] if path.is_absolute() else [
        main_report.parent / path,
        main_report.parent.parent.parent / path,
        handoff.parent / path,
    ]
    return [with_trace_filename(candidate, reference) for candidate in candidates]


def load_document_summary(trace_report):
    direct = trace_report.parent / "文档痕迹" / "summary.json"
    merged = trace_report.parent / "机器汇总.json"
    if direct.is_file():
        return json.loads(direct.read_text(encoding="utf-8")), direct
    if merged.is_file():
        data = json.loads(merged.read_text(encoding="utf-8"))
        document = data.get("branches", {}).get("document", {})
        if document:
            return document, merged
    raise ValueError("找不到文档痕迹/summary.json 或含文档分支的机器汇总.json")


def sha256_path(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def trace_result():
    return {
        "branch_status": "", "trace_report": "", "trace_status": "",
        "expected_hashes": [], "actual_hashes": [], "evidence_valid": False,
    }


def validate_scope(main_text, trace, issues):
    section_text = section(main_text, "文件痕迹检查")
    expected = hashes_in_value(field_value(section_text, "痕迹应检输入SHA-256"))
    trace["expected_hashes"] = sorted(expected)
    if not expected:
        issues.append("主报告痕迹小节缺少非空的痕迹应检输入SHA-256集合")
    registered = registered_hashes(main_text)
    if not registered:
        issues.append("主报告输入版本区未登记SHA-256")
    elif not expected <= registered:
        issues.append("痕迹应检输入包含主报告未登记的SHA-256")
    return expected


def validate_trace(main_report, main_text, overall_status):
    result = trace_result()
    issues = []
    warnings = []
    section_text = section(main_text, "文件痕迹检查")
    if not section_text:
        return result, ["主报告缺少 `## 文件痕迹检查` 小节"], warnings
    branch = clean_status(field_value(section_text, "分支状态"))
    result["branch_status"] = branch
    if branch not in BRANCH_STATUSES:
        issues.append("文件痕迹分支状态缺失或无效")
        return result, issues, warnings
    expected = validate_scope(main_text, result, issues)
    if overall_status == "已完成" and branch != "已完成":
        issues.append(f"主报告标已完成，但文件痕迹分支为{branch}")
    if branch == "进行中" and overall_status != "进行中":
        issues.append("进行中的文件痕迹分支不能作为结案状态交付")
    if branch in {"未执行", "未完成", "进行中"}:
        return result, issues, warnings

    reference = path_reference(field_value(section_text, "配套报告及证据目录"))
    if not reference:
        issues.append(f"文件痕迹分支为{branch}，但未提供配套报告路径")
        return result, issues, warnings
    trace_report = resolve_trace_report(main_report, reference)
    result["trace_report"] = str(trace_report)
    if not trace_report.is_file():
        issues.append(f"文件痕迹报告不存在：{trace_report}")
        return result, issues, warnings
    trace_text = trace_report.read_text(encoding="utf-8")
    trace_status = status_in_text(trace_text, "检查执行状态")
    result["trace_status"] = trace_status
    if trace_status != branch:
        issues.append(f"主报告分支状态{branch}与痕迹报告状态{trace_status or '缺失'}不一致")
    try:
        summary, summary_path = load_document_summary(trace_report)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        issues.append(str(error))
        return result, issues, warnings
    files = summary.get("files", [])
    actual = {item.get("sha256", "") for item in files if item.get("sha256")}
    result["actual_hashes"] = sorted(actual)
    if actual != expected:
        issues.append("痕迹机器摘要输入集合与痕迹应检输入集合不相等")
    if branch == "已完成" and summary.get("status") != "complete":
        issues.append(f"痕迹分支已完成，但文档机器摘要状态为{summary.get('status', '缺失')}")
    for item in files:
        source = Path(item.get("path", ""))
        expected_hash = item.get("sha256", "")
        if source.is_file() and expected_hash:
            if sha256_path(source) != expected_hash:
                issues.append(f"原文件当前哈希与痕迹摘要不一致：{source}")
        else:
            warnings.append(f"原文件不可达，未实算哈希：{source}")
    result["evidence_valid"] = not issues
    return result, issues, warnings


def narrative_claims(text):
    claims = []
    for sentence in re.split(r"[。；\n]+", text):
        clauses = re.split(r"，(?=但|但是|然而)", sentence)
        trace_context = False
        for clause in clauses:
            if TRACE_TOPIC_RE.search(clause):
                trace_context = True
            generic_branch = "分支" in clause
            other_branch = re.search(r"图片|图像|评分|证件|报价|清单", clause) and not TRACE_TOPIC_RE.search(clause)
            if not (TRACE_TOPIC_RE.search(clause) or generic_branch) or other_branch:
                continue
            if INSTRUCTION_RE.search(clause) or re.match(r"^\s*(?:若|如果|当)", clause):
                continue
            if "已完成" in clause:
                claims.append({"kind": "已完成", "text": clause.strip()})
            if "已执行" in clause:
                claims.append({"kind": "已执行", "text": clause.strip()})
    return claims


def validate_handoff(main_report, path, trace):
    text = path.read_text(encoding="utf-8")
    issues = []
    status = clean_status(field_value(text, "文件痕迹分支状态"))
    if status not in BRANCH_STATUSES:
        return ["交接文档缺少有效的文件痕迹分支状态字段"], [], status
    if status != trace.get("branch_status"):
        issues.append("交接文档与主报告的文件痕迹分支状态不一致")
    expected = set(trace.get("expected_hashes", []))
    supplied = hashes_in_value(field_value(text, "文件痕迹输入SHA-256"))
    reference = path_reference(field_value(text, "文件痕迹报告"))
    if status in {"已完成", "部分完成"}:
        if not trace.get("evidence_valid"):
            issues.append("交接文档声明已有痕迹结果，但主报告痕迹证据无效")
        if not reference:
            issues.append("交接文档已有痕迹结果，但未填写实际报告路径")
        else:
            candidates = resolve_handoff_report(main_report, path, reference)
            actual = Path(trace.get("trace_report", ""))
            if not any(candidate == actual and candidate.is_file() for candidate in candidates):
                issues.append("交接文档的痕迹报告路径与主报告实际产物不一致")
        if supplied != expected:
            issues.append("交接文档未附上痕迹应检输入的完整SHA-256集合")
    elif reference:
        candidates = resolve_handoff_report(main_report, path, reference)
        existing = next((candidate for candidate in candidates if candidate.is_file()), None)
        if existing is None:
            issues.append("交接文档提供的续查痕迹报告不存在")
        elif status_in_text(existing.read_text(encoding="utf-8"), "检查执行状态") != status:
            issues.append("交接文档续查路径对应的状态与结构化状态不一致")
        if supplied != expected:
            issues.append("交接文档提供续查产物时未附上完整SHA-256集合")
    claims = narrative_claims(text)
    for claim in claims:
        if claim["kind"] == "已完成" and status != "已完成":
            issues.append(f"交接正文与结构化状态矛盾：{claim['text']}")
        if claim["kind"] == "已执行" and status not in {"已完成", "部分完成"}:
            issues.append(f"交接正文与结构化状态矛盾：{claim['text']}")
    return issues, claims, status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--handoff", type=Path)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    if not args.report.is_file():
        parser.error(f"主报告不存在：{args.report}")
    if args.handoff and not args.handoff.is_file():
        parser.error(f"交接文档不存在：{args.handoff}")
    main_text = args.report.read_text(encoding="utf-8")
    overall = status_in_text(main_text)
    issues = [] if overall else ["主报告缺少有效整体状态"]
    trace, trace_issues, warnings = validate_trace(args.report, main_text, overall)
    issues.extend(trace_issues)
    claims = []
    handoff_status = ""
    if args.handoff:
        handoff_issues, claims, handoff_status = validate_handoff(args.report, args.handoff, trace)
        issues.extend(handoff_issues)
    if args.require_complete and overall != "已完成":
        issues.append(f"要求完整结案，但主报告状态为{overall or '未知'}")
    result = {
        "report": str(args.report), "overall_status": overall, "trace": trace,
        "handoff": str(args.handoff) if args.handoff else "",
        "handoff_status": handoff_status, "handoff_claims": claims,
        "issues": issues, "warnings": warnings, "valid": not issues,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(bool(issues))


if __name__ == "__main__":
    raise SystemExit(main())
