# 授权判定与审计构造按概念归位

gateway-proxy 的权限判定逻辑原散落在 middleware.py（check_call_permission / build_audit_meta / build_journey / classify_error）、auth.py（check_permission）与 permission_middleware.py（on_call_tool / on_list_tools 内联），且授权与审计都各自读 TOOL_REGISTRY 全局（mode 权威）。我们决定立一个纯函数授权 seam：新建 authorization.py，暴露 `authorize(permissions, mcp_name, tool_modes) -> AuthResult`（tool_modes 作为数据注入，不读全局），授权与审计共享一次解析出的 server/tool/mode；同时把审计构造函数（build_audit_meta / build_journey / classify_error / ERROR_TYPES）移入 audit.py，删除 middleware.py。

选择此方案是因为：授权（mode→grant）与审计（记录）是两个独立概念，混在 middleware.py 使"某工具的 mode 从哪来"跨 5+ 文件才可还原（越权 bug 根因），且授权被 TOOL_REGISTRY 全局污染导致测试必须 register/clear 全局。归位后授权是纯函数、测试不碰全局，审计字段契约集中一处。放弃"保留 middleware.py 作授权+审计收容所"与"check_call_permission 薄适配兼容"两个选项——前者延续浅模块（概念混杂），后者保留双实现（授权两条路径）。
