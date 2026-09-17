"""精确 JSON 数值解析：让请求体中的数字以 Decimal 而非 float64 进入校验。

为什么不能依赖 FastAPI/pydantic 的默认 JSON 解析：标准 ``json.loads``
会把 JSON 数值解析为二进制浮点（float64），像
``100.000000000000001``、``0.99999999999999999``、``500.00000000000001``
这样的临界值在进入 pydantic 的 Decimal 校验**之前**就已被归并为
``100``、``1``、``500``，导致小数位与 1.00–500.00 边界校验放过本应
整体拒绝的请求（批量接口同理被污染：任一测点被“洗白”都会污染批次
编排与响应映射）。

FastAPI 在路由处理函数内通过 ``fastapi.routing`` 模块的全局名
``Request`` 实例化请求对象，并调用其 ``json()`` 读取请求体。这里提供
一个只覆盖 ``json()`` 的子类：用 ``parse_float``/``parse_int`` 回调按
数字的原始文本直接构造 :class:`decimal.Decimal`，全程不经过 float；
其余流程（content-type 判定、空体/非法 JSON/模型校验的错误形态、
OpenAPI 请求体文档）全部保持 FastAPI 原生行为不变。

JSON 数字词法保证回调收到的文本必为合法的有限十进制数字；标准 JSON
不接受 NaN/Infinity 词法，这两个常量经 ``parse_constant`` 同样得到
Decimal，再由请求模型的有限性校验拒绝。
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import fastapi.routing
from starlette.requests import Request as StarletteRequest


def _exact_decimal(number_text: str) -> Decimal:
    """json 数字/常量回调：按原始文本构造精确 Decimal，绝不经过 float64。"""
    return Decimal(number_text)


def decode_json_exact(raw: bytes | str) -> Any:
    """按 JSON 文本精确解析：所有数值均为 Decimal，不经 float64。"""
    return json.loads(
        raw,
        parse_float=_exact_decimal,
        parse_int=_exact_decimal,
        parse_constant=_exact_decimal,
    )


class ExactDecimalRequest(StarletteRequest):
    """请求体 JSON 以十进制定点（Decimal）精确解析的 Request。"""

    async def json(self) -> Any:
        if not hasattr(self, "_json"):
            body = await self.body()
            self._json = decode_json_exact(body)
        return self._json


def install_exact_decimal_request_class() -> None:
    """让 FastAPI 路由使用 :class:`ExactDecimalRequest`。

    ``fastapi.routing.request_response`` 在**每次请求**时按模块全局名
    查找 ``Request``，因此替换模块属性即可生效，且不改变路由/端点
    签名与 OpenAPI 文档。须在应用开始接受请求前调用一次。
    """
    fastapi.routing.Request = ExactDecimalRequest
