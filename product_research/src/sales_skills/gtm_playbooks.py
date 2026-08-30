"""
GTM Playbook Utilities and Skill Integrations.
"""

from ..schemas.sales_skills_schema import SalesPlaybook


def enrich_playbook_with_product_context(
    playbook: SalesPlaybook,
    product_name: str,
    target_personas: list,
    discounts: list
) -> SalesPlaybook:
    """Enriches the base sales playbook with dynamically extracted product details."""
    # Update ICP if personas are provided
    if target_personas:
        roles = [p.role_title for p in target_personas if hasattr(p, 'role_title')]
        if roles:
            playbook.icp_summary = (
                f"Primary Decision Makers for {product_name}: {', '.join(roles)}. "
                "Targeting companies facing operational friction and scaling bottlenecks."
            )
    return playbook
