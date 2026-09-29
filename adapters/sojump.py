# -*- coding: utf-8 -*-
from .rest import RestAdapter


class SojumpAdapter(RestAdapter):
    REQUIRED_PUBLIC = ("action", "encode", "appid", "sign")

    @staticmethod
    def is_write_allowed(env):
        return env.get("type") == "test" and env.get("write_enabled") is True

    def build_request(self, case, env):
        params = dict(env.get("common_params") or {})
        params.update(case.get("params") or {})
        missing = [key for key in self.REQUIRED_PUBLIC if not params.get(key)]
        if missing:
            raise ValueError("missing Sojump public parameters: %s" % ",".join(missing))
        if case.get("write", False) and not self.is_write_allowed(env):
            raise PermissionError("write operation requires explicit test environment and write_enabled=true")
        return super().build_request({"params": params}, env)

    def register_resource(self, case_id, resource_id, cleanup_action):
        return {"case_id": case_id, "resource_id": resource_id, "cleanup_action": cleanup_action}
