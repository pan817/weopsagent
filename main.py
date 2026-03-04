#!/usr/bin/env python3
"""
WeOps Intelligent Fault Agent - 主入口

用法:
    # 启动 HTTP API 服务
    python3 main.py serve

    # 直接处理故障（命令行模式）
    python3 main.py handle "订单服务接口超时，500 错误率激增"

    # 初始化知识库
    python3 main.py init-kb

    # 运行测试
    python3 main.py test
"""
import argparse
import logging
import sys

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def cmd_serve(args):
    """启动 HTTP API 服务"""
    from api.server import run_server
    logger.info(f"启动 WeOps Agent HTTP 服务 {args.host}:{args.port}")
    run_server()


def cmd_handle(args):
    """命令行模式直接处理故障"""
    import json
    from agent.fault_agent import FaultAgent

    logger.info("初始化 FaultAgent...")
    agent = FaultAgent(
        console_confirm_mode=True,
        enable_audit_log=True,
    )

    fault_description = args.description
    logger.info(f"开始处理故障: {fault_description[:100]}...")

    result = agent.handle_fault(
        fault_description=fault_description,
        fault_id=args.fault_id,
    )

    print("\n" + "=" * 70)
    print("故障处理结果")
    print("=" * 70)
    print(f"故障 ID  : {result['fault_id']}")
    print(f"推断服务 : {result['service_name']}")
    print(f"告警类型 : {result['alert_type']}")
    print(f"处理状态 : {result['status']}")
    print(f"处理耗时 : {result['elapsed_seconds']}s")
    print("\nAgent 分析和处理结果:")
    print("-" * 70)
    print(result['response'])
    print("=" * 70)


def cmd_init_kb(args):
    """初始化知识库"""
    from memory.long_term import LongTermMemory
    logger.info("开始初始化知识库...")
    ltm = LongTermMemory()
    counts = ltm.load_knowledge_base(force_reload=args.force)
    total = sum(counts.values())
    logger.info(f"知识库初始化完成，共加载 {total} 条知识文档")
    logger.info(f"  通用方案: {counts.get('general', 0)} 条")
    logger.info(f"  场景方案: {counts.get('scenario', 0)} 条")
    logger.info(f"  历史案例: {counts.get('history', 0)} 条")


def cmd_test(args):
    """运行测试"""
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-v", "--tb=short", "--color=yes"],
        cwd=".",
    )
    sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(
        description="WeOps Intelligent Fault Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # serve 命令
    serve_parser = subparsers.add_parser("serve", help="启动 HTTP API 服务")
    serve_parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    serve_parser.add_argument("--port", type=int, default=8080, help="监听端口")

    # handle 命令
    handle_parser = subparsers.add_parser("handle", help="直接处理故障（命令行模式）")
    handle_parser.add_argument("description", help="故障描述文本")
    handle_parser.add_argument("--fault-id", default=None, help="故障 ID（可选）")

    # init-kb 命令
    kb_parser = subparsers.add_parser("init-kb", help="初始化知识库")
    kb_parser.add_argument("--force", action="store_true", help="强制重新加载")

    # test 命令
    subparsers.add_parser("test", help="运行测试套件")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    command_map = {
        "serve": cmd_serve,
        "handle": cmd_handle,
        "init-kb": cmd_init_kb,
        "test": cmd_test,
    }

    try:
        command_map[args.command](args)
    except KeyboardInterrupt:
        logger.info("服务已停止")
    except Exception as e:
        logger.error(f"执行失败: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
