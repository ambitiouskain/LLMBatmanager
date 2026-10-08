from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime

import httpx

from ..domain.models import ApiCheckResult, Backend, RuntimeState


class HttpxNetworkChecker:
    def __init__(
        self,
        client: httpx.Client | None = None,
        timeout: float = 2.0,
        max_response_bytes: int = 1_048_576,
        max_reported_models: int = 100,
    ) -> None:
        self.client = client or httpx.Client(timeout=timeout, trust_env=False)
        self.max_response_bytes = max(1024, max_response_bytes)
        self.max_reported_models = max(1, max_reported_models)

    def check(
        self, base_address: str, backend: Backend, api_key: str | None = None
    ) -> ApiCheckResult:
        base = base_address.rstrip("/")
        if backend != Backend.OLLAMA and base.endswith("/v1"):
            base = base[:-3]
        endpoints = ["/api/tags"] if backend == Backend.OLLAMA else ["/v1/models", "/health"]
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        last: ApiCheckResult | None = None
        for endpoint in endpoints:
            url = base + endpoint
            try:
                response = self.client.get(url, headers=headers)
            except httpx.TimeoutException as error:
                return ApiCheckResult(
                    ready=False,
                    state=RuntimeState.PORT_LISTENING_API_NOT_READY,
                    detail="API 请求超时",
                    checked_url=url,
                    error_kind="timeout",
                )
            except httpx.RequestError as error:
                return ApiCheckResult(
                    ready=False,
                    state=RuntimeState.PORT_LISTENING_API_NOT_READY,
                    detail=str(error) or "API 请求失败",
                    checked_url=url,
                    error_kind="request_error",
                )
            if response.status_code in {401, 403}:
                return ApiCheckResult(
                    ready=False,
                    state=RuntimeState.AUTH_REQUIRED,
                    status_code=response.status_code,
                    detail="API 已响应，但需要认证",
                    checked_url=url,
                    error_kind="authentication_required",
                )
            if response.status_code == 404:
                last = ApiCheckResult(
                    ready=False,
                    state=RuntimeState.PORT_LISTENING_API_NOT_READY,
                    status_code=404,
                    detail="端点不兼容",
                    checked_url=url,
                    error_kind="incompatible_endpoint",
                )
                continue
            if response.is_success:
                if endpoint == "/health":
                    return ApiCheckResult(
                        ready=True,
                        state=RuntimeState.API_READY,
                        status_code=response.status_code,
                        detail="兼容健康检查已就绪",
                        checked_url=url,
                    )
                if len(response.content) > self.max_response_bytes:
                    return ApiCheckResult(
                        ready=False,
                        state=RuntimeState.PORT_LISTENING_API_NOT_READY,
                        status_code=response.status_code,
                        detail="API 响应过大，已拒绝解析",
                        checked_url=url,
                        error_kind="response_too_large",
                    )
                models: list[str] = []
                try:
                    body = response.json()
                    key = "models" if backend == Backend.OLLAMA else "data"
                    if not isinstance(body, dict) or not isinstance(body.get(key), list):
                        raise ValueError("模型端点返回结构无效")
                    items = body[key]
                    models = [
                        str(item.get("name") or item.get("id"))
                        for item in items[:self.max_reported_models]
                        if isinstance(item, dict)
                        and (item.get("name") is not None or item.get("id") is not None)
                    ]
                except (ValueError, AttributeError, TypeError):
                    last = ApiCheckResult(
                        ready=False,
                        state=RuntimeState.PORT_LISTENING_API_NOT_READY,
                        status_code=response.status_code,
                        detail="模型端点返回了无效响应",
                        checked_url=url,
                        error_kind="incompatible_endpoint",
                    )
                    continue
                return ApiCheckResult(
                    ready=True,
                    state=RuntimeState.API_READY,
                    status_code=response.status_code,
                    detail=(
                        "API 已就绪"
                    ),
                    loaded_models=models,
                    checked_url=url,
                )
            return ApiCheckResult(
                ready=False,
                state=RuntimeState.PORT_LISTENING_API_NOT_READY,
                status_code=response.status_code,
                detail=f"API 返回 HTTP {response.status_code}",
                checked_url=url,
                error_kind="http_error",
            )
        return last or ApiCheckResult(
            ready=False,
            state=RuntimeState.PORT_LISTENING_API_NOT_READY,
            detail="没有兼容的 API 端点",
            error_kind="incompatible_endpoint",
        )


def poll_readiness(
    checker: object,
    address: str,
    backend: Backend,
    timeout: float,
    interval: float = 1.0,
    process_alive: Callable[[], bool] = lambda: True,
    cancelled: Callable[[], bool] = lambda: False,
    on_result: Callable[[ApiCheckResult], None] | None = None,
) -> ApiCheckResult:
    deadline = time.monotonic() + timeout
    retries = 0
    last = ApiCheckResult(
        ready=False,
        state=RuntimeState.PORT_LISTENING_API_NOT_READY,
        detail="尚未检查",
    )
    while time.monotonic() < deadline:
        if cancelled():
            return ApiCheckResult(
                ready=False, state=RuntimeState.STOPPED, detail="检查已取消",
                error_kind="cancelled", retry_count=retries,
            )
        if not process_alive():
            return ApiCheckResult(
                ready=False, state=RuntimeState.FAILED, detail="进程已退出",
                error_kind="process_exited", retry_count=retries,
            )
        retries += 1
        last = checker.check(address, backend)
        if cancelled():
            return ApiCheckResult(
                ready=False, state=RuntimeState.STOPPED, detail="检查已取消",
                error_kind="cancelled", retry_count=retries,
            )
        if not process_alive():
            exited = ApiCheckResult(
                ready=False, state=RuntimeState.FAILED, detail="进程已退出",
                error_kind="process_exited", retry_count=retries,
                checked_at=datetime.now(),
            )
            if on_result:
                on_result(exited)
            return exited
        last.retry_count = retries
        last.checked_at = datetime.now()
        if on_result:
            on_result(last)
        if last.ready or last.state == RuntimeState.AUTH_REQUIRED:
            return last
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
    last.detail = "API 就绪检查超时"
    last.error_kind = "startup_timeout"
    last.retry_count = retries
    last.checked_at = datetime.now()
    if on_result:
        on_result(last)
    return last
