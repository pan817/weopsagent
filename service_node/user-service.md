# 用户服务（user-service）全链路依赖清单

## 服务描述
用户服务负责用户注册、登录、用户信息管理、权限验证等核心功能。

## 服务信息
- 服务名称: user-service
- 主机: 192.168.1.111, 192.168.1.112
- 服务端口: 8081
- 健康检查: http://<host>:8081/actuator/health
- 日志路径: /var/log/user-service/app.log, /var/log/user-service/error.log
- 进程关键词: user-service

## 数据库依赖
- 数据库类型: MySQL 8.0
- 主库: 192.168.1.203:3306
- 从库: 192.168.1.204:3306
- 数据库名: user_db
- 连接池: HikariCP，最大连接数 80

## Redis 依赖
- Redis 实例: 192.168.1.150:6379
- 用途: 用户 Session、Token 缓存、登录频率限制
- Key 前缀: session:, token:, login_limit:

## 消息队列依赖
- MQ 类型: RabbitMQ
- MQ 地址: 192.168.1.160:5672
- 发布队列: user.registered, user.profile_updated
- 消费队列: （无）

## 依赖服务
- auth-service（Token 验证，内部调用）
- notification-service（注册欢迎邮件/短信）

## 告警负责人
- 主要负责人: 王工 (wang@example.com)
- 备用负责人: 赵工 (zhao@example.com)
