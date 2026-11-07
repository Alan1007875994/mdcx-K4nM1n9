import xml.etree.ElementTree as ET

import pytest

from mdcx.config.enums import CDChar, NfoInclude, SuffixSort
from mdcx.config.manager import manager
from mdcx.core.file import get_file_info_v2, get_output_name
from mdcx.core.naming import NameRenderOptions, NamingTarget, render_name
from mdcx.core.nfo import write_nfo
from mdcx.models.enums import FileMode
from mdcx.models.flags import Flags
from mdcx.models.types import CrawlersResult
from mdcx.number import get_file_number


@pytest.fixture(autouse=True)
def stage_config(monkeypatch):
    # 所有输入及输出均为测试临时文件，配置修改仅在内存中生效。
    values = {
        "umr_style": "-破解",
        "cnword_style": "-C",
        "cnword_char": ["-C.", "-C-", "ch.", "字幕"],
        "suffix_sort": [SuffixSort.MOWORD, SuffixSort.CNWORD, SuffixSort.DEFINITION],
        "cd_char": [CDChar.LETTER, CDChar.ENDC, CDChar.DIGITAL, CDChar.MIDDLE_NUMBER],
        "cd_name": 1,
        "prevent_char": "",
        "sub_type": [".srt"],
        "string": [],
        "no_escape": [],
        "main_mode": 1,
        "success_file_rename": True,
        "success_file_move": False,
        "soft_link": 0,
        "naming_file": "{{ number }}",
        "file_moword": True,
        "file_cnword": True,
        "file_hd": False,
        "file_name_max": 200,
        "subtitle_add": False,
    }
    for key, value in values.items():
        monkeypatch.setattr(manager.config, key, value)
    monkeypatch.setattr(Flags, "file_mode", FileMode.Default)


def result_for(info):
    data = CrawlersResult.empty()
    data.number = info.number
    data.title = "测试标题"
    data.mosaic = info.mosaic
    return data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("suffix", "expected", "subtitle", "cd"),
    [
        ("-LTX", "-破解-LTX", False, ""),
        ("-U-LTX", "-破解-LTX", False, ""),
        ("-UC", "-破解-C-LTX", True, ""),
        ("-U", "-破解", False, ""),
        ("-破解-L", "-破解-L", False, ""),
        ("-破解-CL", "-破解-CL", True, ""),
        ("-破解-LTX", "-破解-LTX", False, ""),
        ("-破解-C-LTX", "-破解-C-LTX", True, ""),
        ("-LTX-CD1", "-破解-LTX", False, "-CD1"),
        ("-UC-CD1", "-破解-C-LTX", True, "-CD1"),
        ("-破解-L-CD1", "-破解-L", False, "-CD1"),
        ("-破解-CL-CD2", "-破解-CL", True, "-CD2"),
        ("-破解-LTX-CD1", "-破解-LTX", False, "-CD1"),
        ("-破解-C-LTX-CD2", "-破解-C-LTX", True, "-CD2"),
        ("-ltx", "-破解-LTX", False, ""),
        ("-uc4k", "-破解-C-LTX", True, ""),
    ],
)
async def test_stage_output_and_rescrape_are_idempotent(tmp_path, suffix, expected, subtitle, cd):
    path = tmp_path / f"TEST-123{suffix}.mp4"
    info = await get_file_info_v2(path, copy_sub=False)
    assert get_file_number(str(path), []) == info.number == "TEST-123"
    assert info.destroyed == "-破解"
    assert info.mosaic == "无码破解"
    assert info.has_sub is subtitle
    assert info.c_word == ("-C" if subtitle else "")
    assert info.cd_part == cd
    assert info.crawl_task().destroyed == "-破解"
    assert info.crawl_task().mosaic == "无码破解"
    assert info.file_show_name == f"TEST-123{expected}{cd}"
    output = get_output_name(info, result_for(info), tmp_path, ".mp4")
    assert output[1].name == f"TEST-123{expected}{cd}.mp4"
    assert output[2].name == f"TEST-123{expected}{cd}.nfo"
    again = await get_file_info_v2(output[1], copy_sub=False)
    assert again.file_show_name == info.file_show_name
    assert again.cd_part == info.cd_part
    assert again.has_sub is subtitle
    assert get_output_name(again, result_for(again), tmp_path, ".mp4")[1:3] == output[1:3]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("suffix", "expected_cd", "sub"),
    [
        ("-L", "-CD12", False),
        ("-C", "-CD3", False),
        ("-CD1", "-CD1", False),
        ("-U-CD1", "-CD1", False),
    ],
)
async def test_unrelated_cd_rules_are_unchanged(tmp_path, suffix, expected_cd, sub):
    info = await get_file_info_v2(tmp_path / f"TEST-123{suffix}.mp4", copy_sub=False)
    assert info.cd_part == expected_cd
    assert info.has_sub is sub
    assert info.umr_stage == ""


@pytest.mark.asyncio
async def test_plain_c_subtitle_rule_and_letter_setting_remain_configurable(tmp_path, monkeypatch):
    monkeypatch.setattr(manager.config, "cd_char", [CDChar.LETTER])
    info = await get_file_info_v2(tmp_path / "TEST-123-C.mp4", copy_sub=False)
    assert info.cd_part == ""
    assert info.has_sub
    assert info.file_show_name == "TEST-123-C"
    monkeypatch.setattr(manager.config, "cd_char", [])
    info = await get_file_info_v2(tmp_path / "TEST-123-L.mp4", copy_sub=False)
    assert info.cd_part == ""
    assert info.destroyed == info.umr_stage == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        ("-LTX", "-修复-LTX"),
        ("-UC", "-修复-中字-LTX"),
        ("-修复-L", "-修复-L"),
        ("-修复-中字L", "-修复-中字L"),
        ("-修复-LTX", "-修复-LTX"),
        ("-修复-中字-LTX", "-修复-中字-LTX"),
    ],
)
async def test_custom_markers_and_cd_roundtrip(tmp_path, monkeypatch, suffix, expected):
    monkeypatch.setattr(manager.config, "umr_style", "-修复")
    monkeypatch.setattr(manager.config, "cnword_style", "-中字")
    info = await get_file_info_v2(tmp_path / f"TEST-123{suffix}-CD1.mp4", copy_sub=False)
    assert info.file_show_name == f"TEST-123{expected}-CD1"
    output = get_output_name(info, result_for(info), tmp_path, ".mp4")
    assert output[1].stem == info.file_show_name
    again = await get_file_info_v2(output[1], copy_sub=False)
    assert again.file_show_name == info.file_show_name
    assert again.cd_part == "-CD1"


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["-UHD", "-LTXextra", "-破解-LTXextra"])
async def test_stage_requires_a_complete_token(tmp_path, suffix):
    info = await get_file_info_v2(tmp_path / f"TEST-123{suffix}.mp4", copy_sub=False)
    assert info.umr_stage == ""


@pytest.mark.asyncio
async def test_ltx_number_prefix_is_not_a_stage_marker(tmp_path):
    info = await get_file_info_v2(tmp_path / "LTX-123.mp4", copy_sub=False)
    assert info.number == "LTX-123"
    assert info.umr_stage == info.destroyed == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("moword", "cnword", "expected"),
    [
        (True, True, "TEST-123-破解-C-LTX"),
        (True, False, "TEST-123-破解-LTX"),
        (False, True, "TEST-123-C"),
        (False, False, "TEST-123"),
    ],
)
async def test_display_switches_and_explicit_fields(tmp_path, monkeypatch, moword, cnword, expected):
    monkeypatch.setattr(manager.config, "file_moword", moword)
    monkeypatch.setattr(manager.config, "file_cnword", cnword)
    info = await get_file_info_v2(tmp_path / "TEST-123-UC.mp4", copy_sub=False)
    data = result_for(info)
    assert get_output_name(info, data, tmp_path, ".mp4")[1].stem == expected
    rendered = render_name("{{ moword }} {{ cnword }}", info, data, NameRenderOptions(target=NamingTarget.NFO_TITLE))
    assert rendered.text == "-破解-LTX -C"


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["-U", "-UC", "-破解-L", "-破解-CL", "-破解-LTX", "-破解-C-LTX"])
async def test_actual_nfo_keeps_tags_and_renders_explicit_version(tmp_path, monkeypatch, suffix):
    for key, value in {
        "download_files": [],
        "keep_files": [],
        "outline_format": [],
        "naming_media": "{{ number }} {{ moword }} {{ cnword }}",
        "nfo_include_new": [NfoInclude.TAG, NfoInclude.GENRE],
    }.items():
        monkeypatch.setattr(manager.config, key, value)
    info = await get_file_info_v2(tmp_path / f"TEST-123{suffix}.mp4", copy_sub=False)
    data = result_for(info)
    data.tags = ["无码破解"] + (["中文字幕"] if info.has_sub else [])
    nfo_path = get_output_name(info, data, tmp_path, ".mp4")[2]
    assert await write_nfo(info, data, nfo_path, tmp_path, update=True)
    root = ET.parse(nfo_path).getroot()
    assert {node.text for node in root.findall("./tag")} == set(data.tags)
    assert {node.text for node in root.findall("./genre")} == set(data.tags)
    title = root.findtext("title")
    assert title.startswith("TEST-123 -破解")
    if info.umr_stage:
        assert info.umr_stage in title


@pytest.mark.asyncio
async def test_local_subtitle_and_default_nfo_title(tmp_path, monkeypatch):
    path = tmp_path / "TEST-123-LTX.mp4"
    path.with_suffix(".srt").write_text("", encoding="utf-8")
    info = await get_file_info_v2(path, copy_sub=False)
    assert info.file_show_name == "TEST-123-破解-C-LTX"
    assert info.sub_list == [".srt"]
    for key, value in {
        "download_files": [],
        "keep_files": [],
        "outline_format": [],
        "naming_media": "{{ number }} {{ title }}",
        "nfo_include_new": [],
    }.items():
        monkeypatch.setattr(manager.config, key, value)
    nfo_path = path.with_suffix(".nfo")
    assert await write_nfo(info, result_for(info), nfo_path, tmp_path, update=True)
    assert ET.parse(nfo_path).getroot().findtext("title") == "TEST-123 测试标题"


def test_acceptance_ui_terms_match_source():
    # 验收入口来自本次源码，不把它当作 J 已安装版本的界面实测。
    from pathlib import Path

    root = ET.parse(Path("mdcx/views/MDCx.ui")).getroot()
    expected = {
        "pushButton_tool": "工具",
        "pushButton_select_file": "选择文件",
        "pushButton_start_single_file": "刮削",
    }
    for name, text in expected.items():
        assert root.find(f'.//widget[@name="{name}"]/property[@name="text"]/string').text.strip() == text
    assert root.find(
        './/widget[@name="page_tool"]//widget[@name="groupBox_7"]/property[@name="title"]/string'
    ).text.startswith("单文件刮削")
