"""执行层：运行时工具边界与审计合成。

本包只放纯领域逻辑，不 import Claude Agent SDK。SDK 绑定在
``lingxi.adapters.claude_agent_hooks``，这样工具边界的判定逻辑可以在没有
SDK、没有模型额度的 CI 里被完整覆盖。
"""
