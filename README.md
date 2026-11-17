# CycleFlow 机器人样机迭代数据服务

CycleFlow 面向样机研发团队，接收设计修订、部件批次、生产工单和质量试验等独立来源的事件。SQLite 同时保存原始事件身份和派生的业务状态。报表将设计版本、工单、工序耗时、物料批次、试验结果和质量问题连接起来，供团队解释每次迭代的交付效率。

## 运行

依赖版本锁定在 `requirements.txt`。在项目目录运行 `python -m unittest discover -s tests -v` 执行业务测试，`python -m compileall -q cycleflow` 检查源码，`python -m cycleflow.demo` 运行合成数据验收。安装所列依赖后，可设置 `CYCLEFLOW_DB` 为可写的数据库路径，再运行 `uvicorn cycleflow.api:app` 启动 HTTP 服务。命令行帮助为 `python -m cycleflow --db cycleflow.sqlite3 --help`。

演示仅使用临时数据库和本地合成产品。它分两次导入同一清单，验证试验良率，冻结月报，并审计存储的状态链；正常运行无需访问外部服务。

## 事件协议

向 `/events` 提交 `source`、`external_id`、`kind`、带时区的 `occurred_at` 和对象类型的 `payload`。相同事件重放返回既有编号；相同身份提交不同内容返回冲突。缺少前置对象或暂时缺料的事件进入待处理状态，可调用 `/events/{id}/retry` 补齐后重试。其他业务无效事件仍保留拒绝原因。批量接口每次使用稳定的批次编号和完整事件数组；系统核对清单指纹，从已提交的行继续。

事件类型包括 `design.release`、`design.retire`、`lot.receive`、`lot.quarantine`、`job.open`、`job.stage_start`、`job.stage_finish`、`job.scrap`、`plan.create` 和 `test.record`。设计包含产品、版本、部件数量及可选的父版本。部件批次包含料号、供应商和正整数数量。工单绑定一个设计和产量，成功预留物料后依次经过加工、装配和集成。试验计划绑定精确的产品版本，受测数量必须覆盖该工单全部单位。

## 报表与发布

请求 `/reports/{product}?window_start=...&window_end=...` 获取当前视图。时间窗含起点、不含终点，以工单创建时间决定归属。报表提供按版本汇总、良率、工序时长、等待时长、来源事件编号和质量问题。周度、返工分组、瓶颈和供应商视图使用相同的已接受事实。供应商试验归因可以重叠，不能跨供应商直接求和。

向 `/releases` 提交发布编号、产品、时间窗和预览返回的 `expected_fingerprint`。未完成工单或窗口内未解决事件阻止发布。发布后保留原报告和来源事件头；`/releases/{id}/freshness` 比较当前视图与冻结视图，不改写历史。`/quality/audit` 检查事件身份、设计版本链、物料预留、工序顺序、试验记录和发布摘要。

## 可比群组分析

试产放量时设计修订、供应商批次、返工比例和试验覆盖率同时变化，直接比较两期总良率会被结构变化主导。`GET /reports/{product}/cohort-compare` 基于与冻结报表相同的已接受事实重建可比群组，输出可复现 JSON（含指纹）：

- **群组键**：设计修订 × 是否返工 × 供应商构成签名（按料号列出供应商，不含数量，避免产量差异拆散群组）。每个群组另保留物理批次签名。
- **进入对照的条件**：群组在两期都出现、批次签名一致（可用 `require_same_lots=false` 放宽）、各期试验单位数达到 `min_tested_units`、试验覆盖率达到 `min_coverage`。不满足的群组保留在结果中并给出 `exclusion_reasons`（单侧群组、批次变化、样本不足、覆盖率不足），不会被静默丢弃。
- **不确定性**：每个群组给出 Wilson 95% 区间；匹配池给出两比例差的独立 95% 区间。小样本群组的宽区间保持可见，结论只在匹配池区间排除零时给出改善/退化判定，否则为 `no_detectable_change` 或 `inconclusive_no_comparable_cohort`。
- **效应分解**：对称 Kitagawa 分解把朴素总良率差拆成组内质量效应、配置构成效应和产量效应，三项之和等于朴素差值，帮助判断差异主要来自产量、配置构成还是组内质量。
- **数据不足显式化**：未完成、待试、报废和无试验终态工单进入 `data_gaps` 与 `coverage`，绝不进入良率分母；窗口无可试单位时返回 `no_testable_jobs` 状态。
- **迟到事件追踪**：`GET /releases/{id}/cohort-drift` 从冻结报表重建当时的群组分析，与当前分析并列，列出新增/消失/变更工单、两个结论与两组指纹。冻结报表本身始终可按原指纹复现，迟到部件或补测只改变当前视图。CLI 对应 `cohort-compare` 与 `cohort-drift` 子命令，合成演示为 `python -m cycleflow.cohort_demo`。
