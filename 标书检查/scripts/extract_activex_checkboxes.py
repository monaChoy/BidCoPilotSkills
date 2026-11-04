#!/usr/bin/env python3
"""从 Word DOCX 提取 ActiveX Forms.CheckBox 的名称、题注与勾选值；只读。"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import struct
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from xml.etree import ElementTree as ET

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
AX_NS = "http://schemas.microsoft.com/office/2006/activeX"

CHECKBOX_CLASSIDS = {
    "{8BD21D40-EC42-11CE-9E0D-00AA006002F3}",  # Forms.CheckBox.1
    "{8BD21D50-EC42-11CE-9E0D-00AA006002F3}",
}


def qn(ns: str, local: str) -> str:
    return f"{{{ns}}}{local}"


def load_rels(z: zipfile.ZipFile, rels_path: str) -> dict[str, str]:
    if rels_path not in z.namelist():
        return {}
    root = ET.fromstring(z.read(rels_path))
    mapping = {}
    for rel in root:
        rid = rel.attrib.get("Id")
        target = rel.attrib.get("Target")
        if rid and target:
            mapping[rid] = target.replace("\\", "/")
    return mapping


def resolve_part(base: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    base_dir = str(Path(base).parent).replace("\\", "/")
    if base_dir in {"", "."}:
        return target
    parts = base_dir.split("/") + target.split("/")
    out: list[str] = []
    for part in parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if out:
                out.pop()
            continue
        out.append(part)
    return "/".join(out)


def utf16_strings(data: bytes, min_chars: int = 4) -> list[str]:
    found: list[str] = []
    # aligned UTF-16LE runs
    i = 0
    while i + 2 <= len(data):
        if data[i + 1] != 0 or data[i] == 0:
            i += 1
            continue
        chars: list[str] = []
        j = i
        while j + 2 <= len(data) and data[j + 1] == 0 and 32 <= data[j] < 127:
            chars.append(chr(data[j]))
            j += 2
        if len(chars) >= min_chars:
            found.append("".join(chars))
            i = j
            continue
        i += 1
    # Chinese / general UTF-16LE
    try:
        text = data.decode("utf-16le", errors="ignore")
    except UnicodeDecodeError:
        return found
    for match in re.finditer(r"[\u4e00-\u9fffA-Za-z0-9_（）()·\-]{2,40}", text):
        found.append(match.group(0))
    # de-dupe preserve order
    seen: set[str] = set()
    ordered: list[str] = []
    for item in found:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def parse_checkbox_bin(data: bytes) -> dict:
    strings = utf16_strings(data)
    name = next((s for s in strings if re.fullmatch(r"CheckBox\d+", s)), "")
    caption = ""
    for item in strings:
        if item == name or item.startswith("Forms.") or item in {"CheckBox", "Value", "True", "False"}:
            continue
        # OLE 属性袋里中文常被误解；只接受含业务关键词的可读串
        if any(key in item for key in ("评标", "暗标", "明标", "公开", "异地", "本地", "远程", "评审")):
            if re.search(r"[\u4e00-\u9fff]", item) and len(item) >= 2:
                caption = item
                break
    value = None
    value_source = ""
    # Common persistStorage layout for same-size Forms.CheckBox bins in 深圳模版：
    if len(data) >= 724:
        raw = struct.unpack_from("<I", data, 720)[0]
        if raw in (0x00000000, 0xFFFFFFFF):
            value = raw == 0xFFFFFFFF
            value_source = "dword@720"
    if value is None and b"Value" in data:
        # fallback: nearby int after ASCII 'Value'
        idx = data.find(b"Value")
        window = data[idx : idx + 64]
        for off in range(0, max(0, len(window) - 3)):
            raw = struct.unpack_from("<I", window, off)[0]
            if raw in (0x00000000, 0xFFFFFFFF):
                value = raw == 0xFFFFFFFF
                value_source = f"near:Value+{off}"
                break
    return {
        "name": name,
        "caption": caption,
        "strings": strings[:12],
        "value": value,
        "value_label": "已选" if value is True else ("未选" if value is False else "未知"),
        "value_source": value_source,
        "bin_size": len(data),
    }


def cell_text(cell: ET.Element) -> str:
    parts: list[str] = []
    for node in cell.iter():
        if node.tag == qn(W_NS, "t") and node.text:
            parts.append(node.text)
        elif node.tag == qn(W_NS, "sym"):
            font = node.attrib.get(qn(W_NS, "font"), "")
            char = node.attrib.get(qn(W_NS, "char"), "")
            parts.append(f"[SYM:{font}:{char}]")
    return "".join(parts)


def iter_control_rids(cell: ET.Element) -> list[str]:
    rids: list[str] = []
    for el in cell.iter():
        for key, val in el.attrib.items():
            local = key.rsplit("}", 1)[-1]
            if local in {"id", "embed", "link"} or key.endswith("}id"):
                if isinstance(val, str) and val.startswith("rId"):
                    rids.append(val)
    # de-dupe
    seen: set[str] = set()
    out: list[str] = []
    for rid in rids:
        if rid not in seen:
            seen.add(rid)
            out.append(rid)
    return out


def extract(docx_path: Path) -> dict:
    raw = docx_path.read_bytes()
    input_sha256 = hashlib.sha256(raw).hexdigest()
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        doc_rels = load_rels(z, "word/_rels/document.xml.rels")
        root = ET.fromstring(z.read("word/document.xml"))
        controls: list[dict] = []
        # index all activeX parts
        ax_parts = [n for n in z.namelist() if n.startswith("word/activeX/activeX") and n.endswith(".xml")]
        ax_meta: dict[str, dict] = {}
        for part in ax_parts:
            ax_root = ET.fromstring(z.read(part))
            classid = ax_root.attrib.get(qn(AX_NS, "classid"), ax_root.attrib.get("classid", ""))
            ax_rels = load_rels(z, str(Path(part).parent / "_rels" / (Path(part).name + ".rels")).replace("\\", "/"))
            # relationships file path convention
            rels_guess = f"{Path(part).parent.as_posix()}/_rels/{Path(part).name}.rels"
            ax_rels = load_rels(z, rels_guess)
            bin_target = ax_rels.get("rId1") or next(iter(ax_rels.values()), "")
            bin_part = resolve_part(part, bin_target) if bin_target else ""
            parsed = {"classid": classid.upper(), "xml_part": part, "bin_part": bin_part}
            if bin_part and bin_part in z.namelist():
                parsed.update(parse_checkbox_bin(z.read(bin_part)))
            ax_meta[part] = parsed

        # map document rIds that point to activeX xml
        rid_to_ax: dict[str, str] = {}
        for rid, target in doc_rels.items():
            part = resolve_part("word/document.xml", target)
            if part.startswith("word/activeX/") and part.endswith(".xml"):
                rid_to_ax[rid] = part

        # walk tables for nearby text
        context_rows: list[dict] = []
        for tbl_i, tbl in enumerate(root.findall(f".//{qn(W_NS, 'tbl')}")):
            for row_i, row in enumerate(tbl.findall(f"./{qn(W_NS, 'tr')}")):
                row_text = "".join(cell_text(c) for c in row.findall(f"./{qn(W_NS, 'tc')}"))
                for cell_i, cell in enumerate(row.findall(f"./{qn(W_NS, 'tc')}")):
                    for rid in iter_control_rids(cell):
                        ax_part = rid_to_ax.get(rid)
                        if not ax_part:
                            continue
                        meta = dict(ax_meta.get(ax_part, {}))
                        if meta.get("classid") and meta["classid"] not in CHECKBOX_CLASSIDS:
                            # still keep if bin looks like CheckBox
                            if not meta.get("name", "").startswith("CheckBox"):
                                continue
                        item = {
                            **meta,
                            "rId": rid,
                            "table_index": tbl_i,
                            "row_index": row_i,
                            "cell_index": cell_i,
                            "cell_text": cell_text(cell)[:120],
                            "row_text_snippet": re.sub(r"\s+", " ", row_text)[:180],
                        }
                        controls.append(item)
                        context_rows.append(item)

        # also list checkbox ax parts not anchored in tables
        anchored = {c.get("xml_part") for c in controls}
        for part, meta in ax_meta.items():
            if part in anchored:
                continue
            if meta.get("name", "").startswith("CheckBox") or meta.get("classid") in CHECKBOX_CLASSIDS:
                controls.append({**meta, "rId": "", "table_index": None, "row_index": None, "cell_index": None,
                                 "cell_text": "", "row_text_snippet": ""})

    def context_blob(c: dict) -> str:
        return "".join([
            c.get("caption") or "",
            c.get("cell_text") or "",
            c.get("row_text_snippet") or "",
            " ".join(c.get("strings") or []),
        ])

    dark_related = [
        c for c in controls
        if any(k in context_blob(c) for k in ("暗标", "明标", "明暗标", "技术标暗标"))
    ]
    for c in controls:
        # 展示优先用版式邻近文字，避免 OLE 误解码题注
        label = c.get("caption") or ""
        cell_plain = re.sub(r"\[SYM:[^\]]+\]", "", c.get("cell_text") or "")
        cell_plain = re.sub(r"\s+", "", cell_plain)
        if not label or not re.search(r"[\u4e00-\u9fff]", label):
            m = re.search(r"[\u4e00-\u9fffA-Za-z0-9、]{2,40}", cell_plain)
            label = m.group(0) if m else re.sub(r"\[SYM:[^\]]+\]", "", c.get("row_text_snippet") or "")[:40]
        c["display"] = label
    return {
        "file": str(docx_path),
        "input_sha256": input_sha256,
        "total_checkbox_count": len(controls),
        "checkbox_count": len(controls),
        "darkbid_related_count": len(dark_related),
        "darkbid_related": dark_related,
        "checkboxes": controls,
    }


def emit_text(result: dict, rows: list[dict], darkbid_only: bool) -> None:
    total = result.get("total_checkbox_count", result.get("checkbox_count", 0))
    print(f"文件: {result['file']}")
    if result.get("input_sha256"):
        print(f"SHA-256: {result['input_sha256']}")
    print(
        f"CheckBox 数: {len(rows)}"
        + (f"（仅明暗标相关；全量 {total}）" if darkbid_only else f"（全量 {total}）")
    )
    if not rows:
        if darkbid_only and total:
            print(
                f"明暗标关键词未命中任何控件（全量仍有 {total} 个 CheckBox）；"
                "请去掉 --darkbid-only 全量复核，勿直接视为「无勾选项」。"
            )
        else:
            print("未解析到 Forms.CheckBox。")
        return
    print(f"{'Value':<6} {'来源':<10} {'Name':<14} {'邻近文本'}")
    for item in rows:
        label = item.get("value_label") or "未知"
        name = item.get("name") or "-"
        text = (item.get("display") or item.get("cell_text") or item.get("row_text_snippet") or "")[:60]
        print(f"{label:<6} {item.get('value_source', ''):<10} {name:<14} {text}")


def main() -> int:
    parser = argparse.ArgumentParser(description="提取 DOCX 中 ActiveX CheckBox 勾选态")
    parser.add_argument("docx", type=Path, help="招标或其他 DOCX 路径")
    parser.add_argument("--json", action="store_true", help="输出 JSON（stdout；与 -o 的 .json 路径可并用）")
    parser.add_argument("-o", "--output", type=Path, help="写入文件；路径以 .json 结尾则强制 JSON 格式")
    parser.add_argument("--darkbid-only", action="store_true", help="仅输出与明暗标相关的控件")
    args = parser.parse_args()
    if not args.docx.is_file():
        raise SystemExit(f"文件不存在: {args.docx}")
    result = extract(args.docx)
    rows = result["darkbid_related"] if args.darkbid_only else result["checkboxes"]
    as_json = bool(args.json) or (
        args.output is not None and args.output.suffix.lower() == ".json"
    )

    if as_json:
        payload = {
            **result,
            "checkboxes": rows,
            "checkbox_count": len(rows),
            "total_checkbox_count": result["total_checkbox_count"],
            "darkbid_only": bool(args.darkbid_only),
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
            print(f"已写入 JSON: {args.output}", flush=True)
        else:
            print(text, end="")
        return 0

    if args.output is not None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            emit_text(result, rows, args.darkbid_only)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(buf.getvalue(), encoding="utf-8")
        print(f"已写入文本: {args.output}", flush=True)
        return 0

    emit_text(result, rows, args.darkbid_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
