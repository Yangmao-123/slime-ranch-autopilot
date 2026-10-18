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
