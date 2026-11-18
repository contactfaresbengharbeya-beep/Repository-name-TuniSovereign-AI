"""Compile protos/tunisovereign.proto into grpc_service/ and fix the generated import for package use.

Usage (from the repo root):  python scripts/gen_proto.py
`grpc_tools.protoc` emits `import tunisovereign_pb2 as ...` in the *_pb2_grpc.py file, which only works when
grpc_service/ itself is on sys.path. We rewrite it to a package-relative import.
"""
import pathlib
import sys

from grpc_tools import protoc

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "grpc_service"


def main() -> int:
    code = protoc.main([
        "grpc_tools.protoc",
        f"-I{ROOT / 'protos'}",
        f"--python_out={OUT}",
        f"--grpc_python_out={OUT}",
        str(ROOT / "protos" / "tunisovereign.proto"),
    ])
    if code:
        return code
    target = OUT / "tunisovereign_pb2_grpc.py"
    text = target.read_text(encoding="utf-8")
    patched = text.replace(
        "import tunisovereign_pb2 as tunisovereign__pb2",
        "from . import tunisovereign_pb2 as tunisovereign__pb2",
    )
    if patched == text and "from . import tunisovereign_pb2" not in text:
        print("WARNING: expected import line not found; check generated file", file=sys.stderr)
    target.write_text(patched, encoding="utf-8")
    print("generated:", ", ".join(p.name for p in sorted(OUT.glob("tunisovereign_pb2*.py"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
