"""Profile-based adapter for the CHINT smart socket 124U (正泰智能插座).

Profile: https://smarthome-drcn.dbankcdn.com/device/guide/124U/124U.json
型号 1.0 / 正泰 / protocolType=WiFi

已有实体（沿用发布版适配器）:
  * switch  开关            -> switch.on (0/1)
  * sensor  功率            -> power.current (0-6000)
  * sensor  累计电量        -> consumption.consumption (0-1000)
  * select  断电记忆        -> memorySwitch.status (0 关 / 1 开 / 2 保持上次)

本次新增（依据公开 Profile）:
  * binary_sensor 故障      -> faultDetection.status (0 正常 / 1 异常)
  * sensor        故障码    -> faultDetection.code  (0 无故障 / 1 过载警告)

未做实体: timer / timerInfo / delay（object 结构，创建格式未验证）、
update（固件升级）、netInfo 全部字段（RSSI/SSID/BSSID/IP——隐私，且本设备云端不上报，略）。

⚠️ 待真机确认
  1. power.current 的单位：现有映射按 **W** 处理（实测书桌插座读数 121，
     与「书桌上有显示器/笔记本」的功耗量级吻合），请以 App 显示为准；
     若 App 单位是 A 或 0.1W，改 metadata 的 unit / device_class 即可。
  2. consumption 的单位：按 kWh 处理（实测 20）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .api import EntitySpec
from .context import DeviceContext

_MEMORY_OPTIONS = ["关", "开", "保持上次"]
_FAULT_CODES = {"0": "无故障", "1": "过载警告"}


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.casefold() in {"1", "true", "on"}:
            return True
        if value.casefold() in {"0", "false", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _enum_label(field: Mapping[str, Any] | None, value: Any) -> str | None:
    """按 Profile 的 enumList 把原始值翻译成 descCh。"""

    if not field:
        return None
    for item in field.get("enumList", ()):
        if not isinstance(item, Mapping):
            continue
        raw = item.get("enumVal")
        if str(raw) == str(value):
            return str(item.get("descCh") or raw)
        if _as_int(raw) is not None and _as_int(raw) == _as_int(value):
            return str(item.get("descCh") or raw)
    return None


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


async def _turn_on(context: DeviceContext, _data: Mapping[str, Any]) -> None:
    await context.async_send_service("switch", {"on": 1})


async def _turn_off(context: DeviceContext, _data: Mapping[str, Any]) -> None:
    await context.async_send_service("switch", {"on": 0})


async def _set_memory(context: DeviceContext, data: Mapping[str, Any]) -> None:
    value = {"关": 0, "开": 1, "保持上次": 2}.get(str(data.get("option")))
    if value is None:
        raise ValueError(f"unsupported option: {data.get('option')}")
    await context.async_send_service("memorySwitch", {"status": value})


class Product124UAdapter:
    """124U 正泰智能插座。"""

    prod_id = "124U"

    def entities(self, context: DeviceContext) -> tuple[EntitySpec, ...]:
        profile = context.profile
        if profile is None or not context.has_service("switch"):
            return ()

        def switch_state(device: DeviceContext) -> Mapping[str, Any]:
            return {"is_on": _as_bool(device.value("switch", "on"))}

        def power_state(device: DeviceContext) -> Mapping[str, Any]:
            return {"native_value": _as_int(device.value("power", "current"))}

        def consumption_state(device: DeviceContext) -> Mapping[str, Any]:
            return {"native_value": _as_int(device.value("consumption", "consumption"))}

        memory_field = _field(profile, "memorySwitch", "status")

        def memory_state(device: DeviceContext) -> Mapping[str, Any]:
            status = _as_int(device.value("memorySwitch", "status"))
            if status is not None and 0 <= status < len(_MEMORY_OPTIONS):
                return {"current_option": _MEMORY_OPTIONS[status]}
            return {"current_option": _enum_label(memory_field, device.value("memorySwitch", "status"))}

        specs: list[EntitySpec] = [
            EntitySpec(
                platform="switch",
                key="switch",
                name=None,
                state=switch_state,
                actions={"turn_on": _turn_on, "turn_off": _turn_off},
            ),
            EntitySpec(
                platform="sensor",
                key="power",
                name="功率",
                state=power_state,
                metadata={
                    "unit": "W",
                    "device_class": "power",
                    "state_class": "measurement",
                },
            ),
            EntitySpec(
                platform="sensor",
                key="consumption",
                name="累计电量",
                state=consumption_state,
                metadata={
                    "unit": "kWh",
                    "device_class": "energy",
                    "state_class": "total_increasing",
                },
            ),
            EntitySpec(
                platform="select",
                key="memory",
                name="断电记忆",
                state=memory_state,
                metadata={"options": _MEMORY_OPTIONS},
                actions={"select_option": _set_memory},
            ),
        ]

        # ---- 故障检测 ----
        if context.has_service("faultDetection"):
            fault_code_field = _field(profile, "faultDetection", "code")

            def fault_state(device: DeviceContext) -> Mapping[str, Any]:
                status = _as_int(device.value("faultDetection", "status"))
                return {"is_on": None if status is None else status != 0}

            def fault_code_state(device: DeviceContext) -> Mapping[str, Any]:
                raw = device.value("faultDetection", "code")
                return {"native_value": _enum_label(fault_code_field, raw) or raw}

            specs.append(
                EntitySpec(
                    platform="binary_sensor",
                    key="fault",
                    name="故障",
                    state=fault_state,
                    metadata={
                        "device_class": "problem",
                        "entity_category": "diagnostic",
                    },
                )
            )
            specs.append(
                EntitySpec(
                    platform="sensor",
                    key="fault_code",
                    name="故障码",
                    state=fault_code_state,
                    metadata={"entity_category": "diagnostic"},
                )
            )

        # ---- 网络诊断：124U 不上报 ----
        # 实测（.storage 状态快照）：4 台 124U 都有 netInfo 服务但 data 恒为 {}，
        # 云端从不下发 RSSI/SSID，故不做实体，避免出现永远 unknown 的传感器。
        # （105M / 100E / 智慧屏等型号 netInfo 有真实数据，那里才加。）

        return tuple(specs)


ADAPTER = Product124UAdapter()
