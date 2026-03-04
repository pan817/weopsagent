# 接口响应缓慢处理方案

## 适用场景
API 接口响应时间异常升高（>2s），用户体验变差的场景。

## 故障现象
- 接口 P99 响应时间超过 2 秒
- 前端页面加载缓慢
- 超时错误（504 Gateway Timeout）增多
- 监控显示请求积压

## 全链路排查思路

### 第一步：确定瓶颈层
```
客户端 → 网关 → 应用服务 → 数据库/Redis/MQ
```

检查各层响应时间，缩小范围。

### 第二步：检查应用服务
```bash
# 查看 JVM 线程状态（Java 应用）
jstack <PID> | grep -E "BLOCKED|WAITING" | wc -l

# 查看线程池队列积压
# 通过 actuator 或 JMX 获取线程池指标

# 查看 GC 日志
grep "GCtime" /path/to/gc.log | tail -20
```

### 第三步：检查数据库
```sql
-- 查看当前活跃慢查询
SELECT * FROM information_schema.PROCESSLIST
WHERE command != 'Sleep' AND time > 1
ORDER BY time DESC LIMIT 20;

-- 查看锁等待
SELECT * FROM sys.innodb_lock_waits LIMIT 10;

-- 查看索引使用情况
SHOW STATUS LIKE 'Handler%';
```

### 第四步：检查 Redis
```bash
# 连接 Redis 检查慢操作
redis-cli SLOWLOG GET 20
redis-cli INFO stats | grep -E "instantaneous_ops|rejected_connections"

# 检查大 Key
redis-cli --bigkeys
```

### 第五步：检查依赖服务
- 检查下游服务响应时间
- 检查 MQ 消息积压
- 检查 DNS 解析是否异常

## 常见处理方案

### 数据库慢查询
1. 添加缺失索引
2. 优化 SQL 语句（避免全表扫描）
3. 读写分离（读请求走从库）
4. 增加数据库连接池大小

### Redis 缓存问题
1. 缓存预热（避免冷启动）
2. 大 Key 拆分
3. 优化 Redis 集群配置
4. 增加 Redis 副本节点

### 应用层优化
1. 增加服务实例水平扩容
2. 增大线程池核心线程数
3. 添加熔断器防止级联故障
4. 启用异步处理减少阻塞

## 标签
接口慢, 响应超时, 性能, 慢查询, 缓存, 数据库, redis
