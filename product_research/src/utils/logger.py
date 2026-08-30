"""
Console & Pipeline Logger Utilities using Rich.
"""

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


def print_banner():
    console.print(
        Panel.fit(
            "[bold cyan]⚡ Autonomous AI Sales Product Intelligence Pipeline[/bold cyan]\n"
            "[dim]Crawls any product website, extracts 7-pillar knowledge & sales playbooks into zero-latency voice outputs[/dim]",
            border_style="cyan"
        )
    )


def print_extraction_summary(kb, playbook):
    table = Table(title="✨ Extracted 7-Pillar Product Intelligence Summary", show_header=True, header_style="bold magenta")
    table.add_column("Pillar #", style="dim", width=10)
    table.add_column("Pillar Name", style="bold white", width=35)
    table.add_column("Key Extracted Elements", style="green")

    table.add_row("Pillar 1", "Core Specs & Capabilities", f"{len(kb.core_specs.features)} Features, {len(kb.core_specs.integrations.native_integrations)} Native Integrations")
    table.add_row("Pillar 2", "Commercials, Pricing & Packaging", f"{len(kb.commercials_pricing.plans)} Tiers ({', '.join(p.name for p in kb.commercials_pricing.plans)}), {len(kb.commercials_pricing.discount_matrix)} Discount Rules")
    table.add_row("Pillar 3", "Value Proposition & ROI Data", f"{len(kb.value_prop_roi.persona_messaging)} Personas, {len(kb.value_prop_roi.pain_point_matrix)} Pain Mappings, {len(kb.value_prop_roi.case_studies)} Case Studies")
    table.add_row("Pillar 4", "Competitive Intelligence", f"{len(kb.competitive_intel.battlecards)} Competitor Battlecards, Migration timeline: {kb.competitive_intel.displacement_strategy.migration_timeline_days}")
    table.add_row("Pillar 5", "Implementation, Support & SLAs", f"Time-to-Value: {kb.implementation_support.time_to_value_timeline[:40]}..., {len(kb.implementation_support.support_tiers)} Support Tiers")
    table.add_row("Pillar 6", "Security, Privacy & Compliance", f"{', '.join(kb.security_compliance.certifications)}, Hosting: {kb.security_compliance.data_hosting_provider}")
    table.add_row("Pillar 7", "Guardrails & Disqualifiers", f"{len(kb.guardrails_disqualifiers.unsupported_features)} Unsupported specs, {len(kb.guardrails_disqualifiers.disqualification_criteria)} Disqualify criteria")
    table.add_row("GTM Pack", "Sales Psychology & Playbooks", f"{len(playbook.discovery_questions)} Discovery Qs, {len(playbook.objection_library)} Objection Loops, {len(playbook.closing_frameworks)} Closing Techniques")

    console.print(table)


def print_compilation_success(compiled_files: dict):
    table = Table(title="🚀 Compiled Production Artifacts", show_header=True, header_style="bold green")
    table.add_column("Tier / Artifact", style="bold yellow", width=25)
    table.add_column("Purpose & Latency Budget", style="cyan", width=35)
    table.add_column("Output File Path", style="dim white")

    table.add_row("Tier 1: Hot Prompt YAML", "In-prompt Hot Context (0ms latency)", compiled_files["tier1_hot_yaml"])
    table.add_row("Tier 2: Fast Lookup JSON", "In-Memory / Redis Key-Value (<15ms)", compiled_files["tier2_fast_json"])
    table.add_row("Tier 3: Edge Case KB", "Atomic Markdown Micro-Chunks (<50ms)", compiled_files["tier3_edge_md"])
    table.add_row("Voice Agent Master Prompt", "Production System Prompt & State Machine", compiled_files["voice_agent_prompt"])

    console.print(table)
