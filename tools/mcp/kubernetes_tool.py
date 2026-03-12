"""
Kubernetes MCP 工具 - 通过 Kubernetes API 管理和诊断集群资源

提供 Pod 查询、日志获取、Deployment 重启、资源描述等工具。
通过 kubeconfig 或 ServiceAccount 认证访问 K8s API Server。

典型用法：
- 查看故障服务的 Pod 状态（CrashLoopBackOff / OOMKilled）
- 获取 Pod 日志定位错误
- 滚动重启 Deployment 恢复服务
- 查看 Node/Service/Ingress 等资源详情
"""
import json
import logging
import os
import time

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from config.settings import settings
from .client import mcp_request, format_mcp_result

logger = logging.getLogger(__name__)


def _k8s_headers() -> dict:
    """构建 Kubernetes API 认证请求头"""
    headers = {"Accept": "application/json"}
    token = settings.mcp_kubernetes_token
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _k8s_url(path: str) -> str:
    """拼接 Kubernetes API URL"""
    base = settings.mcp_kubernetes_api_url.rstrip("/")
    return f"{base}{path}"


class K8sGetPodsInput(BaseModel):
    """Pod 查询输入"""
    namespace: str = Field(default="default", description="命名空间，如 'default'、'production'、'monitoring'")
    label_selector: str = Field(default="", description="标签选择器，如 'app=order-service'、'tier=backend'")
    field_selector: str = Field(default="", description="字段选择器，如 'status.phase=Running'、'status.phase!=Succeeded'")


class K8sGetPodLogsInput(BaseModel):
    """Pod 日志查询输入"""
    pod_name: str = Field(description="Pod 名称")
    namespace: str = Field(default="default", description="命名空间")
    container: str = Field(default="", description="容器名（多容器 Pod 时指定）")
    tail_lines: int = Field(default=200, description="返回最后 N 行日志")
    since_seconds: int = Field(default=1800, description="获取最近 N 秒的日志（默认 30 分钟）")


class K8sRestartDeploymentInput(BaseModel):
    """Deployment 重启输入"""
    deployment_name: str = Field(description="Deployment 名称")
    namespace: str = Field(default="default", description="命名空间")


class K8sDescribeResourceInput(BaseModel):
    """资源描述输入"""
    resource_type: str = Field(description="资源类型，如 'pod'、'deployment'、'service'、'node'、'ingress'、'configmap'")
    resource_name: str = Field(description="资源名称")
    namespace: str = Field(default="default", description="命名空间（集群级资源如 node 可忽略）")


@tool("k8s_get_pods", args_schema=K8sGetPodsInput)
def k8s_get_pods(
    namespace: str = "default",
    label_selector: str = "",
    field_selector: str = "",
) -> str:
    """查询 Kubernetes Pod 列表及状态。可按命名空间和标签过滤。
    返回 Pod 名称、状态、重启次数、所在节点等信息，快速定位异常 Pod。"""
    start_time = time.time()

    params = {}
    if label_selector:
        params["labelSelector"] = label_selector
    if field_selector:
        params["fieldSelector"] = field_selector

    data = mcp_request(
        url=_k8s_url(f"/api/v1/namespaces/{namespace}/pods"),
        method="GET",
        params=params,
        headers=_k8s_headers(),
        timeout=settings.mcp_kubernetes_timeout,
    )

    elapsed = time.time() - start_time

    if isinstance(data, dict) and "items" in data:
        pods = []
        for item in data["items"]:
            metadata = item.get("metadata", {})
            status = item.get("status", {})
            container_statuses = status.get("containerStatuses", [])
            pods.append({
                "name": metadata.get("name"),
                "namespace": metadata.get("namespace"),
                "phase": status.get("phase"),
                "node": item.get("spec", {}).get("nodeName"),
                "start_time": status.get("startTime"),
                "containers": [
                    {
                        "name": cs.get("name"),
                        "ready": cs.get("ready"),
                        "restart_count": cs.get("restartCount", 0),
                        "state": list(cs.get("state", {}).keys()),
                    }
                    for cs in container_statuses
                ],
                "conditions": [
                    {"type": c.get("type"), "status": c.get("status")}
                    for c in status.get("conditions", [])
                ],
            })
        return format_mcp_result("k8s_get_pods", {
            "namespace": namespace,
            "total": len(pods),
            "pods": pods,
        }, elapsed)

    return format_mcp_result("k8s_get_pods", data, elapsed)


@tool("k8s_get_pod_logs", args_schema=K8sGetPodLogsInput)
def k8s_get_pod_logs(
    pod_name: str,
    namespace: str = "default",
    container: str = "",
    tail_lines: int = 200,
    since_seconds: int = 1800,
) -> str:
    """获取 Kubernetes Pod 的容器日志。返回指定 Pod 最近的日志内容。
    适用于查看服务启动日志、定位运行时错误。"""
    start_time = time.time()

    params = {
        "tailLines": min(tail_lines, 1000),
        "sinceSeconds": since_seconds,
    }
    if container:
        params["container"] = container

    data = mcp_request(
        url=_k8s_url(f"/api/v1/namespaces/{namespace}/pods/{pod_name}/log"),
        method="GET",
        params=params,
        headers=_k8s_headers(),
        timeout=settings.mcp_kubernetes_timeout,
    )

    elapsed = time.time() - start_time

    # 日志接口返回纯文本
    if isinstance(data, dict) and "raw_text" in data:
        log_text = data["raw_text"]
        lines = log_text.strip().split("\n") if log_text.strip() else []
        return format_mcp_result("k8s_get_pod_logs", {
            "pod": pod_name,
            "namespace": namespace,
            "container": container or "(default)",
            "line_count": len(lines),
            "logs": log_text[:8000],  # 截断过长日志
        }, elapsed)

    return format_mcp_result("k8s_get_pod_logs", data, elapsed)


@tool("k8s_restart_deployment", args_schema=K8sRestartDeploymentInput)
def k8s_restart_deployment(
    deployment_name: str,
    namespace: str = "default",
) -> str:
    """⚠️ 危险操作：滚动重启 Kubernetes Deployment。通过 patch restartedAt 注解触发滚动更新。
    执行前需经 HumanConfirmMiddleware 人工确认。"""
    start_time = time.time()

    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    patch_body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": now,
                    }
                }
            }
        }
    }

    headers = _k8s_headers()
    headers["Content-Type"] = "application/strategic-merge-patch+json"

    data = mcp_request(
        url=_k8s_url(f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment_name}"),
        method="PATCH",
        json_body=patch_body,
        headers=headers,
        timeout=settings.mcp_kubernetes_timeout,
    )

    elapsed = time.time() - start_time

    if isinstance(data, dict) and data.get("kind") == "Deployment":
        metadata = data.get("metadata", {})
        spec = data.get("spec", {})
        return format_mcp_result("k8s_restart_deployment", {
            "deployment": deployment_name,
            "namespace": namespace,
            "restarted_at": now,
            "replicas": spec.get("replicas"),
            "generation": metadata.get("generation"),
        }, elapsed)

    return format_mcp_result("k8s_restart_deployment", data, elapsed)


@tool("k8s_describe_resource", args_schema=K8sDescribeResourceInput)
def k8s_describe_resource(
    resource_type: str,
    resource_name: str,
    namespace: str = "default",
) -> str:
    """查看 Kubernetes 资源详情。支持 pod/deployment/service/node/ingress/configmap 等资源类型。
    返回资源的完整规格、状态和事件信息。"""
    start_time = time.time()

    # 资源类型到 API 路径的映射
    api_map = {
        "pod": f"/api/v1/namespaces/{namespace}/pods/{resource_name}",
        "service": f"/api/v1/namespaces/{namespace}/services/{resource_name}",
        "configmap": f"/api/v1/namespaces/{namespace}/configmaps/{resource_name}",
        "node": f"/api/v1/nodes/{resource_name}",
        "namespace": f"/api/v1/namespaces/{resource_name}",
        "deployment": f"/apis/apps/v1/namespaces/{namespace}/deployments/{resource_name}",
        "statefulset": f"/apis/apps/v1/namespaces/{namespace}/statefulsets/{resource_name}",
        "daemonset": f"/apis/apps/v1/namespaces/{namespace}/daemonsets/{resource_name}",
        "ingress": f"/apis/networking.k8s.io/v1/namespaces/{namespace}/ingresses/{resource_name}",
        "job": f"/apis/batch/v1/namespaces/{namespace}/jobs/{resource_name}",
        "cronjob": f"/apis/batch/v1/namespaces/{namespace}/cronjobs/{resource_name}",
    }

    rt = resource_type.lower()
    if rt not in api_map:
        return format_mcp_result("k8s_describe_resource", {
            "error": f"不支持的资源类型 '{resource_type}'，支持: {', '.join(api_map.keys())}",
        }, 0)

    data = mcp_request(
        url=_k8s_url(api_map[rt]),
        method="GET",
        headers=_k8s_headers(),
        timeout=settings.mcp_kubernetes_timeout,
    )

    elapsed = time.time() - start_time

    if isinstance(data, dict) and "metadata" in data:
        # 精简输出：只保留关键字段
        summary = {
            "kind": data.get("kind"),
            "name": data["metadata"].get("name"),
            "namespace": data["metadata"].get("namespace"),
            "labels": data["metadata"].get("labels"),
            "creation_timestamp": data["metadata"].get("creationTimestamp"),
            "spec_summary": _summarize_spec(data.get("spec", {})),
            "status_summary": _summarize_status(data.get("status", {})),
        }
        return format_mcp_result("k8s_describe_resource", summary, elapsed)

    return format_mcp_result("k8s_describe_resource", data, elapsed)


def _summarize_spec(spec: dict) -> dict:
    """精简资源 spec（避免输出过大）"""
    summary = {}
    for key in ["replicas", "selector", "type", "clusterIP", "ports", "nodeName", "containers"]:
        if key in spec:
            val = spec[key]
            if key == "containers" and isinstance(val, list):
                summary[key] = [
                    {"name": c.get("name"), "image": c.get("image")}
                    for c in val
                ]
            else:
                summary[key] = val
    # template 中的 containers
    template_spec = spec.get("template", {}).get("spec", {})
    if "containers" in template_spec and "containers" not in summary:
        summary["containers"] = [
            {"name": c.get("name"), "image": c.get("image"), "ports": c.get("ports")}
            for c in template_spec["containers"]
        ]
    return summary


def _summarize_status(status: dict) -> dict:
    """精简资源 status"""
    summary = {}
    for key in ["phase", "replicas", "readyReplicas", "availableReplicas",
                 "unavailableReplicas", "conditions", "podIP", "hostIP",
                 "loadBalancer", "containerStatuses"]:
        if key in status:
            val = status[key]
            if key == "conditions" and isinstance(val, list):
                summary[key] = [
                    {"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason")}
                    for c in val
                ]
            elif key == "containerStatuses" and isinstance(val, list):
                summary[key] = [
                    {"name": cs.get("name"), "ready": cs.get("ready"),
                     "restartCount": cs.get("restartCount"), "state": list(cs.get("state", {}).keys())}
                    for cs in val
                ]
            else:
                summary[key] = val
    return summary
