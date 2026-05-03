import os
import json
import subprocess
import traceback
import logging
import sys
import re
from datetime import datetime
from fastmcp import FastMCP
from openai import OpenAI
import config

mcp = FastMCP("TheForge")

# --- MCP STREAM PROTECTION ---
# Suppress all third-party Python logging to prevent them from printing 
# rogue text to stdout and corrupting the FastMCP JSON-RPC stream.
logging.getLogger().setLevel(logging.CRITICAL)
logging.getLogger("httpx").setLevel(logging.CRITICAL)
logging.getLogger("openai").setLevel(logging.CRITICAL)

WORKSPACE_DIR = "/app/workspace"
STATE_DIR = os.path.join(WORKSPACE_DIR, "state")
SANDBOX_DIR = os.path.join(WORKSPACE_DIR, "sandbox")
FORGED_TOOLS_DIR = os.path.join(WORKSPACE_DIR, "forged_tools")
MEMORIES_DIR = os.path.join(WORKSPACE_DIR, "memories")
HISTORIES_DIR = os.path.join(WORKSPACE_DIR, "histories")
TOOL_REGISTRY_FILE = os.path.join(STATE_DIR, "tool_registry.json")
MEMORY_REGISTRY_FILE = os.path.join(STATE_DIR, "memory_registry.json")
CURRENT_HISTORY_FILE = os.path.join(STATE_DIR, "current_history.json")

coder_profile = config.LLM_PROFILES[config.ACTIVE_CODER_PROFILE]
summarizer_profile = config.LLM_PROFILES[config.ACTIVE_SUMMARIZER_PROFILE]

if coder_profile.get("base_url"):
    coder_client = OpenAI(base_url=coder_profile["base_url"], api_key=coder_profile["api_key"], timeout=120.0)
else:
    coder_client = OpenAI(api_key=coder_profile["api_key"], timeout=120.0)
    
if summarizer_profile.get("base_url"):
    summarizer_client = OpenAI(base_url=summarizer_profile["base_url"], api_key=summarizer_profile["api_key"], timeout=180.0)
else:
    summarizer_client = OpenAI(api_key=summarizer_profile["api_key"], timeout=180.0)

# --- HELPER FUNCTIONS ---
def load_json(filepath):
    if not os.path.exists(filepath): return {}
    with open(filepath, "r") as f: return json.load(f)

def save_json(filepath, data):
    with open(filepath, "w") as f: json.dump(data, f, indent=4)

# --- MCP TOOLS ---
@mcp.tool()
def view_tool_registry(category: str = None) -> str:
    """Views the custom forged tools registry. Pass NO arguments to see top-level categories. Pass a category string to see detailed tools inside it."""
    registry = load_json(TOOL_REGISTRY_FILE)
    if not registry: return "Tool Registry is empty."
    
    if not category:
        summary = {cat: data.get("category_description", "") for cat, data in registry.items()}
        return json.dumps({"categories": summary}, indent=2) + "\n\nCall this tool again with a specific category name to see its tools."
    else:
        if category in registry:
            return json.dumps({category: registry[category]["tools"]}, indent=2)
        else:
            return f"Category '{category}' not found. Available categories are: {list(registry.keys())}"

@mcp.tool()
def execute_bash(command: str, timeout_seconds: int = 60) -> str:
    """Executes a bash command STRICTLY inside the sandbox directory. 
    'timeout_seconds' defaults to 60. Increase it up to 600 if you expect a long-running process like a massive download."""
    try:
        result = subprocess.run(
            command, 
            shell=True, 
            cwd=SANDBOX_DIR, 
            capture_output=True, 
            text=True, 
            timeout=timeout_seconds,
            stdin=subprocess.DEVNULL,   # Prevents children from stealing the MCP input stream
            start_new_session=True      # Traps grandchild daemons (like Playwright) in an isolated process group
        )
        
        output = result.stdout if result.returncode == 0 else result.stderr
        
        # Preview + File Redirection Prompt ---
        if len(output) > 5000:
            preview = output[:1000] # Give it just enough to see the structure/headers
            return (f"Exit Code: {result.returncode}\n"
                    f"Output Preview (First 1000 chars):\n{preview}\n\n"
                    f"... [SYSTEM WARNING: The full output was over 5000 characters and has been truncated. "
                    f"Do NOT attempt to parse this preview. If you need the full data, run your command again "
                    f"and append `> filename.txt` to save it to a file. Then, write a Python tool to ANALYZE or "
                    f"EXTRACT specific information from that file, ensuring your tool only prints a concise summary or the exact target data.]")

        return f"Exit Code: {result.returncode}\nOutput:\n{output}"
    except Exception as e:
        return f"Error executing command: {str(e)}"

# --- MEMORY TOOLS ---
@mcp.tool()
def view_memory_registry(category: str = None) -> str:
    """Views the long-term Memory Registry. Pass NO arguments to see memory categories. Pass a category to see specific memory titles, short descriptions, and timestamps."""
    memories = load_json(MEMORY_REGISTRY_FILE)
    if not memories: return "Memory Registry is empty."
    
    if not category:
        summary = {cat: data.get("category_description", "No description provided.") for cat, data in memories.items()}
        return json.dumps({"memory_categories": summary}, indent=2) + "\n\nCall this tool again with a category name to see available memories."
    
    if category in memories:
        cat_mems = memories[category].get("memories", {})
        summary = {}
        for title, data in cat_mems.items():
            summary[title] = {"description": data["description"], "timestamp": data["timestamp"]}
        return f"Memories in '{category}':\n{json.dumps(summary, indent=2)}\n\nUse read_memory with the exact title to read the full text."
    return f"Category not found. Available: {list(memories.keys())}"

@mcp.tool()
def read_memory(category: str, memory_title: str) -> str:
    """Reads the full markdown text of a specific memory from the registry."""
    memories = load_json(MEMORY_REGISTRY_FILE)
    try:
        mem_data = memories[category]["memories"][memory_title]
        filepath = os.path.join(WORKSPACE_DIR, mem_data["file"])
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        return f"--- MEMORY: {memory_title} ({mem_data['timestamp']}) ---\n{content}"
    except KeyError:
        return "Error: Memory or Category not found. Use view_memory_registry to see available options."
    except Exception as e:
        return f"Error reading memory file: {str(e)}"

@mcp.tool()
def compress_and_store_context() -> str:
    """Triggers the background Memory Manager to sequence a memory extraction followed by a history compression."""
    current_history = load_json(CURRENT_HISTORY_FILE)
    current_memories = load_json(MEMORY_REGISTRY_FILE)
    
    bloated_text = json.dumps(current_history, indent=2)
    
    # ==========================================
    # STEP 1: EXTRACT MEMORIES (Strict Schema)
    # ==========================================
    memory_schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "memory_extraction",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "extracted_memories": {
                        "type": "array",
                        "description": "A list of important facts, tool creations, or context to remember.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "category": {"type": "string", "description": "Broad category, e.g., 'Tool Concepts', 'User Preferences'"},
                                "category_description": {"type": "string", "description": "1-2 sentences explaining what types of memories belong in this category."},
                                "title": {"type": "string", "description": "Short, unique title"},
                                "short_description": {"type": "string", "description": "1-2 sentences summarizing the memory for the registry overview."},
                                "detailed_markdown": {"type": "string", "description": "The full, exhaustive details, code snippets, and explanations."}
                            },
                            "required": ["category", "category_description", "title", "short_description", "detailed_markdown"],
                            "additionalProperties": False
                        }
                    }
                },
                "required": ["extracted_memories"],
                "additionalProperties": False
            }
        }
    }

    api_args = summarizer_profile["api_params"].copy()
    api_args["model"] = summarizer_profile["model"]
    
    # Explicitly warn the Summarizer to check existing memories first
    sys_prompt = (
        "You are a data extractor. Analyze the chat history and extract NEW crucial long-term facts, completed objectives, or system states into the memory schema. "
        "Write highly detailed markdown files for the 'detailed_markdown' field. "
        "CRITICAL: Cross-reference the provided CURRENT MEMORY REGISTRY. Do NOT extract or duplicate facts that are already saved in the registry!"
    )
    
    user_prompt = f"CURRENT MEMORY REGISTRY (DO NOT DUPLICATE THESE):\n{json.dumps(current_memories, indent=2)}\n\nCHAT HISTORY TO ANALYZE:\n{bloated_text}"

    api_args["messages"] = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt}
    ]
    api_args["response_format"] = memory_schema

    try:
        mem_response = summarizer_client.chat.completions.create(**api_args)
        mem_data = json.loads(mem_response.choices[0].message.content)
        
        added_titles = []
        for memory in mem_data.get("extracted_memories", []):
            cat = memory["category"]
            cat_desc = memory["category_description"]
            title = memory["title"]
            short_desc = memory["short_description"]
            full_text = memory["detailed_markdown"]
            
            if cat not in current_memories: 
                current_memories[cat] = {"category_description": cat_desc, "memories": {}}
            else:
                # Always update the category description to keep it fresh
                current_memories[cat]["category_description"] = cat_desc
            
            # Save the detailed MD file with a perfect chronological sorting name
            safe_title = re.sub(r'[^a-zA-Z0-9_]', '', title)
            timestamp_prefix = datetime.now().strftime("%Y%m%d%H%M%S")
            filename = f"{timestamp_prefix}_{safe_title}.md"
            filepath = os.path.join(MEMORIES_DIR, filename)
            
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(full_text)
            
            # Update the compact registry
            current_memories[cat]["memories"][title] = {
                "description": short_desc,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "file": f"memories/{filename}"
            }
            added_titles.append(title)
            
        save_json(MEMORY_REGISTRY_FILE, current_memories)
        
    except Exception as e:
        return f"FAILED during Memory Extraction Phase. Error: {str(e)}"

    # ==========================================
    # STEP 2: COMPRESS HISTORY (Strict Schema)
    # ==========================================
    compression_schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "history_compression",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "compressed_history": {
                        "type": "array",
                        "description": "The exact message array to replace the chat history. MUST start with the system prompt.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "role": {"type": "string", "enum": ["system", "user", "assistant"]},
                                "content": {"type": "string"}
                            },
                            "required": ["role", "content"],
                            "additionalProperties": False
                        }
                    }
                },
                "required": ["compressed_history"],
                "additionalProperties": False
            }
        }
    }

    api_args["messages"] = [
        {"role": "system", "content": "You are a context compressor. Analyze the bloated chat log. Output a new, tiny chat log containing ONLY the original system prompt, a single 'user' message summarizing the current state, and an 'assistant' acknowledgment. Do NOT include old tool calls or debug logs."},
        {"role": "user", "content": bloated_text}
    ]
    api_args["response_format"] = compression_schema

    try:
        comp_response = summarizer_client.chat.completions.create(**api_args)
        comp_data = json.loads(comp_response.choices[0].message.content)
        
        # Backup the old bloated history before we overwrite it
        backup_file = os.path.join(HISTORIES_DIR, f"backup_history_{datetime.now().strftime('%Y%m%d%H%M%S')}.json")
        save_json(backup_file, current_history)
        
        # Overwrite the active working memory
        new_history = comp_data.get("compressed_history", [])
        save_json(CURRENT_HISTORY_FILE, new_history)
        
        return f"SUCCESS: Context compressed and old history moved to {os.path.basename(backup_file)}. \nNew detailed memories extracted to disk: {added_titles}. \n[SYSTEM INSTRUCTION: Your context has been reset. Proceed with your objective.]"

    except Exception as e:
        return f"FAILED during History Compression Phase. Error: {str(e)}"

@mcp.tool()
def forge_and_register_tool(category: str, category_description: str, tool_name: str, tool_description: str, objective: str) -> str:
    """Delegates writing a Python script to the Coder LLM, and registers it with rich metadata.
    'category_description' explains what the category is for (updates existing descriptions).
    'tool_description' should explain what the tool does and what arguments/parameters it expects.
    'objective' is the raw instruction sent to the coder.
    """

    # --- SECURE PATH SANITIZATION ---
    safe_name = os.path.basename(tool_name)
    if safe_name.endswith('.py'):
        safe_name = safe_name[:-3]
    safe_name = re.sub(r'[^a-zA-Z0-9_]', '', safe_name)
    if not safe_name:
        safe_name = "default_tool_name"

    filename = f"{safe_name}.py"
    file_path = os.path.join(FORGED_TOOLS_DIR, filename)
    
    messages = [
        {"role": "system", "content": config.PROMPTS["coder_system"]},
        {"role": "user", "content": config.PROMPTS["coder_user"].format(objective=objective)}
    ]
    
    for attempt in range(config.MAX_FORGE_RETRIES):
        try:
            api_args = coder_profile["api_params"].copy()
            api_args["model"] = coder_profile["model"]
            api_args["messages"] = messages
            
            if "seed" in api_args:
                base_seed = api_args.get("seed") or 1000
                api_args["seed"] = base_seed + attempt
            
            response = coder_client.chat.completions.create(**api_args)
            coder_msg = response.choices[0].message
            
            # --- Extract reasoning/content ---
            coder_thinking = getattr(coder_msg, 'reasoning_content', None)
            if not coder_thinking and hasattr(coder_msg, 'model_extra') and coder_msg.model_extra:
                coder_thinking = coder_msg.model_extra.get('reasoning_content') or coder_msg.model_extra.get('reasoning')
            if not coder_thinking:
                coder_thinking = getattr(coder_msg, 'reasoning', None)

            raw_content = coder_msg.content or ""
            
            if not coder_thinking and "<think>" in raw_content:
                think_match = re.search(r"<think>(.*?)</think>", raw_content, re.DOTALL)
                if think_match:
                    coder_thinking = think_match.group(1).strip()
                    raw_content = re.sub(r"<think>.*?</think>", "", raw_content, flags=re.DOTALL).strip()
            
            code = raw_content.replace("```python", "").replace("```", "").strip()

            # --- Extract Token Counts ---
            tokens_in = response.usage.prompt_tokens if response.usage else 0
            tokens_out = response.usage.completion_tokens if response.usage else 0
            thinking_tokens = getattr(response.usage.completion_tokens_details, 'reasoning_tokens', 0) if response.usage and hasattr(response.usage, 'completion_tokens_details') and response.usage.completion_tokens_details else 0
                
            token_report = f"[Tokens used by Coder: {tokens_in} in | {tokens_out} out" + (f" ({thinking_tokens} thinking)]" if thinking_tokens > 0 else "]")
                
            with open(file_path, "w") as f: 
                f.write(code)
                
            check = subprocess.run(["python", "-m", "py_compile", filename], cwd=FORGED_TOOLS_DIR, capture_output=True, text=True)
            
            if check.returncode == 0:
                registry = load_json(TOOL_REGISTRY_FILE)
                
                if category not in registry: 
                    registry[category] = {"category_description": category_description, "tools": {}}
                
                registry[category]["category_description"] = category_description
                
                # Save the rich tool data pointing to the new folder
                registry[category]["tools"][tool_name] = {
                    "path": f"/app/workspace/forged_tools/{filename}",
                    "description": tool_description,
                    "usage_objective": objective
                }
                save_json(TOOL_REGISTRY_FILE, registry)
                
                report = f"SUCCESS (Attempt {attempt+1}): Tool '{tool_name}' forged in '{category}'.\n"
                report += f"{token_report}\n"
                
                report += f"Run via: execute_bash('pixi run python /app/workspace/forged_tools/{filename}')\n\n"
                
                report += f"\n<___CODER_CODE___>\n{code}\n</___CODER_CODE___>"
                
                if coder_thinking:
                    report += f"\n<___CODER_THOUGHTS___>\n{coder_thinking}\n</___CODER_THOUGHTS___>"
                    
                return report
            else:
                error_msg = f"Your code failed syntax validation with error:\n{check.stderr}\nPlease fix it and try again. Output ONLY valid python."
                messages.append({"role": "assistant", "content": code})
                messages.append({"role": "user", "content": error_msg})
                
        except Exception as e:
            error_trace = traceback.format_exc()
            return f"Fatal API Error during forging attempt {attempt+1}.\nError: {str(e)}\n\nDetailed Traceback:\n{error_trace}"
            
    return f"FAILED: Coder LLM could not produce valid code after {config.MAX_FORGE_RETRIES} attempts."

if __name__ == "__main__":
    mcp.run()