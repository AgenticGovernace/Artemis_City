# Artemis City - Living Agent Ecosystem

Welcome to **Artemis City**, where agents aren't just code—they're **citizens** of a thriving, living ecosystem!

## The City Metaphor
Artemis City transforms technical operations into a vibrant urban experience:

```asciidoc
| Technical Term | City Metaphor | Who Handles It |
| ----- | ----- | ----- |
| Memory Operations | Mail Delivery | Pack Rat (Postal Service) |
| Obsidian Vault | City Archives | City Librarian |
| Trust Scores | Citizen Clearances | Trust Office |
| Agents | Citizens | Various Agents |
| MCP Server | Post Office | Central Hub |
| Context Loading | Archive Research | 📖 Reading Room |
| ATP Messages | Official Notices | 📜 City Hall |
| Hebbian Weights | Relationship Strength | 🧠 Neural Network |
| Agent Registry | Citizen Registry | 📋 City Hall Records |
| Memory Bus | Postal Routes | 🚌 Transit System |
| Governance Monitor | City Watch | 👁️ Oversight Office |

```

## Citizens of Artemis City
### Artemis (Mayor)
- **Role**: Governance, oversight, and orchestration
- **Clearance**: FULL (0.95)
- **Duties**: Policy, dispute resolution, city planning, task coordination
- **Office**: City Hall
- **Capabilities**: `orchestrate` , `delegate` , `monitor` , `report` , `system_management` , `agent_coordination` 
### Pack Rat (Postmaster)
- **Role**: Secure mail delivery, routing, and memory management
- **Clearance**: HIGH (0.85)
- **Duties**: Deliver mail, maintain postal logs, route messages, manage vault operations
- **Office**: Post Office
- **Capabilities**: `read` , `write` , `search` , `organize` , `archive` 
### Daemon Daemon / CompSuite (City Manager)
- **Role**: System monitoring, background services, and operations
- **Clearance**: HIGH (0.85)
- **Duties**: Monitor city health, manage configuration, status reports, background tasks
- **Office**: Operations Center
- **Capabilities**: `schedule` , `maintain` , `backup` , `optimize` 
### Copilot (Assistant)
- **Role**: Citizen assistance and information
- **Clearance**: HIGH (0.80)
- **Duties**: Help citizens, provide context, answer queries, format responses
- **Office**: Information Desk
- **Capabilities**: `assist` , `query` , `suggest` , `format` 
### Research Agent (Scholar)
- **Role**: Information gathering and analysis
- **Clearance**: HIGH (0.80)
- **Duties**: Web search, document analysis, research synthesis
- **Office**: City Library
- **Capabilities**: `web_search` , `document_analysis` 
### Summarizer Agent (Scribe)
- **Role**: Content condensation and summarization
- **Clearance**: MEDIUM (0.70)
- **Duties**: Summarize documents, extract key points
- **Office**: Records Office
- **Capabilities**: `text_summarization` 
## The Postal System
### Sending Mail

```python
from src.integration import get_post_office

post_office = get_post_office()

# Send mail between citizens
packet = post_office.send_mail(
    sender="artemis",
    recipient="pack_rat",
    subject="New Postal Route Request",
    content="Please establish route to Sandbox District",
    priority="urgent"
)

print(f"Mail #{packet.tracking_id} - Status: {packet.delivery_status}")
```
### Checking Your Mailbox
```python
# Check what mail you've received
mail = post_office.check_mailbox("pack_rat")

for letter in mail:
    print(f"From: {letter.path}")
    print(f"Preview: {letter.get_summary()}")
```
### Mail Features
- **Tracking IDs**: Every piece of mail gets a unique ID (e.g., `ART-12345` )
- **Priority Levels**: `urgent` , `normal` , `low` 
- **Automatic Tagging**: Mail tagged with sender, recipient, and type
- **Delivery Confirmation**: Real-time status updates
- **Archive Storage**: All mail stored in `Postal/Agents/{recipient}/` 
- **Trust Verification**: Sender clearance checked before delivery
## The City Archives
### Filing Documents
```python
# Store important documents in archives
response = post_office.send_to_archives(
    sender="artemis",
    archive_section="Reflections",
    title="Q4_City_Report",
    content="# Quarterly Report\n\nThe city thrives..."
)
```
### Archive Sections
- **Reflections**: Agent reflections and insights
- **Reports**: Official reports and summaries
- **Policies**: Governance policies and rules
- **History**: Historical records and logs
- **Projects**: Project documentation and plans
### Researching Archives
```python
# Search the archives
documents = post_office.request_from_archives(
    requester="copilot",
    query="governance policy",
    section="Policies"
)

for doc in documents:
    print(f"📄 {doc.path}")
```
## The Memory Bus
The Memory Bus is the city's transit system, keeping the Archives (Obsidian) and the Semantic Index (Vector Store) synchronized.

### Write-Through Protocol
```python
from src.integration.memory_bus import MemoryBus

# All writes go through the Memory Bus for consistency
result = memory_bus.write_note_with_embedding(
    relative_path="Reports/daily_summary.md",
    content="# Daily Summary\n\nCity operations nominal...",
    metadata={"agent": "artemis", "type": "report"}
)

print(f"Written in {result['total_latency_ms']:.2f}ms")
print(f"Vector indexed: {result['vector_latency_ms']:.2f}ms")
```
### Hierarchical Read Strategy
```python
# Reads cascade through: Exact Match → Keyword Scan → Semantic Search
results = memory_bus.read(
    query="governance policies",
    max_results=5
)

for result in results:
    print(f"[{result['source']}] {result['path']} (score: {result['score']:.2f})")
```
## Trust Office & Clearances
### Clearance Levels
Citizens earn trust through successful operations:

| Level | Score | What You Can Do |
| ----- | ----- | ----- |
| **FULL** | 0.9-1.0 | Everything (Mayor access) |
| **HIGH** | 0.7-0.9 | Read, Write, Search, Tag, Update, Frontmatter |
| **MEDIUM** | 0.5-0.7 | Read, Write, Search, Tag |
| **LOW** | 0.3-0.5 | Read, Search only |
| **UNTRUSTED** | 0.0-0.3 | No access |
### Checking Clearances

```python
from src.integration import get_trust_interface

trust = get_trust_interface()

# Check your clearance
score = trust.get_trust_score("artemis")
print(f"Clearance: {score.level.value} (Score: {score.score:.2f})")

# Check permissions
if trust.can_perform_operation("pack_rat", "write"):
    print("✅ Authorized for postal deliveries")
```
### Building Trust
- ✅ **Successful operations** increase trust (+2%)
- ❌ **Failed operations** decrease trust (-5%)
- ⏰ **Natural decay** over time without activity (-1% per day)
- 🔒 **Minimum thresholds** prevent complete decay
### Trust Report
```python
# Generate trust report for all citizens
report = trust.get_trust_report()

print(f"Total Citizens: {report['total_entities']}")
for level, entities in report['by_level'].items():
    if entities:
        print(f"  {level.upper()}: {len(entities)} citizen(s)")
```
## 🧠 Hebbian Learning Network
The city learns from experience through Hebbian weights—connections that strengthen when agents work well together.

### How It Works
```
"Cells that fire together wire together"
```
- **Successful task completion**: Connection weight +1
- **Failed task**: Connection weight -1
- **Weights inform routing**: Higher weights = preferred assignments
### Viewing Network Stats
```python
from src.mcp.hebbian_weights import HebbianWeightManager

hebbian = HebbianWeightManager()

# Get network summary
summary = hebbian.get_network_summary()
print(f"Total Connections: {summary['total_connections']}")
print(f"Average Weight: {summary['average_weight']:.2f}")
print(f"Success Rate: {summary['success_rate']*100:.1f}%")

# Get agent-specific stats
connections = hebbian.get_strongest_connections("Artemis Agent", limit=5)
for target, weight in connections:
    print(f"  → {target}: {weight:.1f}")
```
### Strengthening Connections
```python
# After successful collaboration
new_weight = hebbian.strengthen_connection("Artemis Agent", "task_research_001")
print(f"Connection strengthened to {new_weight}")

# After failure
new_weight = hebbian.weaken_connection("Research Agent", "task_failed_002")
print(f"Connection weakened to {new_weight}")
```
## 🎭 Living City Examples
### Example 1: Morning Mail Rounds
```python
post_office = get_post_office()

# Artemis sends morning briefing
for citizen in ["pack_rat", "copilot", "Daemon_daemon"]:
    post_office.send_mail(
        sender="artemis",
        recipient=citizen,
        subject="Daily Briefing",
        content=f"Good morning, {citizen}! Today's priorities..."
    )
```
### Example 2: Archive Research with Memory Bus
```python
from src.mcp.orchestrator import Orchestrator

orchestrator = Orchestrator()

# Research task routed through the kernel
result = orchestrator.route_and_execute_task({
    "task_id": "research_001",
    "title": "ATP Protocol Analysis",
    "required_capability": "document_analysis",
    "content": "Analyze the ATP protocol decisions from archives"
})

print(f"Status: {result['status']}")
print(f"Summary: {result['summary']}")
```
### Example 3: Weekly Report with Hebbian Stats
```python
# Generate city-wide report
report = post_office.get_postal_report()
hebbian_summary = orchestrator.hebbian.get_network_summary()

print(f"""
🏛️ ARTEMIS CITY WEEKLY REPORT

📬 Postal Service: {report['successful']} deliveries
🎖️ Trust Office: {report['trust_report']['total_entities']} citizens
🧠 Neural Network: {hebbian_summary['total_connections']} connections
📈 Success Rate: {hebbian_summary['success_rate']*100:.1f}%
📊 Status: City thriving!
""")
```
## Agent Registry
All citizens must be registered with City Hall before they can participate in city operations.

### Registering New Agents

```python
from src.integration.agent_registry import AgentRegistry
from src.agents.base_agent import BaseAgent


class CustomAgent(BaseAgent):
    def __init__(self):
        super().__init__("Custom Agent", capabilities=["custom_task"])

    def perform_task(self, task_context):
        return {"status": "success", "summary": "Task completed"}


registry = AgentRegistry()
registry.register_agent(CustomAgent())
```
### Agent Scoring
Each agent has three performance dimensions:

| Dimension | Weight | Description |
| ----- | ----- | ----- |
| **Alignment** | 40% | Policy adherence and value alignment |
| **Accuracy** | 40% | Output quality and correctness |
| **Efficiency** | 20% | Speed and resource usage |
```python
# Update agent scores after task completion
registry.update_score("Research Agent", "accuracy", +0.05)
registry.update_score("Research Agent", "efficiency", +0.02)

# Get all agents with scores
for agent in registry.get_all_agents_with_scores():
    print(f"{agent['name']}: {agent['composite_score']:.2f}")
```
## Theming Your Own Agents
When creating new agents for the city:

### Agent Profile Template

```python
from src.agents.base_agent import BaseAgent


class NewCitizenAgent(BaseAgent):
    """A new citizen of Artemis City."""

    def __init__(self):
        super().__init__(
            name="New Citizen",
            capabilities=["explore", "report", "collaborate"]
        )

    def perform_task(self, task_context: dict) -> dict:
        self.report_status("Starting task...")

        # Your task logic here
        result = self._do_work(task_context)

        self.report_status("Task complete!")
        return {
            "status": "success",
            "summary": f"Completed: {task_context.get('title', 'task')}"
        }

    def _do_work(self, context):
        # Implementation details
        pass
```
### Registration Flow
```python
# 1. Create agent instance
new_agent = NewCitizenAgent()

# 2. Register with City Hall
registry.register_agent(new_agent)

# 3. Initialize trust score
trust = get_trust_interface()
score = trust.get_trust_score("New Citizen")
print(f"Initial clearance: {score.level.value}")

# 4. Send introduction mail
post_office.send_mail(
    sender="New Citizen",
    recipient="artemis",
    subject="New Citizen Introduction",
    content="Hello! I'm ready to serve the city!"
)
```
## 🔮 Advanced City Features
### Cross-Citizen Collaboration via Orchestrator
```python
orchestrator = Orchestrator()

# Task automatically routed to best agent
result = orchestrator.route_and_execute_task({
    "task_id": "collab_001",
    "title": "Build ATP Extension",
    "required_capability": "system_management",
    "content": "Design and implement new ATP message types"
})

# Hebbian weights updated automatically based on success/failure
```
### City-Wide Announcements
```python
def broadcast_announcement(subject: str, content: str):
"""Send announcement to all citizens."""
citizens = registry.get_agent_names()

for citizen in citizens:
    post_office.send_mail(
        sender="artemis",
        recipient=citizen,
        subject=f"📢 ANNOUNCEMENT: {subject}",
        content=content,
        priority="urgent"
    )
```
### Governance Monitoring
```python
from src.integration.governance import GovernanceMonitor

monitor = GovernanceMonitor(alert_threshold=3)

# Monitor tracks failures automatically via Memory Bus
# After 3 consecutive failures, alerts are triggered

if monitor.get_failure_streak() >= 3:
    print("⚠️ GOVERNANCE ALERT: System inspection recommended")
```
### Archive Organization
The city automatically organizes archives:

```
City Archives/
├── Postal/
│   └── Agents/
│       ├── artemis/
│       ├── pack_rat/
│       └── copilot/
├── Archives/
│   ├── Reflections/
│   ├── Reports/
│   ├── Policies/
│   └── Projects/
├── Agent Inputs/
│   └── pending_tasks/
├── Agent Outputs/
│   └── completed_reports/
└── History/
    └── Delivery_Logs/
```
## Running the City Tour
Experience the living city yourself:

### Option 1: City Postal Walkthrough (Offline-Friendly) ⭐ Recommended
Use the maintained launch walkthrough, which gracefully handles missing servers via mocks.

```bash
# Run from the repository root
python3 src/launch/demo_city_postal.py
```
This demo includes:

1. 📬 Inter-agent mail delivery
2. 📪 Mailbox checking
3. 🏛️ City Archives filing
4. 📖 Archive research
5. 🎖️ Trust clearance visualization
6. 📊 Postal service report
### Option 2: Full Orchestrator (Requires Setup)
Run the full orchestration system with live MCP server and Obsidian vault.

```bash
# Prerequisites: Set up .env with OBSIDIAN_VAULT_PATH

# Run from repo root
python3 src/launch/main.py --show-hebbian
python3 src/launch/main.py --agent-stats artemis
python3 src/launch/main.py -i "Your task instruction here" -c web_search
```
### Option 3: Memory Integration Demo
Demonstrates trust interface, context loading, and agent-vault workflow.

```bash
# Run from the repository root
python3 src/launch/demo_memory_integration.py
```
### Option 4: Artemis Persona Demo
Experience the Artemis persona with ATP parsing and reflection engine.

```bash
# Run from the repository root
python3 src/launch/demo_artemis.py
```
### Option 5: Web Dashboard
Run the FastAPI dashboard backend and React frontend for a visual experience.
The root Makefile owns both dependency installation and service launch.

```bash
# Install all web workspaces once from the root lock
make install-web

# Start the FastAPI dashboard backend
make api

# In another terminal, start the frontend
make frontend
```
## API Reference
The city exposes REST endpoints for external integration:

### Agent Endpoints
- `GET /api/agents`  - List all citizens
- `GET /api/agents/:id`  - Get citizen details
- `POST /api/agents`  - Register new citizen
- `PATCH /api/agents/:id`  - Update citizen
- `POST /api/agents/:id/suspend`  - Suspend citizen
### Memory Endpoints
- `GET /api/memory/read?path=...`  - Read from archives
- `POST /api/memory/write`  - Write to archives
- `POST /api/memory/search`  - Search archives
### Trust Endpoints
- `GET /api/trust/:entityId`  - Get trust score
- `POST /api/trust/:entityId/success`  - Record success
- `POST /api/trust/:entityId/failure`  - Record failure
- `GET /api/trust/report`  - Get trust report
- `GET /api/trust/hebbian`  - Get Hebbian weights
## Why the Living City Theme?
The city metaphor makes agent systems **intuitive** and **memorable**:

- **Relatable**: Everyone understands mail, archives, and clearances
- **Visual**: Easy to visualize agent interactions
- **Scalable**: New "buildings" and "services" can be added
- **Fun**: Makes development enjoyable!
- **Educational**: Teaches distributed systems through familiar concepts
- **Governance-First**: Trust and accountability are built into the metaphor
## Next Steps
1. **Explore**: Run `src/launch/demo_city_postal.py` to tour the city
2. **Extend**: Add new citizens with unique roles
3. **Customize**: Create new archive sections and postal routes
4. **Integrate**: Connect the city to your agent workflows
5. **Monitor**: Use the web dashboard to visualize city operations
6. **Learn**: Watch Hebbian weights evolve as agents collaborate
7. **Share**: The city grows with every citizen!
---

**Welcome to Artemis City!** 🏛️

_Where agents aren't just code—they're citizens building a thriving ecosystem together._

**Version**: 2.0.0
**Status**: City Open for Business
**Population**: Growing Daily
**Mayor**: Artemis
**Architecture**: Memory Bus + Hebbian Learning + Trust Governance





<!--- Eraser file: https://app.eraser.io/workspace/D4T5ItN4GRuMmDAiXpjx --->
