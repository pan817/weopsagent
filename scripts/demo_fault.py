#!/usr/bin/env python3
"""
故障处理演示脚本

演示如何通过 API 接口提交故障并获取处理结果。
也可以直接调用 FaultAgent（不通过 HTTP）进行测试。

用法:
    # 通过 API 接口（需先启动服务）
    python3 scripts/demo_fault.py --mode api --host localhost:8080

    # 直接调用 Agent（不需要启动服务）
    python3 scripts/demo_fault.py --mode direct
"""
import argparse
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEMO_FAULTS = [
    {
        "description": "订单服务接口出现大量 500 错误，错误率从 0.1% 骤升至 35%，"
                       "日志显示大量 'Unable to acquire JDBC Connection' 错误",
        "expected_service": "order-service",
        "expected_issue": "数据库连接池耗尽",
    },
    {
        "description": "用户服务 Session 功能异常，用户反映频繁掉线，"
                       "日志中出现 'ECONNREFUSED' 连接 Redis 失败的错误",
        "expected_service": "user-service",
        "expected_issue": "Redis 连接失败",
    },
    {
        "description": "订单处理消费者队列积压超过 10 万条，"
                       "监控显示消费者实例只剩 1 个（原本应有 3 个）",
        "expected_service": "order-service",
        "expected_issue": "消费者宕机导致消息积压",
    },
]


def demo_api_mode(base_url: str, fault_index: int = 0):
    """通过 HTTP API 演示故障处理"""
    try:
        import httpx
    except ImportError:
        print("请安装 httpx: pip install httpx")
        sys.exit(1)

    fault = DEMO_FAULTS[fault_index % len(DEMO_FAULTS)]
    print(f"\n{'='*60}")
    print(f"演示故障: {fault['description'][:80]}...")
    print(f"预期服务: {fault['expected_service']}")
    print(f"预期问题: {fault['expected_issue']}")
    print(f"{'='*60}\n")

    with httpx.Client(timeout=120.0) as client:
        response = client.post(
            f"http://{base_url}/api/v1/fault/handle",
            json={"fault_description": fault["description"]},
        )
        response.raise_for_status()
        result = response.json()

    print(f"故障 ID: {result['fault_id']}")
    print(f"推断服务: {result['service_name']}")
    print(f"告警类型: {result['alert_type']}")
    print(f"处理耗时: {result['elapsed_seconds']}s")
    print(f"\n处理结果:\n{result['response']}")


def demo_direct_mode(fault_index: int = 0):
    """直接调用 Agent 演示（不需要启动 HTTP 服务）"""
    import logging
    logging.basicConfig(level=logging.WARNING)  # 减少日志噪音

    print("初始化 FaultAgent（可能需要一些时间加载知识库）...")

    from agents.fault_agent import FaultAgent

    agent = FaultAgent(
        console_confirm_mode=True,   # 控制台交互确认
        enable_audit_log=True,
    )

    fault = DEMO_FAULTS[fault_index % len(DEMO_FAULTS)]
    print(f"\n{'='*60}")
    print(f"演示故障: {fault['description'][:80]}...")
    print(f"预期服务: {fault['expected_service']}")
    print(f"预期问题: {fault['expected_issue']}")
    print(f"{'='*60}\n")

    result = agent.handle_fault(
        fault_description=fault["description"],
        fault_id="DEMO-001",
    )

    print(f"\n{'='*60}")
    print(f"处理结果摘要:")
    print(f"  故障 ID  : {result['fault_id']}")
    print(f"  推断服务 : {result['service_name']}")
    print(f"  告警类型 : {result['alert_type']}")
    print(f"  处理状态 : {result['status']}")
    print(f"  处理耗时 : {result['elapsed_seconds']}s")
    print(f"\nAgent 分析和处理结果:")
    print(result['response'])
    print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(description="WeOps Agent 故障处理演示")
    parser.add_argument(
        "--mode",
        choices=["api", "direct"],
        default="direct",
        help="演示模式: api（通过 HTTP 接口）或 direct（直接调用）",
    )
    parser.add_argument(
        "--host",
        default="localhost:8080",
        help="API 服务地址（仅 api 模式）",
    )
    parser.add_argument(
        "--fault",
        type=int,
        default=0,
        choices=[0, 1, 2],
        help=f"演示哪个故障场景 (0-{len(DEMO_FAULTS)-1})",
    )

    args = parser.parse_args()

    if args.mode == "api":
        demo_api_mode(args.host, args.fault)
    else:
        demo_direct_mode(args.fault)


if __name__ == "__main__":
    main()
