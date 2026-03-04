#!/usr/bin/env python3
"""
知识库初始化脚本

扫描 data/ 目录下的所有 Markdown 文件，
将其加载到 ChromaDB 向量数据库中。

用法:
    python3 scripts/init_knowledge_base.py [--force-reload]
"""
import sys
import os
import argparse
import logging

# 将项目根目录加入 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="初始化 WeOps Agent 知识库")
    parser.add_argument(
        "--force-reload",
        action="store_true",
        help="强制重新加载（清空已有数据后重新导入）",
    )
    args = parser.parse_args()

    logger.info("开始初始化知识库...")

    try:
        from memory.long_term import LongTermMemory
        ltm = LongTermMemory()
        counts = ltm.load_knowledge_base(force_reload=args.force_reload)

        total = sum(counts.values())
        logger.info(f"知识库初始化完成！共加载 {total} 条知识文档")
        logger.info(f"  通用处理方案: {counts.get('general', 0)} 条")
        logger.info(f"  场景处理方案: {counts.get('scenario', 0)} 条")
        logger.info(f"  历史故障案例: {counts.get('history', 0)} 条")

        if total == 0:
            logger.warning("未加载任何知识文档，请检查 data/ 目录下是否有 Markdown 文件")

    except ImportError as e:
        logger.error(f"依赖未安装: {e}")
        logger.error("请先运行: pip install -r requirements.txt")
        sys.exit(1)
    except Exception as e:
        logger.error(f"知识库初始化失败: {e}", exc_info=True)
        logger.warning("知识库初始化失败，但服务仍可启动（长期记忆功能将不可用）")


if __name__ == "__main__":
    main()
