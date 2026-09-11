def test_deliberately_failing_for_ci_negative_check():
    """负向验证：故意让 CI 红，确认它真的会拦。验证完即删。"""
    assert False, "负向验证：这条失败必须让 CI job 失败"
