import gzip
import json
import zipfile

from webvault.storage import wacz_contains_url


def test_wacz_contains_url_reads_compressed_cdx(tmp_path):
    archive_path = tmp_path / "capture.wacz"
    target = "https://example.com/docs?version=1"
    record = json.dumps({"url": target, "status": "200"})
    index = gzip.compress(f"com,example)/docs 20260101000000 {record}\n".encode())
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("indexes/index.cdx.gz", index)

    assert wacz_contains_url(archive_path, target)
    assert wacz_contains_url(archive_path, f"{target}#section")
    assert not wacz_contains_url(archive_path, "https://example.com/missing")
