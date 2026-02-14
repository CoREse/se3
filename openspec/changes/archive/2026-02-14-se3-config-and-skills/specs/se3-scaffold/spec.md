## MODIFIED Requirements

### Requirement: SE 3.0 Project Structure
系统SHALL定义标准的SE 3.0项目文件结构。

标准结构（新增se3.config.yaml）：
```
project/
├── intentions.md
├── demands.md
├── progress.md
├── se3.config.yaml        # 新增：框架配置文件
├── human-calls/
├── agent-comms/
├── openspec/
│   ├── specs/
│   ├── changes/
│   └── archive/
├── .claude/
│   ├── CLAUDE.md
│   └── skills/
│       └── se3-init/      # 新增：初始化Skill
│           └── SKILL.md
└── README.md
```

#### Scenario: 初始化项目结构
- **WHEN** 在一个目录中初始化SE 3.0
- **THEN** 创建上述标准文件结构，包含se3.config.yaml和init skill
