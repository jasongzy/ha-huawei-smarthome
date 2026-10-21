"""Profile-based adapter for the OPPLE Smart Reading Lamp 2S (2BNN).

Profile: https://smarthome-drcn.dbankcdn.com/device/guide/2BNN/2BNN.json
型号 D0.2x108-WTT / 欧普照明 / protocolType=WiFi

实体划分
--------
主灯（沿用上游实现）:
  * light        -> switch.on + brightness(1-100) + cct(3000-5000)
  * select       -> lightMode.mode  (1 读写 / 2 放松 / 3 阅屏 / 5 最爱 / 6 夜灯)

本次新增（基于公开 Profile，未经真机验证，需实测确认）:
  * switch  backlight            背光开关        -> backlight.on
  * switch  study_mode           专注模式        -> studyMode.on
  * sensor  study_mode_left_time 专注剩余时间    -> studyMode.leftTime (string)
  * switch  night_wakeup         夜间唤醒        -> nightWakeup.enable
  * number  night_wakeup_start   夜间唤醒开始    -> nightWakeup.start (0-1440)
  * number  night_wakeup_end     夜间唤醒结束    -> nightWakeup.end   (0-1440)
  * sensor  x5   lightStatistics 只读统计（读写/阅屏/放松/专注/背光时长）

未做实体: timer / delay（定时任务对象，无稳定字段）、update（固件升级）、
netInfo（网络信息）—— 这些不是灯的基本能力，交由 HA 通用诊断即可。

⚠️ 真机验证待办
  1. 亮度与色温的换算方向是否与 App 一致（当前按 Profile 的 min/max 线性映射）；
  2. lightStatistics 五项的单位（推测为分钟，故未声明 unit，确认后可补
     unit="min" + state_class="total_increasing"）；
  3. nightWakeup.start/end 是否为「距 0 点的分钟数」（0-1440 与 Profile 一致）；
  4. studyMode.leftTime 的实际字符串格式。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from .api import EntitySpec
from .context import DeviceContext


_SWITCH_SID = "switch"
_SWITCH_FIELD = "on"
_BRIGHTNESS_SID = "brightness"
_BRIGHTNESS_FIELD = "brightness"
_CCT_SID = "cct"
_CCT_FIELD = "colorTemperature"
_LIGHT_MODE_SID = "lightMode"
_LIGHT_MODE_FIELD = "mode"

_BACKLIGHT_SID = "backlight"
_STUDY_SID = "studyMode"
_STUDY_FIELD = "on"
_STUDY_LEFT_FIELD = "leftTime"
_WAKEUP_SID = "nightWakeup"
_WAKEUP_ENABLE_FIELD = "enable"
_WAKEUP_START_FIELD = "start"
_WAKEUP_END_FIELD = "end"
_STATS_SID = "lightStatistics"
_DELAY_SID = "delay"


# --------------------------------------------------------------------------
# 倒计时（delay）工具
#
# 真机抓包得到的 delay 结构（Profile 里只写了 type=object，未给内部结构）：
#   {"sid":"delay","num":2,"data":{"delay":[
#      {"id":1,"sid":"switch","para":"on","paraValue":-1,"end":"20261019T171556Z"},
#      {"id":2,"sid":"switch","para":"on","paraValue":-1,"start":"20261019T172309Z"}]}}
#   · start / end 都是「到点执行时刻」，15 位 UTC：%Y%m%dT%H%M%SZ
#   · 创建 = action 1 / 更新 = 0 / 删除 = 2（Profile 的 action 枚举）
# ⚠️ paraValue 抓到的都是 -1；开/关的取值（0/1）尚未验证，故 1 分钟按钮用于实测。
# --------------------------------------------------------------------------


def _huawei_ts(moment: datetime) -> str:
    """HA 时间 -> 华为 15 位 UTC 时间戳。"""

    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso_from_huawei(value: Any) -> str | None:
    """华为时间戳 -> HA timestamp sensor 需要的 ISO 字符串。"""

    if not isinstance(value, str):
        return None
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S.%fZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    try:
        return datetime.strptime(value[:15], "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        ).isoformat()
    except ValueError:
        return None


def _delay_items(device: DeviceContext) -> list[Mapping[str, Any]]:
    raw = device.value(_DELAY_SID, "delay")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, Mapping)]
    if isinstance(raw, Mapping):
        return [raw]
    return []


def _countdown_button(minutes: int, turn_on: bool, key: str, name: str) -> EntitySpec:
    """下发一条「N 分钟后开/关灯」的倒计时。"""

    async def press(device: DeviceContext, _data: Mapping[str, Any]) -> None:
        when = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        await device.async_send_service(
            _DELAY_SID,
            {
                "action": 1,
                "num": 1,
                "delay": [
                    {
                        "id": 1,
                        "sid": _SWITCH_SID,
                        "para": _SWITCH_FIELD,
                        "paraValue": 1 if turn_on else 0,
                        "start": _huawei_ts(when),
                    }
                ],
            },
        )

    return EntitySpec(
        platform="button",
        key=key,
        name=name,
        state=lambda device: {},
        metadata={"entity_category": "config"},
        actions={"press": press},
    )


def _cancel_delay_button() -> EntitySpec:
    """删除全部倒计时（action=2）。"""

    async def press(device: DeviceContext, _data: Mapping[str, Any]) -> None:
        await device.async_send_service(
            _DELAY_SID,
            {"action": 2, "num": 0, "delay": []},
        )

    return EntitySpec(
        platform="button",
        key="countdown_cancel",
        name="取消倒计时",
        state=lambda device: {},
        metadata={"entity_category": "config"},
        actions={"press": press},
    )


# --------------------------------------------------------------------------
# 通用工具（沿用上游）
# --------------------------------------------------------------------------


def _field(
    profile: Mapping[str, Any],
    sid: str,
    name: str,
) -> Mapping[str, Any] | None:
    for service in profile.get("services", ()):
        if not isinstance(service, Mapping) or service.get("serviceId") != sid:
            continue
        for field in service.get("characteristics", ()):
            if isinstance(field, Mapping) and field.get("characteristicName") == name:
                return field
    return None


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in {"1", "true", "on"}:
            return True
        if text in {"0", "false", "off"}:
            return False
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def _range(field: Mapping[str, Any] | None) -> tuple[float, float] | None:
    if field is None:
        return None
    minimum = _number(field.get("min"))
    maximum = _number(field.get("max"))
    if minimum is None or maximum is None or maximum <= minimum:
        return None
    return float(minimum), float(maximum)


def _payload_number(value: Any, field: Mapping[str, Any]) -> int | float:
    number = _number(value)
    if number is None:
        raise ValueError("2BNN value must be numeric")
    if str(field.get("characteristicType") or "").casefold() in {
        "int",
        "integer",
        "enum",
    }:
        return int(round(number))
    return int(number) if float(number).is_integer() else number


def _enum_options(field: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    options: list[tuple[str, Any]] = []
    for item in field.get("enumList", ()):
        if not isinstance(item, Mapping) or item.get("enumVal") is None:
            continue
        label = item.get("descCh") or item.get("descEn") or item["enumVal"]
        options.append((str(label), item["enumVal"]))
    return tuple(options)


def _enum_label(field: Mapping[str, Any], value: Any) -> str | None:
    number = _number(value)
    for label, raw in _enum_options(field):
        if number is not None and _number(raw) == number:
            return label
        if str(raw) == str(value):
            return label
    return None


# --------------------------------------------------------------------------
# 通用实体构造器（本次新增能力用）
# --------------------------------------------------------------------------


def _bool_switch(
    sid: str,
    field: str,
    key: str,
    name: str,
    *,
    category: str | None = None,
) -> EntitySpec:
    """声明一个 0/1 布尔开关实体。"""

    async def turn_on(device: DeviceContext, _data: Mapping[str, Any]) -> None:
        await device.async_send_service(sid, {field: 1})

    async def turn_off(device: DeviceContext, _data: Mapping[str, Any]) -> None:
        await device.async_send_service(sid, {field: 0})

    return EntitySpec(
        platform="switch",
        key=key,
        name=name,
        state=lambda device: {"is_on": _bool(device.value(sid, field))},
        metadata={"entity_category": category} if category else {},
        actions={"turn_on": turn_on, "turn_off": turn_off},
    )


def _int_number(
    sid: str,
    field: str,
    field_meta: Mapping[str, Any],
    key: str,
    name: str,
    minimum: float,
    maximum: float,
    *,
    unit: str | None = None,
    step: float = 1,
    category: str = "config",
) -> EntitySpec:
    """声明一个整数数值实体（number 平台必须给出 min/max）。"""

    async def set_value(device: DeviceContext, data: Mapping[str, Any]) -> None:
        value = _number(data.get("value"))
        if value is None:
            raise ValueError(f"2BNN {key} expects a numeric value")
        clamped = min(max(float(value), minimum), maximum)
        await device.async_send_service(
            sid,
            {field: _payload_number(clamped, field_meta)},
        )

    metadata: dict[str, Any] = {
        "min": minimum,
        "max": maximum,
        "step": step,
        "entity_category": category,
    }
    if unit:
        metadata["unit"] = unit
    return EntitySpec(
        platform="number",
        key=key,
        name=name,
        state=lambda device: {"native_value": _number(device.value(sid, field))},
        metadata=metadata,
        actions={"set_value": set_value},
    )


def _raw_sensor(
    sid: str,
    field: str,
    key: str,
    name: str,
    *,
    category: str | None = None,
    unit: str | None = None,
) -> EntitySpec:
    """声明一个只读传感器（数值或字符串原样透传）。"""

    metadata: dict[str, Any] = {}
    if category:
        metadata["entity_category"] = category
    if unit:
        metadata["unit"] = unit
    return EntitySpec(
        platform="sensor",
        key=key,
        name=name,
        state=lambda device: {"native_value": device.value(sid, field)},
        metadata=metadata,
    )


# --------------------------------------------------------------------------
# 主灯（沿用上游逻辑）
# --------------------------------------------------------------------------


def _device_brightness_to_ha(
    value: Any,
    field: Mapping[str, Any],
) -> int | None:
    number = _number(value)
    value_range = _range(field)
    if number is None or value_range is None:
        return None
    minimum, maximum = value_range
    number = min(max(float(number), minimum), maximum)
    return round(1 + (number - minimum) * 254 / (maximum - minimum))


def _ha_brightness_to_device(
    value: Any,
    field: Mapping[str, Any],
) -> int | float:
    number = _number(value)
    value_range = _range(field)
    if number is None or value_range is None:
        raise ValueError("2BNN brightness range is missing from the Profile")
    minimum, maximum = value_range
    number = min(max(float(number), 1), 255)
    return _payload_number(
        minimum + (number - 1) * (maximum - minimum) / 254,
        field,
    )


async def _turn_on(context: DeviceContext, data: Mapping[str, Any]) -> None:
    """Turn on first, then apply any requested primary-light values."""

    await context.async_send_service(_SWITCH_SID, {_SWITCH_FIELD: 1})
    profile = context.profile or {}

    brightness = data.get("brightness")
    brightness_field = _field(profile, _BRIGHTNESS_SID, _BRIGHTNESS_FIELD)
    if brightness is not None:
        if brightness_field is None:
            raise ValueError("2BNN brightness field is missing from the Profile")
        await context.async_send_service(
            _BRIGHTNESS_SID,
            {
                _BRIGHTNESS_FIELD: _ha_brightness_to_device(
                    brightness,
                    brightness_field,
                )
            },
        )

    color_temp_kelvin = data.get("color_temp_kelvin")
    cct_field = _field(profile, _CCT_SID, _CCT_FIELD)
    cct_range = _range(cct_field)
    if color_temp_kelvin is not None:
        if cct_field is None or cct_range is None:
            raise ValueError("2BNN color-temperature range is missing from the Profile")
        minimum, maximum = cct_range
        value = min(max(float(color_temp_kelvin), minimum), maximum)
        await context.async_send_service(
            _CCT_SID,
            {_CCT_FIELD: _payload_number(value, cct_field)},
        )


async def _turn_off(context: DeviceContext, _data: Mapping[str, Any]) -> None:
    await context.async_send_service(_SWITCH_SID, {_SWITCH_FIELD: 0})


class Product2BNNAdapter:
    """Huawei Smart Selection OPPLE Smart Reading Lamp 2S."""

    prod_id = "2BNN"

    def entities(self, context: DeviceContext) -> tuple[EntitySpec, ...]:
        profile = context.profile
        if profile is None:
            return ()

        switch = _field(profile, _SWITCH_SID, _SWITCH_FIELD)
        brightness = _field(profile, _BRIGHTNESS_SID, _BRIGHTNESS_FIELD)
        cct = _field(profile, _CCT_SID, _CCT_FIELD)
        brightness_range = _range(brightness)
        cct_range = _range(cct)
        specs: list[EntitySpec] = []

        # ---- 1. 主灯 ----
        if switch is not None:
            color_mode = (
                "color_temp"
                if cct_range is not None
                else "brightness"
                if brightness_range is not None
                else "onoff"
            )

            def light_state(device: DeviceContext) -> Mapping[str, Any]:
                return {
                    "is_on": _bool(device.value(_SWITCH_SID, _SWITCH_FIELD)),
                    "brightness": (
                        _device_brightness_to_ha(
                            device.value(_BRIGHTNESS_SID, _BRIGHTNESS_FIELD),
                            brightness or {},
                        )
                        if brightness_range is not None
                        else None
                    ),
                    "color_temp_kelvin": (
                        _number(device.value(_CCT_SID, _CCT_FIELD))
                        if cct_range is not None
                        else None
                    ),
                    "color_mode": color_mode,
                }

            metadata: dict[str, Any] = {
                "supported_color_modes": {color_mode},
            }
            if cct_range is not None:
                metadata["min_color_temp_kelvin"] = int(cct_range[0])
                metadata["max_color_temp_kelvin"] = int(cct_range[1])
            specs.append(
                EntitySpec(
                    platform="light",
                    key="light",
                    name=None,
                    state=light_state,
                    metadata=metadata,
                    actions={"turn_on": _turn_on, "turn_off": _turn_off},
                )
            )

        # ---- 2. 灯光模式（场景预设）----
        light_mode = _field(profile, _LIGHT_MODE_SID, _LIGHT_MODE_FIELD)
        options = _enum_options(light_mode) if light_mode is not None else ()
        if light_mode is not None and options:

            async def select_light_mode(
                device: DeviceContext,
                data: Mapping[str, Any],
            ) -> None:
                option = str(data.get("option"))
                for label, raw in options:
                    if label == option:
                        await device.async_send_service(
                            _LIGHT_MODE_SID,
                            {_LIGHT_MODE_FIELD: _payload_number(raw, light_mode)},
                        )
                        return
                raise ValueError(f"2BNN unknown light mode: {option}")

            specs.append(
                EntitySpec(
                    platform="select",
                    key="light_mode",
                    name="灯光模式",
                    state=lambda device: {
                        "current_option": _enum_label(
                            light_mode,
                            device.value(_LIGHT_MODE_SID, _LIGHT_MODE_FIELD),
                        )
                    },
                    metadata={"options": tuple(label for label, _ in options)},
                    actions={"select_option": select_light_mode},
                )
            )

        # ---- 2.5 亮度滑动条（直接用设备 1-100 的原始档位，不换算）----
        # light 实体本身也支持亮度，这里额外给一条独立的滑动条，方便放到仪表盘上。
        if brightness_range is not None:
            specs.append(
                _int_number(
                    _BRIGHTNESS_SID,
                    _BRIGHTNESS_FIELD,
                    brightness or {},
                    "brightness_level",
                    "亮度",
                    brightness_range[0],
                    brightness_range[1],
                    unit="%",
                    category="",
                )
            )

        # ---- 3. 背光开关 ----
        if context.has_service(_BACKLIGHT_SID):
            specs.append(
                _bool_switch(
                    _BACKLIGHT_SID,
                    _SWITCH_FIELD,
                    "backlight",
                    "背光",
                    category="config",
                )
            )

        # ---- 4. 专注模式 + 剩余时间 ----
        if context.has_service(_STUDY_SID):
            specs.append(
                _bool_switch(
                    _STUDY_SID,
                    _STUDY_FIELD,
                    "study_mode",
                    "专注模式",
                    category="config",
                )
            )
            if _field(profile, _STUDY_SID, _STUDY_LEFT_FIELD) is not None:
                specs.append(
                    _raw_sensor(
                        _STUDY_SID,
                        _STUDY_LEFT_FIELD,
                        "study_mode_left_time",
                        "专注模式剩余时间",
                        category="diagnostic",
                    )
                )

        # ---- 5. 夜间唤醒（开关 + 起止时间）----
        if context.has_service(_WAKEUP_SID):
            specs.append(
                _bool_switch(
                    _WAKEUP_SID,
                    _WAKEUP_ENABLE_FIELD,
                    "night_wakeup",
                    "夜间唤醒",
                    category="config",
                )
            )
            for field_name, key, label in (
                (_WAKEUP_START_FIELD, "night_wakeup_start", "夜间唤醒开始"),
                (_WAKEUP_END_FIELD, "night_wakeup_end", "夜间唤醒结束"),
            ):
                field_meta = _field(profile, _WAKEUP_SID, field_name)
                field_range = _range(field_meta)
                if field_meta is None or field_range is None:
                    continue
                minimum, maximum = field_range
                specs.append(
                    _int_number(
                        _WAKEUP_SID,
                        field_name,
                        field_meta,
                        key,
                        label,
                        minimum,
                        maximum,
                        unit="min",
                    )
                )

        # ---- 6. 用光统计（只读）----
        if context.has_service(_STATS_SID):
            for key, label, field_name in (
                ("read_duration", "读写时长", "readDuration"),
                ("screen_duration", "阅屏时长", "readScreenDuration"),
                ("relax_duration", "放松时长", "relaxDuration"),
                ("study_duration", "专注时长", "studyMode"),
                ("backlight_duration", "背光时长", "backlight"),
            ):
                if _field(profile, _STATS_SID, field_name) is None:
                    continue
                specs.append(
                    _raw_sensor(
                        _STATS_SID,
                        field_name,
                        key,
                        label,
                        category="diagnostic",
                    )
                )

        # ---- 7. 倒计时（delay）----
        if context.has_service(_DELAY_SID):

            def delay_state(device: DeviceContext) -> Mapping[str, Any]:
                moments = []
                for item in _delay_items(device):
                    for key in ("start", "end"):
                        moment = _iso_from_huawei(item.get(key))
                        if moment:
                            moments.append(moment)
                return {"native_value": min(moments) if moments else None}

            specs.append(
                EntitySpec(
                    platform="sensor",
                    key="countdown_next",
                    name="倒计时到点时间",
                    state=delay_state,
                    metadata={
                        "device_class": "timestamp",
                        "entity_category": "diagnostic",
                    },
                )
            )
            # ⚠️ 创建倒计时的按钮暂不启用：实测下发的 delay 命令没被云端登记
            #    （App 里看不到倒计时），却会把 paraValue 立即执行（灯当场被关）。
            #    等抓到 App 的真实创建报文结构后再启用，见模块顶部注释。

        return tuple(specs)


ADAPTER = Product2BNNAdapter()
