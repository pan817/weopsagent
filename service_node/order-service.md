# 订单服务（order-service）全链路依赖清单

## 服务描述
订单服务是核心业务服务，负责订单的创建、查询、状态流转等全生命周期管理。

## 服务信息
- 服务名称: order-service
- 主机: 192.168.1.101, 192.168.1.102
- 服务端口: 8080
- 健康检查: http://<host>:8080/actuator/health
- 日志路径: /var/log/order-service/app.log, /var/log/order-service/error.log
- 进程关键词: order-service

## 数据库依赖
- 数据库类型: MySQL 8.0
- 主库: 192.168.1.201:3306
- 从库: 192.168.1.202:3306
- 数据库名: order_db
- 连接池: HikariCP，最大连接数 100

## Redis 依赖
- Redis 实例: 192.168.1.150:6379
- 用途: 订单状态缓存、幂等性 Key、分布式锁
- Key 前缀: order:, lock:order:

## 消息队列依赖
- MQ 类型: RabbitMQ
- MQ 地址: 192.168.1.160:5672
- 发布队列: order.created, order.paid, order.cancelled
- 消费队列: payment.success, inventory.reserved

## 依赖服务
- user-service（用户信息查询）
- inventory-service（库存扣减）
- payment-service（支付状态查询）
- notification-service（订单通知）

## 告警负责人
- 主要负责人: 张工 (zhang@example.com)
- 备用负责人: 李工 (li@example.com)

## 常见故障处理
参考历史案例：
- case_001：数据库连接池耗尽
- 快速恢复：先查日志确认原因，再重启服务
