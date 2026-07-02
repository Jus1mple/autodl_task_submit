"""autodl —— 在 AutoDL 开发者 API 之上的可靠客户端工具。

地基层：稳健的 HTTP 客户端、SSH 连接层（带失败分类）、配置文件、
带锁的 SQLite 实例台账、后台非阻塞执行模型。
提交管线：tasks 模块是 CLI / batch / web / 生成脚本共用的唯一任务提交实现。
"""

__version__ = "1.3.0"
