"""User-contributed protocol for Huawei product V0A2.

Product: 华为智慧屏V系列2021款 (deviceModel ``THAL``, prodId ``V0A2``).
Profile: https://smarthome-drcn.dbankcdn.com/device/guide/V0A2/V0A2.json

The public Profile is unusually thin for this product: it declares only six
services (messageboard, remotecontrol, autoconfig, devicestate,
generalcommand, logreport) and marks **none** of their characteristics
writable, while the TV actually pushes ~45 services.  Everything below was
therefore verified against a real device over MQTT rather than read off the
Profile.

Commands confirmed to take effect (cloud ACK ``errcode=0`` **and** a
``deviceDataChanged`` push carrying the new value):

    speaker.volume        {"volume": 20}          -> push {"volume": 20}
    speaker.mute          {"mute": true}          -> push {"mute": true}
    speaker.equalizer     {"equalizer": "AUTO"}   -> push {"equalizer": "AUTO"}
    inputSource.name      {"name": "HDMI2"}       -> push {"name": "HDMI2"}
    screen.brightness     {"brightness": 100}     -> push {"brightness": 100}
    screen.mode           {"mode": "PICTURE"}     -> push {"mode": "PICTURE"}
    screen.on (off)       {"on": false}           -> push {"on": false}

Known firmware limitation -- waking the panel:

    screen.on = true is accepted (``errcode=0``) and the panel really does
    light up, but the TV never pushes ``on: true`` and never updates its
    reported state.  After waking, this integration keeps reporting the last
    pushed value (``off``) until the TV reports something else.  The vendor
    app behaves the same way.  Home Assistant is told about the mismatch
    through ``extra_state_attributes`` so the entity documents its own
    uncertainty instead of silently showing a stale power state.

Rejected commands (``errcode=-1``, deliberately not exposed): ``systemMode``,
``screenSaver``, ``switch``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .api import EntitySpec
from .context import DeviceContext

_PROD_ID = "V0A2"

_SCREEN_SID = "screen"
_SPEAKER_SID = "speaker"
_INPUT_SOURCE_SID = "inputSource"
_VIDEO_PLAYER_SID = "videoPlayer"
_AUDIO_PLAYER_SID = "audioPlayer"
_DEVICE_STATE_SID = "devicestate"
_MESSAGE_BOARD_SID = "messageboard"
_MESSAGE_BOARD_WIRE_SID = "messageBoard"
_REMOTE_CONTROL_SID = "remotecontrol"
_CHILD_MODE_SID = "childMode"

# The real device pushes the message board with camel casing while the Profile
# spells it lower case, so both are accepted.
_SID_CANDIDATES: dict[str, tuple[str, ...]] = {
    _MESSAGE_BOARD_SID: (_MESSAGE_BOARD_SID, _MESSAGE_BOARD_WIRE_SID),
}

_DEVICE_CLASS_VOLUME = "volume"
_VOLUME_MAX = 100


def _candidates(sid: str) -> tuple[str, ...]:
    return _SID_CANDIDATES.get(sid, (sid,))


def _value(device: DeviceContext, sid: str, field: str) -> Any:
    for candidate in _candidates(sid):
        value = device.value(candidate, field)
        if value is not None:
            return value
    return None


def _service(profile: Mapping[str, Any], sid: str) -> Mapping[str, Any] | None:
    for service in profile.get("services", ()):
        if isinstance(service, Mapping) and service.get("serviceId") == sid:
            return service
    return None


def _field(
    profile: Mapping[str, Any],
    sid: str,
    name: str,
) -> Mapping[str, Any] | None:
    service = _service(profile, sid)
    if service is None:
        return None
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
        if value.casefold() in {"1", "true", "on"}:
            return True
        if value.casefold() in {"0", "false", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def _metadata(value: Any) -> Mapping[str, Any]:
    """Decode the JSON string the player services publish in ``metadata``."""

    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        if isinstance(parsed, Mapping):
            return parsed
    return {}


def _screen_on(device: DeviceContext) -> bool | None:
    value = _bool(_value(device, _SCREEN_SID, "on"))
    if value is not None:
        return value
    # devicestate.screenState mirrors the panel state when screen is absent.
    code = _number(_value(device, _DEVICE_STATE_SID, "screenState"))
    if code is None:
        return None
    return code != 0


# Player ``state`` values that mean something is actually on.  The TV pushes
# these live while playing (observed: AD_PLAYING -> PLAYING as a show starts).
#
# The audio player cannot be trusted on state alone: on the test device it kept
# reporting state=PLAYING with metadata from a track played ten days earlier,
# and never pushed a stopping state.  The video player does push live state, so
# video wins whenever it is active and audio is only a fallback when the video
# player is not playing at all.
_ACTIVE_STATES = frozenset(
    {"PLAYING", "AD_PLAYING", "PAUSED", "BUFFERING", "AD_PAUSED"}
)

# ``vodName`` is what the video player publishes for a title; music uses
# ``title``.  Order matters: the video metadata has no ``title`` field.
_TITLE_KEYS = ("vodName", "title", "name")


def _player_state(sid: str, device: DeviceContext) -> str:
    return str(_value(device, sid, "state") or "").strip().upper()


def _player_title(metadata: Mapping[str, Any]) -> str | None:
    for key in _TITLE_KEYS:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _player_active(sid: str, device: DeviceContext) -> bool:
    """Return whether the given player reports an active state.

    Only an explicitly active state counts: an unrecognised value must never
    claim that something is playing.
    """

    return _player_state(sid, device) in _ACTIVE_STATES


def _now_playing(device: DeviceContext) -> Mapping[str, Any]:
    """Return the item playing right now, or nothing when playback has stopped.

    The video player describes the picture and pushes live state, so it takes
    precedence; the audio player is consulted only when no video is playing.

    Episodes: the video metadata identifies the current episode through
    ``volumeIndex`` (observed 2, then 3 while watching the same series) and the
    series total through ``sum``.  ``volumeIndex`` is 1-based, so it maps to
    HA's ``media_episode`` directly.  The cover URL embeds a ``2-26`` fragment
    that does *not* track the episode, so it must not be parsed for this.
    """

    if _player_active(_VIDEO_PLAYER_SID, device):
        meta = _metadata(_value(device, _VIDEO_PLAYER_SID, "metadata"))
        title = _player_title(meta)
        if title is not None:
            episode_total = _int_or_none(meta.get("sum"))
            episode = _int_or_none(meta.get("volumeIndex"))
            position = _int_or_none(_value(device, _VIDEO_PLAYER_SID, "progress"))
            duration = _int_or_none(meta.get("totalTime"))
            return {
                "title": title,
                "series_title": title,
                "episode": episode if episode and episode > 0 else None,
                "episode_total": episode_total,
                "artist": meta.get("director") or meta.get("actor"),
                "album": meta.get("categoryName"),
                "image": _image_url(meta),
                "position": position,
                "duration": duration,
                "position_updated_at": device.service_updated_at(_VIDEO_PLAYER_SID),
            }

    if _player_active(_AUDIO_PLAYER_SID, device):
        meta = _metadata(_value(device, _AUDIO_PLAYER_SID, "metadata"))
        title = _player_title(meta)
        if title is not None:
            return {
                "title": title,
                "artist": meta.get("artist"),
                "album": meta.get("album"),
                "image": _image_url(meta),
                "position": _int_or_none(_value(device, _AUDIO_PLAYER_SID, "progress")),
                "duration": _int_or_none(meta.get("duration")),
                "position_updated_at": device.service_updated_at(_AUDIO_PLAYER_SID),
            }
    return {}


def _int_or_none(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _image_url(metadata: Mapping[str, Any]) -> str | None:
    """Return the artwork URL the player publishes, if any.

    The video player publishes ``titlePicture``; music metadata carries its
    cover under ``albumArt``/``coverUrl``.  Only http(s) values are accepted so
    the entity never hands Home Assistant a non-URL to fetch.
    """

    for key in ("titlePicture", "albumArt", "coverUrl", "imageUrl"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
            return value.strip()
    return None


def _volume_level(device: DeviceContext) -> float | None:
    value = _number(_value(device, _SPEAKER_SID, "volume"))
    if value is None:
        return None
    value = min(max(value, 0), _VOLUME_MAX)
    return value / _VOLUME_MAX


def _tv_state(device: DeviceContext) -> Mapping[str, Any]:
    """Compose the media_player state.

    Waking the panel does not make the TV report ``on`` again (see the module
    docstring), so the reported power flag can lag behind reality until the TV
    reports something else.
    """

    on = _screen_on(device)
    playing = _now_playing(device)
    return {
        "state": "on" if on else ("off" if on is False else None),
        "volume_level": _volume_level(device),
        "is_volume_muted": _bool(_value(device, _SPEAKER_SID, "mute")),
        "media_title": playing.get("title"),
        "media_artist": playing.get("artist"),
        "media_album_name": playing.get("album"),
        "media_image_url": playing.get("image"),
        "media_series_title": playing.get("series_title"),
        "media_episode": playing.get("episode"),
        # Position is the last pushed reading.  The TV reports it every few
        # minutes, so ``media_position_updated_at`` tells Home Assistant when
        # that reading was taken and lets it extrapolate an accurate progress
        # bar in between.
        #
        # It is set only when the device-reported timestamp parsed: the cloud
        # sends the live ``ts`` with nine fractional digits
        # ("20260914T222103156Z"), which ``parse_remote_timestamp`` does not
        # accept (it expects six or fewer), so the call can legitimately return
        # None.  Reporting None would tell Home Assistant the position was
        # never updated, which is worse than omitting the attribute: without it
        # HA shows the raw position, exactly as before this timestamp existed.
        "media_position": playing.get("position"),
        "media_duration": playing.get("duration"),
        "media_position_updated_at": playing.get("position_updated_at"),
        "source": _value(device, _INPUT_SOURCE_SID, "name"),
    }


async def _turn_on(device: DeviceContext, _data: Mapping[str, Any]) -> None:
    await device.async_send_service(_SCREEN_SID, {"on": True})


async def _turn_off(device: DeviceContext, _data: Mapping[str, Any]) -> None:
    await device.async_send_service(_SCREEN_SID, {"on": False})


async def _set_volume(device: DeviceContext, data: Mapping[str, Any]) -> None:
    raw = data.get("volume")
    level = _number(raw)
    if level is None:
        return
    if isinstance(raw, float) and 0.0 <= raw <= 1.0:
        level = round(raw * _VOLUME_MAX)
    level = int(min(max(level, 0), _VOLUME_MAX))
    await device.async_send_service(_SPEAKER_SID, {"volume": level})


async def _set_mute(device: DeviceContext, data: Mapping[str, Any]) -> None:
    """Mirror ``speaker.mute`` (verified: ACK errcode=0 + deviceDataChanged push)."""
    raw = data.get("mute")
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in ("true", "1", "on", "yes"):
            muted = True
        elif lowered in ("false", "0", "off", "no"):
            muted = False
        else:
            return
    elif isinstance(raw, bool):
        muted = raw
    elif isinstance(raw, (int, float)):
        muted = bool(raw)
    else:
        return
    await device.async_send_service(_SPEAKER_SID, {"mute": muted})


async def _select_source(device: DeviceContext, data: Mapping[str, Any]) -> None:
    source = data.get("source")
    if not isinstance(source, str) or not source.strip():
        return
    await device.async_send_service(_INPUT_SOURCE_SID, {"name": source.strip()})




# 画面模式的可选值取自插件反编译（hdpartner）与真机推送；
# 写入路径见文件头注释：screen.mode {"mode": ...} 已确认可生效。
_PICTURE_MODES: tuple[str, ...] = (
    "STANDARD",
    "PICTURE",
    "VIVID",
    "SPORTS",
    "GAME",
    "USER",
    "SOFT_LIGHT",
)


async def _select_picture_mode(device: DeviceContext, data: Mapping[str, Any]) -> None:
    option = data.get("option")
    if not isinstance(option, str) or option.strip() not in _PICTURE_MODES:
        return
    await device.async_send_service(_SCREEN_SID, {"mode": option.strip()})


def _child_mode_switch() -> EntitySpec:
    """儿童模式开关（childMode.mode）。

    真机验证：下发 ``{"mode": "ON"}`` 时云端返回 ``errcode=-1``（看起来是
    "命令被拒"），但电视**随后真的回推了 mode=ON**，reported_timestamp 由
    20261021T044645Z 更新到 20261021T074121Z —— 命令实际生效，只是返回码
    不准（异步命令的假拒绝）。``{"mode": "OFF"}`` 同理。

    因此这里按"命令实际生效"处理。副作用：HA 调用时会看到一条命令被拒的
    报错日志，但状态最终由电视推送更新过来。
    """
    def state(device: DeviceContext) -> Mapping[str, Any]:
        raw = _value(device, _CHILD_MODE_SID, "mode")
        if isinstance(raw, str):
            u = raw.strip().upper()
            if u in ("ON", "LOCK_ON", "TRUE", "1"):
                return {"is_on": True}
            if u in ("OFF", "FALSE", "0"):
                return {"is_on": False}
        return {"is_on": None}

    async def turn_on(device, data):
        await device.async_send_service(_CHILD_MODE_SID, {"mode": "ON"})

    async def turn_off(device, data):
        await device.async_send_service(_CHILD_MODE_SID, {"mode": "OFF"})

    return EntitySpec(
        platform="switch",
        key="child_mode_switch",
        name="儿童模式",
        state=state,
        actions={"turn_on": turn_on, "turn_off": turn_off},
    )


def _picture_mode_select() -> EntitySpec:
    """画面模式（screen.mode），真机验证可写。

    验证记录（两次独立下发，云端 ``screen.mode`` 与 ``reported_timestamp``
    都真的变了，不是乐观更新）：

        PICTURE  (ts 20261021T044645Z)
          -> 下发 {"mode": "STANDARD"}
        STANDARD (ts 20261021T051455Z)
          -> 下发 {"mode": "VIVID"}
        VIVID    (ts 20261021T051755Z)

    注意：状态回推有约 5-10 分钟延迟，下发后不要立刻断言失败。
    """
    def state(device: DeviceContext) -> Mapping[str, Any]:
        raw = _value(device, _SCREEN_SID, "mode")
        if isinstance(raw, str) and raw.strip() in _PICTURE_MODES:
            return {"current_option": raw.strip()}
        return {"current_option": None}
    return EntitySpec(
        platform="select",
        key="picture_mode",
        name="画面模式",
        state=state,
        metadata={"options": _PICTURE_MODES},
        actions={"select_option": _select_picture_mode},
    )


# 音效模式枚举取自插件反编译（hdpartner utils/p.smali，speaker 服务处理）；
# 括号里是该文件里与之配对的数字代码，说明这份清单是完整的。
_SOUND_MODES: tuple[str, ...] = (
    "AUTO",
    "STANDARD",
    "MUSIC",
    "MOVIE",
    "SPORTS",
    "NEWS",
    "GAME",
    "DEVIALET",
    "USER",
)


async def _select_sound_mode(device: DeviceContext, data: Mapping[str, Any]) -> None:
    option = data.get("option")
    if not isinstance(option, str) or option.strip() not in _SOUND_MODES:
        return
    await device.async_send_service(_SPEAKER_SID, {"equalizer": option.strip()})


def _sound_mode_select() -> EntitySpec:
    """音效模式（speaker.equalizer）。

    文件头把 ``speaker.equalizer {"equalizer": ...}`` 列为已确认命令。
    这里把它做成可选列表，替代原先只能看的 text sensor。

    验证记录（云端 ``speaker.equalizer`` 与 ``reported_timestamp`` 都变了）：

        AUTO  (ts 20261021T050300Z)
          -> 下发 {"equalizer": "MUSIC"}
        MUSIC (ts 20261021T054435Z)
    """
    def state(device: DeviceContext) -> Mapping[str, Any]:
        raw = _value(device, _SPEAKER_SID, "equalizer")
        if isinstance(raw, str) and raw.strip() in _SOUND_MODES:
            return {"current_option": raw.strip()}
        return {"current_option": None}
    return EntitySpec(
        platform="select",
        key="sound_mode",
        name="音效模式",
        state=state,
        metadata={"options": _SOUND_MODES},
        actions={"select_option": _select_sound_mode},
    )
def _text_sensor(sid: str, field_name: str, name: str, key: str) -> EntitySpec:
    def state(device: DeviceContext) -> Mapping[str, Any]:
        value = _value(device, sid, field_name)
        if isinstance(value, str) and not value.strip():
            return {"native_value": None}
        return {"native_value": value}

    return EntitySpec(
        platform="sensor",
        key=key,
        name=name,
        state=state,
        metadata={"entity_category": "diagnostic"},
    )


def _enum_sensor(
    profile: Mapping[str, Any],
    sid: str,
    field_name: str,
    name: str,
    key: str,
) -> EntitySpec | None:
    field = _field(profile, sid, field_name)
    if field is None:
        return None
    labels = {
        str(option["enumVal"]): str(option.get("descCh") or option.get("descEn"))
        for option in field.get("enumList", ())
        if isinstance(option, Mapping)
        and option.get("enumVal") is not None
        and (option.get("descCh") or option.get("descEn"))
    }

    def state(device: DeviceContext) -> Mapping[str, Any]:
        value = _value(device, sid, field_name)
        if value is None:
            return {"native_value": None}
        if labels:
            return {"native_value": labels.get(str(value), str(value))}
        return {"native_value": value}

    return EntitySpec(
        platform="sensor",
        key=key,
        name=name,
        state=state,
        metadata={"entity_category": "diagnostic"},
    )


def _brightness_sensor() -> EntitySpec:
    def state(device: DeviceContext) -> Mapping[str, Any]:
        return {"native_value": _number(_value(device, _SCREEN_SID, "brightness"))}

    return EntitySpec(
        platform="sensor",
        key="screen_brightness",
        name="屏幕亮度",
        state=state,
        metadata={
            "state_class": "measurement",
            "native_unit_of_measurement": "%",
            "entity_category": "diagnostic",
        },
    )


def _mute_binary_sensor() -> EntitySpec:
    def state(device: DeviceContext) -> Mapping[str, Any]:
        return {"is_on": _bool(_value(device, _SPEAKER_SID, "mute"))}

    return EntitySpec(
        platform="binary_sensor",
        key="muted",
        name="静音",
        state=state,
        metadata={"device_class": "sound", "entity_category": "diagnostic"},
    )




def _child_mode_binary_sensor() -> EntitySpec:
    """儿童模式开关（childMode.mode）。

    电视实时推送 ``childMode`` 服务，``mode`` 为 ``"ON"`` / ``"OFF"`` 字符串。
    这里只做**只读**展示 —— 写入路径尚未在真机验证，故不暴露控制。
    """
    def state(device: DeviceContext) -> Mapping[str, Any]:
        raw = _value(device, _CHILD_MODE_SID, "mode")
        if isinstance(raw, str):
            lowered = raw.strip().upper()
            if lowered in ("ON", "TRUE", "1"):
                return {"is_on": True}
            if lowered in ("OFF", "FALSE", "0"):
                return {"is_on": False}
        return {"is_on": None}
    return EntitySpec(
        platform="binary_sensor",
        key="child_mode",
        name="儿童模式",
        state=state,
        metadata={"entity_category": "diagnostic"},
    )


class ProductV0A2Adapter:
    """Keep all V0A2 entity and command choices in this file."""

    prod_id = "V0A2"

    def entities(self, context: DeviceContext) -> tuple[EntitySpec, ...]:
        profile = context.profile
        if profile is None:
            return ()
        entities: list[EntitySpec] = []

        # --- media_player: power, volume and input source -------------------
        if context.has_service(_SCREEN_SID) or context.has_service(_DEVICE_STATE_SID):
            actions: dict[str, Any] = {}
            if context.has_service(_SCREEN_SID):
                actions["turn_on"] = _turn_on
                actions["turn_off"] = _turn_off
            if context.has_service(_SPEAKER_SID):
                actions["volume"] = _set_volume
                actions["mute"] = _set_mute
            if context.has_service(_INPUT_SOURCE_SID):
                actions["select_source"] = _select_source

            entities.append(
                EntitySpec(
                    platform="media_player",
                    key="tv",
                    name=None,
                    state=_tv_state,
                    actions=actions,
                )
            )

        # --- sensors --------------------------------------------------------
        if context.has_service(_SCREEN_SID):
            entities.append(_brightness_sensor())
            entities.append(_picture_mode_select())
        if context.has_service(_SPEAKER_SID):
            entities.append(_mute_binary_sensor())
            if context.has_service(_CHILD_MODE_SID):
                entities.append(_child_mode_binary_sensor())
                entities.append(_child_mode_switch())
            entities.append(_sound_mode_select())
        if context.has_service(_INPUT_SOURCE_SID):
            entities.append(
                _text_sensor(_INPUT_SOURCE_SID, "name", "输入源", "input_source")
            )

        screen_state = _enum_sensor(
            profile, _DEVICE_STATE_SID, "screenState", "屏幕状态", "screen_state"
        )
        if screen_state is not None:
            entities.append(screen_state)

        remote = _enum_sensor(
            profile, _REMOTE_CONTROL_SID, "switchState", "远程控制", "remote_control"
        )
        if remote is not None:
            entities.append(remote)

        if _service(profile, _MESSAGE_BOARD_SID) is not None:
            entities.append(
                _text_sensor(_MESSAGE_BOARD_SID, "sender", "最近留言人", "message_sender")
            )
            entities.append(
                _text_sensor(_MESSAGE_BOARD_SID, "message", "最近留言", "message_text")
            )
            entities.append(
                _text_sensor(_MESSAGE_BOARD_SID, "reply", "最近留言回复", "message_reply")
            )
        return tuple(entities)


ADAPTER = ProductV0A2Adapter()
