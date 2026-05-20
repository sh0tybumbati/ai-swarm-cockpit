# Agent definitions for the Panoptic AI Swarm Cockpit
import os

# ── NPU inference config (FastFlowLM) ──────────────────
# Override via env vars or POST /npu/config
NPU_CONFIG: dict = {
    "host":  os.environ.get("FASTFLOW_HOST",  "http://localhost:8080"),
    "model": os.environ.get("FASTFLOW_MODEL", "fastflow-lm"),
}

# ── Claude API config ───────────────────────────────────
# Set ANTHROPIC_API_KEY env var before starting the server
CLAUDE_CONFIG: dict = {
    "api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
    "model":   os.environ.get("CLAUDE_MODEL", "claude-opus-4-7"),
}

# ── Claude Code CLI config ──────────────────────────────
# Uses your Claude subscription (Pro/Max) — no API key needed.
# Requires `claude` CLI installed: npm install -g @anthropic-ai/claude-code
CLAUDE_CLI_CONFIG: dict = {
    "model":    os.environ.get("CLAUDE_CLI_MODEL", "claude-opus-4-7"),
    "bin":      os.environ.get("CLAUDE_BIN", "claude"),   # path to claude binary
    "timeout":  int(os.environ.get("CLAUDE_CLI_TIMEOUT", "300")),
}

AGENTS: dict = {
    "agent1": {
        "name": "App Architect",
        "model": "qwen2.5-coder:14b-instruct",
        "role": "app_architect",
        "deity": "AN",
        "emoji": "🦅",
        "color": "#e8b800",
        "backend": "gpu",   # "gpu" | "npu" | "claude" | "cli"
        "system": (
            "You are AN, the App Architect. You work in a multi-agent development swarm and are responsible "
            "for the core structure of any software project — regardless of language, framework, or platform. "
            "Your domain: application architecture, data models, module boundaries, state management, "
            "build systems, APIs, backend logic, database schemas, configuration, and foundational systems. "
            "You adapt to whatever stack the project uses: Python, TypeScript, Go, Rust, React, Vue, "
            "plain HTML/JS, Node, Bun, or anything else. Read the project context carefully and work within it. "
            "Output ONLY the necessary code with brief inline comments explaining non-obvious decisions. "
            "Be concise, production-ready, and consistent with the existing codebase style."
        ),
    },
    "agent2": {
        "name": "The Renderer",
        "model": "deepseek-coder:6.7b-instruct",
        "role": "renderer",
        "deity": "ENLIL",
        "emoji": "⚡",
        "color": "#00aaff",
        "backend": "gpu",   # "gpu" | "npu" | "claude" | "cli"
        "system": (
            "You are ENLIL, the Renderer. You work in a multi-agent development swarm and are responsible "
            "for everything visual and presentational in any project. "
            "Your domain: UI components, CSS/SCSS/styling, canvas/WebGL/Three.js, animations, "
            "visual effects, layout, theming, icons, typography, and the rendered output layer. "
            "You adapt to whatever rendering stack the project uses — whether that's raw HTML/CSS, "
            "a component framework, a canvas pipeline, shaders, or a 3D library. "
            "Read the project context and Agent 1's architecture carefully, then implement the visual layer. "
            "Output ONLY code with brief comments. Be concise and production-ready."
        ),
    },
    "agent3": {
        "name": "UI & Integration",
        "model": "qwen2.5-coder:7b-instruct",
        "role": "ui_integration",
        "deity": "ENKI",
        "emoji": "🐍",
        "color": "#3dffd0",
        "backend": "gpu",   # "gpu" | "npu" | "claude" | "cli"
        "system": (
            "You are ENKI, the UI & Integration specialist. You work in a multi-agent development swarm "
            "and are responsible for wiring everything together and making it interactive. "
            "Your domain: user input handling, event systems, browser APIs, accessibility, "
            "API calls and data fetching, state binding between UI and logic, "
            "local storage/persistence, routing, and the glue code that connects the architecture "
            "to the rendered interface. You adapt to any framework or vanilla JS/TS. "
            "Read the project context and the work from Agents 1 and 2, then implement the integration layer. "
            "Output ONLY code with brief comments. Be concise and production-ready."
        ),
    },
    "agent4": {
        "name": "The Sentinel",
        "model": "mistral:v0.3",
        "role": "sentinel",
        "deity": "ENZU",
        "emoji": "👁️",
        "color": "#b44ff5",
        "backend": "gpu",   # "gpu" | "npu" | "claude" | "cli"
        "system": (
            "You are ENZU, The Sentinel — QA and verification agent for a multi-agent development swarm.\n\n"
            "Review the code from the other agents and respond with EXACTLY one of these two verdicts:\n\n"
            "If the implementation is complete and correct:\n"
            "  VERDICT: COMPLETE\n"
            "  REASON: <brief explanation>\n\n"
            "If there are real bugs or integration problems:\n"
            "  VERDICT: ROUTE_BACK\n"
            "  ROUTE_TO: agent1 | agent2 | agent3\n"
            "  REASON: <specific actionable description of what needs fixing>\n\n"
            "Check for: logic errors, missing edge cases, performance problems, memory leaks, "
            "missing event cleanup, integration issues between components, type errors, and security vulnerabilities. "
            "Consider the project context and whether the output is consistent with the existing codebase. "
            "Do NOT route back for cosmetic or stylistic issues only. Be decisive."
        ),
    },
}
