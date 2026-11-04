#!/usr/bin/env python3
"""只读提取 DOCX、PDF、XML 文件痕迹，输出完整清单和紧凑摘要。"""

import argparse
import collections
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

TRACE_PROPERTY_NAMES = {
    "author", "creator", "producer", "lastmodifiedby", "company", "manager", "template",
    "created", "modified", "creationdate", "moddate", "lastprinted", "revision",
    "title", "subject", "keywords", "description", "category", "contentstatus",
}
XML_TRACE_TERMS = (
    "author", "creator", "lastmodifiedby", "company", "organization", "operator",
    "compiler", "software", "serial", "dongle", "machine", "cpu", "mac",
    "作者", "编制人", "编制单位", "编制者", "单位", "软件", "序列号", "加密锁",
    "锁号", "注册码", "机器码", "创建人", "修改人", "保存人", "计算机", "设备",
    "硬盘", "网卡",
)
REVISION_TAGS = {
    "ins", "del", "moveFrom", "moveTo", "moveFromRangeStart", "moveFromRangeEnd",
    "moveToRangeStart", "moveToRangeEnd", "rPrChange", "pPrChange", "tblPrChange",
    "trPrChange", "tcPrChange", "sectPrChange", "numberingChange",
}
SUMMARY_GROUP_LIMIT = 50
SUMMARY_MATCH_LIMIT = 100
TEXT_FIELD_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(term) for term in XML_TRACE_TERMS) + r")\s*[:：=]\s*(.+?)\s*$",
    re.IGNORECASE,
)


def local_name(tag):
    return str(tag).rsplit("}", 1)[-1]


def sha256_path(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def iso_mtime(path):
    value = path.stat().st_mtime
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()


def preview(value, limit=1000):
    text = str(value)
    return {"text": text[:limit], "truncated": len(text) > limit, "characters": len(text)}


def parse_colon_lines(text):
    rows = []
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        rows.append({"name": name.strip(), "value": value.strip()})
    return rows


def property_value(element):
    return "".join(element.itertext()).strip()


def parse_properties(root, part):
    rows = []
    if local_name(root.tag) == "Properties" and any(
        local_name(child.tag) == "property" for child in root
    ):
        for item in root:
            if local_name(item.tag) != "property":
                continue
            value = property_value(item[0]) if len(item) else ""
            rows.append({
                "part": part,
                "name": item.attrib.get("name", ""),
                "value": value,
                "format": item.attrib.get("fmtid", ""),
            })
    else:
        for item in root:
            rows.append({"part": part, "name": local_name(item.tag), "value": property_value(item)})
    return rows


def is_trace_property(row):
    return bool(row["value"]) and row["name"].lower() in TRACE_PROPERTY_NAMES


def xml_parts_for_docx(names):
    selected = []
    for name in names:
        if not name.startswith("word/") or not name.endswith(".xml"):
            continue
        base = Path(name).name
        if base == "document.xml" or base == "settings.xml" or base.startswith(
            ("header", "footer", "comments", "footnotes", "endnotes")
        ):
            selected.append(name)
    return selected


def inspect_docx(path):
    details = {
        "properties": [], "revision_counts": {}, "comments": [], "hidden_runs": [],
        "hidden_format_elements": 0, "rsids": [], "headers_footers": [],
        "external_relationships": [], "custom_xml_parts": [], "embedded_objects": [],
        "macro_parts": [],
    }
    rsids = set()
    revisions = collections.Counter()
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if "word/document.xml" not in names:
            raise ValueError("ZIP 中没有 word/document.xml，不是支持的 DOCX")
        for part in ("docProps/core.xml", "docProps/app.xml", "docProps/custom.xml"):
            if part in names:
                root = ET.fromstring(archive.read(part))
                details["properties"].extend(parse_properties(root, part))
        for part in xml_parts_for_docx(names):
            root = ET.fromstring(archive.read(part))
            base = Path(part).name
            for element in root.iter():
                name = local_name(element.tag)
                if name in REVISION_TAGS:
                    revisions[name] += 1
                if name in {"vanish", "webHidden"}:
                    details["hidden_format_elements"] += 1
                if name == "rsid":
                    for key, value in element.attrib.items():
                        if local_name(key) == "val":
                            rsids.add(value)
                for key, value in element.attrib.items():
                    if local_name(key).startswith("rsid"):
                        rsids.add(value)
            if base.startswith("comments"):
                for comment in root.iter():
                    if local_name(comment.tag) != "comment":
                        continue
                    details["comments"].append({
                        "part": part,
                        "id": next((v for k, v in comment.attrib.items() if local_name(k) == "id"), ""),
                        "author": next((v for k, v in comment.attrib.items() if local_name(k) == "author"), ""),
                        "date": next((v for k, v in comment.attrib.items() if local_name(k) == "date"), ""),
                        "text": "".join(comment.itertext()).strip(),
                    })
            for run_index, run in enumerate(
                (item for item in root.iter() if local_name(item.tag) == "r"), 1
            ):
                run_properties = next((item for item in run if local_name(item.tag) == "rPr"), None)
                if run_properties is None:
                    continue
                kinds = [local_name(item.tag) for item in run_properties if local_name(item.tag) in {"vanish", "webHidden"}]
                if kinds:
                    details["hidden_runs"].append({
                        "part": part,
                        "run_index": run_index,
                        "kinds": kinds,
                        "text": "".join(
                            item.text or "" for item in run.iter()
                            if local_name(item.tag) in {"t", "delText"}
                        ),
                    })
            if base.startswith(("header", "footer")):
                details["headers_footers"].append({"part": part, **preview("".join(root.itertext()).strip())})
        for part in names:
            if part.endswith(".rels"):
                root = ET.fromstring(archive.read(part))
                for relation in root:
                    if relation.attrib.get("TargetMode") != "External":
                        continue
                    details["external_relationships"].append({
                        "part": part,
                        "id": relation.attrib.get("Id", ""),
                        "type": relation.attrib.get("Type", ""),
                        "target": relation.attrib.get("Target", ""),
                    })
        details["custom_xml_parts"] = sorted(
            name for name in names if name.startswith("customXml/") and name.endswith(".xml")
        )
        details["macro_parts"] = sorted(
            name for name in names if name.lower().endswith("vbaproject.bin")
        )
        for part in sorted(names):
            if not part.startswith(("word/embeddings/", "word/media/")) or part.endswith("/"):
                continue
            data = archive.read(part)
            details["embedded_objects"].append({
                "part": part, "bytes": len(data), "sha256": sha256_bytes(data),
            })
    details["revision_counts"] = dict(sorted(revisions.items()))
    details["rsids"] = sorted(rsids)
    trace_properties = [row for row in details["properties"] if is_trace_property(row)]
    non_hyperlink_external = [
        row for row in details["external_relationships"]
        if not row["type"].lower().endswith("/hyperlink")
    ]
    summary = {
        "trace_properties": trace_properties,
        "revision_elements": sum(revisions.values()),
        "comments": len(details["comments"]),
        "comment_authors": sorted({row["author"] for row in details["comments"] if row["author"]}),
        "hidden_runs": len(details["hidden_runs"]),
        "hidden_text": [preview(row["text"], 300) for row in details["hidden_runs"][:20]],
        "rsid_count": len(rsids),
        "headers_footers": details["headers_footers"][:20],
        "external_relationships": len(details["external_relationships"]),
        "non_hyperlink_external_relationships": non_hyperlink_external[:20],
        "custom_xml_parts": len(details["custom_xml_parts"]),
        "embedded_objects": len(details["embedded_objects"]),
        "macro_parts": details["macro_parts"],
        "not_covered": ["样式继承隐藏文字的完整展开", "OLE 对象内部内容", "正文语义查重"],
    }
    comparison_fields = [
        {"key": row["name"], "value": row["value"], "location": row["part"]}
        for row in trace_properties
    ]
    return details, summary, comparison_fields


def command_result(executable, arguments, path):
    command = [executable, *arguments, str(path)]
    result = subprocess.run(command, capture_output=True, text=True, errors="replace")
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def poppler_version(executable):
    result = subprocess.run([executable, "-v"], capture_output=True, text=True, errors="replace")
    return (result.stderr or result.stdout).strip()


def inspect_pdf(path):
    for tool in ("pdfinfo", "pdfdetach"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"找不到 Poppler 工具：{tool}")
    general = command_result("pdfinfo", ["-rawdates"], path)
    custom = command_result("pdfinfo", ["-custom", "-rawdates"], path)
    metadata_xml = command_result("pdfinfo", ["-meta"], path)
    javascript = command_result("pdfinfo", ["-js"], path)
    attachments = command_result("pdfdetach", ["-list"], path)
    commands = {
        "general": general, "custom": custom, "metadata_xml": metadata_xml,
        "javascript": javascript, "attachments": attachments,
    }
    info_rows = parse_colon_lines(general["stdout"]) if general["returncode"] == 0 else []
    custom_rows = parse_colon_lines(custom["stdout"]) if custom["returncode"] == 0 else []
    combined_rows = info_rows + [row for row in custom_rows if row not in info_rows]
    attachment_names = []
    if attachments["returncode"] == 0:
        attachment_names = [
            match.group(1).strip() for line in attachments["stdout"].splitlines()
            if (match := re.match(r"^\s*\d+:\s+(.+)$", line))
        ]
    failures = [name for name, result in commands.items() if result["returncode"] != 0]
    status = "error" if general["returncode"] != 0 else ("partial" if failures else "parsed")
    details = {
        "information": info_rows,
        "custom_information": custom_rows,
        "xmp": metadata_xml["stdout"] if metadata_xml["returncode"] == 0 else "",
        "javascript": javascript["stdout"] if javascript["returncode"] == 0 else "",
        "attachment_names": attachment_names,
        "commands": commands,
        "failed_commands": failures,
        "tools": {"pdfinfo": poppler_version("pdfinfo"), "pdfdetach": poppler_version("pdfdetach")},
        "not_covered": ["完整增量修订历史", "对象级来源鉴定", "可选内容组逐项审计", "批注外观逐项审计"],
    }
    trace_properties = [
        {"part": "PDF Info", "name": row["name"], "value": row["value"]}
        for row in combined_rows
        if row["value"] and row["name"].lower() in TRACE_PROPERTY_NAMES
    ]
    summary = {
        "trace_properties": trace_properties,
        "pages": next((row["value"] for row in info_rows if row["name"] == "Pages"), ""),
        "encrypted": next((row["value"] for row in info_rows if row["name"] == "Encrypted"), ""),
        "form": next((row["value"] for row in info_rows if row["name"] == "Form"), ""),
        "xmp_characters": len(details["xmp"]),
        "javascript_characters": len(details["javascript"]),
        "attachment_names": attachment_names[:50],
        "failed_commands": failures,
        "not_covered": details["not_covered"],
    }
    comparison_fields = [
        {"key": row["name"], "value": row["value"], "location": "PDF Info"}
        for row in trace_properties
    ]
    return status, details, summary, comparison_fields


def xml_match_value(element):
    direct_text = (element.text or "").strip()
    if direct_text:
        return direct_text
    for key, value in element.attrib.items():
        if local_name(key).lower() in {"value", "val", "text"}:
            return value.strip()
    return ""


def inspect_xml(path):
    tree = ET.parse(path)
    root = tree.getroot()
    matches = []
    element_count = 0

    def add(path_text, key, value, source):
        value = value.strip()
        if not value:
            return
        matches.append({
            "path": path_text, "key": key, "value": value[:4000],
            "value_truncated": len(value) > 4000, "source": source,
        })

    def walk(element, parent_path):
        nonlocal element_count
        element_count += 1
        name = local_name(element.tag)
        counts = collections.Counter(local_name(child.tag) for child in element)
        seen = collections.Counter()
        current_path = f"{parent_path}/{name}"
        if any(term.lower() in name.lower() for term in XML_TRACE_TERMS):
            add(current_path, name, xml_match_value(element), "element")
        for attr_name, attr_value in element.attrib.items():
            attr_local = local_name(attr_name)
            attr_path = f"{current_path}/@{attr_local}"
            if any(term.lower() in attr_local.lower() for term in XML_TRACE_TERMS):
                add(attr_path, attr_local, attr_value, "attribute")
            if attr_local.lower() in {"name", "key", "field", "label"} and any(
                term.lower() in attr_value.lower() for term in XML_TRACE_TERMS
            ):
                add(current_path, attr_value, xml_match_value(element), "named_field")
        direct_text = (element.text or "").strip()
        if len(direct_text) <= 500:
            text_match = TEXT_FIELD_RE.match(direct_text)
            if text_match:
                add(current_path, text_match.group(1), text_match.group(2), "text")
        for child in element:
            child_name = local_name(child.tag)
            seen[child_name] += 1
            walk(child, f"{current_path}[{seen[child_name]}of{counts[child_name]}]")

    walk(root, "")
    details = {
        "root": local_name(root.tag), "element_count": element_count, "matches": matches,
        "not_covered": ["专有计价二进制文件", "字段语义及外部真实性", "完整工程量清单输出"],
    }
    summary = {
        "root": local_name(root.tag), "element_count": element_count,
        "match_count": len(matches), "matches": matches[:SUMMARY_MATCH_LIMIT],
        "matches_truncated": len(matches) > SUMMARY_MATCH_LIMIT,
        "not_covered": details["not_covered"],
    }
    comparison_fields = [
        {"key": row["key"], "value": row["value"], "location": row["path"]}
        for row in matches
    ]
    return details, summary, comparison_fields


def detect_format(path):
    with path.open("rb") as stream:
        prefix = stream.read(4096)
    suffix = path.suffix.lower()
    if prefix.startswith(b"%PDF-"):
        return "pdf"
    if prefix.startswith(b"PK\x03\x04"):
        return "docx" if suffix == ".docx" else "zip"
    if prefix.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
        return "legacy-ole-container"
    if suffix == ".xml":
        return "xml"
    return "unknown"


def base_record(path):
    return {
        "path": str(path), "name": path.name, "extension": path.suffix.lower(),
        "bytes": path.stat().st_size, "mtime_utc": iso_mtime(path),
        "sha256": sha256_path(path), "detected_format": detect_format(path),
    }


def inspect_file(path):
    record = base_record(path)
    record.update({"status": "unsupported", "details": {}, "summary": {}, "comparison_fields": [], "errors": []})
    try:
        if record["detected_format"] == "docx":
            details, summary, fields = inspect_docx(path)
            record.update(status="parsed", details=details, summary=summary, comparison_fields=fields)
        elif record["detected_format"] == "pdf":
            status, details, summary, fields = inspect_pdf(path)
            record.update(status=status, details=details, summary=summary, comparison_fields=fields)
        elif record["detected_format"] == "xml":
            details, summary, fields = inspect_xml(path)
            record.update(status="parsed", details=details, summary=summary, comparison_fields=fields)
        else:
            record["summary"] = {"not_covered": ["该格式仅登记文件指纹，未解析内部结构"]}
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, ET.ParseError) as error:
        record["status"] = "error"
        record["errors"].append(f"{type(error).__name__}: {error}")
        record["summary"] = {"not_covered": ["解析失败"]}
    return record


def normalized_value(value):
    value = re.sub(r"\s+", " ", value).strip()
    return value if 0 < len(value) <= 500 else ""


def grouped_cross_file(groups, minimum=2):
    return [group for group in groups if len({item["path"] for item in group["locations"]}) >= minimum]


def compare_records(records):
    file_groups = collections.defaultdict(list)
    object_groups = collections.defaultdict(list)
    field_groups = collections.defaultdict(list)
    for record in records:
        file_groups[record["sha256"]].append({"path": record["path"]})
        for item in record["details"].get("embedded_objects", []):
            object_groups[item["sha256"]].append({"path": record["path"], "part": item["part"]})
        for item in record["comparison_fields"]:
            value = normalized_value(item["value"])
            if value:
                key = (item["key"].lower(), value)
                field_groups[key].append({"path": record["path"], "location": item["location"]})
    exact_files = [
        {"sha256": digest, "locations": locations}
        for digest, locations in file_groups.items() if len(locations) > 1
    ]
    embedded = grouped_cross_file([
        {"sha256": digest, "locations": locations} for digest, locations in object_groups.items()
    ])
    fields = grouped_cross_file([
        {"key": key[0], "value": key[1], "locations": locations}
        for key, locations in field_groups.items()
    ])
    return {"exact_file_groups": exact_files, "embedded_object_groups": embedded, "field_groups": fields}


def limited_groups(groups):
    return {"count": len(groups), "groups": groups[:SUMMARY_GROUP_LIMIT], "truncated": len(groups) > SUMMARY_GROUP_LIMIT}


def write_outputs(records, output):
    comparison = compare_records(records)
    status_counts = dict(collections.Counter(record["status"] for record in records))
    manifest = {
        "schema_version": 1,
        "inputs": records,
        "comparison": comparison,
        "status_counts": status_counts,
        "decision": "machine_extraction_only",
    }
    summary = {
        "schema_version": 1,
        "input_count": len(records),
        "status_counts": status_counts,
        "status": "complete" if set(status_counts) <= {"parsed"} else "incomplete",
        "files": [
            {
                "path": record["path"], "sha256": record["sha256"],
                "detected_format": record["detected_format"], "status": record["status"],
                "errors": record["errors"], "trace": record["summary"],
            }
            for record in records
        ],
        "comparison": {key: limited_groups(value) for key, value in comparison.items()},
        "decision": "machine_extraction_only",
        "interpretation": "字段、哈希或时间相同仅表示可复核事实，不代表串标、造假或招标不符合。",
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path, help="必须是不存在的新目录")
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"输出目录已存在，拒绝覆盖：{args.output}")
    missing = [str(path) for path in args.inputs if not path.is_file()]
    if missing:
        parser.error("输入不存在或不是文件：" + ", ".join(missing))
    args.output.mkdir(parents=True)
    records = [inspect_file(path) for path in args.inputs]
    summary = write_outputs(records, args.output)
    print(json.dumps({
        "output": str(args.output), "summary": str(args.output / "summary.json"),
        "input_count": len(records), "status_counts": summary["status_counts"],
        "comparison_counts": {
            key: value["count"] for key, value in summary["comparison"].items()
        },
    }, ensure_ascii=False, indent=2))
    return int(summary["status"] != "complete")


if __name__ == "__main__":
    raise SystemExit(main())
