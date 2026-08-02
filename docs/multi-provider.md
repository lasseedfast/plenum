# Guide for adding multi-provider LLM support in `rixdagen`

This document is intended to be pasted into an assistant session in VS Code so it can implement the change.

## What is true in the repo right now

The current code is still wired around **one global endpoint**, even though the repo also contains a more flexible provider config template.

### Verified repo facts

1. `backend/services/chat.py` creates `smart_llm`, `fast_llm`, and `communicator_llm` using the same `base_url=os.getenv("LLM_DIRECT_URL")`.
2. `FAST_MODEL` and `SMART_MODEL` are selected from environment variables (`LLM_MODEL_FAST`, `LLM_MODEL_SMART`), but both still flow through that same single endpoint.
3. `providers.template.yaml` already describes a multi-provider design where each provider has:
   - an `id`
   - a `name`
   - a `base_url`
   - optional `api_keys`
   - a list of `models`
   - optional `additional_parameters`
   - per-model `abilities`
4. `providers.template.yaml` explicitly says you can add any **OpenAI API compatible** provider to the `providers` list, and also notes that the OpenAI API is not a standard, so compatibility is not guaranteed.
5. The template also already shows a provider with **multiple API keys** and a `litellm` example, which means the intended design already assumes routing different models to different upstreams.

## Main conclusion

The repo already points toward the right architecture, but the runtime path is not there yet.

Right now, adding more entries to `providers.yaml` alone is **not enough**. The backend still behaves like this:

- choose model alias from env
- send all LLM traffic to one global `LLM_DIRECT_URL`

To support `vLLM`, `berget.ai`, `openai.com`, and potentially other OpenAI-compatible providers cleanly, the app needs to move to this model:

- choose a logical model alias for each role (`smart`, `fast`, `embeddings`, maybe `communicator`)
- resolve that alias through `providers.yaml`
- derive:
  - provider
  - protocol
  - base URL
  - API key env var
  - upstream model ID
  - abilities / extra parameters
- build the client per resolved model

## What needs to be implemented

### 1. Make `providers.yaml` the source of truth for model routing

Do not keep endpoint selection centered on `LLM_DIRECT_URL`.

Instead:

- keep environment variables for **which logical model alias** to use
- move endpoint/model/provider metadata into `providers.yaml`

Recommended env vars:

- `LLM_MODEL_SMART`
- `LLM_MODEL_FAST`
- `LLM_MODEL_EMBEDDINGS`
- `LLM_MODEL_COMMUNICATOR` (optional, if you want it to diverge later)
- provider secrets such as `OPENAI_API_KEY`, `BERGET_API_KEY`, `LLM_VLLM_API_KEY`, etc.

### 2. Add a provider resolver layer

Create a small module, for example `backend/services/llm_provider_registry.py` or `_llm/provider_registry.py`, with responsibilities like:

- load `providers.yaml`
- validate it
- index models by alias or logical name
- resolve one requested model into a runtime configuration object

Recommended shape:

```python
@dataclass
class ResolvedModel:
    alias: str
    provider_id: str
    provider_name: str
    protocol: str
    base_url: str
    api_key: str | None
    api_key_env: str | None
    upstream_model_id: str
    abilities: dict[str, Any]
    additional_parameters: dict[str, Any]
```

Functions to add:

```python
def load_provider_config(path: str = "providers.yaml") -> dict: ...
def resolve_model(alias: str) -> ResolvedModel: ...
def get_api_key_from_env(env_name: str | None) -> str | None: ...
```

### 3. Refactor `_llm.LLM` to use resolved provider config

Today the calling code passes `model=...` and `base_url=...` directly.

Change that so `_llm.LLM` can accept either:

- a logical alias and resolve internally, or
- an already-resolved provider config object

Preferred direction:

```python
LLM(model_alias="smart")
```

or:

```python
resolved = resolve_model(os.getenv("LLM_MODEL_SMART", "smart"))
LLM(resolved_model=resolved)
```

The key point is that `_llm.LLM.generate(...)` should no longer rely on one app-wide endpoint.

### 4. Remove the single-endpoint assumption from `ChatService`

Refactor `backend/services/chat.py` so each role resolves independently.

Current behavior:

- `smart_llm` -> `LLM_DIRECT_URL`
- `fast_llm` -> `LLM_DIRECT_URL`
- `communicator_llm` -> `LLM_DIRECT_URL`

Target behavior:

- `smart_llm` -> resolved from `LLM_MODEL_SMART`
- `fast_llm` -> resolved from `LLM_MODEL_FAST`
- `communicator_llm` -> resolved from `LLM_MODEL_COMMUNICATOR` or fallback to `LLM_MODEL_SMART`

So the code should look more like:

```python
smart_alias = os.getenv("LLM_MODEL_SMART", "smart")
fast_alias = os.getenv("LLM_MODEL_FAST", smart_alias)
communicator_alias = os.getenv("LLM_MODEL_COMMUNICATOR", smart_alias)

self.smart_llm = LLM(model_alias=smart_alias, system_message=ORCHESTRATOR_SYSTEM, temperature=0.2)
self.fast_llm = LLM(model_alias=fast_alias, system_message=WORKER_SYSTEM, temperature=0.05)
self.communicator_llm = LLM(model_alias=communicator_alias, system_message=ORCHESTRATOR_SYSTEM, temperature=0.3)
```

### 5. Keep a fallback path for the current vLLM setup

Do not break the current local workflow.

Recommended fallback logic:

- if `providers.yaml` exists and the requested alias resolves, use it
- otherwise fall back to the current legacy env path:
  - `LLM_DIRECT_URL`
  - `LLM_API_KEY`
  - `LLM_MODEL`

That lets the migration happen incrementally.

### 6. Distinguish protocol from provider

This matters for future-proofing.

Do **not** assume every upstream will behave exactly like local vLLM.

Add a provider field like:

```yaml
protocol: openai
```

Even if you only implement `openai` first.

That keeps the design open for future support of providers that may require slightly different auth, payloads, streaming behavior, or tool-calling semantics.

## Recommended `providers.yaml` structure

Use the existing repo template as the starting point and extend it slightly.

Suggested structure:

```yaml
providers:
  - id: vllm
    name: Local vLLM
    protocol: openai
    base_url: http://localhost:8000/v1
    api_keys:
      vllm: LLM_VLLM_API_KEY
    models:
      - id: meta-llama/Meta-Llama-3.1-70B-Instruct
        alias: smart_local
        name: Smart Local
        provider: vllm
        context: 131072
        abilities:
          temperature:
            supported: true
          system_message:
            supported: true
          tools:
            supported: true

  - id: openai
    name: OpenAI
    protocol: openai
    base_url: https://api.openai.com/v1
    api_keys:
      openai: OPENAI_API_KEY
    models:
      - id: gpt-4.1-mini
        alias: fast_openai
        name: GPT-4.1 mini
        provider: openai
        context: 1000000
        abilities:
          temperature:
            supported: true
          system_message:
            supported: true
          tools:
            supported: true

  - id: berget
    name: Berget
    protocol: openai
    base_url: REPLACE_WITH_VERIFIED_BERGET_BASE_URL
    api_keys:
      berget: BERGET_API_KEY
    models:
      - id: REPLACE_WITH_VERIFIED_MODEL_ID
        alias: smart_berget
        name: Berget Smart
        provider: berget
        context: REPLACE_WITH_VERIFIED_CONTEXT
        abilities:
          temperature:
            supported: true
          system_message:
            supported: true
          tools:
            supported: REPLACE_WITH_VERIFIED_VALUE
```

## Important implementation details

### Alias resolution

The app should refer to models by local aliases, not raw provider model IDs.

That means env vars should point to aliases such as:

```bash
LLM_MODEL_SMART=smart_berget
LLM_MODEL_FAST=fast_openai
LLM_MODEL_COMMUNICATOR=smart_local
```

### API key lookup

The provider file should contain the env var **name**, not the secret itself.

Example:

```yaml
api_keys:
  openai: OPENAI_API_KEY
```

Runtime should do:

```python
env_name = provider.api_keys[model.provider]
api_key = os.getenv(env_name)
```

### Additional provider parameters

The template already supports `additional_parameters` at provider level.

Preserve and pass those through when calling the upstream API. This is especially useful because many OpenAI-compatible providers expose non-standard extensions.

### Abilities gating

Use the `abilities` block to avoid sending unsupported settings.

Examples:

- do not send tools if `tools.supported` is false
- do not send temperature if unsupported
- do not rely on reasoning-specific settings unless the model explicitly supports them

This is important because the template itself warns that OpenAI compatibility is imperfect.

### Streaming and tool calling

You should assume that compatibility varies most in these areas:

- streaming chunk format
- tool/function calling schema
- system message handling
- vision payload format
- reasoning fields

So build adapters conservatively and fail loudly with clear messages.

## Suggested migration plan

### Phase 1: provider registry

- add loader + schema validation for `providers.yaml`
- resolve aliases to provider configs
- keep legacy env fallback

### Phase 2: wire `ChatService`

- replace `LLM_DIRECT_URL` usage in `backend/services/chat.py`
- resolve each model independently

### Phase 3: update `_llm`

- centralize client creation
- add support for provider-level `additional_parameters`
- add abilities filtering

### Phase 4: test with three providers

Test combinations like:

1. all-local
   - smart -> vLLM
   - fast -> vLLM
   - communicator -> vLLM

2. mixed
   - smart -> Berget
   - fast -> OpenAI
   - communicator -> vLLM

3. cloud-only
   - smart -> OpenAI
   - fast -> OpenAI
   - communicator -> OpenAI

## Acceptance criteria

The change is done when all of the following are true:

1. I can switch providers **without code changes**, only by editing `providers.yaml` and env vars.
2. `smart`, `fast`, and `communicator` can point to different providers.
3. The current local vLLM setup still works.
4. Unsupported capabilities are filtered based on model abilities.
5. Failures clearly say which alias/provider/model failed and why.
6. Secrets are only read from environment variables, never committed into YAML.

## External documentation notes that must be verified before implementation

I cannot verify the live OpenAI and Berget documentation from this environment because web access is disabled in this session.

That means the VS Code assistant should **verify these exact facts from the docs before finalizing the patch**:

### OpenAI

Verify from current OpenAI docs:

- the recommended Python SDK import and client initialization pattern
- whether the intended integration path should use `responses` or `chat.completions`
- exact base URL shape for the public API
- auth header expectations
- tool-calling support for the models you intend to use
- streaming response shape if this app streams tokens

### Berget

Verify from `https://docs.berget.ai/quickstart`:

- the exact OpenAI-compatible `base_url`
- the exact auth mechanism / env var examples
- the exact Python example for OpenAI SDK usage
- which model IDs are documented for chat usage
- whether tools/function calling are supported
- whether reasoning / vision / embeddings are separately documented

### One OpenAI-compatible provider in general

Use one documented compatibility layer as a reference point when implementing adapters. The goal is to confirm what the **minimum common denominator** really is across providers.

At minimum, verify:

- expected `/v1` pathing
- supported request schema for chat completions or responses
- tool/function call representation
- streaming format
- how unsupported fields are handled

## Instructions for the coding assistant

Implement this as a minimal, low-risk refactor.

### Required changes

1. Add a provider registry module.
2. Add YAML loading and validation.
3. Refactor `backend/services/chat.py` so each LLM instance resolves independently.
4. Refactor `_llm.LLM` to build clients from resolved provider config.
5. Preserve backwards compatibility with the current env-only setup.
6. Add clear error messages for missing aliases, missing API keys, and unsupported features.

### Nice-to-have changes

1. Add a startup validation command that prints configured aliases and providers.
2. Add unit tests for alias resolution.
3. Add one integration test for local vLLM and one mocked OpenAI-compatible provider.
4. Add comments explaining why compatibility must be treated as provider-specific, not assumed.

## Suggested commit breakdown

1. `add provider registry and providers.yaml loader`
2. `refactor chat service to resolve llm aliases per role`
3. `refactor _llm client creation to use provider config`
4. `add compatibility fallback for legacy env config`
5. `add tests for provider resolution and errors`

## Final note

The most important design change is this:

**Stop routing all models through one global `LLM_DIRECT_URL`.**

Everything else is secondary. Once alias -> provider -> endpoint resolution exists, adding `berget.ai`, `openai.com`, or other OpenAI-compatible providers becomes a configuration problem instead of a code fork.
