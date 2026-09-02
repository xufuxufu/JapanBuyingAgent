from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from dotenv import dotenv_values, load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_DIR = DATA_DIR / "db"
ORIGINAL_DIR = DATA_DIR / "uploads" / "original"
PREVIEW_DIR = DATA_DIR / "uploads" / "preview"
PRODUCT_IMAGE_DIR = DATA_DIR / "products" / "main"
QINSI_PRODUCT_IMAGE_DIR = DATA_DIR / "products" / "qinsi-localized"
REPORT_DIR = DATA_DIR / "reports"
TAG_EVIDENCE_DIR = DATA_DIR / "field-purchases" / "tag-evidence"
SALES_ORDER_SHIPPING_LABEL_DIR = DATA_DIR / "sales-orders" / "shipping-labels"
SALES_ORDER_ITEM_IMAGE_DIR = DATA_DIR / "sales-orders" / "item-images"
PROCUREMENT_DEMAND_IMAGE_DIR = DATA_DIR / "procurement-demands" / "item-images"
IMAGE_SEARCH_DIR = DATA_DIR / "image-search"
IMAGE_SEARCH_MODEL_DIR = IMAGE_SEARCH_DIR / "models"
DEFAULT_DB_PATH = DB_DIR / "japan_buying_agent.sqlite3"
DEFAULT_RAKUTEN_HTTP_REFERER = "https://xufu-cp.taile96adb.ts.net:8020/"
DEFAULT_RAKUTEN_ALLOWED_PUBLIC_IP = "14.10.7.65"


def is_testing() -> bool:
    return os.getenv("JBA_TESTING", "").strip().casefold() in {"1", "true", "yes", "on"} or bool(
        os.getenv("PYTEST_CURRENT_TEST")
    )


if not is_testing():
    load_dotenv(PROJECT_ROOT / ".env", override=False)


def clean_env_value(value: str | None) -> str:
    """Trim env values and remove one accidentally-added matching quote pair."""
    cleaned = (value or "").strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        cleaned = cleaned[1:-1].strip()
    return cleaned


def rakuten_http_referer() -> str:
    value = clean_env_value(os.getenv("JBA_RAKUTEN_HTTP_REFERER")) or DEFAULT_RAKUTEN_HTTP_REFERER
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("JBA_RAKUTEN_HTTP_REFERER must be a complete http or https URL")
    path = parsed.path.rstrip("/") + "/" if parsed.path else "/"
    return urlunparse(parsed._replace(path=path))


def rakuten_allowed_public_ip() -> str:
    return clean_env_value(os.getenv("JBA_RAKUTEN_ALLOWED_PUBLIC_IP")) or DEFAULT_RAKUTEN_ALLOWED_PUBLIC_IP


def first_env_value(*names: str, default: str = "") -> tuple[str, str | None]:
    for name in names:
        value = clean_env_value(os.getenv(name))
        if value:
            return value, name
    return clean_env_value(default), None


def env_value_source(*names: str) -> tuple[str, str | None, str | None]:
    value, name = first_env_value(*names)
    if name is None:
        return value, None, None
    file_values = dotenv_values(PROJECT_ROOT / ".env") if (PROJECT_ROOT / ".env").exists() else {}
    file_value = clean_env_value(file_values.get(name))
    if file_value and file_value == value:
        origin = f"{PROJECT_ROOT / '.env'}:{name}"
    elif file_value:
        origin = f"process environment:{name} (project root .env also defines it; process value takes precedence)"
    else:
        origin = f"process environment:{name}"
    return value, name, origin


@dataclass(frozen=True, slots=True)
class DeepSeekConfig:
    api_key: str
    api_key_variable: str | None
    api_key_source: str | None
    base_url: str
    model: str

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


def get_deepseek_config() -> DeepSeekConfig:
    key, variable, source = env_value_source("JBA_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY")
    base_url = first_env_value(
        "JBA_DEEPSEEK_BASE_URL", "DEEPSEEK_BASE_URL", default="https://api.deepseek.com/v1",
    )[0].rstrip("/") or "https://api.deepseek.com/v1"
    model = first_env_value(
        "JBA_DEEPSEEK_MODEL", "DEEPSEEK_MODEL", default="deepseek-chat",
    )[0] or "deepseek-chat"
    return DeepSeekConfig(
        api_key=key,
        api_key_variable=variable,
        api_key_source=source,
        base_url=base_url,
        model=model,
    )


def is_deepseek_configured() -> bool:
    return get_deepseek_config().configured


def database_url() -> str:
    configured = clean_env_value(os.getenv("JBA_DATABASE_URL"))
    return configured or f"sqlite:///{DEFAULT_DB_PATH.as_posix()}"


def ensure_data_directories() -> None:
    for path in (
        DB_DIR, ORIGINAL_DIR, PREVIEW_DIR, PRODUCT_IMAGE_DIR,
        QINSI_PRODUCT_IMAGE_DIR, REPORT_DIR, TAG_EVIDENCE_DIR, SALES_ORDER_SHIPPING_LABEL_DIR,
        SALES_ORDER_ITEM_IMAGE_DIR, PROCUREMENT_DEMAND_IMAGE_DIR,
        IMAGE_SEARCH_DIR, IMAGE_SEARCH_MODEL_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on", "enabled"}


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return min(maximum, max(minimum, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class ProviderCredentialSettings:
    code: str
    display_name: str
    required_values: tuple[str, ...]
    optional_values: tuple[str, ...] = ()
    implementation_status: str = "available"

    @property
    def configured(self) -> bool:
        return all(value.strip() for value in self.required_values)


def provider_credential_settings() -> tuple[ProviderCredentialSettings, ...]:
    """Read credentials server-side. Callers must never serialize required_values."""
    return (
        ProviderCredentialSettings("local_qinsi", "Local/Qinsi", (), implementation_status="available"),
        ProviderCredentialSettings(
            "rakuten",
            "Rakuten",
            (
                clean_env_value(os.getenv("JBA_RAKUTEN_APPLICATION_ID")),
                clean_env_value(os.getenv("JBA_RAKUTEN_ACCESS_KEY")),
            ),
            (clean_env_value(os.getenv("JBA_RAKUTEN_AFFILIATE_ID")),),
        ),
        ProviderCredentialSettings(
            "yahoo_shopping",
            "Yahoo Shopping",
            (clean_env_value(os.getenv("JBA_YAHOO_CLIENT_ID")),),
        ),
        ProviderCredentialSettings(
            "amazon_creators",
            "Amazon Creators API",
            (
                os.getenv("JBA_AMAZON_CREATORS_PUBLIC_KEY", ""),
                os.getenv("JBA_AMAZON_CREATORS_PRIVATE_KEY", ""),
                os.getenv("JBA_AMAZON_JP_PARTNER_TAG", ""),
                os.getenv("JBA_AMAZON_JP_MARKETPLACE", ""),
            ),
            implementation_status="placeholder",
        ),
        ProviderCredentialSettings(
            "deepseek",
            "DeepSeek",
            (get_deepseek_config().api_key,),
            (
                get_deepseek_config().base_url,
                get_deepseek_config().model,
            ),
            implementation_status="available",
        ),
        ProviderCredentialSettings("manual", "Manual/Fallback", (), implementation_status="available"),
    )


def default_role() -> str:
    role = os.getenv("JBA_DEFAULT_ROLE", "admin").strip().casefold()
    return role if role in {"admin", "buyer", "reviewer", "viewer"} else "viewer"
