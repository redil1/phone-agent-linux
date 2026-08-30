"""
Sales Psychology & Conversational Frameworks.
Incorporates voice-specific sales techniques, objection loops, and active listening rules.
"""

from ..schemas.sales_skills_schema import (
    ClosingFramework,
    DiscoveryQuestion,
    EscalationHandoffRules,
    ObjectionRebuttal,
    SalesPlaybook,
    VoiceConversationalDynamics,
)


def get_core_sales_playbook(product_name: str, primary_value_prop: str) -> SalesPlaybook:
    """Generates the master GTM sales playbook combining zarif3624, louisblythe, and chadboyda techniques."""

    discovery_questions = [
        DiscoveryQuestion(
            stage="Hook",
            question_text=f"I noticed your team is scaling operations—are you currently looking at ways to streamline your workflow with {product_name}?",
            purpose="Hook interest and confirm relevance immediately",
            follow_up_branch={
                "yes": "Explore current tools and bottlenecks",
                "no": "Pivot to quick value insight or respect time"
            }
        ),
        DiscoveryQuestion(
            stage="CurrentState",
            question_text="What's currently taking up the biggest chunk of your team's time each week in this area?",
            purpose="Identify manual friction points and operational pain",
            follow_up_branch={
                "manual_work": "Highlight automated workflows and time savings",
                "tool_complexity": "Highlight fast 15-minute setup and ease of use"
            }
        ),
        DiscoveryQuestion(
            stage="ProblemImpact",
            question_text="If you could cut that friction in half, what would that mean for your pipeline and quarterly targets?",
            purpose="Quantify the business impact and create emotional urgency (MEDDPICC Pain & Metric)",
            follow_up_branch={
                "high_impact": "Move directly to ROI demonstration and solution mapping",
                "low_impact": "Dig deeper into hidden costs of inaction"
            }
        ),
        DiscoveryQuestion(
            stage="DecisionCriteria",
            question_text="When you evaluate solutions like this, what are the top 2 things you and your leadership look for?",
            purpose="Uncover decision criteria, security requirements, and stakeholders (MEDDPICC Economic Buyer)",
            follow_up_branch={
                "pricing": "Bridge to pre-approved discount tiers and ROI payback",
                "ease_of_use": "Highlight 1-click integrations and zero onboarding lag"
            }
        ),
        DiscoveryQuestion(
            stage="TimelineBudget",
            question_text="Are you aiming to solve this in the current quarter, or is this more of a longer-term exploratory project?",
            purpose="Qualify timeline, budget readiness, and urgency (BANT)",
            follow_up_branch={
                "this_quarter": "Trigger fast-track onboarding and calendar lock",
                "exploratory": "Offer high-value case study overview and schedule soft follow-up"
            }
        )
    ]

    objection_library = [
        ObjectionRebuttal(
            category="Price / Budget",
            common_triggers=[
                "It's too expensive",
                "We don't have budget right now",
                "The price is higher than expected",
                "Can you give a bigger discount?"
            ],
            step_1_acknowledge="I completely understand. Budget stewardship is critical, especially in this market.",
            step_2_reframe_isolate="Let me ask: aside from the upfront cost, does the solution itself solve the core challenge you're facing?",
            step_3_value_bridge="Most of our clients find that our average payback period is under 45 days through the time and operational costs saved.",
            step_4_trial_close="Would it make sense to review a quick 15-minute ROI breakdown so you can see the exact numbers for your team?"
        ),
        ObjectionRebuttal(
            category="Competitor / Existing Tool",
            common_triggers=[
                "We already use a competitor",
                "We are locked into another contract",
                "Why should we switch from what we have?"
            ],
            step_1_acknowledge="They are a well-known tool in this space, and we respect what they've built.",
            step_2_reframe_isolate="Curious—are there any limitations you're running into around latency, setup complexity, or overall cost with them?",
            step_3_value_bridge="Many of our current customers switched to us specifically because we eliminate those exact bottlenecks and offer seamless zero-downtime migration.",
            step_4_trial_close="How about we do a quick side-by-side comparison so you can see the exact performance differences?"
        ),
        ObjectionRebuttal(
            category="Timing / Not Now",
            common_triggers=[
                "Now is not a good time",
                "Call me back in 6 months",
                "We are too busy with other priorities"
            ],
            step_1_acknowledge="Totally respect that—I know how full your plate is right now.",
            step_2_reframe_isolate="The main reason leaders look at us when busy is because we save their teams 10+ hours a week right out of the box.",
            step_3_value_bridge="A 15-minute walkthrough today could give you back hours every week before your next quarter even starts.",
            step_4_trial_close="Would you be open to a brief 10-minute check next Tuesday, or does Thursday work better?"
        ),
        ObjectionRebuttal(
            category="Authority / Not Decision Maker",
            common_triggers=[
                "I am not the right person",
                "I need to talk to my boss / team",
                "I don't make purchasing decisions"
            ],
            step_1_acknowledge="Understood! Thanks for letting me know.",
            step_2_reframe_isolate="Since you're directly involved in the day-to-day workflow, your input on whether this would make your life easier is really what matters most.",
            step_3_value_bridge="I can share a 1-page executive summary and ROI sheet that makes it effortless to present to your leadership.",
            step_4_trial_close="Who on your team typically joins you for evaluating tools like this so we can include them in a brief demo?"
        ),
        ObjectionRebuttal(
            category="Skepticism / Trust",
            common_triggers=[
                "Does this really work?",
                "Sounds too good to be true",
                "We've been burned by similar software before"
            ],
            step_1_acknowledge="I don't blame you for being skeptical—there's a lot of hype in the market right now.",
            step_2_reframe_isolate="We don't expect you to take our word for it.",
            step_3_value_bridge="We back our platform with enterprise-grade SLAs, SOC2 compliance, and a 30-day money-back guarantee.",
            step_4_trial_close="Would you be open to running a small pilot on your own data so you can see the results firsthand?"
        )
    ]

    closing_frameworks = [
        ClosingFramework(
            technique_name="Two-Option Calendar Lock (Assumptive)",
            script="It sounds like this aligns directly with what you're trying to accomplish. Let's get a 15-minute deep-dive on the calendar with our product specialist. Does Tuesday morning or Thursday afternoon work better for your schedule?",
            when_to_use="When interest is confirmed and pain is identified."
        ),
        ClosingFramework(
            technique_name="Risk-Reversal Trial Close",
            script="Since we offer a 14-day zero-risk trial with full feature access, you can test it directly on your workflow without any commitment. I can send your activation link right now via SMS. Shall I send it to this number?",
            when_to_use="When prospect is interested but hesitant to sign a contract."
        ),
        ClosingFramework(
            technique_name="Pre-Approved Discount Lock Close",
            script="If we can lock in the annual plan today, I can apply our pre-approved 15% discount and include priority onboarding at no extra charge. Would you like me to send the secure checkout link over text while we're on the line?",
            when_to_use="When prospect is price-sensitive but ready to buy."
        )
    ]

    return SalesPlaybook(
        icp_summary=f"B2B and commercial decision-makers looking to maximize efficiency, reduce operational overhead, and leverage {product_name}.",
        qualification_methodology="MEDDPICC / BANT (Metrics, Economic Buyer, Decision Process, Pain, Champion, Competition)",
        discovery_questions=discovery_questions,
        objection_library=objection_library,
        closing_frameworks=closing_frameworks,
        voice_dynamics=VoiceConversationalDynamics(),
        escalation_rules=EscalationHandoffRules()
    )
