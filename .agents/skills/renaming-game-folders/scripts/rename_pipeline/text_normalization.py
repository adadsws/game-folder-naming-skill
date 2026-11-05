"""名称输出、搜索与契约校验共用的纯格式规范化。"""

from __future__ import annotations

import re
import unicodedata

# 兼容旧调用方导出的常见审查/缺字代替符号集合。
SEARCH_REPLACEMENT_SYMBOLS = frozenset("〇○◯●□■◇◆☆★")

# 只处理明确的语言、版本、翻译和发行特典说明；作品副标题与
# 普通括注不在此范围。仅在标题首尾删除完整说明块，避免误伤正文。
_LANGUAGE_TOKEN = (
    r"(?:日文|日语|日語|日本語|Japanese|JP|"
    r"中文|简体中文|簡體中文|繁体中文|繁體中文|"
    r"简中|簡中|繁中|Chinese|CN|英文|英语|英語|English|EN)"
)
_LANGUAGE_SET = rf"(?:{_LANGUAGE_TOKEN})(?:\s*[/+&・·、, ]\s*{_LANGUAGE_TOKEN})*"
_LANGUAGE_COMPACT_SET = r"(?:[中日英韩韓]{2,}(?:文|語|语)?)"
_VERSION_QUALIFIER = r"(?:v(?:er(?:sion)?)?\.?\s*\d[0-9A-Za-z._-]*|版本\s*\d[0-9A-Za-z._-]*)"
_TRANSLATION_QUALIFIER = (
    r"(?:官中|官方中文|汉化|漢化|翻译|翻譯|机翻|機翻|AI\s*(?:汉化|漢化))"
)
_BONUS_QUALIFIER = (
    r"(?:(?:附带|附帶|同梱|包含)?\s*"
    r"(?:DLsite|Steam|VNDB)?\s*(?:专属|專屬|限定)?\s*"
    r"(?:特典|特典版))"
)
_MARKETING_QUALIFIER = (
    r"(?:エロステータス[^】\]]*|情色状态[^】\]]*|"
    r"VR\s*[／/]\s*非VR\s*(?:両対応|兼容)|"
    r"多(?:语|言語|言语)言?(?:版)?|多言語版)"
)
_LANGUAGE_EDITION_QUALIFIER_BODY = (
    r"(?:"
    r"[【\[(（]\s*(?:"
    + _LANGUAGE_SET + r"|" + _LANGUAGE_COMPACT_SET + r"|"
    + _VERSION_QUALIFIER + r"|" + _TRANSLATION_QUALIFIER + r"|"
    + _BONUS_QUALIFIER + r"|" + _MARKETING_QUALIFIER
    + r")\s*(?:原版|版|版本|original|ver(?:sion)?\.?)?\s*[】\])）]"
    r"|"
    + _LANGUAGE_SET
    + r"\s*(?:原版|版|版本|original|ver(?:sion)?\.?)"
    r"|"
    + _VERSION_QUALIFIER
    + r")"
)
LANGUAGE_EDITION_QUALIFIER_RE = re.compile(
    r"(?ix)(?:^\s*(?:" + _LANGUAGE_EDITION_QUALIFIER_BODY
    + r")\s*|\s*(?:" + _LANGUAGE_EDITION_QUALIFIER_BODY + r")\s*$)"
)


def strip_search_replacement_symbols(value: str) -> str:
    """删除搜索词中的代替符号，并把由此产生的空白压成一个空格。"""
    without_symbols = "".join(
        character for character in value
        if character not in SEARCH_REPLACEMENT_SYMBOLS
    )
    return re.sub(r"\s+", " ", without_symbols).strip()


def sanitize_search_query(value: str) -> str:
    """只保留 Unicode 字母、组合标记和数字，其余字符统一为空格。"""
    sanitized = "".join(
        character
        if (character not in SEARCH_REPLACEMENT_SYMBOLS
            and unicodedata.category(character)[:1] in {"L", "M", "N"})
        else " "
        for character in value
    )
    return re.sub(r"\s+", " ", sanitized).strip()


def search_query_is_from_basename(query: str, basename: str) -> bool:
    """核实查询词的每个片段按原顺序逐字出现在当前游戏根原名中。"""
    chunks = (
        unicodedata.normalize("NFKC", sanitize_search_query(query))
        .casefold().split()
    )
    haystack = unicodedata.normalize("NFKC", basename).casefold()
    if not chunks:
        return False
    offset = 0
    for chunk in chunks:
        position = haystack.find(chunk, offset)
        if position < 0:
            return False
        offset = position + len(chunk)
    return True


def strip_language_edition_qualifiers(value: str | None) -> str | None:
    """删除标题首尾明确发行说明，保留作品自身括注。"""
    if not value:
        return value
    result = value.strip()
    while True:
        stripped = LANGUAGE_EDITION_QUALIFIER_RE.sub(" ", result)
        stripped = re.sub(r"\s+", " ", stripped).strip()
        if stripped == result:
            return stripped
        result = stripped


def title_identity_key(value: str | None) -> str:
    """比较跨语种标题是否实际同文，忽略格式化差异。"""
    if not value:
        return ""
    stripped = strip_language_edition_qualifiers(value) or ""
    normalized = unicodedata.normalize("NFKC", stripped).casefold()
    normalized = re.sub(r"^\s*=\s*", "", normalized)
    return re.sub(r"[^\w]+", "", normalized, flags=re.UNICODE)


def developer_identity_key(value: str | None) -> str:
    """制作方比较只统一宽度、大小写、空格及标点，保留原字形。"""
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)
