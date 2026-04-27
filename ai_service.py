"""DeepSeek 批改服务。

按照项目规范：
- 绝不硬编码 API Key。
- 统一从 Streamlit Secrets 读取配置。
- 生产环境在 Streamlit Cloud 的 Secrets 中配置 ``DEEPSEEK_API_KEY``。
"""

from __future__ import annotations

import json
from typing import Any, Dict

import streamlit as st
from openai import OpenAI
from streamlit.errors import StreamlitSecretNotFoundError


SYSTEM_PROMPT: str = (
    "你是一位严谨的导师。请对比标准答案批改学生作业。"
    "考虑准确性、逻辑性。"
    '输出必须是严格 JSON，格式为 {"score": 0-100, "comment": "..."}。'
)

DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
DEEPSEEK_MODEL: str = "deepseek-chat"
DEEPSEEK_SECRET_NAME: str = "DEEPSEEK_API_KEY"


class MissingDeepSeekAPIKeyError(RuntimeError):
    """未在 Streamlit Secrets 中找到 DeepSeek API Key。"""


def _read_deepseek_api_key() -> str:
    """仅从 Streamlit Secrets 读取 DeepSeek API Key。"""
    try:
        secret_value = st.secrets[DEEPSEEK_SECRET_NAME]
    except (StreamlitSecretNotFoundError, KeyError, FileNotFoundError):
        secret_value = ""

    return str(secret_value).strip()


def _build_client() -> OpenAI:
    """构建 DeepSeek 客户端；缺少密钥时抛出可识别的中文异常。"""
    api_key = _read_deepseek_api_key()
    if not api_key:
        raise MissingDeepSeekAPIKeyError(
            "未检测到 DeepSeek API Key。请在 Streamlit Cloud 的 "
            "Settings → Secrets 中添加 DEEPSEEK_API_KEY，或在本地 "
            ".streamlit/secrets.toml 中配置该值（该文件已加入 .gitignore，"
            "不会被上传到 GitHub）。"
        )
    return OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)


def grade_answer(standard_answer: str, student_answer: str) -> Dict[str, Any]:
    """调用 DeepSeek 接口返回标准化评分结果。

    Returns:
        ``{"score": 0-100, "comment": "..."}``。

    Raises:
        MissingDeepSeekAPIKeyError: 未配置 ``DEEPSEEK_API_KEY``。
        Exception: 网络或上游异常（由调用方负责展示给用户）。
    """
    client = _build_client()

    response = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        temperature=0.2,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "【标准答案】\n"
                    f"{standard_answer}\n\n"
                    "【学生答案】\n"
                    f"{student_answer}\n\n"
                    "请严格输出 JSON，不要输出额外文本。"
                ),
            },
        ],
    )

    content = response.choices[0].message.content or "{}"
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        payload = {}

    score = max(0, min(100, int(payload.get("score", 0))))
    comment = str(payload.get("comment", "")).strip() or "AI 未返回评语。"
    return {"score": score, "comment": comment}
