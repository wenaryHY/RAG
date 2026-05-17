"""Panel 子包：Web 管理面板的后端聚合层 + 静态文件挂载。

设计原则：
- 不重复 /scheduler/、/audit/、/sync/ 已有路由
- 只补：服务健康总览、datasets 代理、手动 upload、tray 健康轮询用的精简端点
- 全部走 127.0.0.1，不加 auth
"""
