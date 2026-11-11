"""按 Git 提交生成用例 ZIP，供独立发布和本体安装包使用。"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[1]
PRODUCT = "mskdsp-testbench-cases"
RUNNER_API = 1


def release_version(channel):
    value = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", value):
        raise ValueError("VERSION 必须为 X.Y.Z")
    if channel == "ci":
        major, minor, patch = map(int, value.split("."))
        number = int(os.environ["GITHUB_RUN_NUMBER"])
        attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
        return f"{major}.{minor}.{patch + 1}-ci.{number}.{attempt}"
    return value


def pack(channel, output):
    version = release_version(channel)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if channel != "bundled" and commit != os.environ["GITHUB_SHA"]:
        raise ValueError("检出的用例提交与工作流提交不同")
    manifest = {"version": version, "source_sha": commit, "runner_api": RUNNER_API}
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"{PRODUCT}_{version}.zip"
    with TemporaryDirectory() as temporary:
        snapshot = Path(temporary) / "snapshot.zip"
        subprocess.run(
            ["git", "archive", "--format=zip", f"--output={snapshot}", commit, "--",
             "pytest.ini", "tests", "case_support", "docs/examples"],
            cwd=ROOT, check=True,
        )
        # 固定文件顺序和压缩参数，同一 Stable 提交重新打包可得到相同内容。
        with ZipFile(snapshot) as source, ZipFile(archive, "w", compression=ZIP_DEFLATED) as target:
            for item in sorted(source.infolist(), key=lambda item: item.filename):
                if not item.is_dir():
                    target.writestr(ZipInfo(item.filename), source.read(item), compress_type=ZIP_DEFLATED)
            target.writestr(ZipInfo("case-pack.json"), json.dumps(manifest, sort_keys=True) + "\n",
                            compress_type=ZIP_DEFLATED)
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("channel", choices=("bundled", "ci", "stable"))
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(pack(args.channel, args.output))


if __name__ == "__main__":
    main()
