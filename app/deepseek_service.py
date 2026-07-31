from __future__ import annotations

import re
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import DeepSeekConfig, get_deepseek_config
from app.product_identity import normalize_product_name_whitespace


DEEPSEEK_TRANSLATION_PROMPT = """你是一名日本药妆/日用品w玩偶等跨境电商商品标题翻译专家。请按以下规则将日文商品名翻译成中文：

1. 品牌名使用官方通用中文译名（如「ラックス」→「力士」）。
   如果品牌没有稳定官方中文译名，保留英文或日文品牌名，不要生造生僻汉字音译。
2. 角色/联名名使用官方中文译名（如「クロミ」→「酷洛米」）。
3. 功效描述意译为主，不逐字硬翻，符合中文美妆日化表达习惯。
4. 必须明确写出商品品类（如洗发露、护发素、沐浴露、洗面奶、面膜、套装等），方便搜索命中。
5. 结构统一为：「品牌×角色（如有） 功效 品类1＋品类2 套装（数量）」，语序自然通顺。
6. 括号精简，不堆砌日文原词，除非必要。
7. 输出格式：只输出「中文翻译结果｜日文原商品名」，中间用英文竖线 | 分隔，竖线两侧不加空格。中文必须放在竖线前面。
8. 整条输出（含中文、竖线、日文）总长度不超过 128 个字符，中文翻译部分在保证品类明确的前提下尽量精简。

输出示例：

输入：

(企画品)ラックス スーパーリッチシャイン ストレートビューティー SP＆CD クロミコラボ ( 1セット )/ ラックス(LUX)

输出：

力士×酷洛米超润光泽直发顺滑洗发露＋护发素套装1套|(企画品)ラックス スーパーリッチシャイン ストレートビューティー SP＆CD クロミコラボ ( 1セット )/ ラックス(LUX)

注意：

* 最终分隔符实际使用英文半角竖线|
* 总长度后端必须再次校验，不只依赖模型
* DeepSeek失败不得阻止商品保存
* 保留原始日文名name_ja
* 中文部分单独保存name_cn
* 展示名由name_cn + "|" + name_ja生成"""


@dataclass(frozen=True, slots=True)
class DeepSeekTranslation:
    name_cn: str
    name_ja: str
    raw_content: str


class DeepSeekServiceError(RuntimeError):
    def __init__(self, category: str, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.http_status = http_status


GIBBERISH_TRANSLATION_MARKERS = set("笢恅靡渾硃髪斌瞳針赤匿廾圻龍院塞滅")


def get_deepseek_client(timeout_seconds: float = 10.0) -> httpx.Client:
    return httpx.Client(timeout=timeout_seconds)


def _truncate_pipe_display(name_cn: str, name_ja: str, limit: int = 128) -> tuple[str, str]:
    name_cn = normalize_product_name_whitespace(name_cn) or ""
    name_ja = normalize_product_name_whitespace(name_ja) or ""
    raw = f"{name_cn}|{name_ja}"
    if len(raw) <= limit:
        return name_cn, name_ja
    ja_budget = min(len(name_ja), max(0, limit // 2))
    cn_budget = limit - 1 - ja_budget
    if cn_budget < 1:
        cn_budget = 1
        ja_budget = limit - 2
    return name_cn[:cn_budget].rstrip(), name_ja[:ja_budget].rstrip()


def parse_deepseek_translation(content: str, original_name_ja: str) -> DeepSeekTranslation:
    value = re.sub(r"^```(?:text)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
    if "|" not in value:
        raise DeepSeekServiceError("invalid_output", "DeepSeek输出缺少分隔符")
    name_cn, returned_ja = (normalize_product_name_whitespace(part) or "" for part in value.split("|", 1))
    if not name_cn:
        raise DeepSeekServiceError("invalid_output", "DeepSeek输出缺少中文名")
    if sum(1 for char in name_cn if char in GIBBERISH_TRANSLATION_MARKERS) >= 2:
        raise DeepSeekServiceError("invalid_output", "DeepSeek输出疑似历史乱码")
    name_ja = normalize_product_name_whitespace(original_name_ja) or returned_ja
    name_cn, name_ja = _truncate_pipe_display(name_cn, name_ja)
    return DeepSeekTranslation(name_cn=name_cn, name_ja=name_ja, raw_content=value)


def _error_from_response(response: httpx.Response) -> DeepSeekServiceError:
    message = str(getattr(response, "text", ""))[:500]
    try:
        body = response.json()
    except ValueError:
        body = {}
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        error = body["error"]
        message = str(error.get("message") or message)[:500]
    lowered = message.casefold()
    if response.status_code in {401, 403}:
        return DeepSeekServiceError("unauthorized", f"DeepSeek未授权：{message[:300]}", http_status=response.status_code)
    if response.status_code == 402 or any(token in lowered for token in ("balance", "insufficient")):
        return DeepSeekServiceError("insufficient_balance", f"DeepSeek余额不足：{message[:300]}", http_status=response.status_code)
    if response.status_code == 429:
        return DeepSeekServiceError("rate_limited", "DeepSeek限流", http_status=response.status_code)
    return DeepSeekServiceError("http_error", f"DeepSeek HTTP {response.status_code}", http_status=response.status_code)


def translate_name_with_deepseek(
    name_ja: str,
    *,
    client: Any | None = None,
    config: DeepSeekConfig | None = None,
    timeout_seconds: float = 10.0,
) -> DeepSeekTranslation:
    config = config or get_deepseek_config()
    source_name = (name_ja or "").strip()
    if not source_name:
        raise DeepSeekServiceError("missing_source_name", "缺少可翻译日文名")
    if not config.configured:
        raise DeepSeekServiceError("unconfigured", "DeepSeek未配置")
    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": DEEPSEEK_TRANSLATION_PROMPT},
            {"role": "user", "content": f"输入：\n\n{source_name}"},
        ],
        "temperature": 0.1,
    }
    headers = {"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}
    owns_client = client is None
    context = get_deepseek_client(timeout_seconds) if owns_client else nullcontext(client)
    try:
        with context as http:
            response = http.post(
                f"{config.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout_seconds,
            )
        if response.status_code >= 400:
            raise _error_from_response(response)
        content = response.json()["choices"][0]["message"]["content"]
        return parse_deepseek_translation(str(content), source_name)
    except DeepSeekServiceError:
        raise
    except httpx.TimeoutException as exc:
        raise DeepSeekServiceError("timeout", "DeepSeek请求超时") from exc
    except httpx.HTTPError as exc:
        raise DeepSeekServiceError("network_error", f"DeepSeek网络错误：{type(exc).__name__}") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise DeepSeekServiceError("invalid_response", f"DeepSeek响应解析失败：{type(exc).__name__}") from exc
