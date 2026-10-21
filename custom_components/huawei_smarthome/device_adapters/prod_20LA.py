"""用户贡献的华为产品 20LA 协议（上海三思 三思LED床头灯 C22RL-CN-7W-RGB）。

证据（2026-10-20）：官方设备定义
``https://smarthome-drcn.dbankcdn.com/device/guide/20LA/20LA.json``
（prodId ``20LA`` / deviceTypeId ``01B`` 智能照明 / manufacturerId ``02B`` 上海三思 /
型号 ``C22RL-CN-7W-RGB`` / protocolType WiFi / uiType H5）。
下列字段与服务均按该 Profile 抄录，未凭猜测扩展。

字段：
  switch.on              bool   RW  总开关（1 开 / 0 关）
  brightness.brightness  int    RW  亮度 1~100，step 1
  lightMode.mode         enum   RW  情境模式 1 休闲 / 5 浪漫 / 7 睡眠 / 8 阅读
  colourMode.mode        enum   RW  0 彩色 / 1 单色 / 2 预置流光 / 3 自定义流光
                                    / 4 设备预置模式 / 100 人体感应夜灯
  colour.red/green/blue  int    RW  各 0~255，step 1
  cct.colorTemperature   int    RW  2700~6500 K，step 20
  yedengsw.yedengsw      bool   RW  夜灯开关（1 开 / 0 关）
  yedengstatus.*         enum   R   夜灯实际状态（0 关闭 / 1 开启）
  timer.* / delay.*      array  RW  定时/延时计划，元素为 object，结构未验证
  streamer.*             array  RW  流光配置，结构未验证
  update.version/progress/bootTime/introduction   R  固件信息
  netInfo.intensity/RSSI/IP/SSID/BSSID            R  网络信息

暴露：
  light          开关 / 亮度 / RGB / 色温
  number         亮度 1~100 / 色温 K / 红 / 绿 / 蓝 —— 独立可拖滑块
  select         灯光模式（lightMode.mode）
  select         色彩模式（colourMode.mode）
  switch         夜灯（yedengsw）
  binary_sensor  夜灯状态（yedengstatus，诊断）—— 诊断分类**只保留这一项**

换算：亮度 1~100 ↔ HA 0~255（HA 传 0 视为关灯）；色温按 Profile 的 step 20 对齐到 2700+20n。

**未暴露及原因**：
  timer / delay / streamer —— ``array[object]``，元素字段没有任何真机样本，
  硬编码结构等于瞎猜，交给后续有真机的人补；
  netInfo / update 的全部字段（信号格数、RSSI、IP、固件版本等）—— 按用户要求
  **诊断分类下只保留「夜灯状态」**，这些信息字段全部移除；
  SSID / BSSID —— 隐私，且对控制无意义；
  colourMode 的 2/3/4/100 分支 —— 预置流光 / 设备预置模式下的 colour/cct 语义未验证，
  云端会继续回报上一次的 colour/cct，属设备行为，这里不伪造。

⚠️ ``_WRITE_COLOUR_MODE``：写颜色时是否先切 ``colourMode.mode=0``（彩色）、写色温时先切
``=1``（单色），属**按 Profile 语义推断**，真机未验证。若出现「设了颜色灯不变」或「模式乱跳」，
把该常量置 ``False`` 即可退化为只发 colour.* / cct.* 的原始行为。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .api import EntitySpec
from .context import DeviceContext

_SW_SID = "switch"
_SW_FIELD = "on"
_BRI_SID = "brightness"
_BRI_FIELD = "brightness"
_BRI_RANGE = (1.0, 100.0)
_RGB_SID = "colour"
_CCT_SID = "cct"
_CCT_FIELD = "colorTemperature"
_CCT_MIN = 2700
_CCT_MAX = 6500
_CCT_STEP = 20
_LIGHT_MODE_SID = "lightMode"
_LIGHT_MODE_FIELD = "mode"
_COLOUR_MODE_SID = "colourMode"
_COLOUR_MODE_FIELD = "mode"
_NIGHT_SW_SID = "yedengsw"
_NIGHT_SW_FIELD = "yedengsw"
_NIGHT_ST_SID = "yedengstatus"
_NIGHT_ST_FIELD = "yedengstatus"

# Profile enumList 的「中文名 → 值」，顺序按 Profile 抄录
_LIGHT_MODES: tuple[tuple[str, int], ...] = (
    ("休闲模式", 1),
    ("浪漫模式", 5),
    ("睡眠模式", 7),
    ("阅读模式", 8),
)
_COLOUR_MODES: tuple[tuple[str, int], ...] = (
    ("彩色", 0),
    ("单色", 1),
    ("预置流光", 2),
    ("自定义流光", 3),
    ("设备预置模式", 4),
    ("人体感应夜灯", 100),
)

# 写 colour.* / cct.* 时是否联动 colourMode（0 彩色 / 1 单色），见文件头说明
_WRITE_COLOUR_MODE = True


def _flag(raw: Any) -> bool | None:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        if raw.casefold() in {"1", "true", "on"}:
            return True
        if raw.casefold() in {"0", "false", "off"}:
            return False
        return None
    if isinstance(raw, (int, float)):
        return bool(raw)
    return None


def _number(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _int(raw: Any) -> int | None:
    value = _number(raw)
    return None if value is None else int(round(value))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _to_ha_brightness(raw: Any) -> int | None:
    """设备 1~100 → HA 0~255。"""

    value = _number(raw)
    if value is None:
        return None
    return int(round(_clamp(value, *_BRI_RANGE) * 255 / 100))


def _to_device_brightness(raw: Any) -> int | None:
    """HA 0~255 → 设备 1~100。"""

    value = _number(raw)
    if value is None:
        return None
    return int(round(_clamp(value * 100 / 255, *_BRI_RANGE)))


def _quantize_cct(kelvin: Any) -> int | None:
    """把任意色温对齐到 Profile 的 2700 + 20n 网格。"""

    value = _number(kelvin)
    if value is None:
        return None
    value = _clamp(value, _CCT_MIN, _CCT_MAX)
    return int(_CCT_MIN + round((value - _CCT_MIN) / _CCT_STEP) * _CCT_STEP)


def _rgb(device: DeviceContext) -> tuple[int, int, int] | None:
    parts = tuple(_number(device.value(_RGB_SID, name)) for name in ("red", "green", "blue"))
    if any(part is None for part in parts):
        return None
    red, green, blue = (int(_clamp(part, 0, 255)) for part in parts if part is not None)
    return (red, green, blue)


def _supported_modes(device: DeviceContext) -> set[str]:
    """按设备**实际拥有**的服务推导 HA 色彩模式。

    不能写死：一旦 Profile 里缺 brightness / colour / cct，写死的 metadata 会让 HA
    声明出设备并不支持的能力，前端控件与实体状态对不上（表现为色轮/亮度条消失）。
    """

    modes: set[str] = set()
    if device.has_service(_RGB_SID):
        modes.add("rgb")
    if device.has_service(_CCT_SID):
        modes.add("color_temp")
    if device.has_service(_BRI_SID):
        modes.add("brightness")
    return modes or {"onoff"}


def _light_state(device: DeviceContext, modes: set[str]) -> Mapping[str, Any]:
    has_rgb = "rgb" in modes
    has_cct = "color_temp" in modes
    colour_mode = _int(device.value(_COLOUR_MODE_SID, _COLOUR_MODE_FIELD))

    # 必须始终返回**具体**的 color_mode：HA 前端靠这个属性决定「渲染色轮还是色温滑块」，
    # 返回 None 时部分版本会退化成只有开关/亮度，色轮直接不出现。
    # 设备在流光/预置/夜灯模式下没有等价的 HA 模式，这里按「彩色」呈现；
    # colour.* / cct 的数值仍然是设备上报的真实值，不伪造颜色本身。
    if has_cct and colour_mode == 1:
        mode = "color_temp"
    elif has_rgb:
        mode = "rgb"
    elif has_cct:
        mode = "color_temp"
    elif "brightness" in modes:
        mode = "brightness"
    else:
        mode = "onoff"

    return {
        "is_on": _flag(device.value(_SW_SID, _SW_FIELD)),
        "brightness": _to_ha_brightness(device.value(_BRI_SID, _BRI_FIELD)),
        "rgb_color": _rgb(device) if has_rgb else None,
        "color_temp_kelvin": (
            _int(device.value(_CCT_SID, _CCT_FIELD)) if has_cct else None
        ),
        "color_mode": mode,
    }


async def _turn_on(device: DeviceContext, data: Mapping[str, Any]) -> None:
    requested = _number(data.get("brightness"))
    if requested is not None and requested <= 0:
        # HA 语义：亮度 0 等同关灯，不去发 1% 的亮度。
        await _turn_off(device, {})
        return

    if device.has_service(_SW_SID):
        await device.async_send_service(_SW_SID, {_SW_FIELD: 1})

    brightness = _to_device_brightness(data.get("brightness"))
    if brightness is not None and device.has_service(_BRI_SID):
        await device.async_send_service(_BRI_SID, {_BRI_FIELD: brightness})

    rgb = data.get("rgb_color")
    kelvin = _quantize_cct(data.get("color_temp_kelvin"))

    if isinstance(rgb, (tuple, list)) and len(rgb) == 3 and device.has_service(_RGB_SID):
        red, green, blue = (int(_clamp(_number(part) or 0, 0, 255)) for part in rgb)
        if _WRITE_COLOUR_MODE and device.has_service(_COLOUR_MODE_SID):
            await device.async_send_service(_COLOUR_MODE_SID, {_COLOUR_MODE_FIELD: 0})
        await device.async_send_service(
            _RGB_SID, {"red": red, "green": green, "blue": blue}
        )

    if kelvin is not None and device.has_service(_CCT_SID):
        if _WRITE_COLOUR_MODE and device.has_service(_COLOUR_MODE_SID):
            await device.async_send_service(_COLOUR_MODE_SID, {_COLOUR_MODE_FIELD: 1})
        await device.async_send_service(_CCT_SID, {_CCT_FIELD: kelvin})


async def _turn_off(device: DeviceContext, _data: Mapping[str, Any]) -> None:
    if device.has_service(_SW_SID):
        await device.async_send_service(_SW_SID, {_SW_FIELD: 0})


def _light_spec(modes: set[str]) -> EntitySpec:
    metadata: dict[str, Any] = {"supported_color_modes": modes}
    if "color_temp" in modes:
        metadata["min_color_temp_kelvin"] = _CCT_MIN
        metadata["max_color_temp_kelvin"] = _CCT_MAX

    def state(device: DeviceContext) -> Mapping[str, Any]:
        return _light_state(device, modes)

    return EntitySpec(
        platform="light",
        key="light",
        name=None,
        state=state,
        metadata=metadata,
        actions={"turn_on": _turn_on, "turn_off": _turn_off},
    )


def _select_spec(
    sid: str,
    field: str,
    key: str,
    name: str,
    options: tuple[tuple[str, int], ...],
) -> EntitySpec:
    def state(device: DeviceContext) -> Mapping[str, Any]:
        value = _int(device.value(sid, field))
        for label, raw in options:
            if value == raw:
                return {"current_option": label}
        return {"current_option": None}

    async def select(device: DeviceContext, data: Mapping[str, Any]) -> None:
        option = str(data.get("option"))
        for label, raw in options:
            if label == option:
                await device.async_send_service(sid, {field: raw})
                return
        raise ValueError(f"unknown option for {sid}: {option}")

    return EntitySpec(
        platform="select",
        key=key,
        name=name,
        state=state,
        metadata={"options": [label for label, _ in options]},
        actions={"select_option": select},
    )


def _night_switch_spec() -> EntitySpec:
    def state(device: DeviceContext) -> Mapping[str, Any]:
        return {"is_on": _flag(device.value(_NIGHT_SW_SID, _NIGHT_SW_FIELD))}

    async def turn_on(device: DeviceContext, _data: Mapping[str, Any]) -> None:
        await device.async_send_service(_NIGHT_SW_SID, {_NIGHT_SW_FIELD: 1})

    async def turn_off(device: DeviceContext, _data: Mapping[str, Any]) -> None:
        await device.async_send_service(_NIGHT_SW_SID, {_NIGHT_SW_FIELD: 0})

    return EntitySpec(
        platform="switch",
        key="night_light",
        name="夜灯",
        state=state,
        actions={"turn_on": turn_on, "turn_off": turn_off},
    )


def _night_status_spec() -> EntitySpec:
    def state(device: DeviceContext) -> Mapping[str, Any]:
        value = _int(device.value(_NIGHT_ST_SID, _NIGHT_ST_FIELD))
        return {"is_on": None if value is None else value == 1}

    return EntitySpec(
        platform="binary_sensor",
        key="night_light_status",
        name="夜灯状态",
        state=state,
        metadata={"entity_category": "diagnostic"},
    )


def _make_number(
    sid: str,
    field: str,
    key: str,
    name: str,
    minimum: float,
    maximum: float,
    step: float = 1,
    *,
    unit: str | None = None,
    entity_category: str | None = None,
) -> EntitySpec:
    """一个可直接拖动的数值滑块（设备页面/仪表盘上都可见）。

    light 实体在 HA 设备页只渲染成一行紧凑的开关，色轮与亮度条要点开弹窗才有；
    这里把亮度/色温/RGB 各自摊成独立 number，既方便面板直接调，也方便脚本/自动化精确取值。
    """

    def state(device: DeviceContext) -> Mapping[str, Any]:
        value = _number(device.value(sid, field))
        return {"native_value": None if value is None else int(round(value))}

    async def set_value(device: DeviceContext, data: Mapping[str, Any]) -> None:
        raw = _number(data.get("value"))
        if raw is None:
            raise ValueError(f"invalid value for {sid}.{field}")
        value = int(_clamp(raw, minimum, maximum))
        if step > 1:
            # 对齐到 Profile 的步进网格（色温 step=20），否则设备可能拒收
            value = int(_clamp(minimum + round((value - minimum) / step) * step,
                               minimum, maximum))
        # 单值写 colour/cct 时也要把 colourMode 切到对应模式，否则设备可能忽略这次写入
        if _WRITE_COLOUR_MODE and sid in (_RGB_SID, _CCT_SID) and device.has_service(
            _COLOUR_MODE_SID
        ):
            await device.async_send_service(
                _COLOUR_MODE_SID, {_COLOUR_MODE_FIELD: 0 if sid == _RGB_SID else 1}
            )
        await device.async_send_service(sid, {field: value})

    metadata: dict[str, Any] = {"min": minimum, "max": maximum, "step": step}
    if unit is not None:
        metadata["unit"] = unit
    if entity_category is not None:
        metadata["entity_category"] = entity_category

    return EntitySpec(
        platform="number",
        key=key,
        name=name,
        state=state,
        metadata=metadata,
        actions={"set_value": set_value},
    )


class Product20LAAdapter:
    """三思 LED 床头灯：light + 两个模式 select + 夜灯开关/状态 + 诊断传感器。"""

    prod_id = "20LA"

    def entities(self, context: DeviceContext) -> tuple[EntitySpec, ...]:
        if context.profile is None:
            return ()

        specs: list[EntitySpec] = []
        # 灯具一律先给 light：即使云端 Profile 暂时没有 brightness/colour/cct，
        # 也不该退化成"只有一个开关"。能力由 _supported_modes 按实际服务自适应。
        if any(
            context.has_service(sid)
            for sid in (_SW_SID, _BRI_SID, _RGB_SID, _CCT_SID)
        ):
            specs.append(_light_spec(_supported_modes(context)))

        # 显式控件：亮度 / 色温 / RGB 三个分量，设备页与仪表盘上都是可拖滑块。
        if context.has_service(_BRI_SID):
            specs.append(
                _make_number(
                    _BRI_SID, _BRI_FIELD, "brightness_slider", "亮度", 1, 100, 1, unit="%"
                )
            )
        if context.has_service(_CCT_SID):
            specs.append(
                _make_number(
                    _CCT_SID,
                    _CCT_FIELD,
                    "color_temperature",
                    "色温",
                    _CCT_MIN,
                    _CCT_MAX,
                    _CCT_STEP,
                    unit="K",
                )
            )
        if context.has_service(_RGB_SID):
            for field_name, key, label in (
                ("red", "colour_red", "红色"),
                ("green", "colour_green", "绿色"),
                ("blue", "colour_blue", "蓝色"),
            ):
                specs.append(
                    _make_number(_RGB_SID, field_name, key, label, 0, 255, 1)
                )
        if context.has_service(_LIGHT_MODE_SID):
            specs.append(
                _select_spec(
                    _LIGHT_MODE_SID, _LIGHT_MODE_FIELD, "light_mode", "灯光模式", _LIGHT_MODES
                )
            )
        if context.has_service(_COLOUR_MODE_SID):
            specs.append(
                _select_spec(
                    _COLOUR_MODE_SID,
                    _COLOUR_MODE_FIELD,
                    "colour_mode",
                    "色彩模式",
                    _COLOUR_MODES,
                )
            )
        if context.has_service(_NIGHT_SW_SID):
            specs.append(_night_switch_spec())
        if context.has_service(_NIGHT_ST_SID):
            specs.append(_night_status_spec())

        # 诊断分类下只保留「夜灯状态」一项；固件/Wi-Fi/Signal/IP 与调试用的
        # 「Profile 服务」传感器均已按用户要求移除。
        return tuple(specs)


ADAPTER = Product20LAAdapter()
