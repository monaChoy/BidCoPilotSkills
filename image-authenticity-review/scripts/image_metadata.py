#!/usr/bin/env python3
"""只读整理图片元数据；依赖 Pillow，不作真伪判断。"""

import argparse
import hashlib
import json
from pathlib import Path

from PIL import ExifTags, Image


def json_value(value):
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "numerator") and hasattr(value, "denominator"):
        return {"numerator": value.numerator, "denominator": value.denominator}
    return str(value)


def jpeg_header(image):
    segments = []
    for marker, payload in image.applist:
        kind = "unclassified"
        for signature, label in ((b"Exif\x00\x00", "exif"),
                                 (b"http://ns.adobe.com/xap/1.0/\x00", "xmp"),
                                 (b"http://ns.adobe.com/xmp/extension/\x00", "extended_xmp"),
                                 (b"Photoshop 3.0\x00", "photoshop_resources"),
                                 (b"ICC_PROFILE\x00", "icc")):
            if payload.startswith(signature):
                kind = label
                break
        if marker == "COM":
            kind = "comment"
        segments.append({"marker": marker, "kind": kind, "bytes": len(payload),
                         "sha256": hashlib.sha256(payload).hexdigest(),
                         "raw_hex": payload.hex()})
    return {"scope": "pillow_header_before_first_scan",
            "segments": segments,
            "quantization_tables": json_value(image.quantization),
            "components": [{"id": item[0], "horizontal_sampling": item[1],
                            "vertical_sampling": item[2], "qtable_id": item[3]}
                           for item in image.layer],
            "huffman_tables": "not_extracted",
            "photoshop_resource_decoding": "not_performed",
            "extended_xmp_assembly": "not_performed"}


def inspect(path):
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    with Image.open(path) as image:
        header = jpeg_header(image) if image.format == "JPEG" else None
        image.load()
        exif = image.getexif()
        fields = {f"{tag}:{ExifTags.TAGS.get(tag, 'Unknown')}": json_value(value)
                  for tag, value in exif.items()}
        for tag, names in ((ExifTags.IFD.Exif, ExifTags.TAGS),
                           (ExifTags.IFD.GPSInfo, ExifTags.GPSTAGS)):
            if tag in exif:
                fields[f"{int(tag)}:IFD"] = {
                    f"{key}:{names.get(key, 'Unknown')}": json_value(value)
                    for key, value in exif.get_ifd(tag).items()
                }
        return {"path": str(path), "status": "read", "sha256": digest,
                "format": image.format, "size": list(image.size), "mode": image.mode,
                "frames": getattr(image, "n_frames", 1), "exif": fields,
                "info": json_value(image.info), "jpeg_header": header,
                "coverage": {"exif": "parsed" if exif else "not_returned_by_pillow",
                             "exif_ifd1_thumbnail": "not_parsed",
                             "png_chunk_inventory": "not_performed",
                             "pdf_docx_structure": "not_performed"},
                "c2pa_validation": "not_performed"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", nargs="+", type=Path)
    args = parser.parse_args()
    results = []
    for path in args.images:
        try:
            results.append(inspect(path))
        except Exception as error:
            # 单张失败也交付其他图片的结果，退出码仍反映不完整。
            results.append({"path": str(path), "status": "error",
                            "error": f"{type(error).__name__}: {error}"})
    print(json.dumps(results, ensure_ascii=False, indent=2, allow_nan=False))
    return int(any(item["status"] == "error" for item in results))


if __name__ == "__main__":
    raise SystemExit(main())
