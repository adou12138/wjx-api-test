# 适配器责任边界

通用测试引擎只依赖适配器协议：

- `build_request(case, env)`：构造请求。
- `send(request)`：发送请求并返回统一响应。
- `classify_response(response)`：分类 HTTP/传输结果。
- `create_fixture(case, env)`：创建隔离测试数据。
- `verify_resource(resource, case, env)`：独立回读和副作用验证。
- `cleanup_fixture(resource, env)`：清理本轮登记资源。

REST 适配器负责通用 HTTP；Sojump 适配器负责 action、encode、appid、sign、ts、账号和资源生命周期。真实凭据只能来自外部环境或配置文件，不能写入 Skill 或示例。

Sojump 写操作必须同时满足 `environment.type == test` 和 `write_enabled == true`；仅凭域名名称不能放行。
