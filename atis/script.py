"""把（席位 + 预设 + METAR）渲染成一份通播稿。

纯函数：不碰 Qt、不碰网络、不读任何全局状态，所以能直接拿来测——和管制端
`radiostack.py` 是同一个路子。

**为什么要单独拎出来。** 这段逻辑原来在 `gui.py` 里出现了两遍：一遍给右侧
预览（`regenerate`），一遍给真正推出去的稿子（`render_for`）。两份代码连注释
都是抄的，而它们一旦改岔，**界面上看到的和播出去的就不是同一份**——这种错在
界面上完全看不出来，只有听的人知道。

一份稿子有两种形态，`template.render` 一次给全：

    text   文字通播，照抄电码（09004MPS），给人看、也发给 FSD
    voice  语音稿，念得出来（wind zero niner zero at four meters per second）

中文稿不是英文的翻译，是 `chinese.py` 从同一份 METAR 独立渲染的——语序和数字
读法都不一样，详见那个模块。
"""

import chinese
import profile as profile_module
import template as template_module
from profile import TYPE_SUFFIX


def build_context(station, preset, metar):
    """组装模板变量。两处渲染共用这一份，免得改岔。"""
    return template_module.build_context(
        metar, station.identifier, station.letter,
        preset.airport_conditions, preset.notams, preset.transition_level,
        # 语音念机场全名：念 "Z S P D" 听着像在拼写，真实通播念的是
        # "Shanghai Pudong International Airport"。席位上没填名称才退回代码。
        facility_voice=station.name or station.identifier,
        # 收尾语跟着预设：不同构型要交代的事不一样。留空用内置那句。
        closing=preset.closing or None)


def voice_for(station, preset, metar, english):
    """按席位的 voice_language 决定语音稿用哪种语言。

    双语时中文在后：中文飞行员听得懂英文的居多，反过来不一定。
    """
    language = getattr(station, "voice_language", profile_module.LANGUAGE_ENGLISH)
    if language == profile_module.LANGUAGE_ENGLISH:
        return english

    # 跑道优先取当前预设的：切到"北向"时英文稿的 ARR RWY 会变，中文稿要是还
    # 念着南向的跑道，同一份通播里两种语言互相矛盾。预设没填才回退到席位上那个。
    runway = getattr(preset, "chinese_runway", "") or station.chinese_runway
    script = chinese.render(
        metar,
        facility=station.chinese_name or station.identifier,
        letter=station.letter,
        runway=runway,
        # 跑道构型、放行频率、应答机这些在中文侧没有对应字段，整段由预设提供
        extra=getattr(preset, "chinese_extra", "") or "")
    if language == profile_module.LANGUAGE_CHINESE:
        return script
    return f"{english} {script}"


def render(station, preset, metar):
    """渲染一份稿子，返回 (文字通播, 语音稿)。

    手动预设不需要天气，只返回用户在主页面编辑的两份稿子。自动预设在席位、
    预设、天气缺任何一样时返回 None——调用方据此显示"还没有天气数据"或者
    干脆不推送，而不是推一份缺了半截的稿子出去。
    """
    if preset is None:
        return None
    if getattr(preset, "manual_text", False):
        return preset.manual_atis, preset.manual_voice
    if station is None or metar is None:
        return None
    context = build_context(station, preset, metar)
    text, voice = template_module.render(preset.template, context,
                                         station.contractions)
    return text, voice_for(station, preset, metar, voice)


def unknown_variables(preset):
    """模板里认不出的变量名，界面用来提示拼错。"""
    return template_module.unknown_variables(preset.template) if preset else []


def summary(station, metar=None, broadcasting=False):
    """席位列表里的一行，只放值班时真正要扫的四样：

        ● ZSPD    J  09004MPS  Q1013
          机场    代码  风      修压

    频率和呼号后缀不放——频率在席位信息里，而一眼要看的是"哪个场、现在第几
    份、风怎么样、压力多少"。综合以外的类型（离场/进场）在机场码后面补一个
    字母，否则同一个机场的两份通播长得一模一样。
    """
    parts = [station.identifier]
    suffix = TYPE_SUFFIX.get(station.atis_type, "_ATIS")
    marker = suffix.strip("_").replace("ATIS", "").strip("_")
    if marker:
        parts[0] += f" {marker}"
    parts.append(station.letter)

    if metar is not None:
        for element in (metar.wind, metar.pressure):
            text = getattr(element, "text", "")
            if text:
                parts.append(text)

    text = "  ".join(parts)
    return f"● {text}" if broadcasting else text
