from agent_memory.subjects import normalize_profile, profile_display_name


def test_profile_display_name_keeps_readable_source_name_without_type_prefix():
    assert profile_display_name("  Qishuo   Daily  ") == "Qishuo Daily"
    assert normalize_profile("  Qishuo   Daily  ") == "qishuo daily"


def test_profile_display_name_hides_internal_source_identifiers():
    assert profile_display_name("relay-20260714T134019Z") == "未命名助手"
    assert profile_display_name("52b11d08-6828-4e01-be17-75a0da188df8") == "未命名助手"
