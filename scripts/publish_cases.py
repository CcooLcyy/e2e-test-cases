"""校对用例包身份，再通过平台软件发布 RPC 上传并回读。"""

import hashlib
import json
import os
from pathlib import Path
import re
import sys
from zipfile import ZipFile

from package_cases import PRODUCT, RUNNER_API, release_version


def publish(channel, archive, runner):
    sys.path.insert(0, str(runner / "scripts"))
    from windows_release import grpc_endpoint, minisign_key_id, require_current_main, transfer

    if channel not in {"ci", "stable"} or os.environ["GITHUB_REF"] != "refs/heads/main":
        raise ValueError("用例包只允许从 main 发布到 CI 或 Stable")
    event = os.environ["GITHUB_EVENT_NAME"]
    if (channel == "stable" and event != "workflow_dispatch") or event not in {"push", "workflow_dispatch"}:
        raise ValueError("工作流触发方式与发布频道不符")
    with ZipFile(archive) as package:
        manifest = json.loads(package.read("case-pack.json"))
    version = manifest["version"]
    source_sha = os.environ["GITHUB_SHA"]
    if (manifest != {"version": version, "source_sha": source_sha, "runner_api": RUNNER_API}
            or archive.name != f"{PRODUCT}_{version}.zip"):
        raise ValueError("用例包名称、提交或接口版本不符")
    if channel == "ci":
        base, _, attempt = release_version(channel).rpartition(".")
        match = re.fullmatch(re.escape(base) + r"\.([1-9]\d*)", version)
        if not match or int(match[1]) > int(attempt):
            raise ValueError("用例包 CI 版本与工作流运行编号不符")
    elif version != release_version(channel):
        raise ValueError("用例包 Stable 版本与 VERSION 不符")
    signature = archive.with_suffix(".zip.sig").read_text(encoding="utf-8").strip()
    if minisign_key_id(signature, 74) != minisign_key_id(os.environ["CASE_PACK_PUBLIC_KEY"].strip(), 42):
        raise ValueError("用例签名使用的密钥与发布公钥不符")
    token = os.environ["CASE_PACK_PUBLISHER_TOKEN"].strip()
    if not token:
        raise ValueError("缺少当前频道的用例包发布令牌")
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    metadata = dict(product=PRODUCT, platform="python-zip", channel=channel, version=version,
                    file_name=archive.name, size=archive.stat().st_size, sha256=digest, signature=signature,
                    source_repository=f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}",
                    source_ref="main", source_sha=source_sha)
    require_current_main()
    transfer(archive, metadata, grpc_endpoint(), token)


if __name__ == "__main__":
    publish(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]))
