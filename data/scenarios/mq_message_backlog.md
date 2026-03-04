# 消息队列积压处理方案

## 适用场景
MQ（RabbitMQ/Kafka）队列消息大量积压，消费者处理不及时的场景。

## 故障现象
- 队列消息数量持续增长（>10万条）
- 消费者数量减少或消费速率下降
- 下游业务数据延迟（订单状态更新慢、通知延迟等）
- 监控显示消息发布速率远大于消费速率

## 根因分析
1. **消费者宕机**：消费者服务实例减少
2. **消费者处理慢**：下游依赖（DB、接口）性能下降
3. **消费失败重试**：消费逻辑异常导致消息不断重试
4. **突发流量**：发布速率突然暴增
5. **死信堆积**：消息进入死信队列无人处理

## 排查步骤

### RabbitMQ

```bash
# 通过 Management API 查看队列状态
curl -u guest:guest http://localhost:15672/api/queues | python3 -m json.tool

# 查看消费者状态
rabbitmqctl list_consumers

# 查看死信队列
rabbitmqctl list_queues name messages consumers durable

# 查看连接
rabbitmqctl list_connections
```

### Kafka

```bash
# 查看消费组积压
kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
  --group <consumer-group> --describe

# 查看 Topic 详情
kafka-topics.sh --bootstrap-server localhost:9092 \
  --topic <topic-name> --describe
```

## 处理方案

### 1. 快速增加消费者（水平扩容）
```bash
# 启动更多消费者实例
# 需根据实际部署方式操作（k8s 扩容、docker 多实例等）
kubectl scale deployment <consumer-deployment> --replicas=5

# 或重启现有消费者
supervisorctl restart all
```

### 2. 修复消费者代码问题
- 检查消费逻辑异常，修复后重新部署
- 增大消费者线程池大小
- 优化消费依赖的数据库查询

### 3. 暂时跳过无法处理的消息
```bash
# RabbitMQ：清空死信队列（谨慎操作！需确认消息可丢弃）
rabbitmqctl purge_queue <dead-letter-queue-name>
```

### 4. 消费者追赶积压
- 临时增加消费者的 prefetch count
- 批量消费减少网络往返

## 预防措施
- 设置队列积压告警（>1000条、>10000条）
- 监控消费者数量，自动扩容
- 配置合理的消息 TTL 和死信队列处理策略
- 消费者实现幂等性，支持安全重试

## 标签
MQ, RabbitMQ, Kafka, 消息积压, 消费者, 队列, 死信
