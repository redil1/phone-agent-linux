"""
Sales Skills & GTM Frameworks Schema.
Synthesizes elite sales frameworks (zarif3624, louisblythe, chadboyda) into structured AI behavioral playbooks.
"""

from pydantic import BaseModel, Field


class DiscoveryQuestion(BaseModel):
    stage: str = Field(..., description="Stage: Hook, CurrentState, ProblemImpact, DecisionCriteria, TimelineBudget")
    question_text: str = Field(..., description="The exact question to ask")
    purpose: str = Field(..., description="Why this question is asked (e.g. identify manual bottlenecks, extract budget)")
    follow_up_branch: dict[str, str] = Field(
        default_factory=dict,
        description="Branching logic based on prospect answer (e.g. {'yes': 'Ask about team size', 'no': 'Pivot to efficiency'})"
    )


class ObjectionRebuttal(BaseModel):
    category: str = Field(..., description="Price, Timing, Competitor, Authority/NotDecisionMaker, FeatureMissing, Skepticism")
    common_triggers: list[str] = Field(..., description="Phrases the prospect says (e.g. 'It's too expensive', 'We don't have budget')")
    step_1_acknowledge: str = Field(..., description="Validation/Empathy (e.g. 'I completely understand why budget is top of mind...')")
    step_2_reframe_isolate: str = Field(..., description="Isolating the real issue (e.g. 'Aside from price, is there anything else holding you back?')")
    step_3_value_bridge: str = Field(..., description="Delivering concrete ROI / Proof")
    step_4_trial_close: str = Field(..., description="Actionable micro-close (e.g. 'Does it make sense to do a 15-min pilot to prove ROI?')")


class ClosingFramework(BaseModel):
    technique_name: str = Field(..., description="e.g. Assumptive Calendar Lock, Two-Option Close, Risk-Reversal Close")
    script: str = Field(..., description="Verbal script to execute")
    when_to_use: str = Field(..., description="Trigger condition in conversation")


class VoiceConversationalDynamics(BaseModel):
    speaking_rate_wpm: int = Field(145, description="Target words per minute for natural human cadence (140-155 WPM)")
    active_listening_cues: list[str] = Field(
        default_factory=lambda: ["Got it.", "Makes sense.", "Totally understand.", "I hear you."],
        description="Brief verbal acknowledgments"
    )
    barge_in_handling_rule: str = Field(
        "If interrupted, stop speaking in <50ms. Listen completely. Say 'My apologies, go ahead' and answer their point directly.",
        description="Handling prospect interruptions"
    )
    filler_word_policy: str = Field(
        "Use occasional micro-pauses and natural bridges ('Sure', 'Right'), avoid robotic transitions ('According to my database').",
        description="Tonality rules"
    )


class EscalationHandoffRules(BaseModel):
    trigger_conditions: list[str] = Field(
        default_factory=lambda: [
            "Prospect explicitly requests a human manager / executive",
            "Complex enterprise custom contract / MSA negotiation (> $25k ARR)",
            "3 consecutive unresolved objections",
            "Urgent security / compliance audit discussion"
        ],
        description="Triggers to transfer or escalate"
    )
    transfer_script: str = Field(
        "I'd love to make sure you get exact answers on that. Let me connect you directly with our Senior Solutions Lead right now, or lock in a quick 15-minute slot on their calendar. Which works better for you?",
        description="What the agent says when initiating handoff"
    )


class SalesPlaybook(BaseModel):
    icp_summary: str = Field(..., description="Ideal Customer Profile definition (zarif3624 framework)")
    qualification_methodology: str = Field("MEDDPICC / BANT", description="Qualification framework")
    discovery_questions: list[DiscoveryQuestion] = Field(default_factory=list)
    objection_library: list[ObjectionRebuttal] = Field(default_factory=list)
    closing_frameworks: list[ClosingFramework] = Field(default_factory=list)
    voice_dynamics: VoiceConversationalDynamics = Field(default_factory=VoiceConversationalDynamics)
    escalation_rules: EscalationHandoffRules = Field(default_factory=EscalationHandoffRules)
