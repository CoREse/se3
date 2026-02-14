## MODIFIED Requirements

### Requirement: Session Startup Protocol
系统SHALL定义渐进式的session启动协议。agent进入项目时MUST通过最小信息快速定位状态，按需加载更多上下文。

启动步骤：
1. 快速探测：读取 `progress.md` 最近一条记录 + `git log --oneline -5`
2. 检查待处理事项：扫描 `human-calls/` 中状态为 `responded` 但未处理的请求
3. 确定工作范围：根据 progress 中的"下一步建议"和 openspec 活跃 changes 确定本 session 目标
4. 按需加载：仅在工作需要时读取相关的 specs、demands、design 文档

若步骤1发现项目为空（无 progress.md、无 git history），则进入**首次启动流程**：
- 通过 human call 获取项目意图
- 将人类响应转化为 `demands.md` 的初始内容
- 创建 `progress.md`

#### Scenario: 成熟项目的session启动
- **WHEN** agent启动session，项目已有 progress.md 和 git history
- **THEN** agent仅读取 progress.md 最近记录和 git log，快速确定工作范围，不读取 demands.md 等其他文件

#### Scenario: 全新项目的首次启动
- **WHEN** agent启动session，项目无 progress.md 且无 git history
- **THEN** agent通过 human call 获取项目意图，创建 demands.md 和 progress.md

#### Scenario: 按需加载深层上下文
- **WHEN** agent在执行任务时需要了解某个 spec 的详细要求
- **THEN** agent在需要时读取对应的 spec 文件，而非在启动时全部预读

### Requirement: Session Execution Boundary
每个session MUST聚焦于有限范围的工作，不得尝试在单个session中完成过多任务。

#### Scenario: Session工作范围限定
- **WHEN** agent通过启动协议确定了工作范围
- **THEN** agent仅执行该范围内的任务，不主动扩展范围

### Requirement: Session Shutdown Protocol
session结束时MUST确保代码处于可合并状态，并更新所有知识传递文件。

结束步骤包括：
1. 确保所有修改的代码可正常运行
2. 更新progress.md记录本session的工作内容和成果
3. 进行git commit，commit message包含对下一session有价值的上下文
4. 更新openspec相关状态

#### Scenario: 正常结束session
- **WHEN** agent完成当前工作范围的所有任务
- **THEN** agent执行shutdown协议，代码处于可合并状态，所有传递文件已更新

#### Scenario: 异常结束session（context window耗尽）
- **WHEN** session因context window耗尽而被迫结束
- **THEN** agent在耗尽前预留足够空间执行最小shutdown协议（至少commit当前工作并更新progress.md）

### Requirement: Progress Tracking File
系统SHALL使用progress.md作为跨session的累积进展记录。

progress.md MUST包含：
- 按时间倒序排列的session记录
- 每条记录包含：日期、完成的工作摘要、遗留问题、下一步建议

#### Scenario: 更新progress文件
- **WHEN** session结束时执行shutdown协议
- **THEN** 在progress.md顶部（倒序）添加本session的工作记录

