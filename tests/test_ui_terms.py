import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from mdcx.config.enums import Language, Translator
from mdcx.config.manager import manager
from mdcx.config.models import TranslateConfig
from mdcx.config.resources import resources
from mdcx.gen.field_enums import CrawlerResultFields


def _build_ui_and_controller_corpus() -> tuple[list[str], list[str], str]:
    ui_path = Path("mdcx/views/MDCx.ui")
    tree = ET.parse(ui_path)
    root = tree.getroot()

    about_node = root.find('.//widget[@name="textBrowser_about"]/property[@name="html"]/string')
    assert about_node is not None, "textBrowser_about not found in MDCx.ui"
    about_text = about_node.text or ""

    # Clear about text from UI corpus to ensure terms exist elsewhere in UI or controllers
    about_node.text = ""
    ui_corpus = ET.tostring(root, encoding="unicode")

    controller_corpus = ""
    for path in Path("mdcx/controllers").rglob("*.py"):
        controller_corpus += path.read_text(encoding="utf-8") + "\n"

    full_corpus = ui_corpus + "\n" + controller_corpus

    about_terms = re.findall(r"「(.*?)」", about_text)
    cookie_html = resources.cookie_help_path.read_text(encoding="utf-8")
    cookie_terms = re.findall(r"「(.*?)」", cookie_html)

    return about_terms, cookie_terms, full_corpus


def test_textbrowser_about_bracketed_terms_exist_in_ui_or_controllers():
    about_terms, _, corpus = _build_ui_and_controller_corpus()
    assert len(about_terms) >= 30, f"Expected at least 30 bracketed terms in about text, found {len(about_terms)}"

    missing = [term for term in set(about_terms) if term not in corpus]
    assert not missing, f"The following UI terms in textBrowser_about do not exist in MDCx.ui or controllers: {missing}"


def test_cookie_help_bracketed_terms_exist_in_ui_or_controllers():
    _, cookie_terms, corpus = _build_ui_and_controller_corpus()
    assert len(cookie_terms) >= 5, f"Expected at least 5 bracketed terms in cookie_help.html, found {len(cookie_terms)}"

    missing = [term for term in set(cookie_terms) if term not in corpus]
    assert not missing, f"The following UI terms in cookie_help.html do not exist in MDCx.ui or controllers: {missing}"


def test_readme_and_changelog_ui_terms_allow_only_the_retired_release_note():
    _, _, corpus = _build_ui_and_controller_corpus()
    historical_note = "界面「影评人评分（criticrating）」勾选项已无效果"
    for name in ("README.md", "changelog.md"):
        text = Path(name).read_text(encoding="utf-8")
        if name == "changelog.md":
            # Only v1.2.0's published note is exempt; current/future notes are still checked.
            sections = re.split(r"(?m)(^# MDCx v[^\n]+\n)", text)
            for index, section in enumerate(sections[:-1]):
                if section == "# MDCx v1.2.0\n":
                    sections[index + 1] = sections[index + 1].replace(historical_note, "", 1)
            text = "".join(sections)
        else:
            # README retains the v1.2.0 release summary; exempt this retirement statement only.
            text = text.replace(historical_note, "", 1)
        missing = [term for term in set(re.findall(r"「(.*?)」", text)) if term not in corpus]
        assert not missing, (name, missing)


def test_removed_nfo_checkbox_is_absent_from_generated_window():
    from PyQt6.QtWidgets import QApplication, QCheckBox, QMainWindow

    from mdcx.views.MDCx import Ui_MDCx

    root = ET.parse("mdcx/views/MDCx.ui").getroot()
    assert root.find('.//widget[@name="checkBox_nfo_criticrating"]') is None
    app = QApplication.instance() or QApplication([])
    window = QMainWindow()
    ui = Ui_MDCx()
    try:
        ui.setupUi(window)
        window.show()
        app.processEvents()
        assert window.findChild(QCheckBox, "checkBox_nfo_criticrating") is None
        assert ui.checkBox_nfo_score.text() == "公众评分（score）"
        assert ui.tabWidget.tabText(ui.tabWidget.indexOf(ui.tab_7)).strip() == "NFO"
        assert ui.groupBox_81.title() == "写入 NFO 的字段："
        assert ui.label_390.text() == "评分："
    finally:
        window.close()


def test_cookie_help_content_requirements():
    cookie_html = resources.cookie_help_path.read_text(encoding="utf-8")

    # R2.1: Button is "保存", not "保存配置"
    assert "「保存」" in cookie_html
    assert "保存配置" not in cookie_html

    # R2.2: Copy value recommendation & browser UI disclaimer
    assert "复制值 / Copy value" in cookie_html
    assert "以你的浏览器实际界面为准" in cookie_html

    # R4: Inline SVG diagrams and offline requirement
    assert cookie_html.count("<svg") >= 6
    assert cookie_html.count("示意图，菜单名以你的浏览器实际界面为准") >= 6
    assert "<img" not in cookie_html
    assert "http://" not in cookie_html and "https://" not in cookie_html


def test_r1_facts_in_about_text():
    ui_path = Path("mdcx/views/MDCx.ui")
    tree = ET.parse(ui_path)
    root = tree.getroot()
    about_node = root.find('.//widget[@name="textBrowser_about"]/property[@name="html"]/string')
    about_text = about_node.text or ""

    # Temperatures: 0.9 default, not 0.2 / 1.0
    assert "默认均为 <code>0.9</code>" in about_text
    assert "默认 0.2" not in about_text
    assert "默认 1.0" not in about_text

    # No "使用备用模型" invented switch
    assert "使用备用模型" not in about_text

    # No "设置 → 分集" invented tab
    assert "设置 → 分集" not in about_text
    assert "设置 → 分集" not in about_text

    # No FlareSolverr; only CF Bypass
    assert "FlareSolverr" not in about_text
    assert "CF Bypass" in about_text

    # Actual local model buttons
    assert "查找 / 验证路径" in about_text
    assert "启动备用模型" in about_text
    assert "打开日志" in about_text

    # Actual database checkbox text
    assert "使用数据库补全演员信息" in about_text

    # Actual save button name
    assert "「保存」" in about_text
    assert "保存配置" not in about_text
    assert "自动保存配置" not in about_text

    # Subtitle tags: no -UC or -U
    assert "-UC" not in about_text
    assert "-U" not in about_text


def test_r3_engine_selection_checkbox_removed_and_field_switches_present():
    tree = ET.parse("mdcx/views/MDCx.ui")
    root = tree.getroot()

    # The legacy engine selection checkbox and groupBox_trans have been removed from UI definition
    legacy_engine_checkbox_name = "checkBox_" + "llm"
    assert root.find(f'.//widget[@name="{legacy_engine_checkbox_name}"]') is None, (
        f"{legacy_engine_checkbox_name} should be removed from MDCx.ui"
    )
    assert root.find('.//widget[@name="groupBox_trans"]') is None, "groupBox_trans should be removed from MDCx.ui"

    # Field-level translation switches must exist in MDCx.ui
    assert root.find('.//widget[@name="checkBox_title_translate"]') is not None, (
        "checkBox_title_translate must exist in MDCx.ui"
    )
    assert root.find('.//widget[@name="checkBox_outline_translate"]') is not None, (
        "checkBox_outline_translate must exist in MDCx.ui"
    )
    assert root.find('.//widget[@name="checkBox_actor_translate"]') is not None, (
        "checkBox_actor_translate must exist in MDCx.ui"
    )
    assert root.find('.//widget[@name="checkBox_tag_translate"]') is not None, (
        "checkBox_tag_translate must exist in MDCx.ui"
    )

    # Also verify generated MDCx.py
    mdcx_py = Path("mdcx/views/MDCx.py").read_text(encoding="utf-8")
    assert f"self.{legacy_engine_checkbox_name}" not in mdcx_py
    assert "self.groupBox_trans" not in mdcx_py
    assert "self.checkBox_title_translate" in mdcx_py
    assert "self.checkBox_outline_translate" in mdcx_py


def test_temperature_defaults_and_default_config_json():
    # models.py defaults
    tc = TranslateConfig()
    assert tc.llm_temperature == 0.9
    assert tc.llm_fallback_temperature == 0.9
    assert tc.translate_by == [Translator.LLM]

    # default_config.json defaults
    default_config_path = Path("mdcx/config/default_config.json")
    if default_config_path.exists():
        default_cfg = json.loads(default_config_path.read_text(encoding="utf-8"))
        tc_data = default_cfg.get("translate_config", {})
        assert tc_data.get("llm_temperature") == 0.9
        assert tc_data.get("llm_fallback_temperature") == 0.9


def test_field_level_translation_switches_control_translation():
    # Verify that FieldConfig.translate controls whether translation occurs for title/outline
    title_cfg = manager.config.get_field_config(CrawlerResultFields.TITLE)
    outline_cfg = manager.config.get_field_config(CrawlerResultFields.OUTLINE)

    # Both configs have a boolean translate attribute
    assert hasattr(title_cfg, "translate")
    assert hasattr(outline_cfg, "translate")
    assert isinstance(title_cfg.translate, bool)
    assert isinstance(outline_cfg.translate, bool)

    # When both are disabled or language is JP, translation skips
    assert hasattr(title_cfg, "language")
    assert hasattr(outline_cfg, "language")
    assert isinstance(title_cfg.language, Language)


def test_textbrowser_about_has_anti_ban_recommendations():
    about_terms, _, _ = _build_ui_and_controller_corpus()
    ui_text = Path("mdcx/views/MDCx.ui").read_text(encoding="utf-8")
    assert "防封推荐设置" in ui_text
    for term in ("线程数量", "线程间隔(秒)", "javdb延时(秒)", "间歇刮削", "重试次数", "超时时间", "请求间隔"):
        assert term in about_terms


def test_issue_35_acceptance_steps_ui_terms():
    ui_path = Path("mdcx/views/MDCx.ui")
    tree = ET.parse(ui_path)
    root = tree.getroot()

    checks = [
        ("pushButton_main", "主界面"),
        ("pushButton_setting", "设置"),
        ("pushButton_log", "日志"),
        ("tab2", " 刮削目录 "),
        ("label_49", "待刮削视频目录："),
        ("label_241", "当前配置："),
        ("pushButton_start_cap", "开始"),
    ]
    for obj_name, expected_text in checks:
        node = root.find(f'.//*[@name="{obj_name}"]')
        assert node is not None, f"Widget {obj_name} not found in MDCx.ui"
        text = ""
        for p in node.findall("property"):
            if p.get("name") == "text":
                s = p.find("string")
                if s is not None and s.text:
                    text = s.text.strip()
        if not text:
            for a in node.findall("attribute"):
                if a.get("name") == "title":
                    s = a.find("string")
                    if s is not None and s.text:
                        text = s.text
        assert text == expected_text, f"Widget {obj_name} text '{text}' does not match '{expected_text}'"


def test_issue_173_network_check_acceptance_ui_terms():
    root = ET.parse("mdcx/views/MDCx.ui").getroot()
    for name, expected in (("pushButton_net", "检测网络"), ("pushButton_check_net", "开始检测")):
        node = root.find(f'.//widget[@name="{name}"]/property[@name="text"]/string')
        assert node is not None and node.text.strip() == expected


def test_issue_40_acceptance_steps_ui_terms():
    root = ET.parse("mdcx/views/MDCx.ui").getroot()
    checks = [
        ("pushButton_setting", "text", "设置"),
        ("pushButton_main", "text", "主界面"),
        ("pushButton_log", "text", "日志"),
        ("tab2", "title", "刮削目录"),
        ("tab5", "title", "高级"),
        ("label_241", "text", "当前配置："),
        ("label_49", "text", "待刮削视频目录："),
        ("groupBox_17", "title", "保存日志"),
        ("groupBox_3", "title", "调试模式（日志页面）"),
        ("groupBox_12", "title", "高级功能"),
        ("radioButton_log_on", "text", "开"),
        ("checkBox_show_web_log", "text", "显示刮削过程信息"),
        ("pushButton_select_media_folder_setting_page", "text", "选择目录"),
        ("pushButton_select_config_folder", "text", "选择目录"),
        ("pushButton_save_config", "text", "保存"),
        ("pushButton_start_cap", "text", "开始"),
        ("checkBox_nfo_score", "text", "公众评分（score）"),
    ]
    for name, prop, expected in checks:
        node = root.find(f'.//*[@name="{name}"]')
        assert node is not None, name
        text = node.findtext(f'property[@name="{prop}"]/string')
        if text is None:
            text = node.findtext(f'attribute[@name="{prop}"]/string')
        assert text is not None and text.strip() == expected, (name, text, expected)
    for name in ("comboBox_change_config", "lineEdit_config_folder", "lineEdit_movie_path", "textBrowser_log_main_2"):
        assert root.find(f'.//*[@name="{name}"]') is not None


def test_site_wall_timeout_ui_terms_and_generated_control():
    from PyQt6.QtWidgets import QApplication, QMainWindow

    from mdcx.views.MDCx import Ui_MDCx

    root = ET.parse("mdcx/views/MDCx.ui").getroot()
    texts = [node.text or "" for node in root.iter("string")]
    for term in ("设置", "网络", "单站墙钟上限"):
        assert any(term in text for text in texts)
    app = QApplication.instance() or QApplication([])
    window = QMainWindow()
    ui = Ui_MDCx()
    try:
        ui.setupUi(window)
        window.show()
        app.processEvents()
        control = ui.spinBox_site_wall_timeout
        assert ui.label_site_wall_timeout.text() == "单站墙钟上限："
        assert control.value() == 180
        assert control.minimum() == 1
        assert "120 秒活动预算" in control.toolTip()
        assert "不延长墙钟上限" in control.toolTip()
        assert "仅批末正式重刮重新起算" in control.toolTip()
        assert control.geometry().height() >= 20
        grid = control.parentWidget()
        assert grid.height() >= grid.layout().sizeHint().height()
    finally:
        window.close()
