"""检查交付快照收集出的 nodeid、分组和顺序与登记清单一致。"""

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile


def verify(archive, runner):
    with TemporaryDirectory() as temporary:
        temporary = Path(temporary)
        suite = temporary / "suite"
        with ZipFile(archive) as package:
            package.extractall(suite)
        output = temporary / "collected.json"
        subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "tests", "--collect-only", "-q", "--strict-markers",
             "-p", "no:cacheprovider", "-p", "testbench.pytest_plugin"],
            cwd=suite, check=True,
            env=dict(os.environ, PYTHONPATH=str(runner.resolve()), TESTBENCH_COLLECT=str(output), PYTHONUTF8="1"),
        )
        groups = json.loads((suite / "tests/case-order.json").read_text(encoding="utf-8"))["groups"]
        expected = [(case, group["name"]) for group in groups for case in group["cases"]]
        actual = [(case["id"], case["group"]) for case in json.loads(output.read_text(encoding="utf-8"))]
        if not expected or actual != expected:
            raise ValueError("用例收集结果与 tests/case-order.json 的分组或执行顺序不一致")
        print(f"已核对 {len(actual)} 项用例的编号、分组及顺序")


if __name__ == "__main__":
    verify(Path(sys.argv[1]), Path(sys.argv[2]))
