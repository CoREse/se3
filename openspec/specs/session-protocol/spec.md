# session-protocol Specification

## Purpose
TBD - created by archiving change se3-core-framework. Update Purpose after archive.
## Requirements
### Requirement: Session Startup Protocol
系统SHALL定义标准的session启动协议，agent进入项目时MUST按规定顺序执行启动步骤以获取项目上下文。

启动步骤包括：
1. 读取intentions.md了解项目意图
2. 读取demands.md了解具体需求
3. 读取openspec specs和changes了解项目进展
4. 读取git最近commit信息了解最新动态
5. 读取progress.md了解跨session的累积进展
6. 确定当前session的工作范围

#### Scenario: Agent首次进入项目
- **WHEN** agent启动一个新session且项目存在完整的SE 3.0配置
- **THEN** agent按照启动协议依序读取所有上下文文件，并确定本session的工作范围

#### Scenario: 项目无progress文件
- **WHEN** agent启动session但progress.md不存在
- **THEN** agent创建progress.md并记录本session为首个session

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

