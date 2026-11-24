"""CLI grammar and argument contract; intentionally no execution or I/O.

Parsing is independent of model, BRAIN and orchestration setup.
"""
import argparse

from alpha_research.state.paths import default_state_root, expand_path


def _path_argument(value):
    return expand_path(value)


def build_parser():
    parser = argparse.ArgumentParser(prog="alpha_research")
    parser.add_argument("--state-root", type=_path_argument, default=default_state_root())
    parser.add_argument("--config", type=_path_argument)
    parser.add_argument("--registry", type=_path_argument,
                        help="model/agent registration JSON; defaults to bundled preset when selecting")
    parser.add_argument("--lead", metavar="ID",
                        help="registered backend ID for research_lead")
    parser.add_argument("--executor", metavar="ID",
                        help="registered backend ID for research_executor")
    sub = parser.add_subparsers(dest="command", required=True)
    readiness = sub.add_parser(
        "preflight", help="check local research/model/BRAIN prerequisites without external calls")
    readiness.add_argument("--region",
                           help="planned research Region; conflicts with fixed scope are blocked")
    brain_read = sub.add_parser(
        "brain-readonly-check",
        help="explicit BRAIN read-only OPTIONS/GET connectivity and Region catalog check")
    brain_read.add_argument("--region", required=True)
    brain_read.add_argument("--delay", type=int, choices=(0, 1), required=True)
    brain_read.add_argument("--universe", required=True)
    brain_read.add_argument("--allow-brain-read", action="store_true",
                            help="authorize authentication and two read-only BRAIN calls")
    brain_read.add_argument("--record-evidence", action="store_true",
                            help="append local read result evidence; NO live authorization")
    read_evidence = sub.add_parser(
        "brain-read-evidence-status",
        help="inspect local read probe evidence; never contact BRAIN")
    read_evidence.add_argument("record_id")
    models = sub.add_parser(
        "models", help="inspect registered models or explicitly probe one model")
    models.add_argument("models_action", choices=(
        "list", "inspect", "doctor", "budget", "acknowledge-unknown", "smoke"))
    models.add_argument("--research-id", help="research ID for budget/acknowledgement")
    models.add_argument("--call-id", help="unresolved model call ID to acknowledge")
    models.add_argument("--reason", help="human explanation for unknown outcome")
    models.add_argument("--role", choices=("research_lead", "research_executor", "both"),
                        help="one role or both sequentially with models smoke")
    models.add_argument("--allow-model-call", action="store_true",
                        help="explicit authorization for one paid model/API invocation")
    orchestrate = sub.add_parser("orchestrate-offline", help="run or resume fixture-only research orchestration")
    orchestrate.add_argument("--workflow", choices=("regular", "super", "optimization"))
    orchestrate.add_argument("--objective")
    orchestrate.add_argument("--alpha-id")
    orchestrate.add_argument("--task-id")
    orchestrate.add_argument("--fixtures", type=_path_argument, required=True)
    orchestrate.add_argument("--budget", type=int, default=1)
    prepare = sub.add_parser(
        "orchestration-prepare",
        help="create a one-batch task and immutable model identity without model/BRAIN calls")
    prepare.add_argument("--workflow", choices=("regular", "super", "optimization"),
                         required=True)
    prepare.add_argument("--objective", required=True)
    prepare.add_argument("--alpha-id")
    prepare.add_argument("--region")
    prepare.add_argument("--delay", type=int)
    prepare.add_argument("--dataset", action="append")
    prepare.add_argument("--category")
    prepare.add_argument("--universe")
    prepare.add_argument("--ppa", action="store_true")
    offline_e2e = sub.add_parser(
        "orchestration-e2e-offline",
        help="prepare and execute one isolated domain-mock Task; no provider/BRAIN")
    offline_e2e.add_argument("--mode", choices=("mock",), required=True)
    offline_e2e.add_argument(
        "--workflow", choices=("regular", "super", "optimization"), required=True)
    offline_e2e.add_argument("--objective", required=True)
    offline_e2e.add_argument("--region")
    offline_e2e.add_argument("--delay", type=int)
    offline_e2e.add_argument("--universe")
    offline_e2e.add_argument("--dataset", action="append")
    offline_e2e.add_argument("--category")
    offline_e2e.add_argument("--ppa", action="store_true")
    offline_e2e.add_argument("--alpha-id")
    offline_e2e.add_argument(
        "--fault", choices=("lost-simulation-response",),
        help="offline fault injection: reserve the Task and prohibit replay")
    mock_run = sub.add_parser(
        "orchestration-run",
        help="run a prepared task with built-in synthetic fixtures only; no model/BRAIN")
    mock_run.add_argument("task_id")
    mock_run.add_argument("--mode", choices=("mock",), required=True)
    domain_mock = sub.add_parser(
        "orchestration-domain-mock",
        help="run Super/Optimization domain workflow using scripted offline Gateway/Model")
    domain_mock.add_argument("task_id")
    domain_mock.add_argument("--mode", choices=("mock",), required=True)
    domain_mock.add_argument("--fault", choices=("lost-simulation-response",))
    research_prepare = sub.add_parser(
        "orchestration-research-prepare",
        help="prepare Agent candidate + internal Manifest in isolated offline mock only")
    research_prepare.add_argument("task_id")
    research_prepare.add_argument("--mode", choices=("mock",), required=True)
    research_status = sub.add_parser(
        "orchestration-research-prepare-status",
        help="read-only Agent preparation journal status; no provider calls")
    research_status.add_argument("task_id")
    runtime_status = sub.add_parser(
        "orchestration-research-runtime-status",
        help="read-only scripted model attempt recovery; never starts any provider")
    runtime_status.add_argument("task_id")
    runtime_plan = sub.add_parser(
        "orchestration-research-runtime-plan",
        help="compose signed research grant, Catalog read scope and cost; NEVER run live")
    runtime_plan.add_argument("task_id")
    runtime_plan.add_argument("--receipt", type=_path_argument)
    runtime_plan.add_argument("--trust-policy", type=_path_argument)
    runtime_plan.add_argument("--input-tokens", type=int)
    runtime_plan.add_argument("--max-output-tokens", type=int)
    runtime_plan.add_argument("--global-consumption-ack", type=_path_argument)
    runtime_plan.add_argument("--consumer-trust-policy", type=_path_argument)
    control_plane = sub.add_parser(
        "orchestration-research-status",
        help="single read-only control-plane status (Task, Agent, Manifest, Grant, live blockers)")
    control_plane.add_argument("task_id")
    admission = sub.add_parser(
        "orchestration-admission-audit",
        help="inspect production authorization blockers without provider or BRAIN calls")
    admission.add_argument("task_id")
    authorization = sub.add_parser(
        "orchestration-authorization-inspect",
        help="verify a signed execution receipt without granting live execution")
    authorization.add_argument("task_id")
    authorization.add_argument("--receipt", required=True, type=_path_argument)
    reserve_receipt = sub.add_parser(
        "orchestration-authorization-reserve",
        help="reserve signed execution receipt once for audit ONLY; never execute")
    reserve_receipt.add_argument("task_id")
    reserve_receipt.add_argument("--receipt", required=True, type=_path_argument)
    simulation_scope = sub.add_parser(
        "orchestration-simulation-scope-check",
        help="verify external Simulation scope receipt and manifest; NEVER dispatch")
    simulation_scope.add_argument("task_id")
    simulation_scope.add_argument("--receipt", required=True, type=_path_argument)
    simulation_scope.add_argument(
        "--manifest", type=_path_argument,
        help="optional external manifest, otherwise use internal Agent snapshot")
    sim_stage = sub.add_parser(
        "orchestration-simulation-stage",
        help="atomically stage one signed Simulation intent; NO BRAIN dispatch")
    sim_stage.add_argument("task_id")
    sim_stage.add_argument("--receipt", required=True, type=_path_argument)
    sim_stage.add_argument(
        "--manifest", type=_path_argument,
        help="optional external manifest, otherwise use internal Agent snapshot")
    sim_stage_status = sub.add_parser(
        "orchestration-simulation-stage-status",
        help="read-only inspection of staged Simulation attempt")
    sim_stage_status.add_argument("task_id")
    consent_status = sub.add_parser(
        "orchestration-consent-status",
        help="read-only one-shot operator consent audit status; NEVER grant execution")
    consent_status.add_argument("task_id")
    scope_handoff = sub.add_parser(
        "orchestration-scope-issuer-request",
        help="read-only scope-signature request; does NOT sign or authorize")
    scope_handoff.add_argument("task_id")
    scope_handoff.add_argument(
        "--manifest", type=_path_argument,
        help="optional reviewed external manifest; default is Agent-produced snapshot")
    manifest_status = sub.add_parser(
        "orchestration-manifest-status",
        help="read-only inspection of Agent-produced Simulation snapshot")
    manifest_status.add_argument("task_id")
    consent_handoff = sub.add_parser(
        "orchestration-consent-issuer-request",
        help="read-only operator approval handoff; requires staged Intent")
    consent_handoff.add_argument("task_id")
    research_issuer = sub.add_parser(
        "orchestration-research-issuer-request",
        help="read-only independent research-only approval request; no model/BRAIN")
    research_issuer.add_argument("task_id")
    research_gate = sub.add_parser(
        "orchestration-research-live-readiness",
        help="read-only model/price/issuer readiness audit; live dispatch disabled")
    research_gate.add_argument("task_id")
    research_gate.add_argument("--receipt", type=_path_argument)
    research_gate.add_argument("--trust-policy", type=_path_argument)
    research_claim = sub.add_parser(
        "orchestration-research-grant-audit-reserve",
        help="single-use LOCAL research grant audit; never authorizes provider/POST")
    research_claim.add_argument("task_id")
    research_claim.add_argument("--receipt", required=True, type=_path_argument)
    research_claim.add_argument("--trust-policy", required=True, type=_path_argument)
    research_claim_status = sub.add_parser(
        "orchestration-research-grant-audit-status",
        help="inspect local research grant audit, never a live permission")
    research_claim_status.add_argument("task_id")
    catalog_contract = sub.add_parser(
        "orchestration-research-catalog-contract",
        help="inspect exact read-only BRAIN Catalog scope; no network")
    catalog_contract.add_argument("task_id")
    for command, help_text in (
        ("orchestration-consent-verify",
         "verify external Ed25519 consent evidence without creating authorization"),
        ("orchestration-consent-reserve",
         "reserve consent evidence once for audit ONLY; never dispatch"),
    ):
        consent_command = sub.add_parser(command, help=help_text)
        consent_command.add_argument("task_id")
        consent_command.add_argument(
            "--receipt", type=_path_argument, required=True)
        consent_command.add_argument(
            "--trust-policy", type=_path_argument, required=True)
    escrow_reserve = sub.add_parser(
        "orchestration-predispatch-escrow-reserve",
        help="one-shot offline evidence escrow; NO platform POST or model call")
    escrow_reserve.add_argument("task_id")
    escrow_status = sub.add_parser(
        "orchestration-predispatch-escrow-status",
        help="read-only pre-POST escrow evidence status; NEVER dispatch BRAIN")
    escrow_status.add_argument("task_id")
    recovery_audit = sub.add_parser(
        "orchestration-simulation-recovery-audit",
        help="inspect persisted Simulation unknown outcomes; no BRAIN/model calls")
    recovery_audit.add_argument("task_id")
    release_gate = sub.add_parser(
        "orchestration-release-gate",
        help="integrated read-only production release checklist; NEVER authorize")
    release_gate.add_argument("task_id")
    release_gate.add_argument(
        "--platform-read-evidence-id",
        help="local BRAIN read-only observation ID; NEVER production authority")
    sim_dispatch_audit = sub.add_parser(
        "orchestration-simulation-dispatch-audit",
        help="read-only full Simulation write admission blockers; never dispatch")
    sim_dispatch_audit.add_argument("task_id")
    handoff = sub.add_parser(
        "orchestration-handoff",
        help="inspect prepared identity and existing domain model ledger without external calls")
    handoff.add_argument("task_id")
    recovery_check = sub.add_parser(
        "orchestration-recovery-check",
        help="read blocked task, Intent and model budget; never call BRAIN")
    recovery_check.add_argument("task_id")
    orchestration_status = sub.add_parser("orchestration-status", help="read a persisted orchestration task")
    orchestration_status.add_argument("task_id")
    pilot = sub.add_parser(
        "orchestration-local-pilot-regular",
        help="explicitly approved single-batch real Regular pilot; NOT P7 production release")
    pilot.add_argument("task_id", help="existing fresh prepared Regular Task ID")
    pilot.add_argument("--allow-model-call", action="store_true",
                       help="approve real metered Lead/Executor model calls within configured policy")
    pilot.add_argument("--allow-brain-simulation", action="store_true",
                       help="approve at most one real BRAIN simulate_batch POST (1-8 variants)")
    run = sub.add_parser("run-once", help="execute one admitted research batch")
    run.add_argument("--objective", required=True)
    run.add_argument("--dataset")
    run.add_argument("--diagnosis-id")
    run.add_argument("--remedy-id")
    run.add_argument("--allow-remedy-round-overrun", action="store_true")
    series = sub.add_parser("run", help="execute a bounded serial research case")
    series.add_argument("--objective", required=True)
    series.add_argument("--dataset")
    series.add_argument("--max-batches", type=int, required=True)
    series.add_argument("--diagnosis-id")
    series.add_argument("--remedy-id")
    series.add_argument("--allow-remedy-round-overrun", action="store_true")
    planning = sub.add_parser("plan-regular", help="discover a Regular Campaign scope without simulation")
    planning.add_argument("--region")
    planning.add_argument("--dataset", action="append")
    planning.add_argument("--category")
    planning.add_argument("--delay", type=int)
    planning.add_argument("--universe")
    campaign = sub.add_parser("run-campaign", help="execute a bounded Regular Campaign")
    campaign_target = campaign.add_mutually_exclusive_group(required=True)
    campaign_target.add_argument("--objective")
    campaign_target.add_argument("--resume-campaign", metavar="ID", help="resume a saved Campaign")
    campaign.add_argument("--region")
    campaign.add_argument("--dataset", action="append")
    campaign.add_argument("--category")
    campaign.add_argument("--delay", type=int)
    campaign.add_argument("--universe")
    campaign.add_argument("--max-batches", type=int, help="optional legacy attempt limit per Dataset")
    campaign.add_argument("--ppa", action="store_true",
                          help="apply the optional PPA Profile")
    campaign.add_argument("--brain-credentials-stdin", action="store_true",
                          help=argparse.SUPPRESS)
    optimization = sub.add_parser(
        "plan-optimization", help="capture an existing Alpha as a read-only optimization baseline")
    optimization.add_argument("--alpha-id", required=True)
    optimization.add_argument("--objective")
    optimization.add_argument("--region")
    optimization_run = sub.add_parser(
        "run-optimization", help="simulate one independent Alpha optimization candidate")
    optimization_run.add_argument("--alpha-id", required=True)
    optimization_run.add_argument("--objective", required=True)
    optimization_run.add_argument("--region")
    super_plan = sub.add_parser(
        "plan-super", help="inspect BRAIN-native Super settings and active Alpha pool")
    super_plan.add_argument("--region")
    super_plan.add_argument("--objective", required=True)
    super_plan.add_argument("--delay", type=int)
    super_run = sub.add_parser(
        "run-super", help="execute one native Super research experiment")
    super_run.add_argument("--region")
    super_run.add_argument("--objective", required=True)
    super_run.add_argument("--delay", type=int)
    research_recovery = sub.add_parser("recover-research", help="reconcile a V1.1 Super or Optimization simulation intent without reposting")
    research_recovery.add_argument("intent_path", type=_path_argument)
    recovery = sub.add_parser("recover", help="reconcile an unfinished Regular batch")
    recovery.add_argument("batch_dir", type=_path_argument)
    unified_recovery = sub.add_parser(
        "recover-workflow",
        help="recover a Regular batch directory or a Super/Optimization simulation intent")
    unified_recovery.add_argument(
        "--workflow", choices=("regular", "super", "optimization"), required=True)
    unified_recovery.add_argument("target", type=_path_argument)
    task_recovery = sub.add_parser(
        "recover-task", help="reconcile a blocked orchestration execute step from its saved workflow intent")
    task_recovery.add_argument(
        "--workflow", choices=("regular", "super", "optimization"), required=True)
    task_recovery.add_argument("task_id")
    void = sub.add_parser("void", help="archive an unsubmitted draft")
    void.add_argument("batch_dir", type=_path_argument)
    void.add_argument("--reason", required=True)
    validity = sub.add_parser("evidence-validity",
                              help="mark an indexed evidence item with an explicit validity")
    validity.add_argument("evidence_id")
    validity.add_argument("--status", choices=("valid", "void", "falsified", "pending"),
                          required=True)
    validity.add_argument("--reason", required=True)
    validity.add_argument("--operator", default="user")
    knowledge = sub.add_parser("knowledge", help="inspect and maintain local research knowledge")
    knowledge_sub = knowledge.add_subparsers(dest="knowledge_command", required=True)
    listing = knowledge_sub.add_parser("list", help="list managed knowledge items")
    listing.add_argument("--kind", choices=("all", "operator", "field", "remedy", "evidence"),
                         default="all")
    show = knowledge_sub.add_parser("show", help="show one managed knowledge item")
    show.add_argument("--kind", choices=("operator", "field", "remedy", "evidence"),
                      required=True)
    show.add_argument("--id", required=True)
    note = knowledge_sub.add_parser("note", help="manage exact operator and field notes")
    note_sub = note.add_subparsers(dest="note_command", required=True)
    note_put = note_sub.add_parser("put", help="add or explicitly replace an exact note")
    note_put.add_argument("--kind", choices=("operator", "field"), required=True)
    note_put.add_argument("--key", required=True)
    note_put.add_argument("--content-file", type=_path_argument, required=True)
    note_put.add_argument("--source", required=True)
    note_put.add_argument("--date", default="unknown")
    note_put.add_argument("--validity", choices=("valid", "void", "falsified", "pending"),
                          default="valid")
    note_put.add_argument("--replace", action="store_true")
    note_put.add_argument("--reason")
    note_put.add_argument("--operator", default="user")
    remedy = knowledge_sub.add_parser("remedy", help="register and review remedies")
    remedy_sub = remedy.add_subparsers(dest="remedy_command", required=True)
    remedy_add = remedy_sub.add_parser("add", help="register a remedy as pending review")
    remedy_add.add_argument("--file", type=_path_argument, required=True)
    remedy_add.add_argument("--operator", default="user")
    remedy_review = remedy_sub.add_parser("review", help="review a registered remedy")
    remedy_review.add_argument("--id", required=True)
    remedy_review.add_argument("--status",
                               choices=("pending_review", "verified", "falsified", "void"),
                               required=True)
    remedy_review.add_argument("--reviewer", required=True)
    remedy_review.add_argument("--reason", required=True)
    search = knowledge_sub.add_parser("search", help="search local indexed evidence")
    search.add_argument("--dataset", default="*")
    search.add_argument("--mechanism", action="append", default=[])
    search.add_argument("--failure-signature", action="append", default=[])
    search.add_argument("--field-generation", action="append", default=[])
    search.add_argument("--region")
    search.add_argument("--delay", type=int)
    search.add_argument("--universe")
    search.add_argument("--text", default="")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--include-history", action="store_true",
                         help="return historical-only evidence; never mixed with valid results")
    prep = sub.add_parser("canary-prepare", help="read-only prepare one pinned BRAIN canary")
    prep.add_argument("--plan-id", required=True)
    execute = sub.add_parser("canary-execute", help="post one explicitly authorized pinned Simulation")
    execute.add_argument("--plan-id", required=True)
    execute.add_argument("--allow-one-simulation", action="store_true")
    inspect = sub.add_parser("canary-status", help="inspect local single-shot reservation")
    inspect.add_argument("--plan-id", required=True)
    review = sub.add_parser("canary-review", help="offline verify saved canary evidence and metric gates")
    review.add_argument("--plan-id", required=True)
    diagnosis = sub.add_parser("canary-diagnose", help="offline analyze verified canary metrics and RA failures")
    diagnosis.add_argument("--plan-id", required=True)
    design = sub.add_parser(
        "canary-design", help="offline design static-contract candidates from failed Canary")
    design.add_argument("--plan-id", required=True)
    shortlist = sub.add_parser(
        "canary-shortlist", help="offline triage design by coverage and matching DataField alternatives")
    shortlist.add_argument("--plan-id", required=True)
    field_review = sub.add_parser(
        "canary-field-review",
        help="offline verify field coverage, semantic risks and alternative expression contracts")
    field_review.add_argument("--plan-id", required=True)
    decision = sub.add_parser(
        "canary-field-decision",
        help="offline determine whether verified field alternatives improve catalog coverage")
    decision.add_argument("--plan-id", required=True)
    distribution = sub.add_parser(
        "canary-distribution-audit",
        help="offline audit signed field/portfolio evidence; no remote calls")
    distribution.add_argument("--plan-id", required=True)
    distribution.add_argument("--evidence-dir", type=_path_argument)
    recordsets = sub.add_parser(
        "canary-recordsets-observe",
        help="opt-in BRAIN GET-only Alpha recordset capture; never simulates")
    recordsets.add_argument("--plan-id", required=True)
    recordsets.add_argument("--allow-brain-read", action="store_true",
                            help="authorize only pinned Alpha and its listed recordset GETs")
    rs_review = sub.add_parser(
        "canary-recordsets-review",
        help="offline typed audit of immutable native Alpha recordset evidence")
    rs_review.add_argument("--plan-id", required=True)
    rs_review.add_argument("--evidence-path", type=_path_argument, required=True)
    package = sub.add_parser(
        "canary-distribution-package",
        help="validate real local CSV exports, bind immutable checksums and run offline audit")
    package.add_argument("--plan-id", required=True)
    package.add_argument("--evidence-dir", type=_path_argument)
    package.add_argument("--universe-source", required=True,
                         help="origin of universe.csv; record actual export provenance")
    package.add_argument("--signal-source", required=True,
                         help="origin of signal.csv; record actual export provenance")
    package.add_argument("--weights-source", required=True,
                         help="origin of weights.csv; record actual export provenance")
    package.add_argument("--universe-complete", action="store_true",
                         help="explicitly attest complete universe; no automatic inference")
    package.add_argument("--signal-exhaustive", action="store_true",
                         help="explicitly attest one signal row per universe instrument/date")
    package.add_argument("--weights-exhaustive", action="store_true",
                         help="explicitly attest complete portfolio weights for all included dates")
    sub.add_parser("status", help="inspect local state without platform access")
    sync_cache = sub.add_parser("sync-backtest-cache", help="index completed BRAIN IS and OS history")
    sync_cache.add_argument("--brain-credentials-stdin", action="store_true", help=argparse.SUPPRESS)
    tags = sub.add_parser("tag-alpha", help="preview or explicitly apply a verified local tag plan")
    tags.add_argument("alpha_id")
    tags.add_argument("--apply", action="store_true", help="apply the reviewed changes to BRAIN")
    tags.add_argument("--diagnostic", action="store_true", help="include audit diagnostic name and color")
    tags.add_argument("--brain-credentials-stdin", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("rebuild-pools", help="rebuild global Candidate/Preferred index without BRAIN requests")
    sub.add_parser("stop", help="block new simulations")
    sub.add_parser("resume", help="clear the stop flag by explicit command")
    return parser
