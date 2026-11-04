#!/usr/bin/env python3
"""登记并验证检查产物，提供增量复用候选；状态索引不替代报告证据。"""

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

KINDS = {"text", "image", "trace", "page-review", "report"}
STATUSES = {"进行中", "部分完成", "未完成", "已完成"}
REQUIRED_FIELDS = ("kind", "input_path", "input_sha256", "scope", "tool_contract", "artifact_path", "status")


def sha256_path(path):
    # Python 3.9+ 兼容（勿用仅 3.11+ 的 hashlib.file_digest）
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_state(path, allow_missing=False):
    if not path.is_file():
        if allow_missing:
            return {"schema_version": 1, "updated_at": "", "entries": []}
        raise ValueError(f"状态索引不存在：{path}")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"状态索引损坏，拒绝覆盖：{path}；{error}") from error
    if state.get("schema_version") != 1 or not isinstance(state.get("entries"), list):
        raise ValueError("状态索引结构不受支持，拒绝覆盖")
    return state


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(state, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def entry_identity(entry):
    material = {
        "kind": entry["kind"], "input_sha256": entry["input_sha256"],
        "scope": entry["scope"], "tool_contract": entry["tool_contract"],
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return f"{entry['kind']}-{hashlib.sha256(encoded).hexdigest()[:16]}"


def image_counts(entry):
    with Path(entry["artifact_path"]).open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows or "visual_review_status" not in rows[0]:
        raise ValueError("图片产物缺少 visual_review_status 列")
    reviewed = sum(row["visual_review_status"].startswith("已初筛") for row in rows)
    return {"registered": len(rows), "visual_reviewed": reviewed, "visual_pending": len(rows) - reviewed}


def verify_entry(entry):
    reasons = []
    details = {}
    source = Path(entry["input_path"])
    if not source.is_file():
        reasons.append("输入不可读")
    elif sha256_path(source) != entry["input_sha256"]:
        reasons.append("输入哈希已变化")
    artifact = Path(entry["artifact_path"])
    if not artifact.is_file():
        reasons.append("产物不可读")
    elif sha256_path(artifact) != entry.get("artifact_sha256"):
        reasons.append("产物哈希已变化")
    report = Path(entry.get("report_path", ""))
    if entry.get("report_path") and not report.is_file():
        reasons.append("关联报告不可读")
    if entry["status"] not in STATUSES:
        reasons.append("状态无效")

    if entry["kind"] == "text" and entry["scope"].get("page_mapping_verified") is not True:
        reasons.append("全文提取未验证分页映射")
    if entry["kind"] == "image" and artifact.is_file():
        try:
            counts = image_counts(entry)
            details.update(counts)
            expected = entry["scope"].get("object_count")
            if expected is not None and counts["registered"] != expected:
                reasons.append("图片对象数与范围不一致")
            if entry["status"] == "已完成" and counts["visual_pending"] != 0:
                reasons.append("图片已完成登记但仍有未初筛对象")
            for sheet in entry["scope"].get("contact_sheets", []):
                if not Path(sheet).is_file():
                    reasons.append(f"接触表不可读：{sheet}")
        except (OSError, ValueError, csv.Error) as error:
            reasons.append(f"图片清单校验失败：{error}")
    if entry["kind"] == "trace":
        expected = set(entry["scope"].get("input_hashes", []))
        if not expected or entry["input_sha256"] not in expected:
            reasons.append("痕迹范围哈希集合无效")
        if not entry.get("report_path"):
            reasons.append("痕迹条目缺主报告路径")
        elif report.is_file():
            script = Path(__file__).with_name("check_report_completeness.py")
            result = subprocess.run(
                [sys.executable, "-B", str(script), "--report", str(report)],
                capture_output=True, text=True,
            )
            details["trace_validator_exit"] = result.returncode
            if result.returncode != 0:
                reasons.append("痕迹主报告完整性校验失败")
    if entry["kind"] == "page-review":
        pages = entry.get("pages", [])
        if not pages or len(set(pages)) != len(pages) or any(not isinstance(page, int) or page < 1 for page in pages):
            reasons.append("逐页复核页号集合无效")
        evidence_value = entry.get("evidence_path", "")
        evidence = Path(evidence_value)
        if not evidence_value or not evidence.is_dir():
            reasons.append("逐页复核证据目录不可读")
        required = set(entry["scope"].get("required_pages", []))
        details["missing_pages"] = sorted(required - set(pages))
        if entry["status"] == "已完成" and details["missing_pages"]:
            reasons.append("逐页复核已完成但仍有应查页")
    return {"entry_id": entry["id"], "reusable": not reasons, "reasons": reasons, "details": details}


def validate_registration(entry):
    missing = [key for key in REQUIRED_FIELDS if key not in entry]
    if missing:
        raise ValueError("登记条目缺字段：" + ", ".join(missing))
    if entry["kind"] not in KINDS:
        raise ValueError("未知产物类型")
    if not isinstance(entry["scope"], dict):
        raise ValueError("scope 必须是对象")
    entry["artifact_sha256"] = sha256_path(Path(entry["artifact_path"]))
    entry["id"] = entry_identity(entry)
    return entry


def filtered_entries(state, args):
    entries = state["entries"]
    if getattr(args, "input_sha256", None):
        entries = [entry for entry in entries if entry["input_sha256"] == args.input_sha256]
    if getattr(args, "kind", None):
        entries = [entry for entry in entries if entry["kind"] == args.kind]
    if getattr(args, "tool_contract", None):
        entries = [entry for entry in entries if entry["tool_contract"] == args.tool_contract]
    if getattr(args, "scope", None):
        scope = json.loads(args.scope)
        entries = [entry for entry in entries if entry["scope"] == scope]
    return entries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("show", "verify"):
        sub = subparsers.add_parser(action)
        sub.add_argument("--state", required=True, type=Path)
        sub.add_argument("--input-sha256")
        sub.add_argument("--kind", choices=sorted(KINDS))
        sub.add_argument("--tool-contract")
        sub.add_argument("--scope", help="精确匹配的 JSON 对象")
    register = subparsers.add_parser("register")
    register.add_argument("--state", required=True, type=Path)
    register.add_argument("--entry", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.action == "register":
            state = load_state(args.state, allow_missing=True)
            entry = validate_registration(json.loads(args.entry.read_text(encoding="utf-8")))
            verification = verify_entry(entry)
            if not verification["reusable"]:
                print(json.dumps({"registered": False, **verification}, ensure_ascii=False, indent=2))
                return 1
            state["entries"] = [item for item in state["entries"] if item["id"] != entry["id"]]
            state["entries"].append(entry)
            save_state(args.state, state)
            print(json.dumps({"registered": True, "entry_id": entry["id"], **verification}, ensure_ascii=False, indent=2))
            return 0
        state = load_state(args.state, allow_missing=args.action == "show")
        entries = filtered_entries(state, args)
        results = [verify_entry(entry) for entry in entries]
        payload = {
            "state": str(args.state), "entry_count": len(results),
            "entries": results,
            "boundary": "reusable 只表示登记产物通过文件与范围校验，不代表任务已完成或内容正确。",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return int(args.action == "verify" and any(not item["reusable"] for item in results))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
