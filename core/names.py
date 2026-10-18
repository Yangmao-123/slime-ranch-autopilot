# -*- coding: utf-8 -*-
"""共享的显示名映射（main_controller 与插件壳共用，避免壳导入重量级 core 模块）。"""

SLIME_NAMES = {
    "PINK_SLIME": "粉色史莱姆", "ROCK_SLIME": "岩石史莱姆", "TABBY_SLIME": "虎斑史莱姆",
    "PHOSPHOR_SLIME": "荧光史莱姆", "RAD_SLIME": "辐射史莱姆", "BOOM_SLIME": "爆炸史莱姆",
    "CRYSTAL_SLIME": "结晶史莱姆", "HONEY_SLIME": "蜂蜜史莱姆", "HUNTER_SLIME": "猎猫史莱姆",
    "PUDDLE_SLIME": "水洼史莱姆", "QUANTUM_SLIME": "量子史莱姆", "DERVISH_SLIME": "旋风史莱姆",
    "MOSAIC_SLIME": "马赛克史莱姆", "TANGLE_SLIME": "藤蔓史莱姆", "FIRE_SLIME": "火焰史莱姆",
    "SABER_SLIME": "剑齿史莱姆",
    "GOLD_SLIME": "金史莱姆", "LUCKY_SLIME": "幸运史莱姆", "QUICKSILVER_SLIME": "水银史莱姆",
}

RARE_SLIMES = {"GOLD_SLIME", "LUCKY_SLIME", "QUICKSILVER_SLIME"}

PLORT_NAMES = {
    "PINK_PLORT": "粉色结晶", "ROCK_PLORT": "岩石结晶", "TABBY_PLORT": "虎斑结晶",
    "PHOSPHOR_PLORT": "荧光结晶", "RAD_PLORT": "辐射结晶", "BOOM_PLORT": "爆炸结晶",
    "CRYSTAL_PLORT": "结晶史莱姆结晶", "HONEY_PLORT": "蜂蜜结晶", "HUNTER_PLORT": "猎猫结晶",
    "PUDDLE_PLORT": "水洼结晶", "QUANTUM_PLORT": "量子结晶", "DERVISH_PLORT": "旋风结晶",
    "MOSAIC_PLORT": "马赛克结晶", "TANGLE_PLORT": "藤蔓结晶", "FIRE_PLORT": "火焰结晶",
    "SABER_PLORT": "剑齿结晶",
}

FOOD_NAMES = {
    "CARROT_VEGGIE": "胡萝卜", "POGO_FRUIT": "波戈果", "BRIAR_HEN": "荆棘鸡",
    "STONE_HEN": "石鸡", "PAINTED_HEN": "彩绘鸡", "OCA_OCA": "奥卡奥卡",
    "MINT_MANGO": "薄荷芒果", "JUNEALE": "六月莓", "CUBERRY": "立方莓",
    "PRICKLY_PEAR": "仙人掌果", "PYRAMID_PLORT": "", "HEART_BEET": "心形甜菜",
}


def display_name(item_id: str) -> str:
    """史莱姆/结晶/食物 Id 转中文名；未知原样返回。"""
    return SLIME_NAMES.get(item_id) or PLORT_NAMES.get(item_id) or FOOD_NAMES.get(item_id) or item_id


_ALL_WORDS = ("", "全部", "所有", "全部结晶", "所有结晶", "全都", "all")


def plort_id_from_text(text: str):
    """玩家售卖指令文字 → 结晶 Id。

    返回：""=全部结晶；具体 Id 字符串（如 "PINK_PLORT"）；None=没听懂。
    识别：中文名/去"结晶"短名/前缀/包含（"卖粉色结晶"整句也能解析出粉色）/
    英文 Id 或短名（pink → PINK_PLORT）。
    """
    t = (text or "").strip()
    if t in _ALL_WORDS:
        return ""
    up = t.upper().replace(" ", "_").replace("结晶", "_PLORT")
    if up in PLORT_NAMES:
        return up
    if up + "_PLORT" in PLORT_NAMES:
        return up + "_PLORT"
    # 中文全名 / 去掉"结晶"的短名
    for pid, cn in PLORT_NAMES.items():
        if t == cn or t == cn.replace("结晶", ""):
            return pid
    # 前缀唯一命中（粉色/岩石/虎斑…）；整句包含（"卖粉色结晶"）
    starts = [pid for pid, cn in PLORT_NAMES.items()
              if cn.startswith(t) or cn.replace("结晶", "").startswith(t)]
    if len(starts) == 1:
        return starts[0]
    contains = [pid for pid, cn in PLORT_NAMES.items()
                if t and (t in cn or cn in t or cn.replace("结晶", "") in t)]
    if len(contains) == 1:
        return contains[0]
    return None
