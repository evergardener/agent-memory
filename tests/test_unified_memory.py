from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from agent_memory.repository import _procedure_query_terms, _temporal_query_terms
from agent_memory.unified_memory import (
    parse_date_range,
    parse_episode,
    parse_preference,
    parse_temporal_rule,
    procedure_applicability,
)

REFERENCE_TIME = datetime(2026, 7, 12, 8, 30, tzinfo=UTC)
SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_date_range_uses_event_year_without_inventing_an_explicit_year() -> None:
    started_at, ended_at, precision, resolution = parse_date_range(
        "7月10日-7月11日", REFERENCE_TIME
    )

    assert started_at == datetime(2026, 7, 10, tzinfo=SHANGHAI)
    assert ended_at == datetime(2026, 7, 11, tzinfo=SHANGHAI)
    assert precision == "range"
    assert resolution["year_explicit"] is False


def test_date_parser_preserves_single_relative_and_cross_year_precision() -> None:
    single = parse_date_range("7月10日去了成都", REFERENCE_TIME)
    assert single[2] == "day"
    assert single[3]["source"] == "explicit_day"

    relative = parse_date_range("昨天去了成都", REFERENCE_TIME)
    assert relative[0] == datetime(2026, 7, 11, tzinfo=SHANGHAI)
    assert relative[3]["source"] == "relative_day"

    crossed = parse_date_range("12月31日-1月2日去了成都", REFERENCE_TIME)
    assert crossed[0] == datetime(2026, 12, 31, tzinfo=SHANGHAI)
    assert crossed[1] == datetime(2027, 1, 2, tzinfo=SHANGHAI)
    assert crossed[3]["crossed_year"] is True

    fuzzy = parse_date_range("7月中旬去了成都", REFERENCE_TIME)
    assert fuzzy[0] == datetime(2026, 7, 11, tzinfo=SHANGHAI)
    assert fuzzy[1] == datetime(2026, 7, 20, tzinfo=SHANGHAI)
    assert fuzzy[2] == "range"
    assert fuzzy[3]["fuzzy"] is True


def test_travel_episode_keeps_experiences_and_participant_roles() -> None:
    episode = parse_episode(
        "我与 user 7月10日-7月11日去了成都旅游，遇到了大学同学小 A，第一次去熊猫基地看了熊猫",
        REFERENCE_TIME,
    )

    assert episode is not None
    assert episode.episode_type == "travel"
    assert episode.accepted is True
    assert {(item.name, item.role) for item in episode.entities} == {
        ("成都", "location"),
        ("小A", "participant"),
        ("熊猫基地", "object"),
        ("熊猫", "object"),
    }
    assert [step.kind for step in episode.steps] == [
        "action",
        "encounter",
        "milestone",
        "observation",
    ]
    classmate = next(item for item in episode.entities if item.name == "小A")
    assert classmate.relationship_type == "university_classmate"


def test_elapsed_time_is_not_misread_as_travel() -> None:
    assert parse_episode("现在过去了几个小时再试试呢", REFERENCE_TIME) is None


def test_technical_episode_stays_candidate_until_reviewed() -> None:
    episode = parse_episode("排查 n8n 容器异常，怀疑 docker 网络与局域网冲突", REFERENCE_TIME)

    assert episode is not None
    assert episode.episode_type == "technical"
    assert episode.accepted is False
    assert episode.steps[0].kind == "hypothesis"
    assert {item.name.casefold() for item in episode.entities} == {"n8n", "docker"}


def test_incidental_visit_is_not_a_preference() -> None:
    assert parse_preference("我去了一次安静咖啡馆") is None


def test_explicit_preference_is_preserved() -> None:
    preference = parse_preference("我更喜欢安静的咖啡馆")

    assert preference is not None
    assert preference.polarity == "prefer"
    assert preference.aspect == "安静的咖啡馆"


def test_explicit_call_name_language_and_style_preferences_are_preserved() -> None:
    call_name = parse_preference("以后称呼我为公子")
    language = parse_preference("以后使用中文回答")
    style = parse_preference("回答时保持简洁风格")

    assert (call_name.aspect, call_name.topic, call_name.polarity) == (
        "称呼",
        "公子",
        "require",
    )
    assert (language.aspect, language.topic, language.polarity) == (
        "回复语言",
        "中文",
        "require",
    )
    assert (style.aspect, style.topic, style.polarity) == (
        "回复风格",
        "简洁",
        "prefer",
    )


def test_explicit_operation_and_reminder_preferences_are_preserved() -> None:
    no_delete = parse_preference("不要自动删除本地文件")
    backup_first = parse_preference("变更前必须先备份")
    reminder = parse_preference("以后通过邮件提醒我")
    allowance = parse_preference("以后允许自动删除本地文件")

    assert (no_delete.aspect, no_delete.topic, no_delete.polarity) == (
        "自动删除本地文件",
        "自动删除本地文件",
        "avoid",
    )
    assert (backup_first.aspect, backup_first.topic, backup_first.polarity) == (
        "变更前备份",
        "先备份",
        "require",
    )
    assert (reminder.aspect, reminder.topic, reminder.polarity) == (
        "提醒方式",
        "邮件",
        "require",
    )
    assert (allowance.aspect, allowance.topic, allowance.polarity) == (
        "自动删除本地文件",
        "自动删除本地文件",
        "allow",
    )


def test_preference_gold_accuracy_is_at_least_ninety_five_percent() -> None:
    positive = (
        "以后称呼我为公子",
        "请叫我 Evergarden",
        "之后称我为小园",
        "以后使用中文回答",
        "请用英文回复",
        "之后用繁体中文交流",
        "回答时保持简洁风格",
        "回复时采用正式方式",
        "以后回答直接一些",
        "我喜欢安静的咖啡馆",
        "我不喜欢嘈杂的餐厅",
        "我偏好早上处理邮件",
        "我更喜欢简短的汇报",
        "我避免夜间收到通知",
        "我要求变更必须留记录",
        "我必须先检查备份",
        "不要自动删除本地文件",
        "禁止修改生产容器",
        "变更前必须先备份",
        "部署前务必运行测试",
        "每次只允许执行只读检查",
        "以后通过邮件提醒我",
        "请用短信提醒我",
        "提醒我时请使用企业微信",
    )
    negative = (
        "我去了一次安静咖啡馆",
        "今天在成都看了熊猫",
        "邮件通知目前是不是暂停了",
        "现在打开客厅灯",
        "允许修改",
        "继续执行",
        "帮我安装 imagegen skill",
        "模型已切换为 qwen",
        "任务已经完成",
        "天气今天转晴",
        "[Home Assistant] 客厅灯 turned off",
        "请问你喜欢什么颜色",
        "他喜欢安静的咖啡馆",
        "我昨天收到邮件提醒",
        "他说以后称呼我为公子",
        "同事建议以后通过邮件提醒我",
        "文档示例：不要自动删除本地文件",
        "生日是七月二十五日",
        "刚才通过短信收到了验证码",
        "电话提醒响了一次",
        "她说我喜欢安静的咖啡馆",
        "请用中文回答可以吗？",
        "可以用邮件提醒我吗？",
        "备份已经完成",
    )
    correct = sum(parse_preference(text) is not None for text in positive)
    correct += sum(parse_preference(text) is None for text in negative)
    accuracy = correct / (len(positive) + len(negative))

    assert accuracy >= 0.95, f"preference gold accuracy={accuracy:.2%}"


def test_birthday_is_a_temporal_rule_not_an_episode() -> None:
    temporal = parse_temporal_rule("我的生日是7月25日")

    assert temporal is not None
    assert temporal.rule_type == "birthday"
    assert (temporal.month, temporal.day, temporal.year) == (7, 25, None)
    assert parse_episode("我的生日是7月25日", REFERENCE_TIME) is None


def test_invalid_calendar_date_is_rejected() -> None:
    assert parse_temporal_rule("我的生日是2月30日") is None


def test_procedure_applicability_requires_an_exact_environment_match() -> None:
    expected = {"host": "hostA", "service": "n8n", "network": "bridge-a"}

    assert procedure_applicability(expected, expected)["status"] == "applicable"
    assert (
        procedure_applicability(expected, {"host": "hostB", "service": "n8n"})["status"]
        == "incompatible"
    )
    unknown = procedure_applicability(expected, {"host": "hostA"})
    assert unknown["status"] == "unknown"
    assert unknown["auto_apply"] is False
    expired = procedure_applicability(
        expected,
        expected,
        valid_to=REFERENCE_TIME - timedelta(seconds=1),
        now=REFERENCE_TIME,
    )
    assert expired["status"] == "expired"
    assert expired["auto_apply"] is False


def test_temporal_query_terms_keep_subjects_but_drop_question_words() -> None:
    terms = _temporal_query_terms("7月什么时候去了成都")

    assert "成都" in terms
    assert "什么时候" not in terms


def test_procedure_query_terms_keep_targets_but_drop_action_words() -> None:
    terms = _procedure_query_terms("Next Terminal 无法连接 VPS 应该如何排查")

    assert {"next", "terminal", "vps"} <= set(terms)
    assert all("排查" not in term and "无法连接" not in term for term in terms)
