"""独立聊天调用；复用供应商传输，不依附调查运行或改变调查预算。"""
import json
import time

from project.agents.logistics_chat import SCHEMA, validate
from project.agents.logistics_model import model_output, estimate_input_tokens
from project.agents.logistics_protocol import ProtocolError
from .investigation_model import model_session
from .logistics_model import configured, output_mode


class ChatModel:
    def __init__(self, env):
        self.env = env

    def generate(self, messages, evidence_ids, reserve, record):
        if not configured(self.env):
            raise ProtocolError('model_not_configured')
        mode = output_mode(self.env)
        body = {'messages': messages, 'stream': False, 'max_tokens': 1800}
        if mode == 'tool_call':
            body.update(tools=[{'type': 'function', 'function': {'name': 'submit_chat',
                'description': '提出物流聊天动作，不执行业务操作', 'parameters': SCHEMA}}],
                tool_choice={'type': 'function', 'function': {'name': 'submit_chat'}}, parallel_tool_calls=False)
        elif mode == 'json_schema':
            body['response_format'] = {'type': 'json_schema', 'json_schema': {'name': 'submit_chat', 'schema': SCHEMA}}
        elif mode == 'json_object':
            body['response_format'] = {'type': 'json_object'}
        elif mode != 'json_text':
            raise ProtocolError('invalid_adapter_mode')
        if mode in ('json_object', 'json_text'):
            body['messages'] = [dict(messages[0], content=messages[0]['content']+'\n输出结构：'+json.dumps(SCHEMA, ensure_ascii=False)), *messages[1:]]
        estimate = estimate_input_tokens(body) + body['max_tokens']
        if estimate > 16000:
            raise ProtocolError('context_budget')
        reserve(estimate)
        started, tokens, failure, diagnostic = time.monotonic(), None, None, None
        def on_failure(value):
            nonlocal diagnostic
            diagnostic = value
        try:
            with model_session(self.env, on_failure=on_failure) as request:
                payload = request(body, started, 30)
            usage = payload.get('usage') if isinstance(payload, dict) else None
            total = usage.get('total_tokens') if isinstance(usage, dict) else None
            tokens = total if type(total) is int and total >= 0 else None
            if tokens is not None and tokens > 16000:
                raise ProtocolError('chat_turn_budget')
            return validate(model_output(payload, 'chat', mode), evidence_ids)
        except ProtocolError as exc:
            failure = exc.code
            raise
        except Exception:
            failure = 'model_transport_failed'
            raise ProtocolError(failure) from None
        finally:
            record(tokens, time.monotonic()-started, failure, diagnostic)
