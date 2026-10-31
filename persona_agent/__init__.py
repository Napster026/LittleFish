"""业务包命名空间。

业务目录保留在仓库根部，通过本包的搜索路径载入。独立测试使用
persona_agent.core 等名称；AstrBot 通过插件内的 .persona_agent 导入。
不修改 sys.path，也不在 sys.modules 注册 core/activity 等全局别名。
"""

from pathlib import Path

__path__ = [str(Path(__file__).resolve().parent.parent)]
