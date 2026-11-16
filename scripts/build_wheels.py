"""Build both wheels into the same resolved directory, including paths with spaces."""
import argparse
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def build(out, uv="uv"):
    destination = Path(out).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    for project in (ROOT, ROOT / "builtins/mcpkit"):
        subprocess.run([str(uv), "build", "--project", str(project), "--wheel",
                        "--out-dir", str(destination)], cwd=ROOT, check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "artifacts/python")
    parser.add_argument("--uv", default="uv")
    args = parser.parse_args()
    build(args.out, args.uv)
