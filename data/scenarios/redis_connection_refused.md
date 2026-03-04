# Redis 连接拒绝/超时处理方案

## 适用场景
应用服务无法连接 Redis，或 Redis 操作出现大量超时的场景。

## 故障现象
- 日志出现 `Connection refused` 或 `ECONNREFUSED`
- 日志出现 `Redis command timeout`
- 依赖 Redis 的功能（Session、缓存、限流等）全部失效
- 接口响应时间突增

## 根因分析
1. **Redis 进程宕机**：Redis 服务意外退出
2. **内存打满**：Redis 内存达到 maxmemory，触发 OOM 策略
3. **连接数超限**：活跃连接数超过 maxclients 配置
4. **网络问题**：网络分区或防火墙规则变更
5. **大 Key 阻塞**：DEL/KEYS 等操作阻塞 Redis 主线程

## 排查步骤

### 1. 确认 Redis 连通性
```bash
# 基础连通测试
redis-cli -h <host> -p 6379 -a <password> ping

# 查看 Redis 进程
ps aux | grep redis

# 检查端口
netstat -tlnp | grep 6379
```

### 2. 收集 Redis 状态
```bash
# 获取详细状态信息
redis-cli INFO all

# 查看内存使用
redis-cli INFO memory | grep -E "used_memory|maxmemory"

# 查看客户端连接
redis-cli CLIENT LIST | wc -l
redis-cli INFO clients

# 查看慢操作
redis-cli SLOWLOG GET 20

# 查看最近命令统计
redis-cli MONITOR  # 短时间抽样观察
```

### 3. 查看 Redis 日志
```bash
# 通常在以下位置
cat /var/log/redis/redis-server.log | tail -100
# 或
journalctl -u redis -n 100 --no-pager
```

## 处理方案

### Redis 内存打满
```bash
# 立即操作：清理过期 Key
redis-cli MEMORY DOCTOR
redis-cli --scan --pattern 'expired:*' | xargs redis-cli DEL

# 调整 maxmemory-policy（允许按 LRU 淘汰）
redis-cli CONFIG SET maxmemory-policy allkeys-lru

# 增加 Redis 内存配置
redis-cli CONFIG SET maxmemory 4gb
```

### 连接数超限
```bash
# 查看当前连接数和上限
redis-cli INFO clients
redis-cli CONFIG GET maxclients

# 临时增大 maxclients
redis-cli CONFIG SET maxclients 10000

# Kill 空闲连接
redis-cli CLIENT KILL ID <client_id>
```

### Redis 进程宕机（需重启，需人工确认）
```bash
systemctl restart redis
# 等待启动并验证
sleep 5 && redis-cli ping
```

### 大 Key 阻塞
```bash
# 异步删除大 Key（非阻塞）
redis-cli UNLINK <key_name>

# 找出大 Key
redis-cli --bigkeys
```

## 预防措施
- 配置 Redis 内存告警（75%、90%）
- 设置合理的 Key 过期时间
- 使用异步删除（UNLINK 替代 DEL）
- 部署 Redis Sentinel 或 Cluster 提高可用性

## 标签
Redis, 缓存, 连接拒绝, 内存, 超时, 慢查询, redis-cli
