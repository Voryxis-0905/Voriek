import json
import logging
import re
import requests

logger = logging.getLogger(__name__)

try:
    from prompts import (
        WRITER_SYSTEM_PROMPT, EXTRACTOR_SYSTEM_PROMPT, PLANNER_SYSTEM_PROMPT,
        WORLD_BUILDER_SKELETON_PROMPT,
        WORLD_BUILDER_CARDS_PROMPT, WORLD_BUILDER_CHARACTERS_PROMPT,
        CONSISTENCY_CHECKER_SYSTEM_PROMPT, SUMMARIZER_SYSTEM_PROMPT,
        WORLD_BUILDER_INTERVIEW_PROMPT, CREATOR_ASSISTANT_PROMPT,
        LINTER_SYSTEM_PROMPT, REWRITE_SYSTEM_PROMPT, EDITOR_SYSTEM_PROMPT,
        ARC_EXTENDER_PROMPT,
        PRELUDE_WRITER_PROMPT, PRELUDE_VALIDATOR_PROMPT,
        LOCATION_MAP_GENERATOR_PROMPT
    )
    from skill_limiter import check_skill_limiter
except ImportError:
    from backend.prompts import (
        WRITER_SYSTEM_PROMPT, EXTRACTOR_SYSTEM_PROMPT, PLANNER_SYSTEM_PROMPT,
        WORLD_BUILDER_SKELETON_PROMPT,
        WORLD_BUILDER_CARDS_PROMPT, WORLD_BUILDER_CHARACTERS_PROMPT,
        CONSISTENCY_CHECKER_SYSTEM_PROMPT, SUMMARIZER_SYSTEM_PROMPT,
        WORLD_BUILDER_INTERVIEW_PROMPT, CREATOR_ASSISTANT_PROMPT,
        LINTER_SYSTEM_PROMPT, REWRITE_SYSTEM_PROMPT, EDITOR_SYSTEM_PROMPT,
        ARC_EXTENDER_PROMPT,
        PRELUDE_WRITER_PROMPT, PRELUDE_VALIDATOR_PROMPT,
        LOCATION_MAP_GENERATOR_PROMPT
    )
    from backend.skill_limiter import check_skill_limiter


class LLMCallError(Exception):
    pass


class RateLimitError(LLMCallError):
    def __init__(self, message: str, retry_after: float = None):
        super().__init__(message)
        self.retry_after = retry_after


_REQUESTS = requests


def _set_requests_module(mod):
    global _REQUESTS
    _REQUESTS = mod


def _retry_after_header_seconds(resp) -> float:
    retry_after = resp.headers.get("Retry-After") if resp is not None else None
    if retry_after:
        try:
            return max(0.5, float(retry_after))
        except (TypeError, ValueError):
            pass
    return None


def get_base_url_for_provider(provider: str, custom_base_url: str = None) -> str:
    provider = (provider or "").lower().strip()
    if custom_base_url and custom_base_url.strip():
        url = custom_base_url.strip()
        if not any(url.endswith(p) for p in ("/chat/completions", "/v1/messages", "/completions", "/messages")):
            if provider in ("anthropic", "claude"):
                url = url.rstrip("/") + "/v1/messages"
            else:
                url = url.rstrip("/") + "/chat/completions"
        return url
    if provider == "featherless":
        return "https://api.featherless.ai/v1/chat/completions"
    elif provider == "openai":
        return "https://api.openai.com/v1/chat/completions"
    elif provider == "deepseek":
        return "https://api.deepseek.com/chat/completions"
    elif provider == "groq":
        return "https://api.groq.com/openai/v1/chat/completions"
    elif provider == "gemini":
        return "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    elif provider == "together":
        return "https://api.together.xyz/v1/chat/completions"
    elif provider == "mistral":
        return "https://api.mistral.ai/v1/chat/completions"
    elif provider in ("anthropic", "claude"):
        return "https://api.anthropic.com/v1/messages"
    elif provider in ("ollama", "ollama_local"):
        return "http://localhost:11434/v1/chat/completions"
    elif provider in ("lmstudio", "lmstudio_local"):
        return "http://localhost:1234/v1/chat/completions"
    return "https://openrouter.ai/api/v1/chat/completions"


def mock_narrator_response(user_input: str) -> str:
    mock = {
        "chapter_text": (
            f"[MOCK - chua gan API key that] Nguoi choi hanh dong: '{user_input}'. "
            "Day la doan chapter gia lap de test pipeline end-to-end."
        ),
        "state_changes": {
            "characters": {},
            "notes": "mock response, khong phai LLM that"
        },
        "chapter_end": True,
        "chapter_title": None
    }
    return json.dumps(mock, ensure_ascii=False)


def mock_prelude_response() -> str:
    mock = {
        "prelude_text": (
            "[MOCK - chua gan API key that] Day la prelude chapter. "
            "Mot khung canh mo dau cho the gioi, dat khong khi va gioi thieu "
            "boi canh truoc khi cau chuyen that su bat dau. "
            "Khong khi lang dong, nhung gi do dang hinh thanh phia duong chan troi... "
            "The gioi nay dang dung truoc mot buoc ngoat."
        ),
        "validation_notes": "[MOCK] Prelude generated without real LLM."
    }
    return json.dumps(mock, ensure_ascii=False)


def mock_consistency_checker_response() -> str:
    mock = {
        "consistent": True,
        "severity": "none",
        "issues": [],
        "explanation": "[MOCK - chua gan API key that] checker khong thuc su chay."
    }
    return json.dumps(mock, ensure_ascii=False)


def mock_summarizer_response(chapter_title: str) -> str:
    mock = {
        "running_summary": f"[MOCK - chua gan API key that] Da qua chuong '{chapter_title or '(khong ten)'}'.",
        "memorable_beats": ["[MOCK beat] An event from the chapter", "[MOCK beat] A line of dialogue"]
    }
    return json.dumps(mock, ensure_ascii=False)


def mock_planner_response(user_input: str) -> str:
    mock = {
        "boundary_check": "Within scope — no boundary violation",
        "scene_outline": "The character observes their surroundings and takes in the scene.",
        "facts_this_turn": ["The character is at the current location"],
        "anchor_keywords": [],
        "open_threads_update": "",
        "state_changes": {
            "characters": {},
            "notes": "mock planner response"
        },
        "suggested_actions": ["Look around carefully", "Approach someone nearby"],
        "is_ooc": False,
        "action_translation": ""
    }
    return json.dumps(mock, ensure_ascii=False)


_VALID_ROLES = frozenset({"planner", "actor", "writer", "extractor", "editor", "checker", "summarizer"})


def _is_openclaw_target(provider: str, base_url: str) -> bool:
    """Return whether this request targets OpenClaw's model-rewriting API.

    ``custom`` means OpenAI-compatible, not OpenClaw. Treating every custom
    endpoint as OpenClaw corrupts ordinary model ids such as ``deepseek-flash``.
    """
    return provider == "openclaw" or "18789" in (base_url or "")


def call_llm(system_prompt: str, user_prompt: str, user_input_for_mock: str = "",
             mock_response: str = None, world_name: str = None,
             role: str = "default") -> str:
    import sys
    main_mod = sys.modules.get("main")
    if main_mod and getattr(main_mod, "call_llm", None) not in (None, call_llm):
        return main_mod.call_llm(system_prompt, user_prompt, user_input_for_mock=user_input_for_mock,
                                 mock_response=mock_response, world_name=world_name, role=role)
    from app.storage import get_effective_fallback_chain, get_effective_fallback_chain_for_role
    fallback = mock_response if mock_response is not None else mock_narrator_response(user_input_for_mock)
    if role in _VALID_ROLES:
        fallback_chain = get_effective_fallback_chain_for_role(world_name, role)
    else:
        fallback_chain = get_effective_fallback_chain(world_name)

    if not fallback_chain:
        return fallback

    last_error = None
    last_resp = None

    for cfg in fallback_chain:
        api_key = cfg.get("api_key", "")
        model = cfg.get("model", "")
        provider = (cfg.get("provider", "openrouter") or "openrouter").lower().strip()
        custom_base_url = cfg.get("base_url", "")

        base_url = get_base_url_for_provider(provider, custom_base_url)

        headers = {"Content-Type": "application/json"}
        if provider in ("anthropic", "claude"):
            if api_key:
                headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
            payload = {
                "model": model,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
                "max_tokens": 4096
            }
        else:
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            if provider == "openrouter":
                headers["HTTP-Referer"] = "https://github.com/Voryxis-0905/Voriek"
                headers["X-Title"] = "Voriek"
            if _is_openclaw_target(provider, base_url):
                headers["x-openclaw-scopes"] = "operator.write"
                if model == "deepseek-web" or "/" not in model:
                    model = "openclaw"
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
            }

        try:
            resp = _REQUESTS.post(
                base_url,
                headers=headers,
                json=payload,
                timeout=600
            )
        except requests.exceptions.RequestException as e:
            last_error = f"Network error with {provider} ({base_url}): {e}"
            continue

        if resp.status_code == 429:
            last_error = f"Rate-limit (429) with {provider}."
            last_resp = resp
            continue

        if resp.status_code >= 500:
            last_error = f"Server error ({resp.status_code}) with {provider}."
            continue

        if resp.status_code in (401, 402, 404):
            last_error = f"Provider {provider} returned error {resp.status_code}."
            if len(fallback_chain) > 1:
                continue

        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            last_error = f"Provider {provider} returned error {resp.status_code}: {e}"
            continue

        content_type = resp.headers.get("Content-Type", "")
        if content_type and "json" not in content_type.lower():
            last_error = f"Provider {provider} returned HTTP {resp.status_code} (not JSON; Content-Type: {content_type})"
            continue

        try:
            data = resp.json()
            if provider in ("anthropic", "claude"):
                content = data["content"][0]["text"]
            else:
                choice = (data.get("choices") or [{}])[0]
                msg = choice.get("message") if isinstance(choice, dict) else {}
                content = ""
                if isinstance(msg, dict):
                    content = msg.get("content") or ""
                if not content and isinstance(choice, dict) and "text" in choice:
                    content = choice.get("text") or ""

            if not content or not str(content).strip():
                last_error = f"Provider {provider} returned empty response content."
                continue
            return str(content)
        except (ValueError, KeyError, IndexError) as e:
            last_error = f"Provider {provider} returned malformed response: {e}"
            continue

    if last_resp and last_resp.status_code == 429:
        raise RateLimitError(
            "All providers are rate-limited or errored. Please try again later.",
            retry_after=_retry_after_header_seconds(last_resp)
        )
    raise LLMCallError(f"Fallback chain failed. Last error: {last_error}")


def test_llm_connection(world_name: str = None, node_index: int = 0) -> dict:
    from app.storage import get_effective_fallback_chain
    fallback_chain = get_effective_fallback_chain(world_name)
    if not fallback_chain:
        return {
            "ok": False, "reason": "no_key",
            "message": "No API key configured yet (neither this UI nor .env)."
        }

    if node_index < 0 or node_index >= len(fallback_chain):
        node_index = 0
    cfg = fallback_chain[node_index]

    api_key = cfg.get("api_key", "")
    model = cfg.get("model", "")
    provider = (cfg.get("provider", "openrouter") or "openrouter").lower().strip()
    custom_base_url = cfg.get("base_url", "")
    base_url = get_base_url_for_provider(provider, custom_base_url)

    headers = {"Content-Type": "application/json"}
    if provider in ("anthropic", "claude"):
        if api_key:
            headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1
        }
    else:
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if provider == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/Voryxis-0905/Voriek"
            headers["X-Title"] = "Voriek"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1
        }

    try:
        resp = _REQUESTS.post(
            base_url,
            headers=headers,
            json=payload,
            timeout=20
        )
    except requests.exceptions.RequestException as e:
        return {"ok": False, "reason": "network", "message": f"Network error calling {provider.capitalize()} ({base_url}): {e}"}

    if resp.status_code == 200:
        return {"ok": True, "reason": "ok", "message": f"Connection OK with {provider.capitalize()} (model: {model})."}
    if resp.status_code == 401:
        return {
            "ok": False, "reason": "unauthorized",
            "message": f"401 Unauthorized from {provider.capitalize()} — the key is wrong, incomplete, or revoked."
        }
    if resp.status_code == 402:
        return {"ok": False, "reason": "no_credit", "message": f"402 — this account is out of credit on {provider.capitalize()}."}
    if resp.status_code == 429:
        return {
            "ok": False, "reason": "rate_limited",
            "message": f"429 — rate limited by {provider.capitalize()}. Try again shortly."
        }
    if resp.status_code == 404:
        return {"ok": False, "reason": "model_not_found", "message": f"404 — model '{model}' was not found on {provider.capitalize()}."}

    provider_message = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            provider_message = (body.get("error") or {}).get("message", "") or body.get("message", "") or ""
    except ValueError:
        pass
    if resp.status_code == 400 and "model" in provider_message.lower():
        return {
            "ok": False, "reason": "model_not_found",
            "message": f"400 — likely an invalid model id ('{model}'): {provider_message}"
        }

    return {
        "ok": False, "reason": "provider_error",
        "message": f"{provider.capitalize()} returned {resp.status_code}: {provider_message or resp.text[:200]}"
    }


def parse_llm_json(raw_text: str, expected_type: type = dict):
    if not raw_text or not isinstance(raw_text, str):
        raise ValueError("LLM response is empty or invalid string.")

    cleaned = raw_text.strip()
    if not cleaned:
        raise ValueError("LLM response is empty.")

    has_think = bool(re.search(r"<think>", cleaned, flags=re.IGNORECASE))
    no_think = re.sub(r"<think>.*?(?:</think>|$)", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()

    if has_think and not no_think:
        raise ValueError(
            "LLM response contained only thinking text (<think>...</think>) without JSON output. "
            "The model likely hit its maximum output token limit while reasoning."
        )

    text_to_parse = no_think if has_think else cleaned

    def _try_parse(s: str):
        s_clean = s.strip()
        try:
            return json.loads(s_clean)
        except json.JSONDecodeError:
            pass
        # Remove trailing commas
        repaired = re.sub(r",\s*([\}\]])", r"\1", s_clean)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            return None

    res = _try_parse(text_to_parse)
    if res is not None and (expected_type is None or isinstance(res, expected_type)):
        return res

    json_fence = re.search(r"```json\s*([\s\S]*?)\s*```", text_to_parse, flags=re.IGNORECASE)
    if json_fence:
        fence_content = json_fence.group(1).strip()
        res = _try_parse(fence_content)
        if res is not None and (expected_type is None or isinstance(res, expected_type)):
            return res

    generic_fence = re.search(r"```(?:\w+)?\s*([\s\S]*?)\s*```", text_to_parse)
    if generic_fence:
        fence_content = generic_fence.group(1).strip()
        res = _try_parse(fence_content)
        if res is not None and (expected_type is None or isinstance(res, expected_type)):
            return res

    decoder = json.JSONDecoder()
    start_chars = ["{", "["]
    if expected_type is list:
        start_chars = ["[", "{"]

    candidate_indices = []
    for i, char in enumerate(text_to_parse):
        if char in start_chars:
            candidate_indices.append((i, char))

    parsed_wrong_type = None
    min_next_idx = 0
    for idx, char in candidate_indices:
        if idx < min_next_idx:
            continue
        try:
            obj, end_idx = decoder.raw_decode(text_to_parse, idx)
            min_next_idx = end_idx
            if expected_type is None or isinstance(obj, expected_type):
                return obj
            elif parsed_wrong_type is None:
                parsed_wrong_type = obj
        except json.JSONDecodeError:
            # Try parsing substring with trailing comma repair
            sub = text_to_parse[idx:]
            end_match = sub.rfind("}" if char == "{" else "]")
            if end_match != -1:
                sub_candidate = sub[:end_match + 1]
                res = _try_parse(sub_candidate)
                if res is not None and (expected_type is None or isinstance(res, expected_type)):
                    return res
            continue

    if parsed_wrong_type is not None:
        raise ValueError(
            f"Parsed JSON is of type '{type(parsed_wrong_type).__name__}', "
            f"expected '{expected_type.__name__ if expected_type else 'any'}'."
        )

    raise ValueError(f"Failed to parse valid JSON from LLM output. Output snippet: {raw_text[:150]!r}")
