"""用户贡献的华为产品 2J7P（杜亚智能百叶 DH12）适配器。

产品：杜亚智能百叶
厂商：杜亚
型号：DH12（deviceTypeId 2BB，百叶帘，Wi-Fi）
Profile：https://smarthome-drcn.dbankcdn.com/device/guide/2J7P/2J7P.json

Profile 服务与实体映射
---------------------
  opener.current   int  R   0~100 %  当前开合度 -> cover 的 current_cover_position
  opener.target    int  RW  0~100 %  目标开合度 -> cover 的 set_position
                    Profile 原文：“下发100时，窗帘全开（全透光）；下发0时，
                    窗帘全关（全遮光）”，与 Home Assistant 的 0=关 / 100=开
                    约定一致，因此不做取反。
  action.action    enum RW  0=下降 1=上升 2=暂停
                    -> cover 的 close / open / stop
                    百叶帘“上升”即收帘（打开），“下降”即放帘（关闭）。

未映射的服务及原因
-----------------
  rotationAction.action  叶片转动（0=上 1=下）。这是一次性动作，没有状态回报
                         字段，写入后无法确认是否生效；Home Assistant 的 cover
                         契约也不含倾角，按失败关闭原则不暴露。
  changeDirection.action 枚举只有一个取值（1=启用），没有控制意义。
  netInfo / update       网络信息与 OTA 运维字段，对日常使用没有价值，不映射。

验证状态（提交时必须如实告知维护者）
----------------------------------
  已在真机（Home Assistant 2026.10.1 / 集成 2026.10.16）实测：开合度回读、开、关、暂停与指定开合度均与设备
  实际一致，开合方向无需取反（_POSITION_REVERSED = False 经真机确认）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .api import EntitySpec
from .context import DeviceContext

# Profile 明确 100=全开、0=全关，与 Home Assistant 一致。真机方向相反时改 True。
_POSITION_REVERSED = False

_POS_MIN = 0
_POS_MAX = 100

# action.action 的枚举取值来自本机 Profile。
_ACTION_CLOSE = 0  # 下降
_ACTION_OPEN = 1  # 上升
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

    Profile 的 enumList 里 enumVal 一律是字符串，而真机只接受数值，
    直接下发字符串会被云端拒绝。
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
    """从 Profile 枚举表里取回 wanted 对应的原始取值。"""

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


def _position(value: Any) -> int | None:
    position = _int(value)
    if position is None:
        return None
    position = min(max(position, _POS_MIN), _POS_MAX)
    return _POS_MAX - position if _POSITION_REVERSED else position


def _clamp_target(value: Any, field: Mapping[str, Any] | None) -> Any:
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


class Product2J7PAdapter:
    """把 2J7P 的实体与控制命令全部约束在本文件内。"""

    prod_id = "2J7P"

    def entities(self, context: DeviceContext) -> tuple[EntitySpec, ...]:
        profile = context.profile
        if profile is None or not context.has_service("opener"):
            return ()

        action_field = _field(profile, "action", "action")
        target_field = _field(profile, "opener", "target")

        def cover_state(device: DeviceContext) -> Mapping[str, Any]:
            position = _position(device.value("opener", "current"))
            return {
                "current_position": position,
                # 只有拿到位置才能判定开合；未知时返回 None，不猜测。
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
            device_value = (
                _POS_MAX - position if _POSITION_REVERSED else position
            )
            await device.async_send_service(
                "opener",
                {"target": _clamp_target(device_value, target_field)},
            )

        actions: dict[str, Any] = {"set_position": set_position}
        if context.has_service("action") and _is_writable(action_field):
            actions["open"] = open_cover
            actions["close"] = close_cover
            actions["stop"] = stop_cover

        return (
            EntitySpec(
                platform="cover",
                key="blind",
                name=None,
                state=cover_state,
                metadata={"device_class": "blind"},
                actions=actions,
            ),
        )


ADAPTER = Product2J7PAdapter()
