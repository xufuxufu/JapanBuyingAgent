from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import first_env_value, get_deepseek_config, provider_credential_settings
from app.deepseek_service import DeepSeekServiceError, translate_name_with_deepseek
from app.models import PlatformProviderState
from app.local_product import is_valid_jan
from app.models import Product
from app.price_providers import (
    AmazonCreatorsPriceProvider,
    LocalQinsiPriceProvider,
    ManualFallbackPriceProvider,
    PriceProvider,
    ProviderResponse,
    RakutenPriceProvider,
    YahooShoppingPriceProvider,
)


FALLBACK_TEST_JAN = "4901234567894"


@dataclass(frozen=True, slots=True)
class ProviderStatusView:
    code: str
    display_name: str
    configured: bool
    implementation_status: str
    credentials_valid: bool | None
    last_success_at: datetime | None
    last_tested_at: datetime | None
    last_test_status: str | None
    last_http_status: int | None
    last_result_count: int
    recent_error: str | None
    request_count: int
    credential_hint: str | None = None
    item_search_endpoint: str | None = None
    item_search_api_version: str | None = None
    application_id_present: bool | None = None
    application_id_length: int | None = None
    access_key_present: bool | None = None
    access_key_length: int | None = None
    credential_variable: str | None = None
    credential_source: str | None = None
    configured_model: str | None = None


class DeepSeekDiagnosticProvider(PriceProvider):
    code = "deepseek"
    display_name = "DeepSeek"
    base_url = "https://api.deepseek.com"

    def is_configured(self) -> bool:
        return get_deepseek_config().configured

    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        config = get_deepseek_config()
        tested_at = datetime.now(timezone.utc).isoformat()
        diagnostics: dict[str, object] = {"deepseek_self_check": {
            "configured": config.configured, "variable": config.api_key_variable, "source": config.api_key_source,
            "model": config.model, "tested_at": tested_at, "http_status": None,
            "error_code": None, "error_message": None, "output": None,
        }}
        check = diagnostics["deepseek_self_check"]
        if not config.configured:
            return ProviderResponse(
                "unconfigured", message="DeepSeek未配置", error_code="UNCONFIGURED", diagnostics=diagnostics,
            )
        try:
            result = translate_name_with_deepseek("テスト商品", config=config, timeout_seconds=timeout_seconds)
            output = f"{result.name_cn}|{result.name_ja}"
            check["http_status"] = 200
            check["output"] = output[:500]
            return ProviderResponse(
                "success", message=f"DeepSeek API验证成功：{output[:200]}",
                http_status=200, diagnostics=diagnostics,
            )
        except DeepSeekServiceError as exc:
            check["http_status"] = exc.http_status
            check["error_code"] = exc.category.upper()
            check["error_message"] = exc.message[:500]
            if exc.category == "unauthorized":
                status, error_code = "unauthorized", "UNAUTHORIZED"
            elif exc.category == "insufficient_balance":
                status, error_code = "insufficient_balance", "INSUFFICIENT_BALANCE"
            elif exc.category == "rate_limited":
                status, error_code = "rate_limited", "RATE_LIMITED"
            elif exc.category == "timeout":
                status, error_code = "timeout", "TIMEOUT"
            elif exc.category == "network_error":
                status, error_code = "network_error", "NETWORK_ERROR"
            elif exc.category == "invalid_output":
                status, error_code = "error", "INVALID_OUTPUT"
            else:
                status, error_code = "error", exc.category.upper()
            return ProviderResponse(
                status, message=exc.message, error_code=error_code,
                http_status=exc.http_status, diagnostics=diagnostics,
            )


def _credential_hint(code: str, values: tuple[str, ...]) -> str | None:
    if code == "rakuten" and len(values) >= 2 and values[0].strip() and values[1].strip():
        return f"env末4：applicationId **{values[0].strip()[-4:]} / accessKey **{values[1].strip()[-4:]}"
    if values and all(value.strip() for value in values):
        return "env已读取（不显示完整值）"
    return None


def diagnostic_summary(response: ProviderResponse) -> str | None:
    diagnostics = response.diagnostics or {}
    parts: list[str] = []
    self_check = diagnostics.get("rakuten_item_self_check")
    if isinstance(self_check, dict):
        parts.append(
            "Item Search自检 "
            f"appId={self_check.get('applicationIdPresent')}/{self_check.get('applicationIdLength')} "
            f"accessKey={self_check.get('accessKeyPresent')}/{self_check.get('accessKeyLength')} "
            f"endpoint={self_check.get('endpoint')} "
            f"version={self_check.get('apiVersion')} "
            f"HTTP {self_check.get('http_status', '—')} "
            f"error={self_check.get('error') or '—'} "
            f"error_description={self_check.get('error_description') or '—'}"
        )
    product = diagnostics.get("product_search")
    if isinstance(product, dict):
        parts.append(f"Product Search HTTP {product.get('http_status', '—')} / {product.get('result_count', 0)}条")
    item = diagnostics.get("item_search")
    if isinstance(item, dict):
        parts.append(f"Item Search HTTP {item.get('http_status', '—')} / {item.get('result_count', 0)}条")
    auth_tests = diagnostics.get("rakuten_auth_tests")
    if isinstance(auth_tests, dict):
        for key in ("item_query", "item_header", "genre_query"):
            result = auth_tests.get(key)
            if not isinstance(result, dict):
                continue
            ip_warning = result.get("rakuten_public_ip_warning")
            if ip_warning:
                parts.append(str(ip_warning))
            ip_status = result.get("rakuten_public_ip_status")
            if isinstance(ip_status, dict):
                parts.append(
                    f"Rakuten出口IP status={ip_status.get('status')} "
                    f"configured={ip_status.get('configured_ip') or '—'} "
                    f"current={ip_status.get('current_public_ip') or '—'}"
                )
            parts.append(
                f"{result.get('label') or key}: HTTP {result.get('http_status', '—')} / "
                f"final_url={result.get('final_url') or '—'} / "
                f"redirects={result.get('redirect_count', '—')} / "
                f"Referer={result.get('request_has_referer')} / "
                f"Content-Type={result.get('content_type') or '—'} / "
                f"body前1000={result.get('body_preview') or '—'} / "
                f"errorCode={result.get('errorCode') or result.get('error') or '—'} / "
                f"errorMessage={result.get('errorMessage') or result.get('error_description') or '—'} / "
                f"异常类型={result.get('exception_type') or '—'}"
            )
    judgment = diagnostics.get("rakuten_final_judgment")
    if judgment:
        parts.append(f"最终判断={judgment}")
    deepseek = diagnostics.get("deepseek_self_check")
    if isinstance(deepseek, dict):
        parts.append(
            f"DeepSeek configured={deepseek.get('configured')} variable={deepseek.get('variable') or '—'} "
            f"source={deepseek.get('source') or '—'} model={deepseek.get('model') or '—'} "
            f"tested_at={deepseek.get('tested_at') or '—'} HTTP={deepseek.get('http_status', '—')} "
            f"error_code={deepseek.get('error_code') or '—'} "
            f"error_message={deepseek.get('error_message') or '—'} output={deepseek.get('output') or '—'}"
        )
    cooldown = diagnostics.get("cooldown_decision")
    if isinstance(cooldown, dict):
        parts.append(f"cooldown {cooldown.get('cooldown_seconds', '—')}秒，不立即重试")
    credential = diagnostics.get("credential_source")
    if credential:
        parts.append(str(credential))
    return "；".join(parts) or None


def _provider_for_code(session: Session, code: str) -> PriceProvider:
    providers: dict[str, PriceProvider] = {
        "local_qinsi": LocalQinsiPriceProvider(session),
        "rakuten": RakutenPriceProvider(),
        "deepseek": DeepSeekDiagnosticProvider(),
        "yahoo_shopping": YahooShoppingPriceProvider(),
        "amazon_creators": AmazonCreatorsPriceProvider(),
        "manual": ManualFallbackPriceProvider(),
    }
    if code not in providers:
        raise LookupError("Provider 不存在")
    return providers[code]


def provider_status_rows(session: Session) -> list[ProviderStatusView]:
    views: list[ProviderStatusView] = []
    for settings in provider_credential_settings():
        state = session.scalar(
            select(PlatformProviderState).where(
                PlatformProviderState.provider_code == settings.code
            )
        )
        if state is None:
            state = PlatformProviderState(
                provider_code=settings.code,
                configured=settings.configured,
            )
            session.add(state)
            session.flush()
        else:
            state.configured = settings.configured
        rakuten_app_id = first_env_value("JBA_RAKUTEN_APPLICATION_ID")[0] if settings.code == "rakuten" else ""
        rakuten_access_key = first_env_value("JBA_RAKUTEN_ACCESS_KEY")[0] if settings.code == "rakuten" else ""
        deepseek_config = get_deepseek_config() if settings.code == "deepseek" else None
        views.append(
            ProviderStatusView(
                code=settings.code,
                display_name=settings.display_name,
                configured=settings.configured,
                implementation_status=settings.implementation_status,
                credentials_valid=state.credentials_valid,
                last_success_at=state.last_success_at,
                last_tested_at=state.last_tested_at,
                last_test_status=state.last_test_status,
                last_http_status=state.last_http_status,
                last_result_count=state.last_result_count,
                recent_error=state.recent_error,
                request_count=state.request_count,
                credential_hint=_credential_hint(settings.code, settings.required_values),
                item_search_endpoint=RakutenPriceProvider.item_endpoint if settings.code == "rakuten" else None,
                item_search_api_version=RakutenPriceProvider.item_api_version if settings.code == "rakuten" else None,
                application_id_present=bool(rakuten_app_id) if settings.code == "rakuten" else None,
                application_id_length=len(rakuten_app_id) if settings.code == "rakuten" else None,
                access_key_present=bool(rakuten_access_key) if settings.code == "rakuten" else None,
                access_key_length=len(rakuten_access_key) if settings.code == "rakuten" else None,
                credential_variable=deepseek_config.api_key_variable if deepseek_config else None,
                credential_source=deepseek_config.api_key_source if deepseek_config else None,
                configured_model=deepseek_config.model if deepseek_config else None,
            )
        )
    session.commit()
    return views


def provider_test_jan(session: Session) -> str:
    candidates = session.scalars(
        select(Product.jan)
        .where(Product.jan.is_not(None))
        .order_by(Product.updated_at.desc(), Product.id.desc())
        .limit(200)
    )
    return next((value for value in candidates if is_valid_jan(value)), FALLBACK_TEST_JAN)


def test_provider_connection(
    session: Session,
    code: str,
    *,
    timeout_seconds: float = 4.0,
    provider: PriceProvider | None = None,
) -> ProviderResponse:
    selected = provider or _provider_for_code(session, code)
    now = datetime.now(timezone.utc)
    test_jan = provider_test_jan(session)
    try:
        response = (
            selected.diagnose_credentials(test_jan, timeout_seconds)
            if provider is None and isinstance(selected, RakutenPriceProvider)
            else selected.search(test_jan, timeout_seconds)
        )
    except (httpx.TimeoutException, TimeoutError):
        response = ProviderResponse(
            "timeout",
            message=f"{selected.display_name} 测试连接超时",
            error_code="TIMEOUT",
        )
    except httpx.HTTPStatusError as exc:
        http_status = exc.response.status_code
        if http_status in {401, 403}:
            error_code = "AUTH_FAILED"
            message = f"{selected.display_name} 认证失败"
        elif http_status == 429:
            error_code = "RATE_LIMITED"
            message = f"{selected.display_name} 触发限流"
        else:
            error_code = f"HTTP_{http_status}"
            message = f"{selected.display_name} HTTP 请求失败"
        response = ProviderResponse(
            "error",
            message=message,
            error_code=error_code,
            http_status=http_status,
        )
    except Exception as exc:
        response = ProviderResponse(
            "error",
            message=f"{selected.display_name} 测试连接失败：{type(exc).__name__}",
            error_code=type(exc).__name__.upper(),
        )
    state = session.scalar(
        select(PlatformProviderState).where(
            PlatformProviderState.provider_code == selected.code
        )
    )
    if state is None:
        state = PlatformProviderState(provider_code=selected.code)
        session.add(state)
    state.configured = selected.is_configured()
    state.request_count = (state.request_count or 0) + 1
    state.last_tested_at = now
    state.last_test_status = response.status
    state.last_http_status = response.http_status
    state.last_result_count = len(response.offers)
    diagnostics = diagnostic_summary(response)
    if response.status in {"success", "empty"}:
        state.credentials_valid = True
        state.last_success_at = now
        state.recent_error = diagnostics or (None if response.status == "success" else "NOT_FOUND")
    elif response.status == "unconfigured":
        state.credentials_valid = None
        state.recent_error = "UNCONFIGURED"
    else:
        if response.error_code in {"AUTH_FAILED", "UNAUTHORIZED"}:
            state.credentials_valid = False
        state.recent_error = diagnostics or response.error_code or response.status.upper()
    session.commit()
    return response
