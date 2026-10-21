"""用户贡献的华为产品 2JJ9（智能梦幻帘 Q1）适配器。

产品：智能梦幻帘
厂商：乐屋
型号：Q1（deviceTypeId 2BE，梦幻帘，Wi-Fi）
Profile：https://smarthome-drcn.dbankcdn.com/device/guide/2JJ9/2JJ9.json

Profile 服务与实体映射
---------------------
  opener.current        int  R   0~100 %  当前开合度 -> cover 的 current_cover_position
  opener.target         int  RW  0~100 %  目标位置   -> cover 的 set_position
                        该字段的 Profile 原文是“目标位置：关闭了百分之多少”，
                        即 100 表示全关、0 表示全开，与 Home Assistant 的
                        0=关 / 100=开 相反，因此 _POSITION_REVERSED 取 True。
  action.action         enum RW  0=关闭窗帘 1=打开窗帘 2=暂停
                        -> cover 的 close / open / stop
                        这一组枚举的语义与数值方向无关，不依赖上面的取反开关。
  rotationAngle.current int  R   0~100 %  当前叶片角度
  rotationAngle.target  int  RW  0~100 %  目标叶片角度 -> 叶片角度（number）

未映射的服务及原因
-----------------
  rotationAction.action    叶片左转/右转/暂停（0=左 1=右 2=暂停）。一次性动作，
                           没有状态回报，写入后无法确认是否生效，按失败关闭
                           原则不暴露；叶片角度本身已用可回读的 target 暴露。
  changeDirection.action   枚举只有一个取值（1=启用），没有控制意义。
  netInfo / update         网络信息与 OTA 运维字段，无日常使用价值，不映射。

验证状态
-------
  已在真机（Home Assistant 2026.10.1 / 集成 2026.10.16）实测：开合度与叶片角度回读、开、关、暂停、指定
  开合度与叶片角度写入均与设备实际一致；开合方向取反（_POSITION_REVERSED
  = True）经真机确认无误。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .api import EntitySpec
from .context import DeviceContext

# 由 opener.target 的 Profile 文字推得：100=全关、0=全开，与 HA 相反。
_POSITION_REVERSED = True

_POS_MIN = 0
_POS_MAX = 100

_ACTION_CLOSE = 0  # 关闭窗帘
_ACTION_OPEN = 1  # 打开窗帘
_ACTION_STOP = 2  # 暂停


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(round(number))


def _field(
    profile: Mapping[str, Any] | None,
    sid: str,
    name: str,
) -> Mapping[str, Any] | None:
    if not isinstance(profile, Mapping):
        return None
    for service in profile.get("services", ()):
        if not isinstance(service, Mapping) or service.get("serviceId") != sid:
            continue
        for field in service.get("characteristics", ()):
            if (
                isinstance(field, Mapping)
                and field.get("characteristicName") == name
            ):
                return field
        return None
    return None


def _is_writable(field: Mapping[str, Any] | None) -> bool:
    if not isinstance(field, Mapping):
        return False
    return "W" in str(field.get("method") or "")


def _coerce(value: Any, field: Mapping[str, Any] | None) -> Any:
    """按 Profile 声明的特征类型把命令值数值化。

    Profile 的 enumList 里 enumVal 一律是字符串，真机只接受数值。
    """

    if not isinstance(field, Mapping):
        return value
    data_type = str(field.get("characteristicType") or "").casefold()
    if data_type in {"bool", "int", "integer", "enum"}:
        number = _number(value)
        if number is None:
            return value
        return int(number) if number.is_integer() else number
    return value


def _enum_value(
    field: Mapping[str, Any] | None,
    wanted: Any,
    fallback: Any,
) -> Any:
    raw: Any = fallback
    if isinstance(field, Mapping):
        for option in field.get("enumList", ()):
            if not isinstance(option, Mapping):
                continue
            candidate = option.get("enumVal")
            if candidate is None:
                continue
            if str(candidate) == str(wanted):
                raw = candidate
                break
    return _coerce(raw, field)


def _clamp(value: Any, field: Mapping[str, Any] | None) -> Any:
    """把写入值夹到 Profile 范围内并对齐步长。"""

    number = _number(value)
    if number is None:
        return None
    minimum = _number((field or {}).get("min"))
    maximum = _number((field or {}).get("max"))
    low = minimum if minimum is not None else float(_POS_MIN)
    high = maximum if maximum is not None else float(_POS_MAX)
    if high < low:
        low, high = high, low
    number = min(max(number, low), high)
    step = _number((field or {}).get("step"))
    if step and step > 0:
        number = low + round((number - low) / step) * step
    return _coerce(min(max(number, low), high), field)


def _to_ha_position(value: Any) -> int | None:
    position = _int(value)
    if position is None:
        return None
    position = min(max(position, _POS_MIN), _POS_MAX)
    return _POS_MAX - position if _POSITION_REVERSED else position


def _to_device_position(position: int) -> int:
    return _POS_MAX - position if _POSITION_REVERSED else position


class Product2JJ9Adapter:
    """把 2JJ9 的实体与控制命令全部约束在本文件内。"""

    prod_id = "2JJ9"

    def entities(self, context: DeviceContext) -> tuple[EntitySpec, ...]:
        profile = context.profile
        if profile is None or not context.has_service("opener"):
            return ()

        entities: list[EntitySpec] = []
        action_field = _field(profile, "action", "action")
        target_field = _field(profile, "opener", "target")

        def cover_state(device: DeviceContext) -> Mapping[str, Any]:
            position = _to_ha_position(device.value("opener", "current"))
            return {
                "current_position": position,
                "is_closed": None if position is None else position == 0,
            }

        async def _send_action(device: DeviceContext, value: int) -> None:
            await device.async_send_service(
                "action",
                {"action": _enum_value(action_field, value, value)},
            )

        async def open_cover(
            device: DeviceContext, _data: Mapping[str, Any]
        ) -> None:
            await _send_action(device, _ACTION_OPEN)

        async def close_cover(
            device: DeviceContext, _data: Mapping[str, Any]
        ) -> None:
            await _send_action(device, _ACTION_CLOSE)

        async def stop_cover(
            device: DeviceContext, _data: Mapping[str, Any]
        ) -> None:
            await _send_action(device, _ACTION_STOP)

        async def set_position(
            device: DeviceContext, data: Mapping[str, Any]
        ) -> None:
            position = _int(data.get("position"))
            if position is None:
                raise ValueError("position is required")
            position = min(max(position, _POS_MIN), _POS_MAX)
            await device.async_send_service(
                "opener",
                {"target": _clamp(_to_device_position(position), target_field)},
            )

        actions: dict[str, Any] = {"set_position": set_position}
        if context.has_service("action") and _is_writable(action_field):
            actions["open"] = open_cover
            actions["close"] = close_cover
            actions["stop"] = stop_cover

        entities.append(
            EntitySpec(
                platform="cover",
                key="curtain",
                name=None,
                state=cover_state,
                metadata={"device_class": "curtain"},
                actions=actions,
            )
        )

        # --- 叶片角度 -------------------------------------------------
        angle_field = _field(profile, "rotationAngle", "target")
        if context.has_service("rotationAngle") and _is_writable(angle_field):
            angle_min = _number((angle_field or {}).get("min"))
            angle_max = _number((angle_field or {}).get("max"))
            angle_step = _number((angle_field or {}).get("step"))

            def angle_state(device: DeviceContext) -> Mapping[str, Any]:
                return {
                    "native_value": _int(
                        device.value("rotationAngle", "current")
                    )
                }

            async def set_angle(
                device: DeviceContext, data: Mapping[str, Any]
            ) -> None:
                value = _clamp(data.get("value"), angle_field)
                if value is None:
                    raise ValueError("angle is required")
                await device.async_send_service(
                    "rotationAngle",
                    {"target": value},
                )

            entities.append(
                EntitySpec(
                    platform="number",
                    key="rotation_angle",
                    name="叶片角度",
                    state=angle_state,
                    metadata={
                        "min": angle_min if angle_min is not None else 0,
                        "max": angle_max if angle_max is not None else 100,
                        "step": angle_step if angle_step else 1,
                        "unit": "%",
                    },
                    actions={"set_value": set_angle},
                )
            )

        return tuple(entities)


ADAPTER = Product2JJ9Adapter()
