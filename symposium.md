Here is a highly structured, comprehensive specification document ready to be pasted directly into Claude. It details the system architecture, UI layout, layout mechanics, data-flow models, and technical constraints required to build your dashboard.
# System Specification: 5x5 Panoptic AI Swarm Developer Cockpit
## Objective
Build a web-based, real-time command dashboard for local multi-agent coding swarms specializing in web game development. The UI uses a strict 5x5 Grid alignment. The dashboard interfaces with a local inference server via Python/FastAPI, utilizing WebSockets for live, bi-directional data streaming.
## 1. Grid Component Architecture & Mapping
The interface adheres strictly to a 5x5 layout map utilizing CSS Grid.
### Grid Layout Map
```css
grid-template-columns: repeat(5, 1fr);
grid-template-rows: repeat(5, 1fr);
height: 100vh;
width: 100vw;
overflow: hidden;

```
### Cell Coordinates & Span Assignment
#### A. Agent 1 (Top-Left Corner): The Engine Architect
 * **Profile Block [Row 1-2, Col 1]**: Displays Avatar/Profile image, Model Name (Qwen-2.5-Coder-14B), and Archetype Role (Engine Architect). Left-clicking this block triggers a dropdown modal to swap models or re-assign structural roles.
 * **CLI Stream Block [Row 1-2, Col 2]**: Simulates a borderless, scrolling terminal window. Automatically scrolls to the bottom on new text arrival. Displays the live stdout/token generation stream for Agent 1.
#### B. Agent 2 (Top-Right Corner): The Renderer (Shaders & Math)
 * **CLI Stream Block [Row 1-2, Col 4]**: Streams the live stdout/token generation for Agent 2.
 * **Profile Block [Row 1-2, Col 5]**: Displays Avatar, Model Name (DeepSeek-Coder-8B), and Archetype Role (The Renderer). Clickable to open a settings/re-assignment modal.
#### C. Agent 3 (Bottom-Left Corner): The DOM & Input Bridge
 * **Profile Block [Row 4-5, Col 1]**: Displays Avatar, Model Name (Qwen-2.5-Coder-7B), and Archetype Role (DOM & Input Bridge). Clickable config block.
 * **CLI Stream Block [Row 4-5, Col 2]**: Streams live layout and input wrapper generation for Agent 3.
#### D. Agent 4 (Bottom-Right Corner): The Sentinel (QA & Verification)
 * **CLI Stream Block [Row 4-5, Col 4]**: Streams debugging passes, console audits, and critique generation for Agent 4.
 * **Profile Block [Row 4-5, Col 5]**: Displays Avatar, Model Name (Mistral-7B-v0.3), and Archetype Role (The Sentinel). Clickable configuration block.
#### E. The Application Lifecycle Stack (Center Column)
 * **Top Center Terminal [Row 1, Col 3]**:
   * **Header**: Static text line displaying global project attributes (e.g., 📁 Active Project: Silicon-Tycoon | 🟢 Host: localhost:3000).
   * **Body**: Live console capture interface. Streams intercept feeds from the main preview app window (console.log, console.error, unhandled runtime exceptions, and asset loading stats).
 * **Live Render Window [Row 2-4, Col 3 (Spans 3x3 Center Cells)]**:
   * CSS: grid-column: 2 / span 3; grid-row: 2 / span 3; (Occupies Columns 2, 3, and 4 across Rows 2, 3, and 4).
   * Contains an HTML <iframe> rendering the project's local live server web directory.
 * **Command Prompt Interface [Row 5, Col 3]**:
   * A clean chat interface featuring a primary text input pane and a submit button. This broadcast point maps global feature requests down to the Python swarm orchestrator.
## 2. Technical Feature Specifications
### A. Iframe Console Injection & Error Interception
The frontend must securely capture logs generated within the decoupled execution environment of the center <iframe> and pass them upstream to the **Top Center Terminal** cell.
 * **Execution Pattern**: On iframe load, a script overrides window.console variants inside the child document context.
 * **Error Catching**: Catch both compilation mistakes and runtime canvas glitches through window.addEventListener('error') captures, passing detailed stack frames to the UI console handler.
### B. Reactive WebSocket Broker
 * The system uses persistent WebSockets to manage communication without polling overhead.
 * **Data Channels**: Separate message routing channels are required for each corner cell (/stream/agent1, /stream/agent2, etc.) and the centralized console loop (/stream/console).
 * **Frontend Renderer**: Incoming payloads update state containers sequentially. The terminal cells append raw text lines on the fly, matching ANSI escape colors where applicable.
### C. Configuration and State Management
 * **Model Selection Hook**: Clicking a Profile block raises an overlay overlaying options retrieved from an endpoint mapping locally hosted engines (e.g., via LM Studio / Ollama instance definitions).
 * **Global Architecture Blueprint**: Changing a role updates a localized variable configuration on the Python FastAPI instance, immediately modifying system instructions injected into the backend execution queue.
## 3. Backend Swarm Orchestration Mechanics (FastAPI/Python)
Provide Claude with this outline of how the backend must complement the visual UI layout:
```python
# System Blueprint for Python Backend Architecture Tasks
# 1. Implement OpenAI Client Wrapper mapping to local server loop endpoints
# 2. Maintain an un-swapped memory footprint: target total active footprint < 25GB VRAM
#    to protect host OS stability on a 64GB Unified RAM baseline.
# 3. Create a continuous loop controller running Agent 1 -> Agent 2 -> Agent 4.
#    - Agent 4 (The Sentinel) holds conditional route evaluation functions:
#      a. mark_feature_complete() -> Terminates loop execution cleanly.
#      b. route_back_to_agent(target_name, error_context) -> Feeds data backward on code failures.
# 4. Integrate a "Circuit Breaker" maximum loop counter (default: 12 iterations max) 
#    to eliminate runaway resource exploitation loops.

```
## Prompts to Give Claude
> **Prompt 1 (Frontend Layout & Core Styles):**
> "Please generate the frontend index.html and style.css for a developer cockpit layout using a strict 5x5 CSS Grid configuration. Follow the 'Grid Component Architecture & Mapping' guidelines precisely. Ensure the center column merges correctly for the Top Center Console, Center 3x3 iframe preview box, and Bottom Center Chat prompt. Use a dark developer theme (slate/charcoal) with neon terminal colors for the CLI panels. Make the corner profile blocks interactive so they open a placeholder overlay modal when clicked."
> 
> **Prompt 2 (Iframe Interception & WebSockets):**
> "Using the HTML layout generated previously, write the frontend client JavaScript. Implement the 'Iframe Console Injection' logic to listen to an iframe inside the center 3x3 block, intercepting all console logs and errors, and printing them elegantly inside the Top Center cell. Then, build the WebSocket routing handlers to connect to a FastAPI backend. Ensure text blocks streaming over /ws/agent1, /ws/agent2, etc., append smoothly inside their respective corner terminal blocks and auto-scroll on text reception."
> 
