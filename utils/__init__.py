"""paper-polish-project 公共工具层。

约定：
- 模型、Prompt、规则等资源一律通过 config_loader / prompt_renderer 读取；
- 业务 Skill 只依赖本层，不直接访问网络或 API Key。
"""

__version__ = "0.1.0"
