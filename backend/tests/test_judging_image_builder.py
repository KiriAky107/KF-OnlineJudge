"""Image construction data tests; actual publication/execution needs the independent VM."""

import importlib.util
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE = Path(__file__).resolve().parents[2] / "scripts/build_judging_images.py"
SPEC = importlib.util.spec_from_file_location("judging_image_builder", MODULE)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def test_catalog_uses_wire_path_order_including_dot_and_directory_boundaries(tmp_path):
    image = tmp_path / "image"
    image.mkdir()
    builder.put(image, "usr/lib/module/a.py", b"data")
    builder.put(image, "usr/lib/module.py", b"module")
    support = tmp_path / "support"
    library = support / "usr/lib/ld-linux-x86-64.so.2"
    library.parent.mkdir(parents=True)
    library.write_bytes(b"synthetic ordinary loader data")
    args = SimpleNamespace(support_dir=support, support_manifest_sha256="a" * 64)
    builder.finish(
        args,
        builder.PINS["python313"],
        image,
        {"usr/lib/ld-linux-x86-64.so.2": {"kind": "file", "sha256": builder.sha(library)}},
    )
    manifest = json.loads((image / "manifest.json").read_bytes())
    assert manifest["architecture"] == "x86_64_v3"
    paths = [item["path"] for item in manifest["files"]]
    assert paths == sorted(paths)
    assert paths.index("usr/lib/module.py") < paths.index("usr/lib/module/a.py")


def test_public_archive_links_cannot_escape_or_cycle():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, target in [("python/outside", "../../secret"), ("python/cycle", "cycle")]:
            item = tarfile.TarInfo(name)
            item.type = tarfile.SYMTYPE
            item.linkname = target
            archive.addfile(item)
    buffer.seek(0)
    with tarfile.open(fileobj=buffer, mode="r:") as archive:
        members = {item.name: item for item in archive.getmembers()}
        for name in members:
            with pytest.raises(ValueError):
                builder.member_bytes(archive, members, name)


def test_corrupt_archive_is_refused_before_output_creation(tmp_path):
    archive = tmp_path / "public.tar.gz"
    archive.write_bytes(b"untrusted replacement")
    output = tmp_path / "image"
    args = SimpleNamespace(language="python313", archive=archive, output=output)
    with pytest.raises(ValueError, match="fixed upstream digest"):
        builder.build(args)
    assert not output.exists()
