"""Canonical research decision prompts, independent of backend transport."""

FIELD_EVIDENCE_KEYS = {
    "nominal_coverage": "coverage",
    "usable_observations": "zeroFraction",
}
REQUIRED_FIELD_DIMENSIONS = ("nominal_coverage",)


TASK_INSTRUCTIONS = {
    "optimize_alpha": (
        "Propose exactly one independent optimization candidate for the supplied "
        "existing BRAIN Alpha. Preserve its Region, Delay, Universe and all other "
        "effective settings. Never request source Alpha mutation or submission. "
        "For REGULAR return {expression, rationale, changes, hypothesis, "
        "failure_criterion, research}; research must contain idea, "
        "data_rationale, operator_rationale, field_relationship, "
        "settings_rationale, expected_behavior, failure_criterion. Use only "
        "source fields and live supported operators. For SUPER return "
        "{selection, combo, rationale, changes, hypothesis, "
        "failure_criterion}. Changes must be a nonempty list of descriptions. "
        "Do not invent platform results or claim candidate eligibility."
    ),
    "construct_super": (
        "Design one BRAIN-native Super Alpha using a selection expression over "
        "submitted ACTIVE Alphas and a separate combo expression. Use only the "
        "supplied live eligible Alpha pool and Super settings schema. Preserve "
        "the user Region, objective, and any fixed Delay or other constraints. "
        "Return {selection, combo, settings, hypothesis, rationale, "
        "failure_criterion}. State a falsifiable economic or diversification "
        "hypothesis and explain why the combo should improve on equal weighting. "
        "Do not claim a platform check passed or invent supported settings."
    ),
    "select_scope": (
        "Region is user-supplied and must never change. Choose supported Delay, Universe "
        "and Neutralization only from the provided live platform combinations. Choose "
        "unsupplied decay, truncation, and pasteurization from the live settings schema "
        "with an economic rationale. Preserve every supplied scope value. Return "
        "{delay, universe, neutralization, decay, truncation, pasteurization, reason}."
    ),
    "select_dataset": (
        "Choose one dataset_id from the supplied live eligible Dataset pool. Explain its economic "
        "relevance and available evidence; do not invent field evidence. Preserve the supplied "
        "Region, Category, Delay, Universe, and objective. "
        "Return {dataset_id, reason}."
    ),
    "plan_probes": (
        "Screen fields using the supplied platform catalog: field description, type, coverage, "
        "dateCoverage, alphaCount, userCount, and valueScore when present. "
        "Popularity and scores are screening signals, not proof of performance. "
        "For routine screening, incomplete semantic detail or missing L2 notes alone "
        "should not exclude an otherwise eligible field. "
        "For probe measures use only these exact dimension IDs: "
        + ", ".join(FIELD_EVIDENCE_KEYS) + ". "
        "Generic diagnostic simulations only provide approximate nominal coverage and, with a "
        "nonzero-value test, usable observations. Do not claim other evidence from these probes. "
        "Do not use field-to-field division or subtraction, including a field divided by itself. "
        "Use only operators in context.operators when supplied. If validation_feedback is present, "
        "correct the rejected probe in previous_probe_plan and return a complete revised plan. "
        "Return core_fields as a nonempty list of unique exact field IDs from context.fields. "
        "Set required_dimensions to nominal_coverage only for each core field and explain the field "
        "choice in requirement_reason as a nonempty string or a map with one nonempty string for "
        "each exact core field ID. Use the platform's coverage as evidence. Return no probes "
        "for routine field screening. If screening_only is true, always return probes: []. "
        "Otherwise propose a probe only when the research objective calls for a "
        "specific diagnostic question that the supplied catalog cannot answer. Each probe "
        "needs field_id (one of core_fields), expression, question, measures, "
        "economic_relevance, expected, failure_criterion, "
        "and a research object with nonempty idea, data_rationale, operator_rationale, "
        "field_relationship, settings_rationale, expected_behavior, failure_criterion. "
        "Diagnostic Sharpe is never Alpha performance. "
        "Return {core_fields, required_dimensions, requirement_reason, probes, reason}; "
        "use an empty probes list when no diagnostic simulation is needed."
    ),
    "construct_batch": (
        "State the economic mechanism before expressions. Translate observed field constraints "
        "into design choices; never invent missing probe evidence. Return a JSON object with "
        "hypothesis, expected_sign, structure_window_rationale, data_rationale, "
        "operator_rationale, field_relationship, settings_rationale, failure_criterion, "
        "mechanism_tags, field_generation_tags, and variants. Use only the screened core_fields "
        "and record relevant limitations in the research rationale. "
        "Use operator knowledge and past research to design a testable baseline when "
        "field semantics are incomplete. State any economic interpretation as a hypothesis "
        "and describe what the simulation can measure. "
        "Each variant needs expression and ex-ante reason. These top-level rationale "
        "fields plus hypothesis, expected_sign, and failure_criterion must be substantive, "
        "nonempty research evidence suitable for the local pre-backtest receipt. "
        "First adapt every VECTOR field with a compatible vec_* operator from the supplied "
        "REGULAR catalog before applying any time-series or cross-sectional operator. "
        "Use one Dataset per expression and one to eight variants. Apply PPA field and "
        "operator limits only when the user explicitly requests PPA; "
        "do not splice signals with + or *, use trade_when/add/multiply, or tune to prior results. "
        "If draft_proposal is present, review it against the supplied exact operator notes, "
        "preserve the hypothesis and field evidence, and return the complete proposal schema. "
        "If remedy_reference is present, keep its Dataset and mechanism fixed, implement only "
        "its structural action, avoid parameter grids, and cite both diagnosis_id and remedy_id "
        "in every variant reason."
    ),
    "construct_field_expressions": (
        "Design Regular Alpha expressions separately for every field in target_field_ids. "
        "Read each target field's description, the Dataset description, and the supplied "
        "REGULAR operator definitions and descriptions. Use them to justify the economic "
        "hypothesis, operator and window choices, and expected direction. Keep the supplied "
        "settings and Dataset fixed. Do not invent field semantics or statistical evidence. "
        "An incomplete field description or missing exact L2 notes is not a reason to omit "
        "a screened field. Use the supplied operator definitions and relevant prior research "
        "to construct at least one single-field baseline. For a MATRIX field, consider a "
        "cross-sectional rank or a historical change when the values support it. "
        "For a VECTOR field whose values cannot safely be compared, consider a vec_count "
        "baseline for record availability if that operator is supplied. Treat these as "
        "exploratory associations. Record the uncertain interpretation in data_rationale, "
        "what the transformation actually measures in operator_rationale, and a measurable "
        "failure criterion. Do not infer a buy/sell direction from a categorical code. "
        "For each field in vector_field_ids, first choose a compatible operator from "
        "vector_adapters using its catalog description. In every expression, apply a "
        "supplied vec_* operator directly to each VECTOR field before any time-series or "
        "cross-sectional processing. Explain what the chosen aggregation measures in "
        "operator_rationale. If paired elements cannot be aligned, first evaluate "
        "single-field vector adaptations on their own merits before skipping the field. "
        "Return {\"fields\":[{\"field_id\": ..., \"variants\":[{\"expression\": ..., "
        "\"reason\": ..., \"research\": {...}}]}]}. For each target return 1 to 3 distinct "
        "executable FASTEXPR variants that reference that target and only screened fields. "
        "Each research object requires idea, data_rationale, operator_rationale, "
        "field_relationship, settings_rationale, expected_behavior, and failure_criterion "
        "as substantive strings. Prefer distinct hypotheses or justified controls over parameter grids. "
        "Do not splice signals with + or *, use trade_when/add/multiply, or repeat prior expressions. "
        "Return an empty variants list only if a concrete field or operator constraint prevents "
        "even a baseline from being expressed. Identify that constraint in unresolved_reason. "
        "If validation_feedback is supplied, revise only the rejected variants using their exact "
        "validation errors and preserve the target field and original economic intent. Return "
        "at most one replacement per rejected variant, in the same order, using the same fields "
        "response schema. Do not repeat any expression in prior_expressions. "
        "A feedback entry with a null expression means the field was skipped. Give that field "
        "one executable baseline under the supplied constraints, stating its uncertainty. "
        "If a repair cannot preserve the hypothesis using the supplied catalog, explain why."
    ),
    "attribute_results": (
        "Return exactly one JSON object with a nonempty summary string and findings, "
        "an array of objects with alpha_id, classification, and reason. Limit the summary "
        "to three sentences and each reason to one sentence. "
        "Do not write any text outside the JSON object. "
        "Report failed checks first, then measured metrics. Compare each result with the "
        "pre-registered failure criterion for its variant when supplied, and classify support, refutation, or insufficient "
        "evidence. Platform ERROR is technical, not hypothesis failure. Do not declare Candidate."
    ),
    "model_probe": (
        "Connectivity contract only: return exactly one JSON object with keys "
        "ready and contract: {ready:true,contract:'model_probe_v1'}. "
        "Do not call tools, browse, alter files, or assume BRAIN is available. "
        "Do not include any other keys or prose."
    ),
    "review_dataset_results": (
        "Synthesize the supplied batch attributions for one Dataset. Explain which hypotheses "
        "the measurements support, what failed, and the limits of the evidence. State concrete "
        "lessons for the next study. Compare settings separately when they differ. "
        "Return {summary, selection, selection_reason}. Select at most one Alpha for one "
        "targeted optimization, only from eligible_sources. Use selection=null when there is "
        "no measured and actionable hypothesis. Otherwise selection is the exact Alpha ID "
        "and selection_reason must name a measured result, an observed weakness and a specific "
        "improvement to test within the source scope. Never declare Candidate or authorize submission."
    ),
    "diagnose_failures": (
        "Explain the deterministic signatures without changing the gate verdict. Choose only "
        "among remedies listed for each Alpha. If no verified remedy is listed, recommend "
        "no_known_remedy, abandon, or mechanism_review; never invent an action or remedy_id. "
        "Return {alphas: {alpha_id: {recommendation, remedy_id, rationale, evidence_refs}}}. "
        "recommendation is no_known_remedy, abandon, mechanism_review, or use_remedy. "
        "use_remedy is allowed only when remedy_id exactly matches a verified candidate."
    ),
    "review_candidate": (
        "Review the verified preregistration, live checks, correlations and result for "
        "unresolved robustness or trade-offs. Return {verdict, reason, evidence_refs}; "
        "verdict must be CLEAR, ROBUSTNESS_REVIEW, or TRADEOFF_REVIEW. Do not mark CLEAR "
        "when evidence is missing or a warning is unresolved. Never authorize submission."
    ),
    "prepare_execution": (
        "You are the Research Executor role. Receive only the authorized "
        "experiment contracts and return exactly {experiment_ids:[...]}, "
        "an ordering of every authorized experiment ID exactly once. "
        "Do not add experiments, alter settings, call BRAIN, invoke tools "
        "or claim any execution result. The orchestrator will run its own "
        "authorized offline gateway after validating the proposed order."
    ),
    "plan_experiments": (
        "You are the Research Lead role. Within the exact Region, design "
        "between 1 and max_experiments independent Alpha experiments with "
        "falsifiable economic hypotheses. Output precisely an object with "
        "experiments, an array of {experiment_id, expression, region, settings, "
        "hypothesis, failure_criterion}. This is a mock-contract workflow, "
        "not permission to run BRAIN or claim performance. Preserve Region "
        "and specify exact settings. Never invent live field evidence."
    ),
    "research_decide": (
        "You are the Research Lead role. Compare synthetic experiment outputs "
        "and the executor attribution; distinguish observations and hypotheses. "
        "Return {action,reason,evidence_refs}, where action is OPTIMIZE, "
        "INVESTIGATE, EXPLORE, CANDIDATE, REJECT, or STOP. Use only provided "
        "evidence_refs, explain uncertainty, and never claim that a mock "
        "observation qualifies an Alpha or authorizes real submission."
    ),
    "decide_next": (
        "Use the attributed result and prior case batches to choose a new independently "
        "justified research direction or pause. Return {action, reason}; action is "
        "continue or pause. Do not repeat expressions, relabel an old branch to reset "
        "its budget, or switch a user-fixed Dataset. A weak batch alone is not economic "
        "exhaustion. Leave unresolved reviews pending and continue only valid work."
    ),
}

KNOWLEDGE_POLICY = (
    "Treat prior_campaign_findings as historical observations for avoiding repeated mistakes. "
    "Do not infer current platform facts or a causal mechanism from prior results. "
    "Use only the supplied knowledge_pack. Treat l1_rules with source_kind=normative as "
    "controlling. l1_rules marked advisory_only are secondary and must be ignored on conflict. "
    "L3 entries marked prior_advisory are candidate historical priors, not policy or causal proof. "
    "Do not treat L3 or field snapshots as current platform facts; current facts come from the "
    "live API evidence in this request. Cite pack source and section/record when relying on it. "
    "An absent exact L2 note means its semantics are unknown from local notes; do not invent one."
)


def instruction_for(task: str, *, forbid_tools: bool = False) -> str:
    """Return identical task/evidence policy across model transports."""
    instruction = TASK_INSTRUCTIONS.get(task, "Return one JSON object only.")
    instruction += " " + KNOWLEDGE_POLICY
    if forbid_tools:
        instruction += " Answer from the supplied evidence. Do not call tools."
    return instruction
