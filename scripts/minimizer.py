# -*- coding: utf-8 -*-

def minimize_body(body, replay):
    current = dict(body)
    removed = []
    for key in list(body):
        candidate = dict(current)
        candidate.pop(key, None)
        if replay(candidate):
            current = candidate
            removed.append(key)
    return {"body": current, "removed": removed}
