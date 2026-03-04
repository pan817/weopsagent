# 服务内存溢出（OOM）处理方案

## 适用场景
应用程序因内存使用超限而被 OOM Killer 终止，或发生 Java OOM 异常的通用处理方案。

## 故障现象
- 进程突然消失，无正常退出日志
- 日志中出现 `java.lang.OutOfMemoryError`
- 系统日志 `/var/log/syslog` 中有 `OOM kill` 记录
- 服务响应时间先变慢后完全不可用

## 根因分析方向
1. **内存泄漏**：对象未释放，堆内存持续增长
2. **堆内存配置过小**：JVM -Xmx 值设置不合理
3. **元空间溢出**：动态类加载过多，Metaspace 耗尽
4. **Native 内存溢出**：直接内存（DirectBuffer）泄漏
5. **数据量超预期**：大数据量查询一次性加载到内存

## 处理步骤

### 1. 确认 OOM 发生
```bash
# 检查系统 OOM 日志
dmesg | grep -i "oom\|kill"
grep -i "oom\|killed" /var/log/syslog | tail -50

# 查看 Java OOM 日志
grep "OutOfMemoryError" /path/to/app.log | tail -20
```

### 2. 分析内存使用
```bash
# 查看当前内存
free -m
cat /proc/meminfo

# Java 堆内存分析
jmap -heap <PID>
jmap -histo:live <PID> | head -30

# 生成 Heap Dump（如进程还存活）
jmap -dump:live,format=b,file=/tmp/heap.hprof <PID>
```

### 3. 临时处理（恢复服务）
```bash
# 重启服务（需人工确认）
systemctl restart <service-name>

# 调整 JVM 堆大小（临时）
# 修改启动脚本中的 -Xms 和 -Xmx 参数
```

### 4. 根本性修复
- 分析 Heap Dump，找出内存泄漏的对象
- 优化大数据量查询（分页、流式处理）
- 增加 JVM 堆内存或服务器内存
- 添加内存使用监控和自动重启策略

## 预防措施
- 启用 JVM OOM 自动 Heap Dump：`-XX:+HeapDumpOnOutOfMemoryError`
- 配置 OOM 后自动重启：`-XX:OnOutOfMemoryError="kill -9 %p"`
- 设置内存使用率告警（75%、90%）

## 标签
OOM, 内存溢出, OutOfMemoryError, JVM, 内存泄漏, 内存
