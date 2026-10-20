"""User-contributed protocol for Huawei product 100E (控客 Wi-Fi 智能插座 MINI K).

设备类型: 智能插座 (Socket), 型号 MINI K
制造商: 控客 (KONKE)
Profile: https://smarthome-drcn.dbankcdn.com/device/guide/100E/100E.json

Profile 声明 6 个服务: switch / timer / delay / indicator / update / timerID。
本设备为**非计量插座**——Profile 与真机数据均无 power / consumption / faultDetection，
故没有功率、用电量、故障检测可暴露（与带计量的 124U 正泰不同）。

真机上报的服务（来自 .storage 状态快照）:
  * switch.on / switch.name        bool/string RW   电源开关 + 设备名
  * indicator.on / indicator.name  bool/string RW   指示灯开关（插座上的 LED）
  * timer / delay                  int/object        云端定时 / 倒计时（云侧管理）
  * update                          OTA              version/progress/bootTime 实测为空/0
  * netInfo                        RSSI/BSSID/IP/SSID/intensity —— **隐私字段，按用户要求不加**

本次暴露（两个 RW 二进制开关）:
  * switch  电源     -> switch.on
  * switch  指示灯   -> indicator.on   (关掉插座 LED，晚上实用)

未做实体: timer/delay 的对象数组（创建格式云侧管理）、
update 的 OTA 字段（实测无数据）、netInfo 全部（隐私）、
switch.name / indicator.name（设备名，无标准 HA 映射）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .api import EntitySpec
from .context import DeviceContext


# ---- 工具函数 ------------------------------------------------------------

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


# ---- 动作函数 ------------------------------------------------------------

async def _turn_on(context: DeviceContext, _data: Mapping[str, Any]) -> None:
    await context.async_send_service("switch", {"on": 1})


async def _turn_off(context: DeviceContext, _data: Mapping[str, Any]) -> None:
    await context.async_send_service("switch", {"on": 0})


async def _indicator_on(context: DeviceContext, _data: Mapping[str, Any]) -> None:
    await context.async_send_service("indicator", {"on": 1})


async def _indicator_off(context: DeviceContext, _data: Mapping[str, Any]) -> None:
    await context.async_send_service("indicator", {"on": 0})


# ---- 适配器 --------------------------------------------------------------

class Product100EAdapter:
    """100E 控客 Wi-Fi 智能插座 MINI K 适配器。"""

    prod_id = "100E"

    def entities(self, context: DeviceContext) -> tuple[EntitySpec, ...]:
        if context.profile is None or not context.has_service("switch"):
            return ()

        def power_state(device: DeviceContext) -> Mapping[str, Any]:
            return {"is_on": _as_bool(device.value("switch", "on"))}

        specs: list[EntitySpec] = [
            EntitySpec(
                platform="switch",
                key="power",
                name="电源",
                state=power_state,
                actions={"turn_on": _turn_on, "turn_off": _turn_off},
            ),
        ]

        # ---- 指示灯（插座 LED，RW）----
        if context.has_service("indicator"):

            def indicator_state(device: DeviceContext) -> Mapping[str, Any]:
                return {"is_on": _as_bool(device.value("indicator", "on"))}

            specs.append(
                EntitySpec(
                    platform="switch",
                    key="indicator",
                    name="指示灯",
                    state=indicator_state,
                    actions={"turn_on": _indicator_on, "turn_off": _indicator_off},
                )
            )

        return tuple(specs)


ADAPTER = Product100EAdapter()
