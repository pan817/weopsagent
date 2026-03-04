# 历史案例-002: Redis 内存打满导致服务雪崩

## 故障现象
2024-02-20 09:15 - 多个服务同时出现大量 Redis 操作失败，影响 Session 登录、
商品缓存、限流等功能，导致部分接口 5xx 错误率升高。

## 故障时间
- 发生时间: 2024-02-20 09:12:00
- 发现时间: 2024-02-20 09:15:00
- 恢复时间: 2024-02-20 09:45:00
- 持续时长: 30 分钟

## 影响范围
- 用户 Session 失效，部分用户需重新登录
- 商品列表页缓存失效，数据库压力骤增
- 接口限流功能失效，下游数据库被打满

## 根因分析
1. 活动促销导致缓存 Key 急剧增加（新增约 2000 万个活动商品缓存 Key）
2. 这些 Key 未设置过期时间，永久占用内存
3. Redis maxmemory 设置为 8GB，已接近满载
4. 活动商品缓存加载后，内存瞬间达到上限
5. Redis 配置的 `maxmemory-policy: noeviction`，无法淘汰旧 Key
6. 后续所有写操作返回 `OOM command not allowed`

## 处理过程

### 步骤 1：确认根因（09:20）
```bash
redis-cli INFO memory
# used_memory_human: 7.98G
# maxmemory_human: 8.00G
# maxmemory_policy: noeviction
```

### 步骤 2：临时调整淘汰策略（09:22）
```bash
redis-cli CONFIG SET maxmemory-policy allkeys-lru
```
策略调整后，Redis 开始自动淘汰最久未使用的 Key，写入操作恢复正常。

### 步骤 3：清理无过期时间的缓存 Key（09:25 - 09:40）
```bash
# 找到活动缓存 Key 前缀并删除（分批异步删除）
redis-cli --scan --pattern 'activity:product:*' | \
  xargs -L 100 redis-cli UNLINK
```

### 步骤 4：紧急扩容 Redis 内存（当天）
- 将 Redis maxmemory 从 8GB 扩容到 16GB

## 最终解决方案
1. 修复代码，所有活动相关缓存 Key 统一设置 7 天过期时间
2. 将 maxmemory-policy 改为 `volatile-lru`（只淘汰有过期时间的 Key）
3. Redis 内存扩容至 16GB
4. 增加 Redis 内存使用率告警（75%、90%）

## 有效性
已确认有效，Redis 内存使用率稳定在 40% 以下。

## 标签
Redis, 内存, OOM, 缓存, maxmemory, 活动促销, 淘汰策略
