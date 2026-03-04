# 服务宕机快速恢复处理方案

## 适用场景
服务进程意外退出、无法响应请求的通用快速恢复处理方案。

## 故障现象
- 服务无法访问，接口全部返回连接拒绝
- 监控显示进程不存在
- 健康检查失败

## 处理步骤

### 1. 快速确认故障范围
```bash
# 检查进程是否存在
pgrep -fa <service-name>
systemctl status <service-name>

# 检查端口是否监听
netstat -tlnp | grep <port>
ss -tlnp | grep <port>
```

### 2. 查看崩溃原因
```bash
# 查看系统日志（进程崩溃原因）
journalctl -u <service-name> -n 100 --no-pager

# 查看应用日志最后100行
tail -100 /path/to/app.log

# 查看 core dump
ls -la /var/crash/ 2>/dev/null || ls -la /core* 2>/dev/null
```

### 3. 重启服务（需人工确认）
```bash
# systemd 托管服务
systemctl restart <service-name>
systemctl status <service-name>

# supervisor 托管服务
supervisorctl restart <service-name>
supervisorctl status <service-name>
```

### 4. 验证服务恢复
```bash
# 等待 10-30 秒后验证
sleep 15
curl -f http://localhost:<port>/health || echo "健康检查失败"

# 查看最新日志确认无启动错误
tail -50 /path/to/app.log
```

### 5. 通知相关人员
- 发送服务宕机和恢复通知
- 记录本次故障的时间和恢复情况

## 根本性修复
- 分析崩溃根因（OOM、代码 bug、资源耗尽等）
- 配置服务自动重启策略
- 增加服务健康检查和监控

## 标签
宕机, 崩溃, crash, 服务不可用, 重启, systemctl, supervisor
