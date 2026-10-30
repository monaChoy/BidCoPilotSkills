#!/usr/bin/env python3
"""使用临时虚构样本验证 PDF 原始 JPEG 与覆盖状态；依赖 pypdf、Pillow。"""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zlib

from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import (ArrayObject, DictionaryObject, EncodedStreamObject,
                          DecodedStreamObject, NameObject, NumberObject)

from image_metadata import inspect


def main():
    script = Path(__file__).with_name("pdf_raw_images.py")
    with tempfile.TemporaryDirectory(prefix="image-pdf-selfcheck-") as name:
        root = Path(name)
        buffer = io.BytesIO()
        exif = Image.Exif()
        exif[305] = "Adobe Photoshop (synthetic fixture)"
        Image.new("RGB", (8, 8), "white").save(buffer, format="JPEG", exif=exif)
        jpeg = buffer.getvalue()
        writer = PdfWriter()

        def add_image(data, filters):
            stream = EncodedStreamObject()
            stream._data = data
            stream.update({NameObject("/Type"): NameObject("/XObject"),
                           NameObject("/Subtype"): NameObject("/Image"),
                           NameObject("/Width"): NumberObject(8),
                           NameObject("/Height"): NumberObject(8),
                           NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                           NameObject("/BitsPerComponent"): NumberObject(8),
                           NameObject("/Filter"): filters})
            return writer._add_object(stream)

        image_ref = add_image(jpeg, NameObject("/DCTDecode"))
        flate_ref = add_image(zlib.compress(b"\xff" * 8 * 8 * 3), NameObject("/FlateDecode"))
        chain_ref = add_image(zlib.compress(jpeg), ArrayObject(
            [NameObject("/FlateDecode"), NameObject("/DCTDecode")]))
        bad_ref = add_image(b"broken JPEG", NameObject("/DCTDecode"))
        form = DecodedStreamObject()
        form.set_data(b"/Picture Do")
        form.update({NameObject("/Type"): NameObject("/XObject"),
                     NameObject("/Subtype"): NameObject("/Form"),
                     NameObject("/BBox"): ArrayObject([NumberObject(x) for x in (0, 0, 8, 8)]),
                     NameObject("/Resources"): writer._add_object(DictionaryObject({NameObject("/XObject"):
                         DictionaryObject({NameObject("/Picture"): image_ref})}))})
        form_ref = writer._add_object(form)
        for index in range(2):
            page = writer.add_blank_page(width=100, height=100)
            page[NameObject("/Resources")] = writer._add_object(DictionaryObject({NameObject("/XObject"):
                DictionaryObject({NameObject("/Picture"): image_ref,
                                  NameObject("/Form"): form_ref,
                                  NameObject("/Flate"): flate_ref,
                                  NameObject("/Chain"): chain_ref,
                                  NameObject("/Bad"): bad_ref})}))
            content = DecodedStreamObject()
            content.set_data(b"/Picture Do /Picture Do" if index == 0 else
                             b"/Form Do /Flate Do /Chain Do /Bad Do "
                             b"q BI /W 1 /H 1 /CS /G /BPC 8 ID \x00 EI Q\n")
            page[NameObject("/Contents")] = writer._add_object(content)
        pdf = root / "fixture.pdf"
        with pdf.open("wb") as stream:
            writer.write(stream)
        before = pdf.read_bytes()
        output = root / "extracted"
        command = [sys.executable, "-B", str(script), str(pdf), str(output)]
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run(command, capture_output=True, text=True, env=environment)
        assert result.returncode == 1, (result.stdout, result.stderr)
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["authenticity_decision"] == "not_performed"
        assert len(manifest["images"]) == 4, manifest["issues"]
        row = next(item for item in manifest["images"] if item["object_id"] == image_ref.idnum)
        assert len(row["references"]) == 3
        assert {ref["page_index_1based"] for ref in row["references"]} == {1, 2}
        assert row["references"][-1]["resource_path"] == ["/Form", "/Picture"]
        assert (output / row["file"]).read_bytes() == jpeg
        assert row["raw_stream_sha256"] == hashlib.sha256(jpeg).hexdigest()
        assert row["metadata"]["exif"]["305:Software"] == exif[305]
        assert row["native_region_review"] == row["thumbnail_review"] == "not_performed"
        pixels = Image.open(io.BytesIO(jpeg)).convert("RGB")
        converted = root / "converted.png"
        Image.frombytes(pixels.mode, pixels.size, pixels.tobytes()).save(converted)
        assert "305:Software" not in inspect(converted)["exif"]
        statuses = [item["extraction_status"] for item in manifest["images"]]
        assert statuses.count("unsupported_filter") == 2 and statuses.count("error") == 1
        assert any(item["status"] == "unsupported_inline_image" for item in manifest["issues"])
        retry = subprocess.run(command, capture_output=True, text=True, env=environment)
        assert retry.returncode != 0 and (output / row["file"]).read_bytes() == jpeg
        assert pdf.read_bytes() == before
        writer.pages[1]["/Contents"].set_data(b"/Form Do\n")
        supported = root / "supported.pdf"
        with supported.open("wb") as stream:
            writer.write(stream)
        successful = subprocess.run([sys.executable, "-B", str(script), str(supported),
                                     str(root / "supported-output")],
                                    capture_output=True, text=True, env=environment)
        assert successful.returncode == 0, (successful.stdout, successful.stderr)
        summary = json.loads(successful.stdout)
        assert summary["unique_image_objects"] == 1 and not summary["incomplete_within_scope"]
        print("通过：原始 JPEG 字节与 EXIF、转换副本字段丢失对照、同对象多次引用及嵌套 Form、未支持滤镜与内联图、失败项、复核状态、原件与已有输出保护、成功退出状态。")


if __name__ == "__main__":
    main()
