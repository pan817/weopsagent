# 支付服务（payment-service）全链路依赖清单

## 服务描述
支付服务负责处理支付请求、支付回调、退款等核心金融功能。是高可用性要求最高的服务。

## 服务信息
- 服务名称: payment-service
- 主机: 192.168.1.121, 192.168.1.122, 192.168.1.123
- 服务端口: 8082
- 健康检查: http://<host>:8082/actuator/health
- 日志路径: /var/log/payment-service/app.log, /var/log/payment-service/error.log
- 进程关键词: payment-service

## 数据库依赖
- 数据库类型: MySQL 8.0（使用独立数据库，不与其他服务共享）
- 主库: 192.168.1.205:3306
- 从库: 192.168.1.206:3306, 192.168.1.207:3306
- 数据库名: payment_db
- 连接池: HikariCP，最大连接数 150

## Redis 依赖
- Redis 实例: 192.168.1.151:6379（支付服务使用独立 Redis 实例）
- 用途: 支付幂等性 Key、支付状态缓存、分布式锁
- Key 前缀: payment:, paylock:

## 消息队列依赖
- MQ 类型: RabbitMQ
- MQ 地址: 192.168.1.160:5672
- 发布队列: payment.success, payment.failed, payment.refunded
- 消费队列: order.pay_request

## 依赖服务
- 外部第三方支付平台（微信支付 API、支付宝 API）

## 外部依赖
- 微信支付 API: api.mch.weixin.qq.com
- 支付宝 API: openapi.alipay.com

## 告警负责人
- 主要负责人: 钱工 (qian@example.com)
- 备用负责人: 孙工 (sun@example.com)
- 高危告警升级: CTO (cto@example.com)

## 特别注意
此服务属于金融核心服务，任何重启操作必须在业务低峰期（凌晨 2-4 点）进行，
且必须确认无进行中的支付事务。
