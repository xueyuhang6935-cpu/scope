"""Optional model planner. Only proposes fields; never reads data or calculates facts."""
import json
from urllib.parse import urlsplit

import httpx

from .supply_analysis import INTENTS, PERIODS, validate_plan
from .supply_context import ACTIONS
from .supply_prompts import SCOPE_PROMPT, SCOPE_PROMPT_VERSION


def configured(env):
    url = urlsplit(env.get("SUPPLY_MODEL_BASE_URL", "https://api.deepseek.com"))
    return bool(env.get("SUPPLY_PLANNER") == "model" and env.get("SUPPLY_MODEL") and
                (env.get("SUPPLY_MODEL_API_KEY") or env.get("DEEPSEEK_API_KEY")) and
                url.scheme == "https" and url.hostname and not url.username and not url.password and not url.query and not url.fragment)


def propose_with_model(env, text, scope, context=None):
    properties = {"intent": {"type": "string", "enum": list(INTENTS)}, "factory": {"type": "string", "enum": ["all", "A", "B"]},
                  "material": {"type": "string"}, "supplier": {"type": "string"}, "period": {"type": "string", "enum": list(PERIODS)},
                  "clarification": {"type": "string", "description": "范围明确且全部条件可支持时为空字符串；信息缺失、冲突、不支持或非只读请求时填写具体追问，不得省略条件后宣称明确。"}}
    if context is not None:
        properties.update(action={"type": "string", "enum": list(ACTIONS)},
                          answer={"type": "string", "description": "解释或建议的文字回答；查询时为空。只依据历史证据，不编造原因、数值，不执行业务操作。"})
    body = {"model": env["SUPPLY_MODEL"], "stream": False, "max_tokens": 800,
            "messages": [{"role": "system", "content": SCOPE_PROMPT},
                         {"role": "user", "content": json.dumps({"question": text, "manualScope": scope}, ensure_ascii=False)}],
            "tools": [{"type": "function", "function": {"name": "propose_scope", "description": "提议待用户确认的范围", "parameters": {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}}}],
            "tool_choice": {"type": "function", "function": {"name": "propose_scope"}}}
    if context is not None:
        body['messages'][1]['content'] = json.dumps({'question': text, 'scope': scope, 'manualScope': context.get('manualScope', {}), 'conversation': context}, ensure_ascii=False)
    base = env.get("SUPPLY_MODEL_BASE_URL", "https://api.deepseek.com").rstrip("/")
    if urlsplit(base).hostname == "api.deepseek.com":
        body["thinking"] = {"type": "disabled"}
    try:
        # No automatic retries: network failure may still have incurred provider usage.
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            with client.stream("POST", base + "/chat/completions", json=body, headers={"Authorization": "Bearer " + (env.get("SUPPLY_MODEL_API_KEY") or env["DEEPSEEK_API_KEY"])}) as response:
                response.raise_for_status()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 100000:
                        raise ValueError()
        payload = json.loads(raw)
        calls = payload["choices"][0]["message"]["tool_calls"]
        if len(calls) != 1 or calls[0]["function"]["name"] != "propose_scope":
            raise ValueError()
        arguments = json.loads(calls[0]["function"]["arguments"])
        action = arguments.pop('action', 'query')
        answer = arguments.pop('answer', '')
        if action not in (*ACTIONS, 'answer') or not isinstance(answer, str) or len(answer) > 6000 or (action in ('answer', 'explain') and not answer.strip()):
            raise ValueError()
        clarification = arguments.pop('clarification')
        if not isinstance(clarification, str) or len(clarification) > 1000:
            raise ValueError()
        plan = validate_plan(arguments, scope["factory"])
        questions = [clarification] if clarification else []
        for key, value in (context.get('manualScope', {}) if context is not None else scope).items():
            if value and key != "factory":
                if plan[key] and plan[key] != value:
                    questions.append('问题解析与手动范围冲突，请核对实际查询范围。')
                plan[key] = value
        tokens = payload.get("usage", {}).get("total_tokens")
        return plan, {"mode": "model", "model": env["SUPPLY_MODEL"], "promptVersion": SCOPE_PROMPT_VERSION, "tokens": tokens if type(tokens) is int and tokens >= 0 else None,
                      "action": action, "answer": answer,
                      "questions": questions, "understood": not questions,
                      "notice": "已解析查询范围。" if not questions else "问题需要澄清，尚未执行查询。"}
    except Exception:
        return None, {"mode": "model", "model": env["SUPPLY_MODEL"], "promptVersion": SCOPE_PROMPT_VERSION, "tokens": None, "notice": "模型解析失败或用量未知，未自动重试。请手动核对并确认范围。"}
