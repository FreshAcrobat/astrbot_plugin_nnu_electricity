# NNU Electricity Bill Inquiry Extensions

这是用于查询南京师范大学学生公寓电费的 AstrBot 插件。

## 支持的范围

支持查询仙林校区除博士楼，青教以外的所有宿舍楼，暂不支持随园校区。

## 安装与加载

0. 安装依赖：`httpx`
1. 将 `astrbot_plugin_nnu_electricity` 复制到 AstrBot 的 `data/plugins/` 目录
2. 在插件管理中启用并重载

## 指令列表

- `bill <楼栋> <宿舍>` - 查询指定宿舍电费
- `b` - 快速查询
- `bill sub` <楼栋> <宿舍> - 订阅每日低电量提醒
- `bill unsub` - 取消订阅
- `bill on/off` - 启用/禁用当前群组响应

## ToDo

- 完善订阅的处理逻辑
- 规范配置文件、配置热重载

## 免责声明

本插件仅供学习与交流使用，使用过程中产生的风险由使用者自行承担。
为了遵守网络安全相关法律法规，本仓库已对所有敏感信息进行脱敏处理。