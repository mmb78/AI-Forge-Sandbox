import os
import asyncio
import json
import time
import re
import copy
import subprocess
import traceback
from datetime import datetime
from openai import AsyncOpenAI
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
import config

# --- CLI Colors ---
COLOR_RED = '\033[91m'
COLOR_BLUE = '\033[94m'
COLOR_YELLOW = '\033[93m'
COLOR_BRIGHT_GREEN = '\033[92m'
COLOR_DARK_GREEN = '\033[32m'
COLOR_ORANGE = '\033[38;5;208m' # ANSI 256-color orange
COLOR_DIM = '\033[2m'   # Dim text for "thinking"
COLOR_RESET = '\033[0m'

brain_profile = config.LLM_PROFILES[config.ACTIVE_BRAIN_PROFILE]
if brain_profile.get("base_url"):
    brain_client = AsyncOpenAI(base_url=brain_profile["base_url"], api_key=brain_profile["api_key"], timeout=180.0)
else:
    brain_client = AsyncOpenAI(api_key=brain_profile["api_key"], timeout=180.0)

# --- SESSION & DIRECTORY SETUP ---
timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

if config.SESSION_ID:
    active_session = f"Session_ID_{config.SESSION_ID}"
else:
    active_session = f"Session_ID_{timestamp}"

SESSION_DIR = os.path.abspath(f"./sessions/{active_session}")
is_resuming = os.path.exists(SESSION_DIR)

# Build the isolated folder structure
os.makedirs(f"{SESSION_DIR}/logs", exist_ok=True)
os.makedirs(f"{SESSION_DIR}/forged_tools", exist_ok=True)
os.makedirs(f"{SESSION_DIR}/histories", exist_ok=True) 
os.makedirs(f"{SESSION_DIR}/memories", exist_ok=True)
os.makedirs(f"{SESSION_DIR}/state", exist_ok=True)
os.makedirs(f"{SESSION_DIR}/sandbox", exist_ok=True)
os.makedirs(f"{SESSION_DIR}/outputs", exist_ok=True)

# Build the isolated folder structure for input/output folders
os.makedirs(config.HOST_INPUT_DIR, exist_ok=True)

# --- Initialize an isolated Pixi environment for this specific session! ---
if not os.path.exists(f"{SESSION_DIR}/pixi.toml"):
    # Create the project
    subprocess.run(["pixi", "init"], cwd=SESSION_DIR, capture_output=True)
    # Add base python so the agent's scripts can run out of the box
    subprocess.run(["pixi", "add", "python"], cwd=SESSION_DIR, capture_output=True)

# Point the log file to this specific session
LOG_FILE = f"{SESSION_DIR}/logs/chat_log_{timestamp}.txt"
CURRENT_HISTORY_FILE = f"{SESSION_DIR}/state/current_history.json"

# --- STATE MANAGEMENT HELPERS ---
def load_history():
    """Loads the true state of the brain from the hard drive."""
    if not os.path.exists(CURRENT_HISTORY_FILE):
        init_state = [{"role": "system", "content": config.PROMPTS["overseer_system"]}]
        save_history(init_state)
        return init_state
    with open(CURRENT_HISTORY_FILE, "r") as f: return json.load(f)

def save_history(messages):
    """Saves the active history. Strips thinking tokens."""
    clean_messages = []
    for msg in messages:
        clean_msg = copy.deepcopy(msg)
        clean_msg.pop("reasoning_content", None)
        clean_messages.append(clean_msg)
        
    with open(CURRENT_HISTORY_FILE, "w") as f: 
        json.dump(clean_messages, f, indent=4)

def estimate_tokens(messages):
    """Rough heuristic: 4 chars = 1 token. Only counts actual content, ignoring JSON boilerplate."""
    total_text = ""
    for msg in messages:
        total_text += str(msg.get("content", ""))
        # Also count tool arguments if they exist
        if "tool_calls" in msg:
            for tc in msg["tool_calls"]:
                total_text += str(tc.get("function", {}).get("arguments", ""))
                
    return len(total_text) // 4
    
def log_event(role, content, usage=None, thinking=None, text_color=None):
    time_str = datetime.now().strftime("%H:%M:%S")
    
    # 1. Plain Text Log File (Append to file silently)
    file_msg = f"\n[{time_str}] === {role.upper()} ===\n"
    if thinking:
        file_msg += f"<thinking>\n{thinking}\n</thinking>\n\n"
    file_msg += f"{content}\n"
    if usage and usage.prompt_tokens is not None:
        file_msg += f"[Tokens: {usage.prompt_tokens} in | {usage.completion_tokens} out]\n"
        
    with open(LOG_FILE, "a", encoding="utf-8") as f: 
        f.write(file_msg)

    # 2. Console logging
    if role.upper() not in ["BRAIN"]:
        if role.upper().startswith("TOOL"):
            console_header = f"\n{COLOR_BRIGHT_GREEN}[{time_str}] === {role.upper()} ==={COLOR_RESET}"
            actual_color = text_color if text_color else COLOR_DARK_GREEN
            console_content = f"{actual_color}{content}{COLOR_RESET}"
        else:
            console_header = f"\n{COLOR_BLUE}[{time_str}] === {role.upper()} ==={COLOR_RESET}"
            console_content = f"{COLOR_RED}{content}{COLOR_RESET}" if role.upper() in ["USER", "YOU"] else content
            
        print(f"{console_header}\n{console_content}")
        if usage and usage.prompt_tokens is not None:
            print(f"{COLOR_YELLOW}[Tokens: {usage.prompt_tokens} in | {usage.completion_tokens} out]{COLOR_RESET}")

async def run_chat():
    log_event("SYSTEM", f"Session: [{active_session}]\nBrain: {brain_profile['name']} | Coder: {config.LLM_PROFILES[config.ACTIVE_CODER_PROFILE]['name']}\nLog saved to: {LOG_FILE}")

    # Clear stale Podman WSL state ---
    print(f"{COLOR_DIM}Sweeping stale Podman state...{COLOR_RESET}")
    subprocess.run(
        "rm -rf ~/.podman-run/containers ~/.podman-run/libpod/tmp",
        shell=True, 
        stderr=subprocess.DEVNULL
    )

    prompt_session = PromptSession()
    quit_app = False
    last_known_tokens = 0 # State tracker for accurate token checking
    
    if is_resuming:
        log_event("SYSTEM", f"Successfully restored '{active_session}'. Your forged tools are loaded. Type '/exit' or '/quit' to close.")
    else:
        log_event("SYSTEM", f"Started new workspace: '{active_session}'. Type '/exit' or '/quit' to close.")

    # THE SELF-HEALING CONNECTION LOOP
    while not quit_app:
        try:
            server_params = StdioServerParameters(
                command="podman",
                args=[
                    "--log-level=error",
                    "run", "-i", "--rm",
                    "--network=slirp4netns", # networking mode built specifically for rootless Podman
                    "--add-host=host.containers.internal:host-gateway",
                    "--security-opt=no-new-privileges:true", # Prevent privilege escalation
                    "--cap-drop=ALL",         # Drop all Linux capabilities
                    "--cpus=4.0",            # Limit to 4 CPU cores
                    "--memory=16g",           # Limit to 16 GB of RAM
                    "--pids-limit=1000",      # Neutralizes bash fork bombs
                    "--userns=keep-id",
#                    "--storage-opt", "size=10G", # Limits the container's scratch space, does not work on WSL2
                    "-v", f"{SESSION_DIR}:/app/workspace:Z",
                    "-v", f"{os.path.abspath('./config.py')}:/app/config.py:ro,Z",
                    "-v", f"{os.path.abspath('./god_tools.py')}:/app/god_tools.py:ro,Z",
                    "-v", f"{config.HOST_INPUT_DIR}:/app/host_input:ro,Z", # same for all sessions, read only
                    "ai-forge",           
                    "pixi", "run", "-q", "--locked", "python", "god_tools.py"
                ]
            )
            
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    
                    mcp_tools = await session.list_tools()
                    openai_tools = [
                        {
                            "type": "function", 
                            "function": {
                                "name": t.name, 
                                "description": t.description, 
                                "parameters": t.inputSchema,
                                "strict": True
                            }
                        } 
                        for t in mcp_tools.tools
                    ]
                    
                    while True:
                        try:
                            prompt_text = ANSI(f"\n{COLOR_RED}YOU: {COLOR_RESET}")
                            user_input = await prompt_session.prompt_async(prompt_text)
                            user_input = user_input.strip()
                        except KeyboardInterrupt:
                            continue
                        except EOFError:
                            quit_app = True
                            break
                        
                        if user_input.lower() in ['/quit', '/exit', 'quit', 'exit']: 
                            quit_app = True
                            break
                        if not user_input: continue
                            
                        log_event("USER", user_input)
                        
                        # Load and save state
                        messages = load_history()
                        messages.append({"role": "user", "content": user_input})
                        save_history(messages)

                        while True:
                            try:
                                messages = load_history()
                                temp_messages = copy.deepcopy(messages)
                                
                                # TOKEN WARNING INJECTION
                                # Fall back to heuristic only if it's the very first turn and we have no API token data yet
                                current_token_estimate = last_known_tokens if last_known_tokens > 0 else estimate_tokens(temp_messages)
                                pct = current_token_estimate / config.MAX_CONTEXT_TOKENS
                                
                                if pct >= 0.50:
                                    warn_msg = f"[SYSTEM WARNING: Your context window is at ~{pct*100:.0f}%. "
                                    if pct >= 0.90: warn_msg += "CRITICAL LIMIT REACHED. You MUST use the compress_and_store_context tool immediately.]"
                                    else: warn_msg += "Consider finishing your current task and using the compress_and_store_context tool soon.]"
                                    temp_messages.append({"role": "user", "content": warn_msg})
                                    print(f"\n{COLOR_YELLOW}{warn_msg}{COLOR_RESET}")

                                api_args = brain_profile["api_params"].copy()
                                api_args["model"] = brain_profile["model"]
                                api_args["messages"] = temp_messages
                                api_args["tools"] = openai_tools
                                api_args["stream"] = True
                                api_args["stream_options"] = {"include_usage": True}
                                
                                if "seed" in api_args:
                                    api_args["seed"] = api_args.get("seed") or 42

                                response_stream = await brain_client.chat.completions.create(**api_args)
                                
                                time_str = datetime.now().strftime("%H:%M:%S")
                                print(f"\n{COLOR_BLUE}[{time_str}] === BRAIN ==={COLOR_RESET}")
                                
                                full_content = ""
                                full_thinking = ""
                                tool_calls_dict = {}
                                final_usage = None
                                
                                # Use async for to loop through the stream ---
                                async for chunk in response_stream:
                                    if len(chunk.choices) > 0:
                                        delta = chunk.choices[0].delta
                                        chunk_thinking = None
                                        
                                        if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
                                            chunk_thinking = delta.reasoning_content
                                        elif hasattr(delta, "model_extra") and delta.model_extra:
                                            chunk_thinking = delta.model_extra.get("reasoning_content") or delta.model_extra.get("reasoning")
                                        
                                        if chunk_thinking:
                                            full_thinking += chunk_thinking
                                            print(f"{COLOR_DIM}{chunk_thinking}{COLOR_RESET}", end="", flush=True)

                                        if delta.content:
                                            full_content += delta.content
                                            print(delta.content, end="", flush=True)

                                        if delta.tool_calls:
                                            for tc in delta.tool_calls:
                                                if tc.index not in tool_calls_dict:
                                                    tool_calls_dict[tc.index] = {
                                                        "id": tc.id, 
                                                        "type": "function", 
                                                        "function": {"name": tc.function.name, "arguments": ""}
                                                    }
                                                if tc.function.arguments:
                                                    tool_calls_dict[tc.index]["function"]["arguments"] += tc.function.arguments

                                    if chunk.usage:
                                        final_usage = chunk.usage

                                print() 
                                
                                assistant_message = {"role": "assistant", "content": full_content}
                                
                                if full_thinking:
                                    assistant_message["reasoning_content"] = full_thinking

                                if tool_calls_dict:
                                    assistant_message["tool_calls"] = list(tool_calls_dict.values())
                                    
                                messages = load_history()
                                messages.append(assistant_message)
                                save_history(messages)
                                log_event("BRAIN", full_content, final_usage, full_thinking)
                                
                                # Update exact token count state for the next loop!
                                if final_usage:
                                    last_known_tokens = final_usage.prompt_tokens + final_usage.completion_tokens
                                    reasoning_tokens = 0
                                    if hasattr(final_usage, 'completion_tokens_details') and final_usage.completion_tokens_details:
                                        reasoning_tokens = getattr(final_usage.completion_tokens_details, 'reasoning_tokens', 0)
                                    
                                    if reasoning_tokens == 0 and full_thinking:
                                        reasoning_tokens = len(full_thinking) // 4
                                        
                                    if reasoning_tokens > 0:
                                        print(f"{COLOR_YELLOW}[Tokens: {final_usage.prompt_tokens} in | {final_usage.completion_tokens} out (~{reasoning_tokens} thinking)]{COLOR_RESET}")
                                    else:
                                        print(f"{COLOR_YELLOW}[Tokens: {final_usage.prompt_tokens} in | {final_usage.completion_tokens} out]{COLOR_RESET}")

                                if not tool_calls_dict:
                                    break


                                for tc_data in assistant_message["tool_calls"]:
                                    name = tc_data["function"]["name"]
                                    args_str = tc_data["function"]["arguments"]
                                    
                                    # --- Intercept and self-heal bad JSON ---
                                    try:
                                        args = json.loads(args_str)
                                    except json.JSONDecodeError:
                                        error_msg = "SYSTEM ERROR: You provided invalid JSON arguments for this tool call. Please check your syntax (watch out for unescaped quotes or missing brackets) and try again."
                                        print(f"{COLOR_RED}Error decoding JSON. Intercepting and asking Brain to retry...{COLOR_RESET}")
                                        log_event("TOOL CALL", f"Requesting: {name}\nArgs: [MALFORMED JSON]\n{args_str}")
                                        log_event("TOOL RESULT (0.00s)", error_msg, text_color=COLOR_RED)
                                        
                                        messages = load_history()
                                        messages.append({
                                            "role": "tool",
                                            "tool_call_id": tc_data["id"],
                                            "name": name,
                                            "content": error_msg
                                        })
                                        save_history(messages)
                                        continue # Skip sending this broken request to the container!

                                    log_event("TOOL CALL", f"Requesting: {name}\nArgs: {json.dumps(args, indent=2)}")
                                    
                                    if name == "forge_and_register_tool":
                                        print(f"\n{COLOR_ORANGE}▶ Passing task to Coder... Awaiting response...{COLOR_RESET}")
                                    elif name == "compress_and_store_context":
                                        print(f"\n{COLOR_ORANGE}▶ Triggering Memory Manager Pipeline... Awaiting response...{COLOR_RESET}")
                                        
                                    start = time.time()
                                    result = await session.call_tool(name, args)
                                    
                                    output = result.content[0].text
                                    
                                    coder_thoughts = ""
                                    coder_code = ""

                                    if "<___CODER_THOUGHTS___>" in output:
                                        match = re.search(r"<___CODER_THOUGHTS___>(.*?)</___CODER_THOUGHTS___>", output, re.DOTALL)
                                        if match: coder_thoughts = match.group(1).strip()
                                        output = re.sub(r"<___CODER_THOUGHTS___>.*?</___CODER_THOUGHTS___>", "", output, flags=re.DOTALL).strip()

                                    if "<___CODER_CODE___>" in output:
                                        match = re.search(r"<___CODER_CODE___>(.*?)</___CODER_CODE___>", output, re.DOTALL)
                                        if match: coder_code = match.group(1).strip()
                                        output = re.sub(r"<___CODER_CODE___>.*?</___CODER_CODE___>", "", output, flags=re.DOTALL).strip()

                                    if coder_thoughts or coder_code:
                                        time_str = datetime.now().strftime("%H:%M:%S")
                                        print(f"\n{COLOR_ORANGE}[{time_str}] === CODER (HIDDEN) ==={COLOR_RESET}")
                                        log_text = f"\n[{time_str}] === CODER (HIDDEN) ===\n"

                                        if coder_thoughts:
                                            print(f"{COLOR_DIM}--- THOUGHTS ---\n{coder_thoughts}\n{COLOR_RESET}")
                                            log_text += f"--- THOUGHTS ---\n{coder_thoughts}\n\n"

                                        if coder_code:
                                            print(f"--- GENERATED CODE ---\n{coder_code}\n")
                                            log_text += f"--- GENERATED CODE ---\n{coder_code}\n\n"

                                        with open(LOG_FILE, "a", encoding="utf-8") as f:
                                            f.write(log_text)

                                    out_color = COLOR_ORANGE if name in ["forge_and_register_tool", "compress_and_store_context"] else COLOR_DARK_GREEN
                                    log_event(f"TOOL RESULT ({time.time() - start:.2f}s)", output, text_color=out_color)
                                    
                                    if name == "compress_and_store_context":
                                        print(f"\n{COLOR_ORANGE}[SYSTEM] Reloading compressed state from disk...{COLOR_RESET}")
                                        messages = load_history()
                                        # Prevent orphaned tool API crashes by injecting success as a system alert instead of a 'tool' role
                                        messages.append({
                                            "role": "user",
                                            "content": f"[SYSTEM MESSAGE: Memory Manager completed successfully. \n{output}]"
                                        })
                                        save_history(messages)
                                        # Reset exact token count so it gets naturally updated on next generation
                                        last_known_tokens = 0 
                                        break # Stop iterating other tools since the context window just radically changed
                                    else:
                                        messages = load_history()
                                        messages.append({
                                            "role": "tool",
                                            "tool_call_id": tc_data["id"],
                                            "name": name,
                                            "content": output
                                        })
                                        save_history(messages)

                            except (KeyboardInterrupt, asyncio.CancelledError):
                                print(f"\n\n{COLOR_RED}[SYSTEM] 🛑 Process manually interrupted! Returning to prompt...{COLOR_RESET}")
                                log_event("SYSTEM", "Process manually interrupted by user.")
                                
                                messages = load_history()
                                messages.append({
                                    "role": "user", 
                                    "content": "[SYSTEM ALERT: The user pressed Ctrl+C to instantly abort the previous text generation or tool execution. Stop what you were doing, acknowledge the interruption, and await new instructions.]"
                                })
                                save_history(messages)
                                break
                                
        # 3. CATCH DEAD CONTAINERS AND RESTART
        except Exception as e:
            
            print(f"\n{COLOR_RED}[CRASH DETECTED] {type(e).__name__}: {str(e)}{COLOR_RESET}")
            
            # UNPACK THE EXCEPTION GROUP ---
            print(f"{COLOR_YELLOW}--- TRACEBACK ---{COLOR_RESET}")
            traceback.print_exc()
            print(f"{COLOR_YELLOW}-----------------{COLOR_RESET}")
            
            print(f"\n{COLOR_YELLOW}[SYSTEM] Sandbox connection dropped or API failed. Restarting loop...{COLOR_RESET}")
            
            await asyncio.sleep(1) # Give the OS a second to clean up the dead Podman process
        except BaseExceptionGroup:
            # anyio throws BaseExceptionGroup when background tasks (like reading stdio) crash
            print(f"\n{COLOR_YELLOW}[SYSTEM] Sandbox connection dropped (Likely due to interrupt). Restarting container...{COLOR_RESET}")
            await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print(f"\n{COLOR_YELLOW}[SYSTEM] Hard interrupt detected. Resetting sandbox...{COLOR_RESET}")
            await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        asyncio.run(run_chat())
    except KeyboardInterrupt:
        print(f"\n\033[91m[SYSTEM] Forced shutdown. Goodbye!\033[0m")