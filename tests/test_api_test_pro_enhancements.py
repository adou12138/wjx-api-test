# -*- coding: utf-8 -*-
"""参考 API Test Pro 能力补强的回归测试：
1) OpenAPI 3.x：$ref 解析 + 嵌套递归展开
2) Swagger 2.0：definitions + host/basePath 地址拼接
3) YAML 契约加载
4) 安全载荷清单驱动用例生成（G 维度）
5) GraphQL 适配器：HTTP 200 + body.errors 判失败（反假通过）
6) gRPC 适配器：缺依赖实例化即报错（懒加载防呆）
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from contract_parser import parse_openapi, load_contract  # noqa: E402


OPENAPI3 = {
    "openapi": "3.0.0",
    "servers": [{"url": "https://api.example.test"}],
    "paths": {
        "/users": {
            "post": {
                "operationId": "createUser",
                "summary": "创建用户",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/UserReq"}
                        }
                    }
                },
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/UserResp"}
                            }
                        }
                    }
                },
            }
        }
    },
    "components": {
        "schemas": {
            "UserReq": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string", "maxLength": 50, "description": "用户名"},
                    "profile": {
                        "type": "object",
                        "properties": {
                            "bio": {"type": "string", "description": "简介"},
                        },
                    },
                    "tags": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/Tag"},
                    },
                },
            },
            "Tag": {
                "type": "object",
                "properties": {"label": {"type": "string"}},
            },
            "UserResp": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
            },
        }
    },
}


class TestOpenApi3(unittest.TestCase):
    def test_ref_and_nested_recursion(self):
        c = parse_openapi(OPENAPI3, action="createUser")
        self.assertIsNotNone(c)
        names = [f["name"] for f in c["request_fields"]]
        # $ref 解析：顶层字段来自 components/schemas/UserReq
        self.assertIn("name", names)
        # 嵌套 object 递归：profile.bio 用点路径
        self.assertIn("profile.bio", names)
        # 嵌套 array + $ref 递归：tags[].label
        self.assertIn("tags[].label", names)
        # required 传递
        req = {f["name"]: f["required"] for f in c["request_fields"]}
        self.assertTrue(req["name"])
        # maxLength 抽取
        name_f = [f for f in c["request_fields"] if f["name"] == "name"][0]
        self.assertEqual(name_f["max_len"], 50)
        # 响应 $ref
        self.assertIn("id", [f["name"] for f in c["response_fields"]])
        # 地址拼接
        self.assertEqual(c["endpoint"], "https://api.example.test/users")
        self.assertEqual(c["method"], "POST")

    def test_circular_ref_does_not_hang(self):
        spec = {
            "openapi": "3.0.0",
            "paths": {"/n": {"post": {
                "operationId": "op",
                "requestBody": {"content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/Node"}}}},
                "responses": {"200": {"content": {"application/json": {
                    "schema": {"type": "object"}}}}},
            }}},
            "components": {"schemas": {"Node": {
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                    "child": {"$ref": "#/components/schemas/Node"},
                },
            }}},
        }
        c = parse_openapi(spec, action="op")
        names = [f["name"] for f in c["request_fields"]]
        self.assertIn("value", names)
        # 自引用被防环截断，不会无限递归
        self.assertTrue(len(names) < 50)


SWAGGER2 = {
    "swagger": "2.0",
    "host": "api2.example.test",
    "basePath": "/v1",
    "schemes": ["https"],
    "paths": {
        "/orders": {
            "post": {
                "operationId": "createOrder",
                "parameters": [
                    {"name": "token", "in": "header", "type": "string", "required": True},
                    {"name": "body", "in": "body", "required": True,
                     "schema": {"$ref": "#/definitions/Order"}},
                ],
                "responses": {"200": {"schema": {"$ref": "#/definitions/OrderResp"}}},
            }
        }
    },
    "definitions": {
        "Order": {
            "type": "object",
            "required": ["amount"],
            "properties": {"amount": {"type": "number", "description": "金额"}},
        },
        "OrderResp": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
        },
    },
}


class TestSwagger2(unittest.TestCase):
    def test_v2_body_and_address(self):
        c = parse_openapi(SWAGGER2, action="createOrder")
        self.assertIsNotNone(c)
        names = [f["name"] for f in c["request_fields"]]
        self.assertIn("token", names)      # header 参数
        self.assertIn("amount", names)     # body $ref → definitions
        # 地址：schemes + host + basePath + path
        self.assertEqual(c["endpoint"], "https://api2.example.test/v1/orders")
        self.assertIn("order_id", [f["name"] for f in c["response_fields"]])


class TestYamlLoad(unittest.TestCase):
    def test_yaml_contract(self):
        import tempfile, os
        yaml_text = (
            "openapi: 3.0.0\n"
            "servers:\n  - url: https://y.example.test\n"
            "paths:\n"
            "  /ping:\n"
            "    get:\n"
            "      operationId: ping\n"
            "      responses:\n"
            "        '200':\n"
            "          content:\n"
            "            application/json:\n"
            "              schema:\n"
            "                type: object\n"
            "                properties:\n"
            "                  ok: {type: boolean}\n"
        )
        fd, path = tempfile.mkstemp(suffix=".yaml")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(yaml_text)
            c = load_contract(path, action="ping")
            self.assertEqual(c["format"], "openapi")
            self.assertEqual(c["endpoint"], "https://y.example.test/ping")
            self.assertIn("ok", [f["name"] for f in c["response_fields"]])
        finally:
            os.unlink(path)


class TestSecurityPayloads(unittest.TestCase):
    def _contract(self):
        return {
            "action": "1000107", "name": "t", "endpoint": "https://x",
            "request_fields": [
                {"name": "title", "type": "string", "desc": "标题", "required": True,
                 "remark": "", "enum": [], "max_len": None, "min_len": None,
                 "default": None, "forbid": [], "semantics": ["name"]},
                {"name": "content", "type": "string", "desc": "正文内容", "required": False,
                 "remark": "", "enum": [], "max_len": None, "min_len": None,
                 "default": None, "forbid": [], "semantics": ["html"]},
                {"name": "count", "type": "int", "desc": "数量", "required": False,
                 "remark": "", "enum": [], "max_len": None, "min_len": None,
                 "default": None, "forbid": [], "semantics": ["count"]},
            ],
            "response_fields": [], "request_example": {}, "response_example": {},
            "source": "t", "format": "json",
        }

    def test_payload_cases_generated(self):
        from case_generator import gen_security_payload_cases
        cases = gen_security_payload_cases(self._contract(), {"action": "1000107"}, "1000107")
        self.assertTrue(cases)
        dims = {c["dimension"] for c in cases}
        self.assertEqual(dims, {"G"})
        # 所有安全用例都应被拦截（success=False）
        self.assertTrue(all(c["expect"]["success"] is False for c in cases))
        # int 字段不注入字符串载荷
        self.assertFalse(any("count" in c["id"] for c in cases))
        # html 字段有 XSS 用例
        self.assertTrue(any("G-XSS-content" in c["id"] for c in cases))

    def test_payload_lib_wellformed(self):
        lib_path = ROOT / "assets" / "security_payloads.json"
        lib = json.loads(lib_path.read_text(encoding="utf-8"))
        self.assertTrue(lib["categories"])
        for cat in lib["categories"]:
            self.assertTrue(cat["id"] and cat["payloads"], cat)
            # applies_to 是生成器唯一的匹配依据：漏写就静默不生效
            self.assertTrue(cat["applies_to"], cat)
            self.assertTrue(cat["err_keywords"], cat)
        # 内容安全词库：至少覆盖四个常见违规类目
        ids = {c["id"] for c in lib["categories"]}
        self.assertTrue({"AD", "ILLEGAL", "ABUSE", "PORN"} <= ids)

    def test_content_categories_hit_html_and_name_only(self):
        """内容安全类目只挂在内容/标题类字段上，不泛化到所有字符串字段。"""
        from case_generator import _payload_categories_for, _load_security_payloads
        lib = _load_security_payloads()
        content = _payload_categories_for(
            {"name": "content", "type": "string", "semantics": ["html"]}, lib)
        for cid in ("AD", "ILLEGAL", "ABUSE", "PORN"):
            self.assertIn(cid, content)
        plain = _payload_categories_for(
            {"name": "remark", "type": "string", "semantics": []}, lib)
        self.assertEqual(set(plain) & {"AD", "ILLEGAL", "ABUSE", "PORN"}, set())
        # 非字符串字段一律不注入
        self.assertEqual(
            _payload_categories_for({"name": "n", "type": "int", "semantics": ["count"]}, lib),
            [])


class TestGraphqlAdapter(unittest.TestCase):
    def test_http200_with_errors_is_failure(self):
        from adapters.graphql import GraphqlAdapter
        from adapters.base import Response
        a = GraphqlAdapter()
        resp = Response(status_code=200, body={"errors": [{"message": "boom"}]})
        self.assertEqual(a.classify_response(resp)["status"], "graphql_error")
        resp_ok = Response(status_code=200, body={"data": {"ok": True}})
        self.assertEqual(a.classify_response(resp_ok)["status"], "ok")

    def test_build_request_requires_query(self):
        from adapters.graphql import GraphqlAdapter
        a = GraphqlAdapter()
        with self.assertRaises(ValueError):
            a.build_request({"params": {}}, {"base_url": "https://x/graphql"})
        req = a.build_request(
            {"params": {"query": "{ me { id } }"}}, {"base_url": "https://x/graphql"})
        self.assertEqual(req.body["query"], "{ me { id } }")
        self.assertEqual(req.method, "POST")


class TestGrpcAdapter(unittest.TestCase):
    def test_missing_dependency_raises(self):
        try:
            import grpc  # noqa: F401
            self.skipTest("grpcio 已安装，跳过缺依赖报错测试")
        except ImportError:
            pass
        from adapters.grpc import GrpcAdapter
        from adapters.base import TransportError
        with self.assertRaises(TransportError):
            GrpcAdapter()


if __name__ == "__main__":
    unittest.main()
