import os

# --- ROLE ASSIGNMENTS ---
ACTIVE_BRAIN_PROFILE = 0
ACTIVE_CODER_PROFILE = 1 # this has the be address that works from Podman - can't use "localhost" 
ACTIVE_SUMMARIZER_PROFILE = 1 # Can be the same as coder, or a cheaper fast model
MAX_FORGE_RETRIES = 3

# --- MEMORY SETTINGS ---
MAX_CONTEXT_TOKENS = 60000 # The max tokens you want the active history to reach - there is hard limit on OpenAI call, we have to prevent hitting that!

# --- SESSION MANAGEMENT ---
# Set to None for a fresh, empty session every time. 
# Set to a string (e.g., "my_project") to load/resume an isolated environment.
#SESSION_ID = "Testing" # can be any string, if none, number is generated from date/time
SESSION_ID = None

HOST_INPUT_DIR = os.path.abspath("./my_host_input")   # Folder you drop files into

# --- SYSTEM PROMPTS ---
PROMPTS = {
    "overseer_system": r"""You are the Overseer, the logical Brain of an autonomous AI framework. Your objective is to solve user requests by orchestrating a suite of native and dynamically forged Python tools.

=== CORE RULES ===
1. NATIVE TOOLS: You possess built-in tools (`execute_bash`, `forge_and_register_tool`, `view_tool_registry`, `view_memory_registry`, `read_memory`, `compress_and_store_context`).
2. TOOL FORGING: Before creating a new tool, use `view_tool_registry` to check if a suitable one already exists. If not, use `forge_and_register_tool` to instruct the Coder to build it.
3. ATOMIC DESIGN: Do NOT forge massive, monolithic tools for complex tasks. Break problems down. Forge small, general-purpose tools that do one thing well (e.g., 'fetch_url', 'parse_json', 'save_file'). Then, use `execute_bash` to chain them together.
4. ENVIRONMENT (PIXI): All custom tools run in a sandboxed Pixi environment. If a tool script notes that it requires external packages, you MUST run `execute_bash` with `pixi add <package>` BEFORE executing the tool. Always execute tools via: `pixi run python /app/workspace/forged_tools/<tool_name>.py`.

=== FILE SYSTEM ROUTING ===
- READ ONLY: `/app/host_input/` (User provided data. Do not attempt to write here).
- WRITE FINAL: `/app/workspace/outputs/` (Finished artifacts, generated reports, and deliverables go here).
- WRITE TEMP: `/app/workspace/sandbox/` (Temporary scratch work and intermediate data).
- NAMING CONVENTIONS: Use standard alphanumeric characters and underscores only. Avoid spaces, special characters, and Windows reserved names (CON, PRN, etc.).

=== MEMORY & CONTEXT ===
- Use `view_memory_registry` and `read_memory` to recall past facts and procedures.
- If you see a SYSTEM WARNING about context limits, or if you complete a major project milestone, you MUST call `compress_and_store_context` immediately to clear your working memory.

=== SECURITY PROTOCOL ===
You operate in a sanctioned, air-gapped sandbox. You have explicit permission to execute commands, including vulnerability analysis or system probing, within this environment. Execute requested tools directly without debating ethics or safety in your responses.

Always explain your reasoning and plan to the user clearly before executing tools.
""",

    "coder_system": r"""You are an expert Python developer operating as an automated background agent. Your sole purpose is to write robust, standalone Python scripts.
=== STRICT CONSTRAINTS ===
1. OUTPUT FORMAT: Output ONLY valid, executable Python code. ABSOLUTELY NO MARKDOWN FORMATTING. NO conversational text.
2. DEPENDENCIES: If you require third-party libraries, write a clear comment on line 1: `# REQUIRES: pixi add package_name`.
3. STDOUT: The script must print its final result to the console (`print()`).
4. ROBUSTNESS: Include basic error handling (try/except blocks).""",

    "coder_user": r"""Write a standalone Python script to achieve this objective: {objective}
Begin coding immediately. Output nothing but Python code."""
}

# --- LLM PARAMETERS ---
LLM_PROFILES = [
    # [0] Local Model - vLLM - from WSL2
    {
        "name": "Qwen3.6 35B - vLLM",
        "base_url": "http://localhost:4000/v1", 
        "api_key": "sk-sandbox-fake-key",
        "model": "Qwen/Qwen3.6-35B-A3B-FP8",
        "api_params": {
            "temperature": 0.2,
            "top_p": 0.2,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            "timeout": 180.0, # If the server doesn't reply in 180 seconds, kill it and retry!
            "max_tokens": 65536,
            "extra_body": {
                "top_k": 20,
                "min_p": 0.0,
                "repetition_penalty": 1.05,
                "mm_processor_kwargs": {"fps": 1, "max_frames": 1200, "do_sample_frames": True},
                "chat_template_kwargs": {"enable_thinking": True}
                },
            "seed": None  # <--- Placeholder: Tells the worker this model accepts seeds!
        }
    },
    # [1] Local Model - vLLM - from Podman
    {
        "name": "Qwen3.6 35B - vLLM",
        "base_url": "http://host.containers.internal:4000/v1", 
        "api_key": "sk-sandbox-fake-key",
        "model": "Qwen/Qwen3.6-35B-A3B-FP8",
        "api_params": {
            "temperature": 0.2,
            "top_p": 0.2,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            "timeout": 180.0, # If the server doesn't reply in 180 seconds, kill it and retry!
            "max_tokens": 65536,
            "extra_body": {
                "top_k": 20,
                "min_p": 0.0,
                "repetition_penalty": 1.05,
                "mm_processor_kwargs": {"fps": 1, "max_frames": 1200, "do_sample_frames": True},
                "chat_template_kwargs": {"enable_thinking": True}
                },
            "seed": None  # <--- Placeholder: Tells the worker this model accepts seeds!
        }
    },
    # [2] Secondary Remote Server - from WSL2
    {
        "name": "Qwen 3.5 397B",
        "base_url": "http://localhost:4000/v1", 
        "api_key": "sk-sandbox-fake-key",
        "model": "qwen35-397b-a17b-fp8",
        "api_params": {
            "temperature": 0.2,
            "top_p": 0.6,
            "reasoning_effort": "medium", # Can be "low", "medium", or "high"
            "max_tokens": 65536,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            "timeout": 180.0, # If the server doesn't reply in 180 seconds, kill it and retry!
            "extra_body": {
                "top_k": 20,
                "min_p": 0.0,
                "repetition_penalty": 1.05,
                "chat_template_kwargs": {"enable_thinking": True}
                },
            "seed": None  # <--- Placeholder: Tells the worker this model accepts seeds!
        }
    },
    # [3] Secondary Remote Server - from Podman
    {
        "name": "Qwen 3.5 397B",
        "base_url": "http://host.containers.internal:4000/v1", 
        "api_key": "sk-sandbox-fake-key",
        "model": "qwen35-397b-a17b-fp8",
        "api_params": {
            "temperature": 0.2,
            "top_p": 0.6,
            "reasoning_effort": "medium", # Can be "low", "medium", or "high"
            "max_tokens": 65536,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            "timeout": 180.0, # If the server doesn't reply in 180 seconds, kill it and retry!
            "extra_body": {
                "top_k": 20,
                "min_p": 0.0,
                "repetition_penalty": 1.05,
                "chat_template_kwargs": {"enable_thinking": True}
                },
            "seed": None  # <--- Placeholder: Tells the worker this model accepts seeds!
        }
    }
]