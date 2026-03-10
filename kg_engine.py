"""
Knowledge Graph Engine
- Per-domain concept graph management
- Mastery tracking + decay
"""

import os
import json
import time

KNOWLEDGE_DIR = os.path.join(os.path.dirname(__file__), "knowledge")


def ensure_dir():
    os.makedirs(KNOWLEDGE_DIR, exist_ok=True)


def list_domains():
    ensure_dir()
    return [f.replace(".json", "") for f in os.listdir(KNOWLEDGE_DIR) if f.endswith(".json")]


def load_domain(domain_id):
    path = os.path.join(KNOWLEDGE_DIR, f"{domain_id}.json")
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return None


def save_domain(domain_id, graph):
    ensure_dir()
    path = os.path.join(KNOWLEDGE_DIR, f"{domain_id}.json")
    with open(path, "w") as f:
        json.dump(graph, f, indent=2, ensure_ascii=False)


def create_domain(domain_id, display_name):
    graph = {
        "domain_id": domain_id,
        "display_name": display_name,
        "created": time.strftime("%Y-%m-%d"),
        "last_studied": None,
        "level": None,
        "concepts": {},
    }
    save_domain(domain_id, graph)
    return graph


def add_concept(graph, concept_id, display_name, prerequisites=None):
    if concept_id not in graph["concepts"]:
        graph["concepts"][concept_id] = {
            "display_name": display_name,
            "mastery": 0.0,
            "times_tested": 0,
            "times_correct": 0,
            "last_tested": None,
            "prerequisites": prerequisites or [],
        }
    return graph


def update_mastery(graph, concept_id, correct):
    if concept_id not in graph["concepts"]:
        return graph
    c = graph["concepts"][concept_id]
    c["times_tested"] += 1
    c["last_tested"] = time.strftime("%Y-%m-%d")
    if correct:
        c["times_correct"] += 1
        c["mastery"] = min(1.0, c["mastery"] + 0.2)
    else:
        c["mastery"] = max(0.0, c["mastery"] - 0.15)
        c["times_correct"] = 0
    return graph


def decay_mastery(graph, days_threshold=3):
    today = time.strftime("%Y-%m-%d")
    for cid, c in graph["concepts"].items():
        if c["last_tested"]:
            days = (time.mktime(time.strptime(today, "%Y-%m-%d")) -
                    time.mktime(time.strptime(c["last_tested"], "%Y-%m-%d"))) / 86400
            if days >= days_threshold:
                decay = 0.05 * (days // days_threshold)
                c["mastery"] = max(0.0, c["mastery"] - decay)
    return graph


def get_weakest_concepts(graph, n=5):
    concepts = [(cid, c) for cid, c in graph["concepts"].items()]
    concepts.sort(key=lambda x: x[1]["mastery"])
    return concepts[:n]


def get_ready_concepts(graph):
    ready = []
    for cid, c in graph["concepts"].items():
        if c["mastery"] >= 0.7:
            continue
        prereqs_met = all(
            graph["concepts"].get(p, {}).get("mastery", 0) >= 0.5
            for p in c["prerequisites"]
        )
        if prereqs_met:
            ready.append((cid, c))
    ready.sort(key=lambda x: x[1]["mastery"])
    return ready


def graph_summary(graph):
    if not graph["concepts"]:
        return f"Domain: {graph['display_name']} - no concepts yet"
    lines = [f"Domain: {graph['display_name']} (Level: {graph['level'] or 'unknown'})"]
    strong, medium, weak, unknown = [], [], [], []
    for cid, c in graph["concepts"].items():
        name = c["display_name"]
        if c["times_tested"] == 0:
            unknown.append(name)
        elif c["mastery"] >= 0.7:
            strong.append(name)
        elif c["mastery"] >= 0.4:
            medium.append(name)
        else:
            weak.append(name)
    if strong: lines.append(f"  Strong: {', '.join(strong)}")
    if medium: lines.append(f"  Medium: {', '.join(medium)}")
    if weak: lines.append(f"  Weak: {', '.join(weak)}")
    if unknown: lines.append(f"  Untested: {', '.join(unknown)}")
    return "\n".join(lines)


def graph_status_display(graph):
    if not graph["concepts"]:
        print(f"  📚 {graph['display_name']} - empty")
        return
    print(f"\n📊 {graph['display_name']} (Level: {graph['level'] or '?'})")
    for cid, c in graph["concepts"].items():
        m = c["mastery"]
        tested = c["times_tested"]
        emoji = "⚪" if tested == 0 else "🟢" if m >= 0.7 else "🟡" if m >= 0.4 else "🔴"
        bar = "█" * int(m * 10) + "░" * (10 - int(m * 10))
        print(f"  {emoji} {c['display_name']:30s} [{bar}] {m:.0%} ({tested} tested)")
    print()
