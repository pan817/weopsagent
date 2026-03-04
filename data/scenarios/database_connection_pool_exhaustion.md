# 数据库连接池耗尽处理方案

## 适用场景
应用服务因数据库连接池耗尽，导致数据库相关请求全部失败或长时间等待。

## 故障现象
- 日志出现 `Unable to acquire JDBC Connection`
- 日志出现 `Connection pool exhausted` 或 `Timeout waiting for connection`
- 数据库相关接口全部 500 报错
- 非数据库操作仍然正常

## 根因分析
1. **连接池配置过小**：最大连接数设置不足
2. **连接泄漏**：代码中 Connection 未正确关闭
3. **数据库操作慢**：慢查询导致连接长时间被占用
4. **流量突增**：并发请求超过连接池上限

## 排查步骤

### 1. 确认连接池状态
```bash
# HikariCP 连接池状态（通过应用日志）
grep -i "hikari\|connection pool" /path/to/app.log | tail -50

# 或通过 Actuator 接口（Spring Boot）
curl http://localhost:8080/actuator/metrics/hikaricp.connections
curl http://localhost:8080/actuator/metrics/hikaricp.connections.active
```

### 2. 检查数据库活跃连接
```sql
-- 查看所有活跃连接
SELECT user, host, db, command, time, state, left(info, 100) as query
FROM information_schema.PROCESSLIST
WHERE command != 'Sleep'
ORDER BY time DESC;

-- 统计连接来源
SELECT host, COUNT(*) as connection_count
FROM information_schema.PROCESSLIST
GROUP BY host
ORDER BY connection_count DESC;
```

### 3. 查找长时间运行的事务
```sql
SELECT trx_id, trx_started, trx_state, trx_mysql_thread_id,
       left(trx_query, 200) as query
FROM information_schema.INNODB_TRX
WHERE trx_started < NOW() - INTERVAL 30 SECOND
ORDER BY trx_started;
```

## 处理方案

### 紧急处理（恢复服务）
```sql
-- Kill 长时间占用连接的进程
KILL <thread_id>;

-- 批量 Kill 指定用户的 Sleep 连接（谨慎操作）
SELECT CONCAT('KILL ', id, ';') FROM information_schema.PROCESSLIST
WHERE user = 'app_user' AND command = 'Sleep' AND time > 300;
```

### 临时增大连接池（重启服务）
修改应用配置：
```yaml
spring:
  datasource:
    hikari:
      maximum-pool-size: 50  # 从默认值增加
      connection-timeout: 30000
      idle-timeout: 600000
```

### 根本性修复
1. 修复代码中的连接泄漏（确保 try-with-resources 或 finally 关闭连接）
2. 优化慢 SQL，减少连接占用时间
3. 合理设置连接池大小（一般 = CPU 核心数 * 2 + 有效磁盘数）
4. 增加数据库服务器最大连接数配置

## 标签
连接池, 数据库, 连接泄漏, HikariCP, MySQL, 连接耗尽
