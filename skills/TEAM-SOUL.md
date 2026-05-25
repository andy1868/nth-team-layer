# TEAM SOUL (Core Summary)

## Absolute Anti-Patterns
1. **Bare API calls** — 禁止直接调用外部 API，必须加 timeout + retry
2. **Cross-agent memory pollution** — 子代理结果必须通过 sidechain 隔离
3. **Mutable shared state** — 所有状态通过 Git append-only 日志
4. **Context explosion** — 压缩阈值 75%，必须分层执行
5. **Unaudited tool execution** — 所有工具调用必须通过 permission_gate

## Preferred Stack
- **Memory**: CLAUDE.md 式可编辑 + 向量索引 + append-only 账本
- **Compression**: 5 层管线（廉价优先）
- **Safety**: 7 层权限模型 + ML classifier + 沙箱隔离
- **Sync**: Git SSOT + 原子级热加载 + 零冲突日志命名

## Evolution Policy
- 触发条件：同类错误 ≥3 次 AND 浪费 token > 进化预算的 1.5 倍
- 流程：Reflector Subagent → Verifier → Evolution Gate
- 低风险自动 Merge，高风险等待人工审批

[动态加载指令]
当遇到新类型错误时，执行 `load_skill(error-category)`，从 skills/registry/ 按需召回
