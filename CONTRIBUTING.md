# 贡献说明

感谢帮助改进 Compute Studio。提交前请先说明问题、预期行为和验证方式；涉及调度、文件清理或运行控制时，请保留现有实验的可追溯性。

## 本地开发

1. 建立 Python 虚拟环境并安装 `requirements-dev.txt`。
2. 使用独立的 `--data-dir` 或隔离测试数据，不要拿真实实验练习清理、重跑和进程控制。
3. 运行 `python -m pytest tests -q`；修改前端布局／交互时检查桌面和手机宽度。
4. 相关改动还可运行 `node tests/test_graph_layout.js` 和 `node tests/test_benchmark_state.js`。

## 提交格式

采用 Conventional Commits：

```text
feat(results): support dataset-specific metrics
fix(scheduler): preserve archived tasks outside the queue
docs: clarify batch experiment configuration
test(storage): cover interrupted file transfers
chore: update release privacy checks
```

一个提交解决一个清晰问题。提交正文说明行为变化与验证，避免包含个人服务器、项目编号、实验结果或电脑路径。

## 数据和凭据

不要提交数据库、保存地址配置、访问令牌、SSH 配置、私钥、主机信任文件、日志、数据集、模型权重、实验输出或打包的个人工作空间。测试使用生成的临时数据，示例使用明确的占位名称与值。

`.gitignore` 是第一道保护；分享整个文件夹前仍需核对实际文件。可运行 `scripts/prepare_public_release.py` 创建允许列表导出，再运行 `scripts/audit_public_release.py`。需要检测个人路径或项目编号时，可通过 `--private-markers` 传入本机 JSON 字符串数组；该规则文件本身也不得加入发布包。

修改 `.sh` 文件保持 LF 换行。保留第三方库随附的许可证与来源说明。本仓库暂未添加项目级许可证。
