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

## 实现实验前确定公共代码目录

每次实现实验前，先阅读[实验脚本与批量配置指导](./实验脚本与批量配置指导.md)，按其中的运行目录、输入输出和依赖约定编写公共代码、脚本与 `experiments.json`。本节用于确定代码应写到哪里。以下示例在控制器本机运行，项目 ID、端口和保存目录取自项目卡片复制的提示词。

```powershell
$projectId = '替换为提示词中的完整项目ID'
$studioCli = Join-Path '替换为提示词中的算力工坊目录' 'studio_cli.py'
$sourceDir = '.\code'  # 在 AI 工作目录运行。如果当前目录就是源码根，用 '.'；用户指定目录时直接填该路径
$controller = @()  # 默认正式工作空间。其他工作空间填入提示词中的全局参数，如 @('--demo', '--port', '8777', '--data-dir', 'D:\DemoData')
$raw = python $studioCli @controller source-status --project $projectId
if ($LASTEXITCODE -ne 0) { throw '查询绑定失败，请先核对控制器。' }
$source = $raw | ConvertFrom-Json
if (-not $source.path) {
    $raw = python $studioCli @controller source-init --project $projectId --path $sourceDir
    if ($LASTEXITCODE -ne 0) { throw '初始化失败，请核对错误和原绑定。' }
    $binding = $raw | ConvertFrom-Json
    if ($binding.outcome -eq 'skipped') { Write-Output $binding.reason }
    $source = $binding.source
}
$source | Select-Object path, enabled, status, revision
```

- **已有绑定**（`path` 有值）：在该目录编辑公共代码，保留原同步开关和排除规则。即使路径暂时不可访问或同步暂停，也应保留绑定。
- **首次绑定**（`outcome = created`）：CLI 按调用时的工作目录解析 `--path`。控制器绑定该目录并启用自动同步。目录已有源码时原地使用，路径缺失时创建目录。空目录在首次写入前为 `waiting`。返回的 `source.path` 是实际绑定的绝对路径。重复或并发请求返回已有绑定（`existing`），即使后来传入了另一条路径也是如此。
- **跳过**（`outcome = skipped`）：查看 `reason`。已有受管代码或实验的项目继续按原方式维护，需要导入公共代码时用 `import-code`。已归档、有结果表或副本、有待处理的文件操作，或曾经主动解绑的项目，也会跳过自动绑定。

例如 AI 工作目录为 `D:\Research\Example` 时，可将公共源码放在 `.\code`，批量导入包放在同级的 `.\batch`。若用户直接指定了已有源码根，则使用该路径本身。公共源码写好且自动同步已启用时，运行 `python $studioCli @controller source-sync --project $projectId`，确认返回的 `revision`，再用 `import-experiments` 导入包含 `experiments.json`、脚本与实验专属输入的批量包。保留暂停设置的项目应先向用户核对发布安排。

| 控制器 API | 行为 |
| --- | --- |
| `GET /api/projects/{project_id}/source` | 只读查询。返回 `path`、`enabled`、`status` 和已有的 `revision` 等字段。 |
| `POST /api/projects/{project_id}/source/initialize-at` | 传 `{"path":"D:/Research/Example/code"}`。HTTP 调用使用控制器本机的绝对路径。控制器重新检查绑定和项目内容，返回 `{outcome, source}`，跳过时另有 `reason`。首次自动绑定须选择程序目录和工作空间保存目录以外的源码路径。 |
| `POST /api/projects/{project_id}/source/sync` | 传 `{}`。立即同步当前绑定，返回 HTTP 202 和作业 ID。CLI 等待该作业，超时后用 `job` 查询。 |

网页配置使用的 `POST /api/projects/{project_id}/source` 可主动更换路径或解绑。AI 的首次自动绑定使用上表中的 `/initialize-at`。若该接口返回 404，请先更新并重启控制器。

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
| `source-status --project 项目` | 查询编辑目录和当前同步配置。 |
| `source-init --project 项目 --path 源码目录` | 为空白且未绑定的项目绑定指定的源码目录，接受绝对路径或相对当前工作目录的路径。 |
| `source-sync --project 项目 [--wait 秒数]` | 发布绑定目录中的代码，等待同步作业，返回状态和版本。 |
| `tasks [--project 项目]` | 列出实验，返回 `id`、`project_id`、`seq`、`name`、`status` 等字段。等待原因可见于 `reason`。 |
| `import-experiments --project 项目 [--preview-only] 目录` | 从目录读取实验配置。返回 `batch_id`、`configuration_revision`、`count` 和 `tasks`。 |
| `commit 批次ID --revision 修订号` | 按该版预览创建草稿。同一批次已成功时，返回原结果。 |
| `import-code --project 项目 [--task 实验ID] 路径` | 导入目录或 ZIP。默认合并到项目公共代码，指定 `--task` 时导入该项目的实验专属文件。返回 `upload_id`、`project_id`。 |
| `import-dataset --name 名称 路径` | 从目录或 ZIP 建立数据集，并按控制器设置同步。返回 `upload_id`、`dataset_id`。实验配置通过 `datasets` 引用数据集名称。 |
| `queue 实验ID` | 将指定实验入队，返回其实验 ID 和状态。运行进度通过 `tasks` 查询。 |
| `log 实验ID` | 返回近期日志 `text`。并发读取时可能返回 `pending: true`，稍后再查。 |
| `job 作业ID [--wait 秒数]` | 查询导入、提交等后台作业。进行中返回 `id`、`status` 等字段，成功时在 `result` 中返回业务结果。 |
| `result-tables --project 项目 [--dataset 数据集]` | 查询表名与表 ID。省略数据集会返回已保存的各数据集及默认表目录。 |
| `result-runs --project 项目 [--offset 0 --limit 100]` | 分页列出当前成功运行及成功回传的历史运行，返回实验 ID、运行 ID、当前实验配置的数据集和回传时间。 |
| `result-files --project 项目 --attempt 运行ID [--query 路径片段] [--offset 0 --limit 100]` | 列出本机回传产物的相对路径与大小。省略筛选可遍历全部输出。 |
| `result-evidence --project 项目 --task 实验ID --attempt 运行ID (--path 相对路径 或 --log) [--offset 0 --limit 32768]` | 按字节分段读取文本产物或本机日志，返回 `text`、`next_offset`、`has_more` 和来源路径。 |
| `result-merge --project 项目 [--dataset 数据集] 观测.json` | 按运行与观测键新增或补齐指标，自动创建所需命名表。传 `-` 可从标准输入读取 UTF-8 JSON。 |
| `result-show --project 项目 [--dataset 数据集 --table 表ID] [--row-id 行ID]` | 回读结果。可重复传行 ID 核对本次写入；省略时也返回分组统计。 |
| `result-chart --project 项目 [--dataset 数据集 --table 表ID] [--mode box --metric auc --group group:基线]` | 返回数值指标、组别与绘图数据；可重复指定指标和组，支持 `--offset` 与 `--limit`。 |

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

传输详情中的哈希和时间表示该次作业的实际观测。同一文件在多台服务器核验通过时，接口仍分别返回各服务器的记录。需要确认现在的文件内容时，发起新的 `verify` 作业。数据结构与维护流程见[项目架构与存储](./项目架构与存储.md)。

## 操作项目结果表

令 `P=/api/projects/{project_id}/results`。下面操作通过本机会话鉴权，写请求使用相同的 CSRF 机制。查询参数里的数据集名称须做 URL 编码。

- `GET P/tables?dataset=名称` 列出本数据集的表名和稳定的 `table_id`。空 ID 表示原默认表。
- `POST P/tables?dataset=名称` 创建表。请求示例为 `{"table_id":"ablation_01","name":"消融实验","columns":[{"id":"f1","name":"F1","type":"number","direction":"max"}],"fields":["label","group","task"]}`。为每张新表生成独立 ID，重试同一次创建沿用该 ID。可传空指标列表。
- `GET P?dataset=名称&table=表ID` 返回该表的版本、行列及分组汇总。写入同一路径使用 POST，发送当前 `revision`、新的 `request_id`、完整 `columns` 和 `rows`。可同时更新 `name` 与 `fields`。行可填写 `label`、`group`、`seed`、`values`、`notes`，关联实验时填写 `task_id` 和可选的 `attempt_id`。行和列的数组顺序对应表内顺序。收到 409 时重新读取并核对差异。
- `GET P/export?dataset=名称&table=表ID&view=summary` 导出分组统计，`view=rows` 导出逐行数据。省略 `table` 会访问该数据集的默认表。

## 自动整理项目结果

项目卡片的「复制 AI 提示词」提供项目名称、完整 ID、控制器端口和工作空间目录。AI 按你随后给出的任务操作。任务产生新结果时，直接使用该次实验和运行 ID 读取产物、整理入表；你明确要求补整理历史结果时，再按指定范围查找历史运行。网页表格和图表共用整理后的数据。下列示例中的 `$projectId`、`$taskId`、`$attemptId`、`$tableId` 和路径均以实际回执替换。演示或自定义工作空间须沿用提示词里的全局参数。

1. **确认运行和目标表。** 用 `projects` 核对完整项目 ID，用 `result-tables` 找到目标表。已有本次运行 ID 时直接读取其文件。按要求查找历史运行时使用 `result-runs`。运行和文件清单每页最多 200 项，按返回条数递增偏移，直到达到 `total`。标记 `current=false` 的记录来自历史运行，当前实验的数据集配置仅供定位，旧运行的实际数据集和设置仍应从产物核实。若同一 Seed 重跑，区分运行并核对采用哪次测量。
2. **读证据。** 优先读取小型评估汇总。先找 JSON、CSV、TSV、报告等输出，筛选无匹配时再列全部输出。文本按实际格式解析，`has_more=true` 时从 `next_offset` 继续。未找到产物时可读取已回传日志。若 `log_tail_snapshot=true`，读到的是历史日志尾部快照。运行状态、文件名和训练进度不足以证明最终指标，证据不明确时在回执列明原因。

```powershell
python studio_cli.py result-runs --project $projectId --limit 100
python studio_cli.py result-files --project $projectId --attempt $attemptId --query metrics
python studio_cli.py result-evidence --project $projectId --task $taskId --attempt $attemptId --path "outputs/metrics.json"
```

3. **选择可比的表并合并。** 按数据集、划分、指标定义和单位核对已有表。完整基线、消融和探索性尝试可独立建表。对于已有表，先用 `result-show` 取列 ID、既有绑定和统计口径。每条观测使用实际的实验 ID、运行 ID 和稳定的 `key`。例如同一运行输出多个划分或 Seed，可分别使用 `test/seed42`、`validation/seed42`。保留原单位和精度，省略缺失指标；已有的跨 Seed 汇总不能当作多个原始观测。下面是提交格式示例，示例数值须换成实际读到的值。

```json
{
  "name": "测试集基线",
  "columns": [{"id":"auc","name":"AUROC","type":"number","direction":"max","precision":4,"unit":""}],
  "rows": [{
    "task_id": "实际实验ID", "attempt_id": "实际运行ID", "key": "test/seed42",
    "label": "模型 A · Seed 42", "group": "模型 A / test", "seed": "42",
    "values": {"auc": 0.9127}, "sources": {"auc": "outputs/metrics.json#test.auroc"}
  }]
}
```

将内容写入 UTF-8 编码的 `observations.json`，再执行下列命令。一次提交对应一张表。首次可传 `name` 来定位或新建同名表，后续优先用回执的 `table_id` 代替 `name`，这样表被改名后仍能定位。所有指标可共用一条 `source`，也可用 `sources` 分别标注文件和 JSON 字段、CSV 行列或日志行。来源是整理者核实后写下的引用。退出码非零时检查错误回执。

```powershell
$receipt = python studio_cli.py result-merge --project $projectId --dataset ECG observations.json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw '合并失败，请核对回执' }
$tableId = $receipt.table_id
python studio_cli.py result-show --project $projectId --dataset ECG --table $tableId --row-id $receipt.row_ids[0]
python studio_cli.py result-chart --project $projectId --dataset ECG --table $tableId --mode box --metric auc
```

4. **核对并报告。** 按回执的全部 `row_ids` 回读，核对运行关联、数值和来源。报告各表的 `created`（新增）、`updated`（补齐）、`unchanged`（已有）以及跳过或冲突的运行。同表、同运行、同观测键的相同数据可安全重试。遇到同一运行的手工行时，先核实它是否就是该观测，再传该行的 `row_id` 补齐空值。已有非空值、关联或组别冲突时，整次合并不会写入；先报告差异。人工作出更正决定后，可以使用普通结果表的版本化保存接口。

### 整理与图表的 HTTP 接口

| 方法与路径 | 请求与响应 |
| --- | --- |
| `GET P/runs?offset=0&limit=100` | 成功运行清单，返回 `runs`、`total`、`offset`、`limit`。 |
| `GET /api/projects/{project_id}/runs/{attempt_id}/files?scope=outputs&query=metrics&offset=0&limit=100` | 既有回传产物目录，返回 `files` 与分页总数 `total`。 |
| `GET P/runs/{task_id}/{attempt_id}/evidence?path=outputs%2Fmetrics.json&offset=0&limit=32768` | 校验项目、实验、成功运行和输出归属后，读取已回传文本。将 `path` 换为 `log=1` 可读取本机日志。每次最多 131072 字节。 |
| `POST P/merge?dataset=ECG` | 请求体为上述观测对象。返回 `project_id`、`dataset`、`table_id`、`name`、`revision`、计数与按请求顺序排列的 `row_ids`。单表原子提交，认证和 CSRF 与网页一致。 |
| `GET P/chart-data?dataset=ECG&table=表ID&mode=box&metric=auc` | `mode` 支持 `bar/line/point/box`。可重复传 `metric`（列 ID）、`group`（表内汇总的 `key`）。`metrics` 带单位和方向，`groups[].values` 带有效 n、均值、样本标准差和最佳均值标志。箱线图额外返回最小值、Q1、中位数、Q3、最大值。最佳值以全部匹配组为范围；并列最佳全部标记，少于两个有效组时不标记。分页参数与运行清单相同。 |

## 可以直接交给 AI 的指令

> 阅读仓库的 `docs/AI操作接口.md`。把我给的本机实验目录导入我指定的项目，先用 `projects` 核对项目 ID。导入后保留草稿，回报批次 ID、实验数、实验 ID 和依赖关系。若连接中断，用已返回的作业或批次恢复。等我说「排队」再执行。
