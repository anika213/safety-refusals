"""OpenRouter chat completions API utils."""

import asyncio
import os
import time

from dotenv import load_dotenv
from openai import AsyncOpenAI, APIConnectionError, APITimeoutError, RateLimitError
from openai.types.chat import ChatCompletion
from pydantic import BaseModel
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from tqdm.asyncio import tqdm_asyncio

from safety_refusals.cache import make_batch_key, load_batch, save_batch

load_dotenv()

_EXPONENTIAL_WAIT = wait_exponential(multiplier=2, min=4, max=60)


def _seconds_until_retry(exc: BaseException) -> float | None:
    """Read Retry-After / X-RateLimit-Reset from a 429, if present."""
    headers: dict = {}
    response = getattr(exc, "response", None)
    if response is not None:
        headers.update(dict(getattr(response, "headers", {}) or {}))
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        meta = (body.get("error") or {}).get("metadata") or {}
        headers.update(meta.get("headers") or {})

    lower = {str(k).lower(): v for k, v in headers.items()}
    retry_after = lower.get("retry-after")
    if retry_after is not None:
        try:
            return min(max(float(retry_after), 1.0), 120.0)
        except (TypeError, ValueError):
            pass

    reset = lower.get("x-ratelimit-reset")
    if reset is not None:
        try:
            ts = float(reset)
            if ts > 1e12:
                ts /= 1000.0
            delay = ts - time.time()
            if delay > 0:
                return min(delay + 0.5, 120.0)
        except (TypeError, ValueError):
            pass
    return None


def _wait_rate_limit(retry_state) -> float:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if exc is not None:
        delay = _seconds_until_retry(exc)
        if delay is not None:
            return delay
    return _EXPONENTIAL_WAIT(retry_state)


class AsyncRateLimiter:
    """Sliding-window limiter: at most `rpm` starts per 60 seconds."""

    def __init__(self, rpm: int | None):
        self.rpm = rpm
        self._started: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if not self.rpm:
            return
        while True:
            async with self._lock:
                now = time.monotonic()
                self._started = [t for t in self._started if now - t < 60.0]
                if len(self._started) < self.rpm:
                    self._started.append(now)
                    return
                sleep_for = 60.0 - (now - self._started[0]) + 0.05
            await asyncio.sleep(max(sleep_for, 0.05))


def get_openrouter_client() -> AsyncOpenAI:
    """Get an AsyncOpenAI client configured for OpenRouter."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY not found in .env file!")

    return AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )


def get_openai_client() -> AsyncOpenAI:
    """Get an AsyncOpenAI client configured for OpenAI."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY not found in .env file!")

    return AsyncOpenAI(api_key=api_key)


@retry(
    retry=retry_if_exception_type((RateLimitError, APIConnectionError, APITimeoutError)),
    stop=stop_after_attempt(10),
    wait=_wait_rate_limit,
)
async def call_api(
    client: AsyncOpenAI,
    model: str,
    messages: list,
    response_format: type[BaseModel] | None = None,
    temperature: float = 1.0,
    max_tokens: int = 16000,
    top_p: float = 1.0,
    logprobs: bool = False,
    top_logprobs: int | None = None,
    n: int = 1,
    stop: list[str] | str | None = None,
    tools: list[dict] | None = None,
    extra_body: dict | None = None,
    **kwargs,
):
    """
    Make a chat completion API call.

    Args:
        client: AsyncOpenAI client.
        model: Model identifier.
        messages: Chat messages.
        response_format: Pydantic model for structured output.
        temperature: Sampling temperature.
        max_tokens: Maximum tokens in response.
        top_p: Nucleus sampling threshold.
        logprobs: Enable logprobs.
        top_logprobs: Number of top logprobs to return.
        n: Number of completions to generate.
        stop: Stop sequence(s) to end generation.
        tools: Tool definitions for function calling.
        extra_body: Extra parameters to pass in request body (e.g., top_k for OpenRouter).
        **kwargs: Additional parameters forwarded to the API call (e.g., parallel_tool_calls, seed).

    Returns:
        Full response object from the API.
    """
    if response_format is not None:
        response = await client.beta.chat.completions.parse(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            logprobs=logprobs,
            top_logprobs=top_logprobs,
            response_format=response_format,
            extra_body=extra_body,
            **kwargs,
        )
    else:
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            logprobs=logprobs,
            top_logprobs=top_logprobs,
            n=n,
            stop=stop,
            tools=tools,
            extra_body=extra_body,
            **kwargs,
        )
    return response


async def process_one(
    client: AsyncOpenAI,
    model: str,
    messages: list,
    semaphore: asyncio.Semaphore,
    response_format: type[BaseModel] | None = None,
    temperature: float = 1.0,
    max_tokens: int = 16000,
    top_p: float = 1.0,
    logprobs: bool = False,
    top_logprobs: int | None = None,
    extra_body: dict | None = None,
    rate_limiter: AsyncRateLimiter | None = None,
    **kwargs,
):
    """Process a single request with semaphore-based concurrency control."""
    async with semaphore:
        if rate_limiter is not None:
            await rate_limiter.acquire()
        return await call_api(
            client=client,
            model=model,
            messages=messages,
            response_format=response_format,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            logprobs=logprobs,
            top_logprobs=top_logprobs,
            extra_body=extra_body,
            **kwargs,
        )


async def process_batch(
    client: AsyncOpenAI,
    model: str,
    messages_list: list,
    response_format: type[BaseModel] | None = None,
    temperature: float = 1.0,
    max_tokens: int = 16000,
    max_concurrent: int = 10,
    top_p: float = 1.0,
    logprobs: bool = False,
    top_logprobs: int | None = None,
    extra_body: dict | None = None,
    return_exceptions: bool = False,
    cache: bool = True,
    requests_per_minute: int | None = None,
    **kwargs,
) -> list:
    """
    Process all requests concurrently with a semaphore.

    Args:
        client: AsyncOpenAI client.
        model: Model identifier.
        messages_list: List of chat message lists.
        response_format: Pydantic model for structured output.
        temperature: Sampling temperature.
        max_tokens: Maximum tokens in response.
        max_concurrent: Maximum concurrent requests.
        top_p: Nucleus sampling threshold.
        logprobs: Enable logprobs.
        top_logprobs: Number of top logprobs to return.
        extra_body: Extra parameters to pass in request body (e.g., top_k for OpenRouter).
        return_exceptions: If True, exceptions are returned in results instead of raised.
        cache: If True, cache results in SQLite. Set False for ephemeral calls like summaries.
        requests_per_minute: If set, start at most this many requests per rolling 60s window.
        **kwargs: Additional parameters forwarded to the API call (e.g., parallel_tool_calls, seed).

    Returns:
        List of response objects from the API.
        If return_exceptions=True, failed requests return Exception objects.
    """
    # Check batch cache
    if cache:
        cache_params = dict(
            model=model,
            messages_list=messages_list,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            logprobs=logprobs,
            top_logprobs=top_logprobs,
            extra_body=extra_body,
            **kwargs,
        )
        cache_key = make_batch_key(
            n_samples=len(messages_list),
            **cache_params,
        )
        cached = load_batch(cache_key)
        if cached is not None:
            print(f"Cache hit ({len(cached)} responses)")
            return [ChatCompletion.model_validate(r) for r in cached]

    semaphore = asyncio.Semaphore(max_concurrent)
    rate_limiter = AsyncRateLimiter(requests_per_minute)
    coroutines = [
        process_one(
            client=client,
            model=model,
            messages=m,
            semaphore=semaphore,
            response_format=response_format,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            logprobs=logprobs,
            top_logprobs=top_logprobs,
            extra_body=extra_body,
            rate_limiter=rate_limiter,
            **kwargs,
        )
        for m in messages_list
    ]

    if return_exceptions:
        # tqdm_asyncio.gather doesn't support return_exceptions, use asyncio.gather
        # with tqdm wrapper for progress tracking
        async def wrap_with_progress(coro, pbar):
            try:
                result = await coro
                pbar.update(1)
                return result
            except Exception as e:
                pbar.update(1)
                return e

        from tqdm import tqdm
        pbar = tqdm(total=len(coroutines))
        wrapped = [wrap_with_progress(c, pbar) for c in coroutines]
        results = await asyncio.gather(*wrapped)
        pbar.close()
    else:
        results = await tqdm_asyncio.gather(*coroutines)

    # Save to cache (only if no exceptions in results)
    if cache and not any(isinstance(r, Exception) for r in results):
        save_batch(cache_key, cache_params, [r.model_dump() for r in results])

    return results
