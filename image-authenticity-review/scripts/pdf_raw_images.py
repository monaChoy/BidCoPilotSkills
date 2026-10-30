#!/usr/bin/env python3
"""登记 PDF 页面绘制的图片，按原字节提取直接 DCT JPEG；依赖 pypdf、Pillow。"""

import argparse
import hashlib
import json
from pathlib import Path

import pypdf
from pypdf.generic import ContentStream

from image_metadata import inspect


def extract(source, output):
    reader = pypdf.PdfReader(source)
    if reader.is_encrypted:
        raise ValueError("暂不处理加密 PDF，请先提供已授权解密的工作副本")
    output.mkdir(parents=True, exist_ok=False)
    images = {}
    issues = []

    def record_image(obj, reference):
        indirect = obj.indirect_reference
        if indirect is None:
            raise ValueError("图片缺少间接对象标识，无法稳定登记")
        identity = f"{indirect.idnum}-{indirect.generation}"
        if identity in images:
            images[identity]["references"].append(reference)
            return
        filters = obj.get("/Filter", [])
        if hasattr(filters, "get_object"):
            filters = filters.get_object()
        filters = list(filters) if isinstance(filters, list) else [filters]
        filters = [str(item) for item in filters]
        row = {"object_id": indirect.idnum, "generation": indirect.generation,
               "filters": filters, "width": obj.get("/Width"), "height": obj.get("/Height"),
               "color_space": str(obj.get("/ColorSpace")),
               "bits_per_component": obj.get("/BitsPerComponent"),
               "has_mask": "/Mask" in obj or "/SMask" in obj,
               "has_decode_transform": "/Decode" in obj,
               "references": [reference], "source_kind": "pdf_parser_encoded_stream",
               "extraction_status": "not_attempted", "raw_metadata": "not_examined",
               "thumbnail_review": "not_performed", "native_region_review": "not_performed",
               "page_review": "not_performed"}
        images[identity] = row
        try:
            # shortcut: 使用 pypdf 私有原始缓冲区，升级库后先跑合成 PDF 自检。
            raw = obj._data
            if not isinstance(raw, bytes):
                raise ValueError("解析器未提供原始编码字节，不能回退到转换导出")
            row["raw_stream_sha256"] = hashlib.sha256(raw).hexdigest()
            row["raw_stream_bytes"] = len(raw)
            if filters not in (["/DCTDecode"], ["/DCT"]):
                row["extraction_status"] = "unsupported_filter"
                return
            if not raw.startswith(b"\xff\xd8"):
                raise ValueError("DCT 流不是独立 JPEG 起始字节，不能作为原始 JPEG 检查")
            target = output / f"object-{identity}.jpg"
            with target.open("xb") as stream:
                stream.write(raw)
            row.update(file=target.name, extraction_status="raw_jpeg_saved",
                       byte_preservation="parser_buffer_written_unchanged")
            row["metadata"] = inspect(target)
            row["raw_metadata"] = "read_with_documented_tool_limits"
        except Exception as error:
            # 保留已取得的流信息，单个对象失败不掩盖其余对象及引用。
            row["extraction_status"] = "error"
            row["error"] = f"{type(error).__name__}: {error}"

    def walk(content, resources, page, path, ancestors):
        resources = resources.get_object() if hasattr(resources, "get_object") else resources
        for index, (operands, operator) in enumerate(ContentStream(content, reader).operations):
            reference = {"page_index_1based": page, "resource_path": path,
                         "operation_index_0based": index}
            if operator == b"INLINE IMAGE":
                issues.append({**reference, "status": "unsupported_inline_image"})
            if operator != b"Do":
                continue
            try:
                name = str(operands[0])
                reference["resource_path"] = [*path, name]
                obj = resources["/XObject"][name].get_object()
                if obj.get("/Subtype") == "/Image":
                    record_image(obj, reference)
                elif obj.get("/Subtype") == "/Form":
                    identity = id(obj)
                    if identity in ancestors:
                        raise ValueError("Form 循环引用，停止该分支")
                    walk(obj, obj.get("/Resources", resources), page,
                         reference["resource_path"], ancestors | {identity})
            except Exception as error:
                issues.append({**reference, "status": "error",
                               "error": f"{type(error).__name__}: {error}"})

    for page_number, page in enumerate(reader.pages, 1):
        try:
            walk(page.get_contents(), page.get("/Resources", {}), page_number, [], set())
        except Exception as error:
            issues.append({"page_index_1based": page_number, "status": "error",
                           "error": f"{type(error).__name__}: {error}"})
    with source.open("rb") as stream:
        source_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    manifest = {"pdf": str(source), "pdf_sha256": source_hash,
                "pypdf_version": pypdf.__version__, "pages": len(reader.pages),
                "scope": "page_content_Do_and_nested_Forms",
                "not_covered": ["annotation_appearances", "patterns", "unused_resources",
                                "mask_image_traversal", "full_pdf_revision_audit"],
                "stream_semantics": "parser_encoded_bytes_not_physical_pdf_offsets",
                "images": list(images.values()), "issues": issues,
                "authenticity_decision": "not_performed"}
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output", type=Path, help="必须是不存在的新目录")
    args = parser.parse_args()
    manifest = extract(args.pdf, args.output)
    incomplete = bool(manifest["issues"]) or any(
        row["extraction_status"] != "raw_jpeg_saved" for row in manifest["images"])
    print(json.dumps({"manifest": str(args.output / "manifest.json"),
                      "unique_image_objects": len(manifest["images"]),
                      "incomplete_within_scope": incomplete}, ensure_ascii=False))
    return int(incomplete)


if __name__ == "__main__":
    raise SystemExit(main())
