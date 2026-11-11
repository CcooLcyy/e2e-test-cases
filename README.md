# MskDSP 测试用例

`tests/` 包含可独立运行的 pytest 用例和分组顺序。`case_support/` 包含随用例交付的场景、页面操作及断言。通用执行器、测试环境、设备会话、截图和报告在 [测试上位机](https://github.com/CcooLcyy/e2e-tests) 中维护。

新增用例时，在 `tests/case-order.json` 的对应分组末尾登记 pytest nodeid。需要 slaveSim 和 Modbus 采集时限的用例声明 `pytest.mark.requires_modbus`。保持用例的准备、最终断言、失败留证和清理自包含。

## 独立发布

PR 和 `main` 推送都会用指定的 Python 执行器收集用例，核对 nodeid、分组及顺序。`main` 推送在平台发布已启用时签名并发布 `VERSION` 下一补丁版本的 `-ci.<运行编号>.<尝试次数>` 包。Stable 使用 `VERSION` 里的正式版本号，从 `main` 手动选择 `stable` 发布。同一正式版本对应固定提交及制品，发布新版前先更新 `VERSION`。

| 位置 | 配置 | 用途 |
| --- | --- | --- |
| 仓库变量 | `TESTBENCH_RUNNER_REF` | 当前用例包支持的测试上位机执行器提交 SHA，校验和发布使用同一基准 |
| 仓库变量 | `PLATFORM_GRPC_ENDPOINT` | 平台软件目录和发布 RPC 地址 |
| 仓库变量 | `CASE_PACK_PUBLIC_KEY` | 用例签名公钥，与本体构建所用的同名变量一致 |
| 仓库变量 | `CASE_PACK_PUBLISH_ENABLED` | 平台部署用例包支持并配置 CI 令牌后设为 `true` |
| `cases-ci` / `cases-stable` 环境 Secret | `CASE_PACK_SIGNING_KEY`、`CASE_PACK_SIGNING_PASSWORD` | 用例签名密钥和密码 |
| `cases-ci` / `cases-stable` 环境 Secret | `CASE_PACK_PUBLISHER_TOKEN` | 平台为相应频道配置的专用令牌 |

用例包使用独立的 `mskdsp-testbench-cases` / `python-zip` 产品身份。用例包中的 `case-pack.json` 记录用例版本、源码提交及执行器接口版本。平台发布后，测试上位机在首次打开或点击「刷新用例」时按用例频道获取并验证。修改本仓库只运行用例工作流。测试上位机发版本体时可更新 `case-suite` 子模块，作为随安装包交付的离线基线。
