# 算力工坊 AI 操作接口

有本机终端权限的 AI 可以在算力工坊仓库根目录运行 `python studio_cli.py`，直接把本机目录导入用户指定的项目。CLI 和网页连接同一个控制器，创建的草稿、上传进度和实验日志都可以在网页继续查看。

## 第一次连接

控制器须已启动，导入路径须能从控制器所在电脑访问。客户端需要 Python 3.9 或更高版本，仅依赖标准库。以下示例使用 PowerShell。

```powershell
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
python studio_cli.py --help
python studio_cli.py projects
```

第二条命令查询正式工作空间（本机端口 `8776`）。从返回的项目中核对 `name`、`archived` 和 `id`。后续使用选中的 `id`，这样即使项目重名也能准确定位。演示工作空间使用 `python studio_cli.py --demo projects`（端口 `8777`），后续每条命令也要带 `--demo`。

自定义控制器使用 `python studio_cli.py --port 端口 --data-dir "控制器保存目录" projects`。全局参数放在子命令之前。`--data-dir` 用于定位工作空间凭据，待导入的目录作为子命令的位置参数单独传入。客户端在进程内完成登录，调用方无需复制令牌。

## 从本机目录创建实验草稿

下面用仓库自带的两个实验演示。将 `$projectId` 替换为刚查询到的 ID。用户只要求检查配置时，执行到预览即可。

```powershell
$projectId = '替换为实际项目ID'
$source = '.\examples\batch-experiments'
$previewJson = python studio_cli.py import-experiments --project $projectId --preview-only $source
if ($LASTEXITCODE -ne 0) { throw '预览未返回，请按作业回执查询。' }
$preview = $previewJson | ConvertFrom-Json
$preview | Select-Object batch_id, configuration_revision, status, count
$preview.tasks | Select-Object key, id, name, command, depends_on
```

这个示例应返回 `status = ready`、`count = 2`，其中 `evaluate` 的 `depends_on` 包含 `train` 的 ID。此时项目中尚未创建这两个实验。用户要求导入时，用**同一份预览**的批次 ID 和修订号创建草稿。

```powershell
$resultJson = python studio_cli.py commit $preview.batch_id --revision $preview.configuration_revision
if ($LASTEXITCODE -ne 0) { throw '提交未返回，请按批次或作业回执查询。' }
$result = $resultJson | ConvertFrom-Json
$result.tasks | Select-Object id, key, name, status
python studio_cli.py tasks --project $projectId
```

按 `result.tasks[].id` 与项目实验列表逐项核对。成功批次的 `status` 为 `success`，新建实验的 `status` 为 `draft`。如果用户一开始就要求直接导入，单条命令 `python studio_cli.py import-experiments --project $projectId $source` 会完成预览和提交。对已经提交的同一目录再次运行导入命令会产生新批次。

仅在用户要求运行或排队时，对指定的每个实验执行 `queue`。例如用户只选中了上面示例中的训练实验，可接着执行以下命令。

```powershell
$trainId = ($result.tasks | Where-Object key -eq 'train').id
python studio_cli.py queue $trainId
python studio_cli.py log $trainId
```

## 命令速查

以下命令均接在 `python studio_cli.py` 后。方括号表示可选参数。项目可用完整 ID 或唯一的精确名称，实验、批次和作业均使用相应接口返回的 ID。网页中的 `#序号` 对应 `seq`。

| 命令 | 用途与返回值 |
| --- | --- |
| `projects` | 列出项目，返回含 `id`、`name` 和归档信息的数组。 |
| `create-project 名称 [--description 说明]` | 创建项目，返回含项目 `id` 的记录。 |
| `tasks [--project 项目]` | 列出实验，返回 `id`、`project_id`、`seq`、`name`、`status` 等字段。等待原因可见于 `reason`。 |
| `import-experiments --project 项目 [--preview-only] 目录` | 从目录读取实验配置。返回 `batch_id`、`configuration_revision`、`count` 和 `tasks`。 |
| `commit 批次ID --revision 修订号` | 按该版预览创建草稿。同一批次已成功时，返回原结果。 |
| `import-code --project 项目 [--task 实验ID] 路径` | 导入目录或 ZIP。默认合并到项目公共代码，指定 `--task` 时导入该项目的实验专属文件。返回 `upload_id`、`project_id`。 |
| `import-dataset --name 名称 路径` | 从目录或 ZIP 建立数据集，并按控制器设置同步。返回 `upload_id`、`dataset_id`。实验配置通过 `datasets` 引用数据集名称。 |
| `queue 实验ID` | 将指定实验入队，返回其实验 ID 和状态。运行进度通过 `tasks` 查询。 |
| `log 实验ID` | 返回近期日志 `text`。并发读取时可能返回 `pending: true`，稍后再查。 |
| `job 作业ID [--wait 秒数]` | 查询导入、提交等后台作业。进行中返回 `id`、`status` 等字段，成功时在 `result` 中返回业务结果。 |

批量配置中用 `key` 标识本批实验，用 `depends_on` 描述依赖，`script` 和 `files` 指向实验脚本及专属输入。新建实验前可检查配置指南和完整样例。[实验脚本与批量配置指导](./实验脚本与批量配置指导.md) · [示例 experiments.json](../examples/batch-experiments/experiments.json)

### 导入代码与数据集

下面是三种独立操作。按用户要求选一条，将示例路径和名称替换为实际值。控制器直接读取本机目录或 ZIP，来源与工具的保存目录须互不重叠。

```powershell
python studio_cli.py import-code --project $projectId "D:\inputs\shared code"
python studio_cli.py import-code --project $projectId --task $trainId "D:\inputs\train-files.zip"
python studio_cli.py import-dataset --name "样本集" "D:\inputs\samples.zip"
```

数据集导入后可在「上传与进度」查看同一记录。若同名数据集已存在，先查它的上传状态和服务器副本。已经在本机就绪的数据集可用原 `dataset_id` 继续服务器同步。

## 回执、超时与恢复

为每次操作保留工作空间和目标项目，并**分别读取标准输出和标准错误**。成功时，标准输出是一份 UTF-8 JSON。标准错误会逐行输出后台回执，程序应在收到时保存。以下为示意，实际 ID 以接口返回为准。

| 标准错误中的回执 | 中断后如何继续 |
| --- | --- |
| `{"job_id":"J1","phase":"preview"}` | 用 `job J1` 查询。在成功响应的 `result` 中取出 `batch_id` 和 `configuration_revision`。仅在需要创建草稿时提交。 |
| `{"batch_id":"B1","configuration_revision":0}` | 已有批次。按原修订号执行 `commit B1 --revision 0`。若已有提交作业回执，优先查该作业。 |
| `{"job_id":"J2","batch_id":"B1"}` | 用 `job J2` 查询提交结果，从 `result.tasks` 获取实验 ID。 |
| `{"job_id":"J3","upload_id":"U1"}` | 用 `job J3` 查询处理进度。网页「上传与进度」及下方上传接口可按上传记录查看详情。 |

三个导入命令和 `commit` 都支持 `--wait 秒数`，默认每阶段等待 300 秒。写入命令等待超时会以退出码 `1` 结束，后台仍可继续。单独调用 `job` 默认立即返回，`queued` 或 `running` 也会返回退出码 `0`。判断作业是否完成应读取 `status`。用户只要求发起操作时，可以交付回执并让用户在网页看进度。

运行错误通常以退出码 `1` 和标准错误中的 `{"error":"原因"}` 返回，命令语法错误由参数解析器以退出码 `2` 返回。若写入后连接丢失且没有收到 ID，先查网页操作记录和项目实验列表。修订冲突时重新读取并核对当前批次。来源文件已变化或预览过期时，按最新文件重新生成预览。

## 本机 HTTP 接口

导入和管理实验优先使用上述 CLI。需要查询流程图、分页读取数据集文件或核验服务器副本时，可以从仓库根目录复用同一客户端。下面的 Python 示例只列出正式工作空间的数据集 ID 和名称。

```python
from argparse import Namespace

from studio_cli import Studio, token_file

client = Studio(8776)
client.login(token_file(Namespace(demo=False, data_dir=None)))
for dataset in client.json("/api/state")["datasets"]:
    print(dataset["id"], dataset["name"])
```

`client.json(路径)` 发出 GET 请求，`client.json(路径, 字典)` 发出 JSON POST 请求，空请求体的 POST 传 `{}`。客户端自动完成 `POST /login`，保留会话 Cookie，并从首页取得写请求所需的 `X-CSRF-Token`。演示工作空间使用 `Studio(8777)` 和 `demo=True`。

| 方法与路径 | 请求或响应 |
| --- | --- |
| `GET /api/state` | 返回项目、实验、数据集等摘要，可按名称查 ID。 |
| `GET /api/projects/{project_id}/graph` | 返回 `nodes` 和 `edges`。每条边的 `source` 是前置实验 ID，`target` 是依赖它的实验 ID。 |
| `POST /api/projects/{project_id}/batch-import/preview` | 传 `{"source_path":"D:/experiments/demo"}`。HTTP 202 返回预览作业，`id` 是作业 ID。 |
| `GET /api/jobs/{job_id}` | 返回 `{"job":{…}}`。成功的预览作业用 `job.result.id` 表示批次 ID，CLI 将此字段命名为 `batch_id`。 |
| `GET /api/batch-imports/{batch_id}` | 读取当前预览、实验列表和 `configuration_revision`。 |
| `POST /api/batch-imports/{batch_id}/commit` | 传 `{"configuration_revision":0}`，将示例中的 0 换成实际修订号。HTTP 202 返回作业，已成功提交时 HTTP 200 返回原批次。 |
| `POST /api/uploads/local` | 数据集传 `{"kind":"dataset","name":"数据集名称","source_path":"D:/inputs/data.zip"}`。公共代码传 `kind="project"`、`project_id`、`source_path`，实验专属文件再加 `task_id`。HTTP 202 响应中的 `id` 是上传 ID，`job_id` 是处理作业 ID。 |
| `GET /api/uploads/{upload_id}?summary=1` | 读取单次上传的 `status`、`progress` 和关联 ID。大文件集可用 `summary=1` 避免返回全部文件。 |
| `POST /api/uploads/{upload_id}/interrupt` | 用户要求中断此上传时传 `{}`。返回 `interrupting` 表示正在停止，待状态变为 `interrupted` 后再继续。 |
| `POST /api/uploads/{upload_id}/finish` | 本机目录或 ZIP 导入处理中断、且本机文件尚未就绪时，传 `{}` 继续原上传，并记录响应中的 `job_id`。保留原始路径和文件。 |
| `GET /api/datasets/{dataset_id}` | `dataset` 含本机导入状态、`file_count`、`total_bytes`，`servers[].replica` 给出各服务器的副本状态。 |
| `GET /api/datasets/{dataset_id}/files?offset=0&limit=200` | 返回清单 `files` 和 `total_count`，每项含 `path`、`size`、`sha256`。每页 1–200 项。 |
| `POST /api/datasets/{dataset_id}/upload` | 传 `{}`，对已在本机就绪且有清单的数据集重新发起服务器同步，返回作业 `id`。 |
| `POST /api/datasets/{dataset_id}/verify` | 传 `{}`，为已有副本发起仅核验作业，返回作业 `id`。核验差异可在作业错误及副本状态中查看。 |

**用户要求“确保没有遗漏”时**，用原始目录或 ZIP 的预期文件清单做基准。按实际返回条数递增 `offset`，读完数据集清单的全部 `total_count` 项。逐项比对相对路径、字节数、SHA-256，同时检查缺失和多出的路径。用户还要求核对服务器时，在同步结束后调用 `verify`，查到该核验作业 `success`，再检查每台目标服务器的 `replica.status` 为 `success` 及其 `verified_at`。向用户报告预期与实得文件数、总字节数、差异和核验过的服务器。仅完成本机导入时，报告本机结果并注明服务器进度。

## 可以直接交给 AI 的指令

> 阅读仓库的 `docs/AI操作接口.md`。把我给的本机实验目录导入我指定的项目，先用 `projects` 核对项目 ID。导入后保留草稿，回报批次 ID、实验数、实验 ID 和依赖关系。若连接中断，用已返回的作业或批次恢复。等我说「排队」再执行。
