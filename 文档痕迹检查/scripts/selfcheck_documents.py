#!/usr/bin/env python3
"""使用临时合成 DOCX、PDF、XML 验证文档痕迹提取与覆盖状态。"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile


def make_docx(path, body):
    core = """<?xml version="1.0" encoding="UTF-8"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/">
<dc:creator>合成作者</dc:creator><cp:lastModifiedBy>合成保存人</cp:lastModifiedBy><dcterms:created>2026-01-02T03:04:05Z</dcterms:created></cp:coreProperties>"""
    app = """<?xml version="1.0" encoding="UTF-8"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Company>合成单位</Company><Template>合成模板.dotx</Template></Properties>"""
    custom = """<?xml version="1.0" encoding="UTF-8"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"><property name="项目痕迹" fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}"><lpwstr>合成自定义值</lpwstr></property></Properties>"""
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
<w:p w:rsidR="00AA11BB"><w:r><w:t>{body}</w:t></w:r></w:p>
<w:p><w:ins w:author="合成修订者"><w:r><w:t>新增修订</w:t></w:r></w:ins></w:p>
<w:p><w:r><w:rPr><w:vanish/></w:rPr><w:t>合成隐藏文字</w:t></w:r></w:p>
</w:body></w:document>"""
    comments = """<?xml version="1.0" encoding="UTF-8"?>
<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:comment w:id="1" w:author="合成批注者" w:date="2026-01-02T03:04:05Z"><w:p><w:r><w:t>合成批注</w:t></w:r></w:p></w:comment></w:comments>"""
    settings = """<?xml version="1.0" encoding="UTF-8"?>
<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:rsids><w:rsid w:val="00AA11BB"/></w:rsids></w:settings>"""
    header = """<?xml version="1.0" encoding="UTF-8"?>
<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>旧项目合成页眉</w:t></w:r></w:p></w:hdr>"""
    rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate" Target="file:///tmp/synthetic.dotx" TargetMode="External"/></Relationships>"""
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>"""
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in {
            "[Content_Types].xml": content_types,
            "docProps/core.xml": core,
            "docProps/app.xml": app,
            "docProps/custom.xml": custom,
            "word/document.xml": document,
            "word/comments.xml": comments,
            "word/settings.xml": settings,
            "word/header1.xml": header,
            "word/_rels/settings.xml.rels": rels,
            "customXml/item1.xml": "<synthetic/>",
        }.items():
            archive.writestr(name, text)
        archive.writestr("word/embeddings/synthetic.bin", b"shared-embedded-object")
        archive.writestr("word/media/synthetic.png", b"shared-image-object")
        archive.writestr("word/vbaProject.bin", b"synthetic-macro")


def make_pdf(path):
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >>",
        b"<< /Length 0 >>\nstream\n\nendstream",
        b"<< /Title (Synthetic PDF) /Author (Synthetic Author) /Producer (Synthetic Producer) /CreationDate (D:20260102030405Z) >>",
    ]
    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode("ascii"))
        data.extend(obj)
        data.extend(b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    data.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    data.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 5 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    path.write_bytes(data)


def run(script, inputs, output):
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, "-B", str(script), *(str(path) for path in inputs), "--output", str(output)],
        capture_output=True, text=True, env=environment,
    )


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    script = Path(__file__).with_name("inspect_documents.py")
    with tempfile.TemporaryDirectory(prefix="document-trace-selfcheck-") as name:
        root = Path(name)
        first = root / "first.docx"
        second = root / "second.docx"
        duplicate = root / "duplicate.docx"
        pdf = root / "sample.pdf"
        xml = root / "cost.xml"
        make_docx(first, "NORMAL-BODY-TEXT-ONE")
        make_docx(second, "NORMAL-BODY-TEXT-TWO")
        shutil.copyfile(first, duplicate)
        make_pdf(pdf)
        xml.write_text(
            "<Cost><Software>合成计价软件</Software><Serial>LOCK-SYNTHETIC</Serial>"
            "<Item><Description>UNRELATED-BOQ-TEXT</Description></Item></Cost>",
            encoding="utf-8",
        )
        inputs = [first, second, duplicate, pdf, xml]
        before = {path: digest(path) for path in inputs}
        output = root / "output"
        result = run(script, inputs, output)
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert all(digest(path) == value for path, value in before.items())
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        assert summary["status"] == "complete"
        assert summary["status_counts"] == {"parsed": 5}
        rows = {Path(row["path"]).name: row for row in manifest["inputs"]}
        doc = rows["first.docx"]
        assert doc["details"]["revision_counts"] == {"ins": 1}
        assert len(doc["details"]["comments"]) == 1
        assert len(doc["details"]["hidden_runs"]) == 1
        assert doc["details"]["rsids"] == ["00AA11BB"]
        assert doc["details"]["custom_xml_parts"] == ["customXml/item1.xml"]
        assert doc["details"]["macro_parts"] == ["word/vbaProject.bin"]
        assert len(doc["details"]["external_relationships"]) == 1
        assert len(doc["details"]["embedded_objects"]) == 2
        pdf_row = rows["sample.pdf"]
        properties = {row["name"]: row["value"] for row in pdf_row["details"]["information"]}
        assert properties["Author"] == "Synthetic Author"
        assert properties["Producer"] == "Synthetic Producer"
        pdf_summary = next(row for row in summary["files"] if row["path"].endswith("sample.pdf"))
        assert pdf_summary["trace"]["pages"] == "1"
        assert pdf_summary["trace"]["encrypted"] == "no"
        xml_row = rows["cost.xml"]
        assert xml_row["details"]["element_count"] == 5
        assert {row["key"] for row in xml_row["details"]["matches"]} == {"Software", "Serial"}
        comparison = manifest["comparison"]
        assert len(comparison["exact_file_groups"]) == 1
        assert comparison["embedded_object_groups"]
        assert any(row["key"] == "creator" and row["value"] == "合成作者" for row in comparison["field_groups"])
        summary_text = json.dumps(summary, ensure_ascii=False)
        assert "NORMAL-BODY-TEXT-ONE" not in summary_text
        assert "NORMAL-BODY-TEXT-TWO" not in summary_text
        assert "UNRELATED-BOQ-TEXT" not in summary_text
        protected_summary = (output / "summary.json").read_bytes()
        retry = run(script, inputs, output)
        assert retry.returncode != 0
        assert (output / "summary.json").read_bytes() == protected_summary

        legacy = root / "legacy.doc"
        legacy.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"synthetic")
        malformed = root / "bad.xml"
        malformed.write_text("<Cost><Software>broken</Cost>", encoding="utf-8")
        failed = run(script, [legacy, malformed], root / "failed-output")
        assert failed.returncode == 1, (failed.stdout, failed.stderr)
        failed_manifest = json.loads((root / "failed-output" / "manifest.json").read_text(encoding="utf-8"))
        assert failed_manifest["status_counts"] == {"error": 1, "unsupported": 1}

        utf16_le = root / "utf16-le.xml"
        utf16_be = root / "utf16-be.xml"
        xml_text = "<?xml version=\"1.0\" encoding=\"UTF-16\"?><Cost><Software>UTF16合成软件</Software></Cost>"
        utf16_le.write_bytes(xml_text.encode("utf-16"))
        utf16_be.write_bytes(b"\xfe\xff" + xml_text.encode("utf-16-be"))
        encoded = run(script, [utf16_le, utf16_be], root / "utf16-output")
        assert encoded.returncode == 0, (encoded.stdout, encoded.stderr)
        encoded_manifest = json.loads((root / "utf16-output" / "manifest.json").read_text(encoding="utf-8"))
        assert encoded_manifest["status_counts"] == {"parsed": 2}
        for row in encoded_manifest["inputs"]:
            assert row["details"]["matches"][0]["value"] == "UTF16合成软件"

        empty = root / "empty.xml"
        not_xml = root / "not-xml.xml"
        empty.write_bytes(b"")
        not_xml.write_text("not an XML document", encoding="utf-8")
        invalid = run(script, [empty, not_xml], root / "invalid-xml-output")
        assert invalid.returncode == 1
        invalid_manifest = json.loads((root / "invalid-xml-output" / "manifest.json").read_text(encoding="utf-8"))
        assert invalid_manifest["status_counts"] == {"error": 2}
        print("通过：DOCX/PDF/XML、UTF-16 LE/BE、紧凑摘要、跨文件比较、失败语义、输入只读及输出保护。")


if __name__ == "__main__":
    main()
