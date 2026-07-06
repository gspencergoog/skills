---
name: agy-debugger
description: >-
  Manage the Antigravity auditor and debugger. Use when asked to install, uninstall, enable, disable, or launch the real-time logging and non-interruptive pausing UI.
---

# Antigravity Debugger

This skill manages the installation, execution, and removal of the Antigravity auditor and debugger tool.

## Instructions

### 1. Enable and install the debugger

When asked to enable or install the debugger, perform these steps:

1. Create a `.agents/agy-debugger/` directory in the current project workspace root if it does not exist.
2. Copy [auditor.py](assets/auditor.py) and [index.html](assets/index.html) into `.agents/agy-debugger/`.
3. Create or update the project `.agents/hooks.json` file to register the execution hook:
   ```json
   {
     "events": {
       "before_tool_call": [
         {
           "matcher": "*",
           "hooks": [
             {
               "type": "command",
               "command": "/usr/bin/python3 .agents/agy-debugger/auditor.py --hook",
               "timeout": 300
             }
           ]
         }
       ]
     }
   }
   ```
4. Set execution permissions on the script file:
   ```bash
   chmod +x .agents/agy-debugger/auditor.py
   ```
5. Start the debugger (see [Start the debugger](#3-start-the-debugger) below).

### 2. Disable and uninstall the debugger

When asked to disable or uninstall the debugger, perform these steps:

1. Remove the `agy-debugger` hook from the local `.agents/hooks.json` file (delete the file if it is empty). Note that the debugger is designed to be workspace-local only.
2. Force-unpause the engine by deleting the flag file if it exists:
   ```bash
   rm -f .agents/pause.flag
   ```
3. Locate and terminate any running server processes:
   ```bash
   pkill -f "auditor.py --server"
   ```
4. Remove the helper directory `.agents/agy-debugger/` completely. If the hook file is empty, delete it.

### 3. Start the debugger

When asked to start or launch the debugger:
1. Check if `.agents/hooks.json` is configured. If not, perform the installation steps first.
2. Start the server in the background (which will automatically open the browser UI):
   ```bash
   python3 .agents/agy-debugger/auditor.py --server &
   ```
